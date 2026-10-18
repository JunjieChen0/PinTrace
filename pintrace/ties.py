"""Resolve the six archived equal-timestamp hints using commit DAG and trees."""

import argparse
import csv
import gzip
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .cohort import digest, output_path, save_detail
from .github import GitHub, github_token
from .history import git, workflow_files
from .pilot import timestamp


def topological_winner(commits, is_ancestor):
    commits = sorted(set(commits))
    candidates = [candidate for candidate in commits
                  if all(other == candidate or is_ancestor(other, candidate) for other in commits)]
    return candidates[0] if len(candidates) == 1 else None


def winner_tree_state(files):
    if any(row.get("source_content_matches") for row in files):
        return "live"
    deletions = [row for row in files if row.get("source_marker") == "deletion"]
    if deletions and all(row.get("resolved_path") and row["tree_path_present"] is False for row in deletions):
        return "deleted"
    return None


def extract_ties(root):
    source = root / "reports/source_frame_17301952.csv"
    with source.open(encoding="utf-8", newline="") as input_file:
        targets = [row for row in csv.DictReader(input_file) if row["archived_time_ambiguous"] == "True"]
    wanted = {(row["uid"], timestamp(row["archived_timestamp_utc"])) for row in targets}
    evidence = {uid: [] for uid, _ in wanted}
    metadata = root / "data/raw/zenodo-17301952/workflows.csv.gz"
    with gzip.open(metadata, "rt", encoding="utf-8", newline="") as input_file:
        for row in csv.DictReader(input_file):
            if (row["uid"], int(row["committed_date"])) in wanted:
                evidence[row["uid"]].append({key: row.get(key) for key in (
                    "repository", "uid", "commit_hash", "committed_date", "file_path",
                    "file_hash", "git_change_type", "valid_workflow")})
    result = {"source_sha256": {source.relative_to(root).as_posix(): digest(source),
                                metadata.relative_to(root).as_posix(): digest(metadata)},
              "targets": targets, "equal_timestamp_records": evidence}
    save_detail(root / "data/derived/cohort-v1/tie_source_records.json", result)
    return result


def ancestor(directory, before, after):
    result = subprocess.run(["git", "-C", str(directory), "merge-base", "--is-ancestor", before, after],
                            capture_output=True, timeout=180)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-500:])
    return result.returncode == 0


def resolve(root, target, rows, client):
    repository = target["repository_name_key"]
    evidence = {"repository": repository, "uid": target["uid"],
                "timestamp_utc": target["archived_timestamp_utc"],
                "status": "tie_unresolved", "source_records": rows}
    baseline_path = output_path(root, repository)
    if not baseline_path.exists():
        return dict(evidence, reason="baseline_collection_pending")
    summary = json.loads(baseline_path.read_text(encoding="utf-8"))["summary"]
    if not summary.get("git_directory"):
        return dict(evidence, reason=summary["status"])
    directory = root / summary["git_directory"]
    canonical = summary["canonical_repository"]
    commits = sorted({row["commit_hash"] for row in rows})
    objects = []
    try:
        order = git(directory, "rev-list", "--first-parent", summary["observed_head"]).decode().splitlines()
        positions = {sha: index for index, sha in enumerate(order)}
        for commit in commits:
            info = {"commit": commit,
                    "tree": git(directory, "rev-parse", "--verify", commit + "^{tree}").decode().strip(),
                    "parents": git(directory, "show", "-s", "--format=%P", commit).decode().strip().split(),
                    "first_parent_position_from_head": positions.get(commit), "files": []}
            files = {row["path"]: row for row in workflow_files(directory, commit)}
            for row in [row for row in rows if row["commit_hash"] == commit]:
                deletion = row["git_change_type"] == "D" and not row["file_path"] and not row["file_hash"]
                paths = ([row["file_path"]] if row["file_path"] else
                         sorted({r["file_path"] for r in rows if r["file_path"]}) if deletion else [""])
                for path in paths:
                    item = files.get(path)
                    file_evidence = {"path": row["file_path"], "resolved_path": path,
                                     "source_content_sha256": row["file_hash"],
                                     "source_marker": "deletion" if deletion else None,
                                     "tree_path_present": item is not None}
                    if item is not None and item["kind"] == "blob" and item["mode"] in ("100644", "100755"):
                        content = client.blob(canonical, commit, path, item["blob"])
                        file_evidence.update(blob=item["blob"], content_sha256=hashlib.sha256(content).hexdigest(),
                                             source_content_matches=hashlib.sha256(content).hexdigest() == row["file_hash"])
                    info["files"].append(file_evidence)
            objects.append(info)
        comparisons = [{"before": a, "after": b, "is_ancestor": ancestor(directory, a, b)}
                       for a in commits for b in commits if a != b]
        lookup = {(row["before"], row["after"]): row["is_ancestor"] for row in comparisons}
        winner = topological_winner(commits, lambda a, b: lookup[(a, b)])
        evidence.update(objects=objects, dag_comparisons=comparisons,
                        baseline_commit=summary.get("baseline_commit"), baseline_tree=summary.get("baseline_tree"))
        if winner is None:
            return dict(evidence, reason="incomparable_commit_branches", interval_commits=commits)
        observed = next(row for row in objects if row["commit"] == winner)
        matching = [row for row in observed["files"] if row.get("source_content_matches")]
        winning_state = winner_tree_state(observed["files"])
        if winning_state is None:
            return dict(evidence, reason="winning_tree_does_not_match_source_live_content",
                        interval_commits=commits)
        return dict(evidence, status="resolved_dag_tree", winning_commit=winner,
                    winning_first_parent_position=positions.get(winner),
                    winning_state=winning_state,
                    winning_blobs=sorted({row["blob"] for row in matching}),
                    reason="unique_topological_descendant_and_verified_" +
                           ("deletion" if winning_state == "deleted" else "tree_content"))
    except Exception as error:
        return dict(evidence, objects=objects, reason=f"{type(error).__name__}: {error}", interval_commits=commits)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract-only", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source_path = root / "data/derived/cohort-v1/tie_source_records.json"
    source = json.loads(source_path.read_text(encoding="utf-8")) if source_path.exists() else extract_ties(root)
    if args.extract_only:
        print(json.dumps({"target_count": len(source["targets"]),
                          "records_per_uid": {key: len(value) for key, value in source["equal_timestamp_records"].items()}}, indent=2))
        return
    client = GitHub(root / "data/cache", github_token())
    results = [resolve(root, target, source["equal_timestamp_records"][target["uid"]], client)
               for target in source["targets"]]
    for index, row in enumerate(results, 1):
        path = root / "data/derived/cohort-v1/ties" / f"tie-{index}.json"
        save_detail(path, row)
    with (root / "reports/tie_resolution.csv").open("w", encoding="utf-8", newline="") as output:
        fields = ["repository", "uid", "timestamp_utc", "status", "winning_commit", "reason", "evidence_path"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows({**{key: row.get(key) for key in fields},
                          "evidence_path": f"data/derived/cohort-v1/ties/tie-{index}.json"}
                         for index, row in enumerate(results, 1))
    save_detail(root / "reports/tie_resolution_summary.json", {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "total": len(results),
        "resolved": sum(row["status"] == "resolved_dag_tree" for row in results),
        "tie_unresolved": sum(row["status"] == "tie_unresolved" for row in results),
        "source_records_sha256": digest(source_path), "implementation_sha256": digest(Path(__file__))})
    print(json.dumps([{key: row.get(key) for key in ("repository", "status", "winning_commit", "reason")}
                      for row in results], indent=2))


if __name__ == "__main__":
    main()
