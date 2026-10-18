"""Audit the complete identity ledger and cached baseline Git objects."""

import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .cohort import RETRYABLE, digest, output_path, save_detail
from .history import git, workflow_files
from .workflow import action_counts


def verify_blobs(state, directory):
    required, missing, mismatches = set(), [], []
    for item in state.get("files", []):
        if item["kind"] != "blob" or item["mode"] not in ("100644", "100755"):
            continue
        blob = item["blob"]
        if blob in required:
            continue
        required.add(blob)
        path = directory / blob
        if not path.exists():
            missing.append(blob)
            continue
        content = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if actual != blob:
            mismatches.append(blob)
    return {"required_blobs": len(required), "missing_blobs": missing, "mismatched_blobs": mismatches}


def audit(root):
    source = root / "reports/candidate_source_membership.csv"
    with source.open(encoding="utf-8", newline="") as input_file:
        candidates = list(csv.DictReader(input_file))
    problems, records, identities = [], [], defaultdict(list)
    required, missing, mismatched, known_id_or_code = 0, 0, 0, 0
    manifests = {}
    for row in candidates:
        repository = row["repository"]
        path = output_path(root, repository)
        if not path.exists():
            problems.append({"repository": repository, "reason": "baseline_pending"})
            continue
        detail = json.loads(path.read_text(encoding="utf-8"))
        record, state = detail["summary"], detail["baseline_snapshot"]
        records.append(record)
        manifests[path.relative_to(root).as_posix()] = digest(path)
        has_identity = bool(record.get("repository_id"))
        has_code = record["status"] == "inaccessible_unknown" and record.get("http_status") in (404, 451)
        known_id_or_code += has_identity or has_code
        if has_identity:
            identities[record["repository_id"]].append(record)
        if not has_identity and not has_code:
            problems.append({"repository": repository, "reason": "identity_or_error_code_missing"})
        if record["status"] in RETRYABLE:
            problems.append({"repository": repository, "reason": record["status"], "error": record.get("error")})
        if state is None:
            continue
        blobs = verify_blobs(state, root / "data/cache/blobs")
        required += blobs["required_blobs"]
        missing += len(blobs["missing_blobs"])
        mismatched += len(blobs["mismatched_blobs"])
        if blobs["missing_blobs"] or blobs["mismatched_blobs"]:
            problems.append({"repository": repository, "reason": "blob_identity_or_availability", **blobs})
        if state["counts"] != action_counts(state["references"]):
            problems.append({"repository": repository, "reason": "saved_reference_counts_differ"})
        try:
            directory = root / record["git_directory"]
            tree = git(directory, "rev-parse", state["sha"] + "^{tree}").decode().strip()
            actual = {(item["path"], item["blob"], item["mode"], item["kind"])
                      for item in workflow_files(directory, state["sha"])}
            saved = {(item["path"], item["blob"], item["mode"], item["kind"]) for item in state["files"]}
            if actual != saved or tree != record["baseline_tree"]:
                problems.append({"repository": repository, "reason": "baseline_tree_differs"})
        except Exception as error:
            problems.append({"repository": repository, "reason": "tree_check_failed", "error": str(error)})
    aliases = []
    for identity, group in identities.items():
        if len(group) <= 1:
            continue
        values = {(record.get("baseline_commit"), record.get("baseline_tree"), record["status"]) for record in group}
        aliases.append({"repository_id": identity, "names": [record["repository"] for record in group],
                        "baseline_agrees": len(values) == 1})
        if len(values) > 1:
            problems.append({"repository_id": identity, "reason": "alias_baselines_disagree"})
    archive = json.loads((root / "reports/source_frame_17301952_audit.json").read_text(encoding="utf-8"))
    tie_path = root / "reports/tie_resolution.csv"
    ties = list(csv.DictReader(tie_path.open(encoding="utf-8", newline=""))) if tie_path.exists() else []
    tie_complete = len(ties) == 6 and all(row["reason"] != "baseline_collection_pending" for row in ties)
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "candidate_names": len(candidates),
              "completed_names": len(records), "stable_id_or_error_code_names": known_id_or_code,
              "observed_unique_repository_ids": len(identities), "alias_groups": aliases,
              "required_blob_instances": required, "missing_blob_instances": missing,
              "missing_blob_rate": missing / required if required else None,
              "mismatched_blob_instances": mismatched,
              "archive_empty_or_path_markers": archive["invalid_content_or_path_markers"],
              "archive_real_missing_content_hashes": archive["missing_required_content_hashes"],
              "six_ties_accounted_for": tie_complete, "problems": problems,
              "baseline_freeze_ready": len(records) == len(candidates) == known_id_or_code == 580
                                       and not problems and tie_complete,
              "source_sha256": {source.relative_to(root).as_posix(): digest(source),
                                "implementation": digest(Path(__file__))},
              "note": "Parse-incomplete histories remain unknown; they are never classified as absence."}
    save_detail(root / "data/derived/cohort-v1/verified_baseline_manifest.json", manifests)
    save_detail(root / "reports/cohort_verification.json", result)
    return result


def main():
    root = Path(__file__).resolve().parents[1]
    result = audit(root)
    print(json.dumps({key: value for key, value in result.items() if key not in ("alias_groups", "problems")}, indent=2))
    if result["problems"]:
        print(json.dumps(result["problems"], indent=2))


if __name__ == "__main__":
    main()
