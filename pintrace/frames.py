"""Export archive candidate evidence without changing frozen pilot outputs."""

import argparse
import csv
import gzip
import hashlib
import json
import re
import tarfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .workflow import TARGET, parse_workflow


def pre_event_states(rows, cutoff):
    latest = {}
    repositories = set()
    total = 0
    for row in rows:
        total += 1
        repositories.add(row["repository"].lower())
        timestamp = int(row["committed_date"])
        if timestamp >= cutoff:
            continue
        key = (row["repository"].lower(), row["uid"])
        value = {key: row[key] for key in (
            "repository", "uid", "commit_hash", "file_hash", "file_path", "valid_workflow")}
        value["timestamp"] = timestamp
        value["git_change_type"] = row.get("git_change_type")
        previous = latest.get(key)
        value["latest_time_ambiguous"] = False
        if previous and timestamp == previous["timestamp"]:
            ambiguous = previous["latest_time_ambiguous"] or value["file_hash"] != previous["file_hash"]
            previous["latest_time_ambiguous"] = value["latest_time_ambiguous"] = ambiguous
        if previous is None or (timestamp, value["commit_hash"]) > (previous["timestamp"], previous["commit_hash"]):
            latest[key] = value
        if total % 500000 == 0:
            print(f"Metadata rows: {total}", flush=True)
    return list(latest.values()), {
        "metadata_rows": total, "workflow_repositories": len(repositories),
        "latest_timestamp_tie_histories": sum(row["latest_time_ambiguous"] for row in latest.values()),
    }


def usable_archive_state(row):
    return (row["valid_workflow"] == "True" and bool(row["file_path"])
            and bool(re.fullmatch(r"[0-9a-f]{64}", row["file_hash"])))


def scan_archive(path, required):
    found, missing = {}, set(required)
    members = 0
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            members += 1
            if members % 500000 == 0:
                print(f"Archive members: {members}", flush=True)
            digest = Path(member.name).name
            if not member.isfile() or digest not in missing:
                continue
            content = archive.extractfile(member).read()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Archive content hash mismatch: " + digest)
            missing.remove(digest)
            if TARGET.encode() not in content.lower():
                continue
            parsed = parse_workflow(content.decode("utf-8"), ".github/workflows/archived.yml")
            references = [ref for ref in parsed["references"]
                          if ref["kind"] == "action" and ref["action"] == TARGET]
            found[digest] = {"references": references, "errors": parsed["errors"]}
    return found, {"archive_members": members, "missing_required_content_hashes": len(missing),
                   "missing_content_hashes": sorted(missing)}


def candidate_evidence(states, contents, record):
    evidence = []
    for state in states:
        parsed = contents.get(state["file_hash"])
        if not parsed or parsed["errors"] or not parsed["references"]:
            continue
        refs = parsed["references"]
        evidence.append({
            "repository_name_key": state["repository"].lower(),
            "repository": state["repository"], "source_frame": "zenodo:" + record,
            "source_family": "zenodo:10259013", "identity_status": "name_only_unverified",
            "baseline_status": "archive_hint_requires_git_confirmation",
            "uid": state["uid"], "archived_path": state["file_path"],
            "archived_commit": state["commit_hash"], "archived_content_sha256": state["file_hash"],
            "archived_timestamp_utc": datetime.fromtimestamp(state["timestamp"], timezone.utc).isoformat(),
            "archived_time_ambiguous": state["latest_time_ambiguous"],
            "target_references": len(refs),
            "full_sha_references": sum(ref["ref_type"] == "full_sha" for ref in refs),
        })
    return sorted(evidence, key=lambda row: (row["repository_name_key"], row["uid"]))


def export_frame(root, record, anchor):
    raw = root / "data/raw" / ("zenodo-" + record)
    metadata_path = raw / "record.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    hashes = {}
    for item in metadata["files"]:
        if item["key"] not in {"workflows.csv.gz", "workflows.tar.gz", "repositories.csv.gz"}:
            continue
        path = raw / item["key"]
        with path.open("rb") as source:
            digest = "md5:" + hashlib.file_digest(source, "md5").hexdigest()
        if path.stat().st_size != item["size"] or digest != item["checksum"]:
            raise ValueError("Source checksum failed: " + item["key"])
        hashes[item["key"]] = {"bytes": item["size"], "md5": digest}
    cutoff = int(datetime.fromisoformat(anchor.replace("Z", "+00:00")).timestamp())
    with gzip.open(raw / "workflows.csv.gz", "rt", encoding="utf-8", newline="") as source:
        states, counts = pre_event_states(csv.DictReader(source), cutoff)
    source_flag_valid = [row for row in states if row["valid_workflow"] == "True"]
    invalid_markers = [row for row in source_flag_valid if not usable_archive_state(row)]
    live = [row for row in source_flag_valid if usable_archive_state(row)]
    contents, archive_counts = scan_archive(raw / "workflows.tar.gz", {row["file_hash"] for row in live})
    evidence = candidate_evidence(live, contents, record)
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    evidence_path = reports / ("source_frame_" + record + ".csv")
    fields = ["repository_name_key", "repository", "source_frame", "source_family", "identity_status",
              "baseline_status", "uid", "archived_path", "archived_commit", "archived_content_sha256",
              "archived_timestamp_utc", "archived_time_ambiguous", "target_references", "full_sha_references"]
    with evidence_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(evidence)
    repositories = {row["repository_name_key"] for row in evidence}
    with gzip.open(raw / "repositories.csv.gz", "rt", encoding="utf-8", newline="") as source:
        source_roster_size = sum(1 for _ in csv.DictReader(source))
    audit = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "record": record,
        "conceptrecid": metadata.get("conceptrecid"), "anchor": anchor,
        "source_roster_size": source_roster_size, **counts, **archive_counts,
        "latest_pre_anchor_histories": len(states), "source_flag_valid_histories": len(source_flag_valid),
        "usable_source_valid_histories": len(live),
        "invalid_content_or_path_markers": len(invalid_markers),
        "invalid_markers_by_change_type": {kind: sum(row["git_change_type"] == kind for row in invalid_markers)
                                           for kind in sorted({row["git_change_type"] or "unknown"
                                                               for row in invalid_markers})},
        "invalid_marker_examples": invalid_markers[:5],
        "candidate_repositories": len(repositories), "candidate_workflow_evidence_rows": len(evidence),
        "candidate_time_ambiguous_rows": sum(row["archived_time_ambiguous"] for row in evidence),
        "missing_content_workflow_evidence": [row for row in live if row["file_hash"] in
                                             archive_counts["missing_content_hashes"]][:10],
        "unresolved_target_content_hashes": sum(bool(row["errors"]) for row in contents.values()),
        "source_files": hashes,
        "source_metadata_sha256": hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        "candidate_evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "limitations": ["Name keys do not establish stable GitHub repository identity.",
                       "Archive candidates require Git baseline confirmation.",
                       "Source valid_workflow filtering is retained for pilot comparability.",
                       "Timestamp ties use lexical commit ordering as in the pilot and require review.",
                       "The source repository roster was selected after the incident."],
    }
    (reports / ("source_frame_" + record + "_audit.json")).write_text(
        json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in audit.items()
                      if key not in {"invalid_marker_examples", "missing_content_workflow_evidence"}},
                     indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", default="17301952", choices=["17301952"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    export_frame(root, args.record, config["event_anchor"])


if __name__ == "__main__":
    main()
