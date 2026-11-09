"""Coverage of the cohort against GitHub's current dependents list of the target Action.

Three steps, each its own command:
  sample     intersect the crawled list with the source frame and draw a seeded sample of dependents outside it;
  collect    for each sampled repository, apply the cohort's baseline rule (a direct reference at the last first-parent
             commit before the anchor) and record the covariates used for control matching;
  summarize  estimate how many current dependents already depended on the Action before the incident, the cohort's
             share of them, and compare their covariates with the formal cohort.

The dependents list contains only current dependents, so the estimate covers pre-incident dependents that still use
the Action; repositories that removed it are absent from the list. Output goes to new paths only.

Usage: python -m pintrace.coverage_audit {sample,collect,summarize} [--crawl <dir>] [--workers 4]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from .baseline import baseline_status, pre_event_point
from .cohort import digest, save_detail
from .control_baseline import activity, reference_profile
from .github import GitHub, github_token
from .history import clone, first_parent_history, git
from .pilot import snapshot, timestamp
from .workflow import TARGET

RULE_VERSION = "coverage-audit-v1"
OUTPUT = "data/derived/coverage-v1"
CLONES = "data/cache/coverage-repos"
SEED = "pintrace-coverage-20261109-v1"
SAMPLE_SIZE = 300
Z = 1.959963984540054
RESOLVED = ("confirmed", "not_at_baseline", "no_pre_event_history")
COVARIATES = ("git_commits_180d", "baseline_workflow_files", "action_references", "mutable_share")


# ---------------------------------------------------------------- frame and sample

def frame_names(root):
    """Lower-cased spellings of every source-frame name, including the canonical name each resolved to."""
    with (Path(root) / "reports/candidate_source_membership.csv").open(encoding="utf-8", newline="") as stream:
        names = {row["repository"].lower() for row in csv.DictReader(stream)}
    for path in (Path(root) / "data/derived/cohort-v1/baselines").glob("*.json"):
        canonical = json.loads(path.read_text(encoding="utf-8"))["summary"].get("canonical_repository")
        if canonical:
            names.add(canonical.lower())
    return names


def formal_records(root):
    """Formal cohort: repository_id -> cohort baseline summary (alphabetically first alias)."""
    manifest = json.loads((Path(root) / "reports/working_input_manifest.json").read_text(encoding="utf-8"))
    formal = {str(i) for i in manifest["formal_population"]["formal_eligible_repository_ids"]}
    records = {}
    for path in sorted((Path(root) / "data/derived/cohort-v1/baselines").glob("*.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
        if summary.get("repository_id") in formal and summary["status"] == "confirmed":
            records.setdefault(summary["repository_id"], summary)
    if set(records) != formal:
        raise ValueError(f"Formal cohort records incomplete: {len(records)} of {len(formal)}")
    return records


def read_dependents(directory):
    """Unique dependents in list order (case-insensitive); the crawl must be complete."""
    if not (directory / "complete.json").exists():
        raise ValueError(f"Crawl is not complete: {directory}")
    seen, ordered = set(), []
    with (directory / "dependents.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if row["repository"].lower() not in seen:
                seen.add(row["repository"].lower())
                ordered.append(row["repository"])
    return ordered


def draw_sample(dependents, excluded, size=SAMPLE_SIZE, seed=SEED):
    """A seeded simple random sample of dependents whose name is not in ``excluded``; independent of list order."""
    outside = sorted((name for name in dependents if name.lower() not in excluded), key=str.lower)
    return random.Random(seed).sample(outside, min(size, len(outside)))


# ---------------------------------------------------------------- collection

def output_path(root, repository):
    return Path(root) / OUTPUT / "baselines" / (repository.replace("/", "__") + ".json")


def collect(repository, root, anchor, token):
    """The cohort baseline rule applied to one sampled repository; saved once, reused on rerun."""
    output = output_path(root, repository)
    if output.exists():
        return json.loads(output.read_text(encoding="utf-8"))["summary"]
    record = dict(repository=repository, status="identity_pending", rule_version=RULE_VERSION,
                  checked_at_utc=datetime.now(timezone.utc).isoformat())
    state, phase = None, "identity"
    try:
        client = GitHub(Path(root) / "data/cache", token)
        metadata = client.api("repos/" + repository)
        record.update(repository_id=str(metadata["id"]), canonical_repository=metadata["full_name"],
                      created_at=metadata.get("created_at"), current_fork=metadata["fork"],
                      current_archived=metadata["archived"], owner_type=(metadata.get("owner") or {}).get("type"))
        phase = "history"
        directory = clone(metadata["full_name"], Path(root) / CLONES / (repository.replace("/", "__") + ".git"))
        history = first_parent_history(directory)
        record.update(ancestry_commits=len(history), git_commits_180d=activity(history, anchor, 180))
        point = pre_event_point(history, anchor)
        if point is None:
            record["status"] = "no_pre_event_history"
        else:
            phase = "blobs"
            state = snapshot(client, directory, metadata["full_name"], point, {})
            counts = state["counts"].get(TARGET, {})
            record.update(status=baseline_status(state), baseline_commit=state["sha"], baseline_date=state["date"],
                          baseline_tree=git(directory, "rev-parse", state["sha"] + "^{tree}").decode().strip(),
                          baseline_workflow_files=len(state["files"]), target_full_sha=counts.get("full_sha", 0),
                          target_mutable=counts.get("mutable_or_unresolved", 0), **reference_profile(state["references"]))
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else None
        record.update(status="inaccessible_unknown" if phase == "identity" and code in (404, 451) else "collection_failed",
                      http_status=code, error=f"{phase}: HTTPError {code}")
    except Exception as error:
        record.update(status="collection_failed", error=f"{phase}: {type(error).__name__}: {error}")
    if record["status"] != "collection_failed":
        save_detail(output, dict(summary=record, baseline_snapshot=state))
    return record


# ---------------------------------------------------------------- summary

def wilson(successes, total):
    if not total:
        return None
    p, denominator = successes / total, 1 + Z * Z / total
    centre = (p + Z * Z / (2 * total)) / denominator
    half = Z * (p * (1 - p) / total + Z * Z / (4 * total * total)) ** 0.5 / denominator
    return dict(estimate=p, low=centre - half, high=centre + half, successes=successes, total=total)


def describe(rows):
    """Median and quartiles of each covariate, organisation share and the baseline pinned share of the target."""
    output = dict(repositories=len(rows))
    for key in COVARIATES:
        values = sorted(r[key] for r in rows if r.get(key) is not None)
        q = statistics.quantiles(values, n=4) if len(values) > 1 else values * 3
        output[key] = dict(n=len(values), median=statistics.median(values) if values else None, q1=q[0] if q else None,
                           q3=q[-1] if q else None)
    output["organization_share"] = wilson(sum(r.get("owner_type") == "Organization" for r in rows), len(rows))
    output["target_all_sha_share"] = wilson(sum(r.get("target_full_sha", 0) > 0 and r.get("target_mutable", 0) == 0
                                                for r in rows), len(rows))
    return output


def not_fork(row):
    return not row.get("current_fork")


def estimate(listed, frame_listed, formal_listed, sample_rows, eligible=lambda row: True):
    """Pre-incident dependents among current dependents, and the formal cohort's share of them. ``eligible`` narrows
    which confirmed rows count (for example non-forks); the denominator stays every resolved sampled row."""
    statuses = Counter(r["status"] for r in sample_rows)
    successes = sum(r["status"] == "confirmed" and eligible(r) for r in sample_rows)
    rate = wilson(successes, sum(statuses[s] for s in RESOLVED))
    outside = listed - frame_listed
    scale = lambda p: formal_listed + p * outside
    return dict(sample_status=dict(statuses), pre_incident_rate=rate, dependents_outside_frame=outside,
                estimated_pre_incident_outside=dict(estimate=rate["estimate"] * outside, low=rate["low"] * outside,
                                                    high=rate["high"] * outside) if rate else None,
                formal_coverage=dict(estimate=formal_listed / scale(rate["estimate"]), low=formal_listed / scale(rate["high"]),
                                     high=formal_listed / scale(rate["low"])) if rate else None)


# ---------------------------------------------------------------- commands

def latest_crawl(root):
    crawls = sorted((Path(root) / OUTPUT).glob("crawl-*"))
    if not crawls:
        raise ValueError("No crawl directory")
    return crawls[-1]


def run_sample(root, crawl):
    dependents = read_dependents(crawl)
    chosen = draw_sample(dependents, frame_names(root))
    path = Path(root) / OUTPUT / "sample.csv"
    if path.exists():
        raise ValueError(f"Sample already drawn: {path}")
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows([("position", "repository"), *enumerate(chosen)])
    save_detail(Path(root) / OUTPUT / "sample_manifest.json",
                dict(rule_version=RULE_VERSION, seed=SEED, size=len(chosen), crawl=crawl.relative_to(root).as_posix(),
                     dependents_sha256=digest(crawl / "dependents.csv"), sample_sha256=digest(path),
                     created_at_utc=datetime.now(timezone.utc).isoformat()))
    return len(chosen)


def read_sample(root):
    with (Path(root) / OUTPUT / "sample.csv").open(encoding="utf-8", newline="") as stream:
        return [row["repository"] for row in csv.DictReader(stream)]


def run_collect(root, workers):
    config = tomllib.loads((Path(root) / "study.toml").read_text(encoding="utf-8"))["study"]
    anchor, token = timestamp(config["event_anchor"]), github_token()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(collect, name, root, anchor, token) for name in read_sample(root)]
        return Counter(job.result()["status"] for job in as_completed(jobs))


def run_summarize(root, crawl):
    root = Path(root)
    dependents = {name.lower() for name in read_dependents(crawl)}
    frame = frame_names(root)
    formal = formal_records(root)
    formal_listed = sum(r["repository"].lower() in dependents or (r.get("canonical_repository") or "").lower() in dependents
                        for r in formal.values())
    sample = [json.loads(output_path(root, name).read_text(encoding="utf-8"))["summary"]
              for name in read_sample(root) if output_path(root, name).exists()]
    covariates = {str(r["repository_id"]): r for r in
                  json.loads((root / "data/derived/control-v1/treated_covariates.json").read_text(encoding="utf-8"))}
    cohort_rows = [dict(covariates[i], target_full_sha=formal[i].get("target_full_sha", 0),
                        target_mutable=formal[i].get("target_mutable", 0)) for i in formal]
    pages = [json.loads(l) for l in (crawl / "pages.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  crawl=dict(directory=crawl.relative_to(root).as_posix(), pages=len(pages), total_shown=pages[0]["total_shown"],
                             first_fetch=pages[0]["fetched_at_utc"], last_fetch=pages[-1]["fetched_at_utc"],
                             listed_unique=len(dependents), dependents_sha256=digest(crawl / "dependents.csv")),
                  frame=dict(names=len(frame), listed=len(dependents & frame)),
                  formal=dict(repositories=len(formal), listed=formal_listed),
                  sample=dict(planned=len(read_sample(root)), collected=len(sample), seed=SEED),
                  **estimate(len(dependents), len(dependents & frame), formal_listed, sample),
                  # The page shows a larger count than it lists; scaling to that count is a sensitivity bound.
                  coverage_if_count_shown=estimate(pages[0]["total_shown"], len(dependents & frame), formal_listed,
                                                   sample)["formal_coverage"],
                  # The formal cohort holds no forks; forks inherit workflows, so the like-for-like figure excludes them.
                  formal_forks=sum(bool(r.get("current_fork")) for r in formal.values()),
                  sample_forks=sum(bool(r.get("current_fork")) for r in sample if r["status"] == "confirmed"),
                  non_fork=estimate(len(dependents), len(dependents & frame), formal_listed, sample, not_fork),
                  characteristics=dict(formal=describe(cohort_rows),
                                       sample_pre_incident=describe([r for r in sample if r["status"] == "confirmed"]),
                                       sample_pre_incident_non_fork=describe([r for r in sample if r["status"] == "confirmed"
                                                                              and not_fork(r)])),
                  implementation_sha256=digest(Path(__file__)),
                  scope="Current dependents only: pre-incident dependents that later removed the Action are not listed.")
    save_detail(root / OUTPUT / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"), result)
    save_detail(root / "reports/coverage_audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sample", "collect", "summarize"))
    parser.add_argument("--crawl")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("workers must be 1..8")
    root = Path(__file__).resolve().parents[1]
    crawl = root / args.crawl if args.crawl else latest_crawl(root)
    if args.command == "sample":
        print(json.dumps(dict(sampled=run_sample(root, crawl))))
    elif args.command == "collect":
        print(json.dumps(dict(run_collect(root, args.workers))))
    else:
        result = run_summarize(root, crawl)
        print(json.dumps({k: result[k] for k in ("crawl", "frame", "formal", "sample_status", "pre_incident_rate", "formal_coverage")},
                         indent=2))


if __name__ == "__main__":
    main()
