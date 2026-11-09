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
import gzip
import hashlib
import json
import math
import random
import statistics
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import coverage_crawl as crawler
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
COUNT_COVARIATES = ("git_commits_180d", "baseline_workflow_files", "action_references")
IMPLEMENTATION = ("pintrace/coverage_audit.py", "pintrace/coverage_crawl.py", "pintrace/baseline.py", "pintrace/cohort.py",
                  "pintrace/control_baseline.py", "pintrace/github.py", "pintrace/history.py", "pintrace/pilot.py",
                  "pintrace/workflow.py")
FIXED_INPUTS = ("study.toml", "reports/candidate_source_membership.csv", "reports/working_input_manifest.json",
                "data/derived/control-v1/treated_covariates.json", f"{OUTPUT}/sample.csv", f"{OUTPUT}/sample_manifest.json")


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


def verify_crawl(directory):
    """Re-derive the list from the raw pages and check the crawl end to end; raises on any inconsistency.

    Pages must be numbered 0..n-1 as recorded in complete.json; page 0 is the list's first page and each later page
    is the previous page's Next link; each raw page matches its SHA-256 and re-parses to the recorded rows and Next
    link; only the last page lacks a Next link, and it carries the list's disabled-Next end marker."""
    if not (directory / "complete.json").exists():
        raise ValueError(f"Crawl is not complete: {directory}")
    pages = [json.loads(line) for line in (directory / "pages.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    declared = json.loads((directory / "complete.json").read_text(encoding="utf-8"))["pages"]
    if [p["page"] for p in pages] != list(range(len(pages))) or len(pages) != declared:
        raise ValueError("Crawl pages are not numbered 0..n-1 as declared")
    with (directory / "dependents.csv").open(encoding="utf-8", newline="") as stream:
        listed = [(int(r["page"]), r["repository"]) for r in csv.DictReader(stream)]
    parsed = []
    for previous, page in zip([None, *pages], pages):
        expected_url = crawler.BASE if previous is None else previous["next"]
        if page["url"] != expected_url:
            raise ValueError(f"Page {page['page']} does not follow the previous page's Next link")
        raw = gzip.decompress((directory / "raw" / f"{page['page']:05d}.html.gz").read_bytes())
        if hashlib.sha256(raw).hexdigest() != page["sha256"]:
            raise ValueError(f"Raw page {page['page']} does not match its SHA-256")
        body = raw.decode("utf-8")
        repositories, following, _ = crawler.parse_page(body)
        if following != page["next"] or len(repositories) != page["rows"]:
            raise ValueError(f"Raw page {page['page']} does not re-parse to its record")
        last = page is pages[-1]
        if (following is None) != last or (last and not crawler.has_disabled_next(body)):
            raise ValueError(f"Page {page['page']} ends the list without the end marker, or the last page has a Next link")
        parsed += [(page["page"], name) for name in repositories]
    if parsed != listed:
        raise ValueError("dependents.csv differs from the rows re-parsed from the raw pages")
    return [name for _, name in parsed]


def read_dependents(directory):
    """Unique dependents in list order (case-insensitive), from a crawl that passes verify_crawl."""
    seen, ordered = set(), []
    for name in verify_crawl(directory):
        if name.lower() not in seen:
            seen.add(name.lower())
            ordered.append(name)
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


def smd_continuous(first, second):
    """Standardized mean difference, first minus second, over the pooled standard deviation; None if undefined."""
    if len(first) < 2 or len(second) < 2:
        return None
    pooled = ((statistics.variance(first) + statistics.variance(second)) / 2) ** 0.5
    return (statistics.mean(first) - statistics.mean(second)) / pooled if pooled else None


def smd_proportion(first, second):
    pooled = ((first * (1 - first) + second * (1 - second)) / 2) ** 0.5
    return (first - second) / pooled if pooled else None


def standardized_differences(cohort, sample):
    """Cohort minus sample. Counts are compared on log(1 + x) because they are heavily skewed."""
    output = {}
    for key in COVARIATES:
        transform = math.log1p if key in COUNT_COVARIATES else (lambda v: v)
        values = lambda rows: [transform(r[key]) for r in rows if r.get(key) is not None]
        output[key] = dict(smd=smd_continuous(values(cohort), values(sample)),
                           transform="log1p" if key in COUNT_COVARIATES else "none")
    for key, test in (("organization_share", lambda r: r.get("owner_type") == "Organization"),
                      ("target_all_sha_share", lambda r: r.get("target_full_sha", 0) > 0 and r.get("target_mutable", 0) == 0)):
        share = lambda rows: sum(map(test, rows)) / len(rows)
        output[key] = dict(smd=smd_proportion(share(cohort), share(sample)) if cohort and sample else None,
                           transform="proportion")
    return output


def not_fork(row):
    return not row.get("current_fork")


def identity_check(record, anchor):
    """The necessary identity conditions applied to the formal cohort (entity_identity.classify_group) that need no
    source roster: a valid current identity, and a current entity created before the anchor. A repository created
    later can carry pre-anchor history that belongs to another entity, e.g. a fork created after the incident."""
    reasons = []
    if not record.get("repository_id") or not record.get("canonical_repository"):
        reasons.append("current_metadata_identity_invalid")
    try:
        created = datetime.fromisoformat(str(record.get("created_at")).replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("no time zone")
    except ValueError:
        reasons.append("current_created_invalid")
    else:
        if created >= anchor:
            reasons.append("current_entity_created_not_before_anchor")
    return reasons


def apply_identity(rows, anchor):
    """Confirmed rows that fail the identity check become historical_identity_unknown, as in the formal cohort."""
    output = []
    for row in rows:
        reasons = identity_check(row, anchor) if row["status"] == "confirmed" else []
        output.append(dict(row, status="historical_identity_unknown", identity_reasons=reasons) if reasons else row)
    return output


def estimate(listed, frame_listed, formal_listed, sample_rows, eligible=lambda row: True):
    """Pre-incident dependents among current dependents, and the formal cohort's share of them. ``eligible`` narrows
    which confirmed rows count (for example non-forks); the denominator stays every resolved sampled row.

    ``missing_bounds`` treats every unresolved sampled row (failed, inaccessible, unparsed or of unknown identity) as
    not eligible (low) and as eligible (high), with every planned row in the denominator."""
    statuses = Counter(r["status"] for r in sample_rows)
    successes = sum(r["status"] == "confirmed" and eligible(r) for r in sample_rows)
    resolved = sum(statuses[s] for s in RESOLVED)
    rate = wilson(successes, resolved)
    outside = listed - frame_listed
    scale = lambda p: formal_listed + p * outside
    planned, unknown = len(sample_rows), len(sample_rows) - resolved
    low_rate, high_rate = successes / planned, (successes + unknown) / planned
    return dict(sample_status=dict(statuses), pre_incident_rate=rate, dependents_outside_frame=outside,
                estimated_pre_incident_outside=dict(estimate=rate["estimate"] * outside, low=rate["low"] * outside,
                                                    high=rate["high"] * outside) if rate else None,
                formal_coverage=dict(estimate=formal_listed / scale(rate["estimate"]), low=formal_listed / scale(rate["high"]),
                                     high=formal_listed / scale(rate["low"])) if rate else None,
                missing_bounds=dict(planned=planned, unknown=unknown, rate_low=low_rate, rate_high=high_rate,
                                    coverage_low=formal_listed / scale(high_rate), coverage_high=formal_listed / scale(low_rate)))


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


def bound_inputs(root, crawl):
    """Every file the summary reads, with its SHA-256. pages.jsonl binds each raw page through its recorded hash,
    which verify_crawl re-checks."""
    root = Path(root)
    paths = [*FIXED_INPUTS, *(crawl.relative_to(root).as_posix() + "/" + n for n in ("pages.jsonl", "dependents.csv", "complete.json"))]
    paths += [output_path(root, n).relative_to(root).as_posix() for n in read_sample(root) if output_path(root, n).exists()]
    paths += sorted(p.relative_to(root).as_posix() for p in (root / "data/derived/cohort-v1/baselines").glob("*.json"))
    return {path: digest(root / path) for path in paths}


def check_sample_manifest(root, crawl):
    """The sample being summarized is the one drawn, from this crawl, from this dependents list."""
    manifest = json.loads((Path(root) / OUTPUT / "sample_manifest.json").read_text(encoding="utf-8"))
    if manifest["crawl"] != crawl.relative_to(root).as_posix():
        raise ValueError(f"Sample was drawn from {manifest['crawl']}, not {crawl}")
    if manifest["sample_sha256"] != digest(Path(root) / OUTPUT / "sample.csv"):
        raise ValueError("sample.csv differs from the sample manifest")
    if manifest["dependents_sha256"] != digest(crawl / "dependents.csv"):
        raise ValueError("dependents.csv differs from the list the sample was drawn from")
    if manifest["size"] != len(read_sample(root)):
        raise ValueError("Sample size differs from the sample manifest")


def run_check(root):
    """Recompute every bound hash of reports/coverage_audit.json and re-verify the crawl; lists any difference."""
    root = Path(root)
    report = json.loads((root / "reports/coverage_audit.json").read_text(encoding="utf-8"))
    crawl = root / report["crawl"]["directory"]
    verify_crawl(crawl)
    check_sample_manifest(root, crawl)
    recorded = {**report["input_sha256"], **report["implementation_sha256"]}
    current = {**bound_inputs(root, crawl), **{p: digest(root / p) for p in IMPLEMENTATION}}
    differences = sorted(p for p in recorded.keys() | current.keys() if recorded.get(p) != current.get(p))
    return dict(ok=not differences, files=len(recorded), differences=differences)


def run_summarize(root, crawl):
    root = Path(root)
    check_sample_manifest(root, crawl)
    dependents = {name.lower() for name in read_dependents(crawl)}
    frame = frame_names(root)
    formal = formal_records(root)
    formal_listed = sum(r["repository"].lower() in dependents or (r.get("canonical_repository") or "").lower() in dependents
                        for r in formal.values())
    anchor = datetime.fromisoformat(tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]["event_anchor"]
                                    .replace("Z", "+00:00"))
    # A failed collection is not saved; it stays in the sample as collection_failed so that it reaches the bounds.
    saved = [json.loads(output_path(root, name).read_text(encoding="utf-8"))["summary"] if output_path(root, name).exists()
             else dict(repository=name, status="collection_failed") for name in read_sample(root)]
    sample = apply_identity(saved, anchor)
    non_fork_pre = [r for r in sample if r["status"] == "confirmed" and not_fork(r)]
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
                  sample=dict(planned=len(sample), collected=sum(r["status"] != "collection_failed" for r in sample), seed=SEED),
                  identity_excluded=dict(all=sum(r["status"] == "historical_identity_unknown" for r in sample),
                                         non_fork=sum(r["status"] == "historical_identity_unknown" and not_fork(r) for r in sample),
                                         reasons=dict(Counter(x for r in sample for x in r.get("identity_reasons", [])))),
                  population="Pre-incident dependents that the dependents list still shows. formal_coverage is the formal "
                             "repositories still listed over those plus the estimate outside the frame; the share of all "
                             "pre-incident dependents, including those that removed the Action, is not observable here.",
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
                                       sample_pre_incident_non_fork=describe(non_fork_pre),
                                       standardized_difference_non_fork=standardized_differences(cohort_rows, non_fork_pre)),
                  input_sha256=bound_inputs(root, crawl),
                  implementation_sha256={path: digest(root / path) for path in IMPLEMENTATION},
                  scope="Current dependents only: pre-incident dependents that later removed the Action are not listed.")
    save_detail(root / OUTPUT / "runs" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"), result)
    save_detail(root / "reports/coverage_audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sample", "collect", "summarize", "check"))
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
    elif args.command == "check":
        result = run_check(root)
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    else:
        result = run_summarize(root, crawl)
        print(json.dumps({k: result[k] for k in ("crawl", "frame", "formal", "sample_status", "pre_incident_rate", "formal_coverage")},
                         indent=2))


if __name__ == "__main__":
    main()
