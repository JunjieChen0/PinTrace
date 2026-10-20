"""Collect pre-anchor baselines for the control candidate pool without touching the treated cohort ledger.

Each record has the same shape as a cohort-v1 baseline file, so the existing measurement code can replay it. A control is
eligible when its observable first-parent baseline parses completely, holds at least one action reference and does not
reference the target action. Git-derived activity covariates are recorded here and used later for matching.
"""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import tomllib

import requests

from .cohort import digest, save_detail
from .github import GitHub, github_token
from .history import clone, first_parent_history, git
from .pilot import snapshot, timestamp
from .workflow import TARGET

RULE_VERSION = "control-baseline-v1"
DIRECTORY = "data/derived/control-v1/baselines"
CLONES = "data/cache/control-repos"
RETRYABLE = {"collection_failed", "identity_pending", "blob_unavailable"}
DAY = 86400


def output_path(root, repository):
    return Path(root) / DIRECTORY / (repository.replace("/", "__") + ".json")


def eligible_controls(root, selected_ids):
    """One baseline record per selected repository ID. Renamed repositories can appear under two names; as in the
    treated cohort, the alphabetically first name represents the ID and a differing baseline commit is flagged."""
    selected = {str(identity) for identity in selected_ids}
    grouped = {}
    for path in sorted((Path(root) / DIRECTORY).glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))["summary"]
        if row.get("status") == "control_eligible" and row.get("repository_id") in selected:
            row["cohort_record_path"] = path.relative_to(root).as_posix()
            row["cohort_record_sha256"] = digest(path)
            grouped.setdefault(row["repository_id"], []).append(row)
    records = []
    for identity in sorted(grouped, key=int):
        rows = sorted(grouped[identity], key=lambda row: row["repository"].lower())
        records.append(dict(rows[0], candidate_aliases=[row["repository"] for row in rows],
                            alias_baseline_conflict=len({row["baseline_commit"] for row in rows}) > 1))
    return records


def activity(history, anchor, days):
    """First-parent commits whose committer time falls in the window before the anchor."""
    return sum(anchor - days * DAY <= point["timestamp"] < anchor for point in history)


def reference_profile(references):
    """Action references other than the target; local, docker and reusable-workflow entries are excluded as in the pipeline."""
    actions = [ref for ref in references if ref["kind"] == "action" and ref["action"] != TARGET]
    mutable = sum(ref["ref_type"] != "full_sha" for ref in actions)
    return dict(action_references=len(actions), mutable_references=mutable,
                mutable_share=mutable / len(actions) if actions else None,
                target_references=sum(ref["kind"] == "action" and ref["action"] == TARGET for ref in references))


def classify(state):
    if not state["complete"]:
        return "parse_incomplete"
    profile = reference_profile(state["references"])
    if profile["target_references"]:
        return "target_present"
    return "control_eligible" if profile["action_references"] else "no_action_references"


def collect(row, root, config, token, retry=False):
    repository = row["repository"]
    root = Path(root)
    output = output_path(root, repository)
    if output.exists():
        saved = json.loads(output.read_text(encoding="utf-8"))
        if not retry or saved["summary"]["status"] not in RETRYABLE:
            return saved["summary"]
    anchor = timestamp(config["event_anchor"])
    record = dict(repository=repository, status="identity_pending", rule_version=RULE_VERSION,
                  checked_at_utc=datetime.now(timezone.utc).isoformat(), event_anchor_utc=config["event_anchor"],
                  roster_commits=int(row["commits"]), roster_commit_stratum=int(row["roster_commit_stratum"]))
    state, phase = None, "identity"
    try:
        client = GitHub(root / "data/cache", token)
        metadata = client.api("repos/" + repository)
        record.update(repository_id=str(metadata["id"]), canonical_repository=metadata["full_name"],
                      observed_default_branch=metadata["default_branch"], current_archived=metadata["archived"],
                      current_fork=metadata["fork"], owner_type=(metadata.get("owner") or {}).get("type"))
        if metadata.get("private"):
            record.update(status="inaccessible_unknown", error="Repository is no longer public")
        else:
            phase = "history"
            name = repository.replace("/", "__") + ".git"
            directory = clone(metadata["full_name"], root / CLONES / name)
            history = first_parent_history(directory)
            record.update(git_directory=directory.relative_to(root).as_posix(),
                          observed_head=history[0]["sha"] if history else None, ancestry_commits=len(history),
                          clock_inversions=sum(a["timestamp"] < b["timestamp"] for a, b in zip(history, history[1:])),
                          git_commits_180d=activity(history, anchor, 180), git_commits_365d=activity(history, anchor, 365))
            point = next((p for p in history if p["timestamp"] < anchor), None)
            if point is None:
                record["status"] = "no_pre_event_history"
            else:
                phase = "blobs"
                state = snapshot(client, directory, metadata["full_name"], point, {})
                record.update(status=classify(state), baseline_commit=state["sha"], baseline_date=state["date"],
                              baseline_parse_complete=state["complete"],
                              baseline_tree=git(directory, "rev-parse", state["sha"] + "^{tree}").decode().strip(),
                              baseline_workflow_files=len(state["files"]), **reference_profile(state["references"]))
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else None
        record.update(status="inaccessible_unknown" if phase == "identity" and code in (404, 451)
                      else "blob_unavailable" if phase == "blobs" else "collection_failed",
                      http_status=code, error=f"{phase}: HTTPError {code}")
    except Exception as error:
        record.update(status="identity_pending" if phase == "identity" else "collection_failed",
                      error=f"{phase}: {type(error).__name__}: {error}")
    save_detail(output, dict(summary=record, baseline_snapshot=state))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--pool", default="candidate_pool.csv", help="file name inside data/derived/control-v1")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or args.limit is not None and args.limit < 1:
        parser.error("workers must be 1..8 and limit positive")
    root = Path(__file__).resolve().parents[1]
    pool_path = root / "data/derived/control-v1" / args.pool
    with pool_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows = rows[:args.limit] if args.limit else rows
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    inputs = [pool_path, root / "study.toml", Path(__file__), *[root / "pintrace" / n for n in ("github.py", "history.py", "pilot.py", "workflow.py", "cohort.py")]]
    manifest = dict(rule_version=RULE_VERSION, started_at_utc=datetime.now(timezone.utc).isoformat(), arguments=vars(args),
                    planned=len(rows), source_sha256={p.relative_to(root).as_posix(): digest(p) for p in inputs})
    save_detail(root / "data/derived/control-v1" / ("baseline-run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json"), manifest)
    token = github_token()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(collect, row, root, config, token, args.retry) for row in rows]
        for job in as_completed(jobs):
            record = job.result()
            done += 1
            print(f"{done}/{len(rows)} {record['repository']} {record['status']}", flush=True)


if __name__ == "__main__":
    main()
