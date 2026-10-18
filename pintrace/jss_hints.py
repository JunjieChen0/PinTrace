"""Recover historical change hints, never baseline exposure, from JSS diffs."""

import ast
import csv
import hashlib
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .workflow import TARGET, parse_workflow


def references_from_diff(path, value):
    """Wrap only workflow locations that can actually contain direct uses."""
    if path == ["jobs", "<job_id>", "steps", "Number", "uses"]:
        document = {"jobs": {"observed": {"steps": [{"uses": value}]}}}
    else:
        if path not in [[], ["jobs"], ["jobs", "<job_id>"],
                        ["jobs", "<job_id>", "steps"], ["jobs", "<job_id>", "steps", "Number"]]:
            return [], None
        if not isinstance(value, str) or len(value) > 1_000_000:
            return [], "unsupported_or_large_literal"
        try:
            node = ast.literal_eval(value)
        except (ValueError, SyntaxError, RecursionError):
            return [], "unparsed_literal"
        if not path:
            document = node
        elif len(path) == 1:
            document = {"jobs": node}
        elif len(path) == 2:
            document = {"jobs": {"observed": node}}
        elif len(path) == 3:
            document = {"jobs": {"observed": {"steps": node}}}
        else:
            document = {"jobs": {"observed": {"steps": [node]}}}
    parsed = parse_workflow(yaml.safe_dump(document), ".github/workflows/derived-hint.yml")
    if parsed["errors"]:
        return [], "parsed_fragment_requires_review"
    return [ref for ref in parsed["references"] if ref["kind"] == "action" and ref["action"] == TARGET], None


def main():
    import pyarrow as arrow
    import pyarrow.compute as compute
    import pyarrow.parquet as parquet

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    cutoff = datetime.fromisoformat(config["event_anchor"].replace("Z", "+00:00"))
    cutoff = cutoff.astimezone(timezone.utc).replace(tzinfo=None)
    path = root / "data/raw/source-audit-20261018/path_df.parquet"
    audit = json.loads((root / "reports/source_package_audit.json").read_text(encoding="utf-8"))
    if not audit["jss_full_archive_verified"]:
        raise ValueError("Verified JSS package required")
    expected = audit["jss"]["parquet"]["code/data/path_df.parquet"]["sha256"]
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != expected:
            raise ValueError("JSS path table hash mismatch")
    table = parquet.ParquetFile(path)
    hints, skipped = {}, {}
    candidate_rows, scanned = 0, 0
    columns = ["repository", "commit_hash", "committed_date", "uid", "diff_type", "value", "cleaned_path"]
    for batch in table.iter_batches(batch_size=65536, columns=columns):
        scanned += batch.num_rows
        mask = compute.and_(
            compute.less(batch.column("committed_date"), arrow.scalar(cutoff, type=arrow.timestamp("ns"))),
            compute.match_substring(compute.utf8_lower(batch.column("value")), TARGET))
        for row in batch.filter(mask).to_pylist():
            candidate_rows += 1
            refs, error = references_from_diff(row["cleaned_path"], row["value"])
            if error:
                skipped[error] = skipped.get(error, 0) + 1
            for ref in refs:
                key = (row["repository"].lower(), row["commit_hash"], row["uid"], row["diff_type"], ref["value"])
                hints[key] = {"repository_name_key": row["repository"].lower(), "repository": row["repository"],
                              "commit": row["commit_hash"], "date_utc": row["committed_date"].replace(tzinfo=timezone.utc).isoformat(),
                              "uid": row["uid"], "diff_type": row["diff_type"], "uses_value": ref["value"],
                              "source_frame": "zenodo:18414913", "source_family": "zenodo:10259013",
                              "baseline_status": "historical_change_hint_requires_git_confirmation"}
    fields = ["repository_name_key", "repository", "commit", "date_utc", "uid", "diff_type", "uses_value",
              "source_frame", "source_family", "baseline_status"]
    output = root / "reports/jss_pre_event_hints.csv"
    with output.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(hints[key] for key in sorted(hints))
    with (root / "reports/source_frame_17301952.csv").open(encoding="utf-8", newline="") as source:
        base = {row["repository_name_key"] for row in csv.DictReader(source)}
    jss = {row["repository_name_key"] for row in hints.values()}
    with (root / "reports/pilot_results.csv").open(encoding="utf-8", newline="") as source:
        pilot = {row["repository"].lower(): row["status"] for row in csv.DictReader(source)}
    with (root / "reports/candidate_source_membership.csv").open("w", encoding="utf-8", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(["repository", "source_frames", "source_family", "git_baseline_status"])
        for name in sorted(base | jss):
            frames = (["zenodo:17301952"] if name in base else []) + (["zenodo:18414913"] if name in jss else [])
            status = "pilot_confirmed" if pilot.get(name) == "eligible" else "not_confirmed"
            writer.writerow([name, json.dumps(frames), "zenodo:10259013", status])
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "anchor": config["event_anchor"],
              "scanned_change_rows": scanned, "pre_event_substring_rows": candidate_rows,
              "parsed_hint_rows": len(hints), "unresolved_or_skipped": skipped,
              "base_candidate_names": len(base), "jss_historical_hint_names": len(jss),
              "intersection_names": len(base & jss), "extra_hint_names": len(jss - base),
              "union_candidate_hint_names": len(base | jss), "new_confirmed_baseline_repositories": 0,
              "source_parquet_sha256": expected,
              "inputs_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
                                 ["study.toml", "reports/source_frame_17301952.csv", "reports/source_package_audit.json"]},
              "outputs_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
                                  ["reports/jss_pre_event_hints.csv", "reports/candidate_source_membership.csv"]},
              "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "limitations": ["This is a union of candidate hints, not a union of confirmed baseline users.",
                             "Diffs include removals and historical use; a pre-event occurrence does not establish baseline presence.",
                             "Cleaned paths erase original job/step identity; complete workflow reconstruction is not claimed.",
                             "Both packages share original source population 17301952.",
                             "Naive parquet timestamps are interpreted as UTC from the publisher notebook's Unix conversion.",
                             "Name deduplication does not resolve repository renames or transfers."]}
    (root / "reports/jss_hint_audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
