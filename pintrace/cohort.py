"""Lock the complete candidate ledger while preserving earlier frozen batches."""

import argparse
import csv
import hashlib
import json
import tomllib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests

from .baseline import baseline_status, pre_event_point
from .github import GitHub, github_token
from .history import clone, first_parent_history, git
from .pilot import iso, snapshot, timestamp
from .workflow import TARGET

RULE_VERSION = "cohort-v1"
RETRYABLE = {"collection_failed", "identity_pending", "blob_unavailable"}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_detail(path, detail):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A same-directory temporary file avoids reading a partially written checkpoint.
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def output_path(root, repository):
    return root / "data/derived/cohort-v1/baselines" / (repository.replace("/", "__") + ".json")


def reused_detail(root, repository):
    extra = root / "data/derived/baseline-jss-extra-v1" / (repository.replace("/", "__") + ".json")
    if extra.exists():
        detail = json.loads(extra.read_text(encoding="utf-8"))
        return detail["summary"], detail["baseline_snapshot"], extra
    # Windows paths are case-insensitive, as are GitHub repository names.
    pilot = root / "data/derived/repositories" / (repository.replace("/", "__") + ".json")
    if pilot.exists():
        detail = json.loads(pilot.read_text(encoding="utf-8"))
        summary = detail["summary"]
        state = next((s for s in detail["snapshots"] if s["sha"] == summary.get("baseline_commit")), None)
        if state is not None:
            return summary, state, pilot
    return None


def collect(row, root, config, token, retry=False):
    repository = row["repository"]
    output = output_path(root, repository)
    if output.exists():
        saved = json.loads(output.read_text(encoding="utf-8"))
        if not retry or saved["summary"]["status"] not in RETRYABLE:
            return saved["summary"]
    anchor = timestamp(config["event_anchor"])
    record = {"repository": repository, "source_frames": row["source_frames"],
              "source_family": row["source_family"], "status": "identity_pending",
              "rule_version": RULE_VERSION, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "event_anchor_utc": config["event_anchor"]}
    state, provenance = None, None
    phase = "identity"
    try:
        reused = reused_detail(root, repository)
        if reused is not None and reused[0].get("status") == "unavailable":
            old, state, source = reused
            record.update(status="inaccessible_unknown", http_status=404 if "404" in old.get("error", "") else 451,
                          error=old.get("error"))
            provenance = {"source": source.relative_to(root).as_posix(), "sha256": digest(source)}
        else:
            client = GitHub(root / "data/cache", token)
            metadata = client.api("repos/" + repository)
            record.update(repository_id=str(metadata["id"]), canonical_repository=metadata["full_name"],
                          observed_default_branch=metadata["default_branch"],
                          current_archived=metadata["archived"], current_fork=metadata["fork"])
            if metadata.get("private"):
                record.update(status="inaccessible_unknown", error="Repository is no longer public")
            else:
                phase = "history"
                if reused is not None:
                    old, state, source = reused
                    record.update(observed_head=old["observed_head"],
                                  git_directory=("data/cache/baseline-repos/" if "baseline-jss" in str(source)
                                                 else "data/cache/repos/") + source.stem + ".git")
                    record["clock_inversions"] = old.get("clock_inversions", 0)
                    provenance = {"source": source.relative_to(root).as_posix(), "sha256": digest(source),
                                  "original_observation_time": old.get("checked_at_utc")}
                else:
                    name = repository.replace("/", "__") + ".git"
                    directory = root / "data/cache/cohort-repos" / name
                    if retry and directory.exists():
                        # Preserve incomplete clones instead of deleting or overwriting them.
                        try:
                            git(directory, "rev-parse", "HEAD")
                        except Exception:
                            directory = root / "data/cache/cohort-repos-retry" / name
                    directory = clone(metadata["full_name"], directory)
                    history = first_parent_history(directory)
                    record.update(git_directory=directory.relative_to(root).as_posix(),
                                  observed_head=history[0]["sha"] if history else None,
                                  ancestry_commits=len(history),
                                  clock_inversions=sum(a["timestamp"] < b["timestamp"]
                                                       for a, b in zip(history, history[1:])))
                    point = pre_event_point(history, anchor)
                    if point is None:
                        record["status"] = "no_pre_event_history"
                    else:
                        phase = "blobs"
                        state = snapshot(client, directory, metadata["full_name"], point, {})
                if state is not None:
                    directory = root / record["git_directory"]
                    counts = state["counts"].get(TARGET, {})
                    record.update(status=baseline_status(state), baseline_commit=state["sha"],
                                  baseline_date=state["date"], baseline_parse_complete=state["complete"],
                                  baseline_tree=git(directory, "rev-parse", state["sha"] + "^{tree}").decode().strip(),
                                  baseline_workflow_files=len(state["files"]),
                                  target_full_sha=counts.get("full_sha", 0),
                                  target_mutable=counts.get("mutable_or_unresolved", 0),
                                  evidence_url=f"https://github.com/{metadata['full_name']}/tree/{state['sha']}/.github/workflows")
    except requests.HTTPError as error:
        code = error.response.status_code if error.response is not None else None
        inaccessible = phase == "identity" and code in (404, 451)
        record.update(status="inaccessible_unknown" if inaccessible else "blob_unavailable" if phase == "blobs" else "collection_failed",
                      http_status=code, error=f"{phase}: HTTPError {code}")
    except Exception as error:
        record.update(status="identity_pending" if phase == "identity" else "collection_failed",
                      error=f"{phase}: {type(error).__name__}: {error}")
    save_detail(output, {"summary": record, "baseline_snapshot": state, "reused_source": provenance})
    return record


def summarize(records, planned):
    identities = defaultdict(list)
    for record in records:
        if record.get("repository_id"):
            identities[record["repository_id"]].append(record)
    return {"rule_version": RULE_VERSION, "planned_names": planned, "completed_names": len(records),
            "status_counts": dict(Counter(row["status"] for row in records)),
            "identity_or_inaccessible_code": sum(bool(row.get("repository_id")) or
                                                  (row["status"] == "inaccessible_unknown" and row.get("http_status") in (404, 451))
                                                  for row in records),
            "observed_unique_repository_ids": len(identities),
            "confirmed_unique_repository_ids": len({row["repository_id"] for row in records if row["status"] == "confirmed"}),
            "alias_groups": [[row["repository"] for row in group] for group in identities.values() if len(group) > 1],
            "incomplete_or_retryable": sum(row["status"] in RETRYABLE or row["status"] == "parse_incomplete" for row in records)}


def write_reports(root, records, candidates):
    by_name = {row["repository"]: row for row in records}
    ledger = [by_name.get(row["repository"], dict(row, status="pending")) for row in candidates]
    fields = sorted({key for row in ledger for key in row})
    path = root / "reports/cohort_flow.csv"
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(ledger)
    summary = summarize(records, len(candidates))
    (root / "reports/cohort_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("workers must be between 1 and 8")
    root = Path(__file__).resolve().parents[1]
    source = root / "reports/candidate_source_membership.csv"
    with source.open(encoding="utf-8", newline="") as input_file:
        candidates = sorted(csv.DictReader(input_file), key=lambda row: row["repository"])
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    inputs = [source, root / "study.toml", Path(__file__),
              *[root / "pintrace" / name for name in ("baseline.py", "github.py", "history.py", "pilot.py", "workflow.py")]]
    manifest = {"rule_version": RULE_VERSION, "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "arguments": vars(args), "candidate_names": len(candidates),
                "source_sha256": {path.relative_to(root).as_posix(): digest(path) for path in inputs},
                "preserved_batches": ["reports/run_manifest.json", "reports/baseline_manifest.json"]}
    manifest_path = root / "data/derived/cohort-v1" / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    save_detail(manifest_path, manifest)
    token = github_token()
    records = []
    write_reports(root, records, candidates)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(collect, row, root, config, token, args.retry) for row in candidates]
        for job in as_completed(jobs):
            record = job.result()
            records.append(record)
            write_reports(root, records, candidates)
            print(f"{len(records)}/{len(candidates)} {record['repository']} {record['status']}", flush=True)
    print(json.dumps(write_reports(root, records, candidates), indent=2))


if __name__ == "__main__":
    main()
