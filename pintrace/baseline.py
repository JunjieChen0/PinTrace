"""Check additional historical candidates without rewriting the frozen pilot."""

import argparse
import csv
import hashlib
import json
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from .github import GitHub, github_token
from .history import clone, first_parent_history, git
from .pilot import iso, snapshot, timestamp
from .workflow import TARGET


def pre_event_point(history, anchor):
    # The list is in ancestry order, which must not be replaced by date sorting.
    return next((point for point in history if point["timestamp"] < anchor), None)


def baseline_status(state):
    if not state["complete"]:
        return "parse_incomplete"
    return "confirmed" if TARGET in state["counts"] else "not_at_baseline"


def collect(row, root, anchor, token):
    repository = row["repository"]
    name = repository.replace("/", "__")
    output = root / "data/derived/baseline-jss-extra-v1" / (name + ".json")
    if output.exists():
        return json.loads(output.read_text(encoding="utf-8"))["summary"]
    record = {"repository": repository, "status": "failed",
              "source_frames": row["source_frames"], "source_family": row["source_family"],
              "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "event_anchor_utc": iso(anchor)}
    state = None
    try:
        client = GitHub(root / "data/cache", token)
        metadata = client.api("repos/" + repository)
        if metadata.get("private"):
            raise ValueError("Repository is no longer public")
        canonical = metadata["full_name"]
        record.update(repository_id=str(metadata["id"]), canonical_repository=canonical,
                      observed_default_branch=metadata["default_branch"],
                      current_archived=metadata["archived"], current_fork=metadata["fork"])
        directory = clone(canonical, root / "data/cache/baseline-repos" / (name + ".git"))
        history = first_parent_history(directory)
        if not history:
            raise ValueError("No observable first-parent commits")
        observed_branch = git(directory, "symbolic-ref", "--short", "HEAD").decode().strip()
        record.update(observed_head=history[0]["sha"], cloned_branch=observed_branch,
                      metadata_branch_matches=observed_branch == metadata["default_branch"],
                      ancestry_commits=len(history),
                      clock_inversions=sum(a["timestamp"] < b["timestamp"]
                                           for a, b in zip(history, history[1:])))
        point = pre_event_point(history, anchor)
        if point is None:
            record["status"] = "no_pre_event_history"
        else:
            state = snapshot(client, directory, canonical, point, {})
            counts = state["counts"].get(TARGET, {})
            record.update(status=baseline_status(state), baseline_commit=point["sha"],
                          baseline_date=state["date"], baseline_parse_complete=state["complete"],
                          baseline_workflow_files=len(state["files"]),
                          target_full_sha=counts.get("full_sha", 0),
                          target_mutable=counts.get("mutable_or_unresolved", 0),
                          evidence_url=f"https://github.com/{canonical}/tree/{point['sha']}/.github/workflows")
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else None
        record.update(status="unavailable" if code in (404, 451) else "failed",
                      error=f"HTTPError: {code}")
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"summary": record, "baseline_snapshot": state},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def summarize(records):
    identities = {}
    for record in records:
        if record["status"] == "confirmed":
            identities.setdefault(record["repository_id"], []).append(record["repository"])
    return {"completed": len(records), "status_counts": dict(Counter(r["status"] for r in records)),
            "confirmed_unique_repository_ids": len(identities),
            "confirmed_identity_aliases": [names for names in identities.values() if len(names) > 1],
            "clock_inversion_repositories": sum(r.get("clock_inversions", 0) > 0 for r in records)}


def write_reports(root, records, total):
    fields = ["repository", "status", "repository_id", "canonical_repository", "source_frames",
              "source_family", "checked_at_utc", "event_anchor_utc", "observed_default_branch",
              "cloned_branch", "metadata_branch_matches", "observed_head", "ancestry_commits",
              "clock_inversions", "baseline_commit", "baseline_date", "baseline_parse_complete",
              "baseline_workflow_files", "target_full_sha", "target_mutable", "current_archived",
              "current_fork", "evidence_url", "error"]
    with (root / "reports/baseline_results.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(records, key=lambda record: record["repository"].lower()))
    summary = dict(summarize(records), planned=total,
                   scope="Additional JSS historical names absent from zenodo:17301952 candidates",
                   limitations=["Current default-branch ancestry does not prove historical branch identity or absence of force pushes.",
                                "Committer timestamps may be nonmonotonic or modified; clock anomalies are recorded.",
                                "No execution, compromise, incident causation, or full-window coverage is inferred."])
    (root / "reports/baseline_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=91)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.limit < 1 or not 1 <= args.workers <= 8:
        parser.error("limit must be positive; workers must be between 1 and 8")
    root = Path(__file__).resolve().parents[1]
    source = root / "reports/candidate_source_membership.csv"
    with source.open(encoding="utf-8", newline="") as input_file:
        candidates = [row for row in csv.DictReader(input_file)
                      if "zenodo:18414913" in json.loads(row["source_frames"])
                      and "zenodo:17301952" not in json.loads(row["source_frames"])]
    candidates = sorted(candidates, key=lambda row: row["repository"].lower())[:args.limit]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    anchor = timestamp(config["event_anchor"])
    inputs = [source, root / "study.toml", Path(__file__),
              *[root / "pintrace" / name for name in ("github.py", "history.py", "pilot.py", "workflow.py")]]
    manifest = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "arguments": vars(args),
                "planned": len(candidates), "repositories": [row["repository"] for row in candidates],
                "source_sha256": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in inputs}}
    (root / "reports/baseline_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    token = github_token()
    records = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(collect, row, root, anchor, token) for row in candidates]
        for job in as_completed(jobs):
            record = job.result()
            records.append(record)
            write_reports(root, records, len(candidates))
            print(f"{len(records)}/{len(candidates)} {record['repository']} {record['status']}", flush=True)
    print(json.dumps(write_reports(root, records, len(candidates)), indent=2))


if __name__ == "__main__":
    main()
