"""Export the nine logical research tables with explicit scope and source hashes."""

import csv
import hashlib
import json
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .cohort import save_detail
from .measurement_io import IO_RULES_VERSION, file_sha256 as digest, sections
from .measurement import BATCH_DIRECTORY, RULES_VERSION as MEASUREMENT_RULES_VERSION
from .opportunity_join import RISKSET_RULES_VERSION, RULES_VERSION as EVIDENCE_DIRECTORY
from .opportunity_join import _opportunity as normalize_opportunity

RULES_VERSION = "tables-v5"

FIELDS = {
    "repositories": ["repository_id", "candidate_name", "canonical_repository", "status", "baseline_commit", "baseline_tree", "payload"],
    "snapshots": ["repository_id", "snapshot_sha", "date_utc", "complete", "configuration_complete", "payload"],
    "references": ["reference_id", "repository_id", "snapshot_sha", "lineage_id", "action", "ref", "ref_type",
                   "path", "job", "step_index", "line", "comment_kind", "measured_comment_identity_state",
                   "comment_identity_state", "identity_join_status", "identity_evidence", "configuration_coverage", "payload"],
    "lineage_events": ["event_id", "repository_id", "commit", "previous_commit", "event", "before_lineages", "after_lineages", "payload"],
    "opportunities": ["evidence_record_id", "opportunity_id", "opportunity_identity_state", "latency_model", "normalization_error",
                      "repository", "tag", "channel", "evidence_grade", "available_at", "available_after", "available_by", "target", "payload"],
    "configurations": ["repository_id", "snapshot_sha", "tool", "path", "parse_state", "inheritance_unknown", "payload"],
    "pr_events": ["repository_id", "pr_id", "event", "date_utc", "payload"],
    "checks": ["repository_id", "check_type", "passed", "payload"],
    "sources": ["candidate_name", "repository_id", "source_frame", "source_family", "payload"],
}


def identifier(*values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def observation_key(reference, commit=None, identity=None):
    return (str(identity if identity is not None else reference.get("repository_id")),
            commit or reference.get("snapshot_sha"), reference.get("path"), reference.get("job"),
            reference.get("step_index"), reference.get("line"), reference.get("action"),
            reference.get("ref", reference.get("sha")))


def valid_locator(key):
    identity, commit, path, job, index, line, action, target = key
    return (identity not in ("None", "") and all(isinstance(value, str) and value for value in (commit, path, job, action, target))
            and type(index) is int and index >= 0 and type(line) is int and line > 0)


def index_evidence(rows):
    indexed, invalid = defaultdict(list), []
    for row in rows:
        key = observation_key(row)
        if valid_locator(key):
            indexed[key].append(row)
        else:
            invalid.append(row)
    return dict(rows=indexed, invalid=invalid, used=set())


def event_lineages(value):
    references = value if isinstance(value, list) else [value] if value else []
    return [ref["lineage_id"] for ref in references if ref.get("lineage_id")]


def opportunity_row(record):
    """Keep evidence identity distinct from the joiner's canonical opportunity."""
    error = None
    try:
        canonical = normalize_opportunity(record)
    except (TypeError, ValueError, OverflowError) as exception:
        canonical = None
        error = type(exception).__name__ + ": " + str(exception)
    state = ("canonical_opportunity" if canonical is not None else
             "excluded_current_only" if record.get("evidence_grade") == "C" else "unresolved_opportunity")
    row = {key: record.get(key) for key in FIELDS["opportunities"]}
    row.update(evidence_record_id=identifier(record), opportunity_id=canonical["opportunity_id"] if canonical else None,
               opportunity_identity_state=state,
               latency_model="requires_reference_applicability" if canonical else
                             "excluded_current_only" if state == "excluded_current_only" else "excluded_unresolved_opportunity",
               normalization_error=error, payload=record)
    return row


def _index_lineage(index, reference):
    # Repository identity is constant per file, including when summary appears last.
    key = observation_key(reference, identity="")[1:]
    index[key] = None if key in index else {field: reference[field] for field in ("lineage_id", "lineage_status") if field in reference}


def measurement_header(path):
    """First pass: retain check payloads and a minimal, conflict-aware locator map."""
    metadata, lineage_map = {}, {}
    prefixes = {"summary", "lineages.references.item", "lineages.conservation", "lineages.quality", "replay", "timeline"}
    for prefix, value in sections(path, prefixes):
        if prefix == "lineages.references.item":
            _index_lineage(lineage_map, value)
        else:
            metadata[prefix] = value
    return dict(summary=metadata["summary"], replay=metadata["replay"], timeline=metadata["timeline"],
                lineages={"conservation": metadata["lineages.conservation"], "quality": metadata["lineages.quality"]},
                _measurement_path=path, _lineage_map=lineage_map)


def _snapshot_rows(identity, state, lineage_map, evidence_index, audit):
    commit = state["sha"]
    payload = {key: value for key, value in state.items() if key != "references"}
    yield "snapshots", dict(repository_id=identity, snapshot_sha=commit, date_utc=state["date"],
                            complete=state["complete"], configuration_complete=state["configuration_complete"], payload=payload)
    for index, ref in enumerate(state["references"]):
        key = observation_key(ref, commit, identity)
        locator = key[1:]
        traced = lineage_map[locator] if locator in lineage_map and lineage_map[locator] is not None else ref
        if ref.get("kind") == "action" and locator in lineage_map and lineage_map[locator] is None:
            audit["lineage_locator_conflicts"] += 1
        measured_state = ref.get("comment_identity_state")
        identity_state = "none" if measured_state == "none" and not ref.get("comment_version") else "unverified"
        evidence, join_status = None, "not_full_sha_action"
        if ref.get("kind") == "action" and ref.get("ref_type") == "full_sha":
            audit["full_sha_observations"] += 1
            rows = evidence_index["rows"].get(key, []) if evidence_index else []
            if evidence_index and rows:
                evidence_index["used"].add(key)
            if not valid_locator(key):
                audit["invalid_reference_locators"] += 1
                join_status = "invalid_reference_locator"
            elif len(rows) > 1:
                audit["identity_locator_conflicts"] += 1
                evidence, join_status = {"conflicting_rows": rows}, "evidence_locator_conflict"
            elif len(rows) == 1 and rows[0].get("lineage_id") != traced.get("lineage_id"):
                audit["identity_lineage_conflicts"] += 1
                evidence, join_status = rows[0], "lineage_identity_conflict"
            elif len(rows) == 1:
                evidence, join_status = rows[0], "matched"
                identity_state = evidence["comment_identity_state"]
                audit["identity_matched_observations"] += 1
                audit["identity_state_" + identity_state] += 1
            else:
                audit["identity_missing_observations"] += 1
                join_status = "missing_evidence" if evidence_index is not None else "join_not_available"
        payload = dict(ref, **{key: traced[key] for key in ("lineage_id", "lineage_status") if key in traced},
                       derived_identity_join_status=join_status, derived_identity_evidence=evidence)
        yield "references", dict(reference_id=identifier(identity, commit, index), repository_id=identity,
                                 snapshot_sha=commit, lineage_id=traced.get("lineage_id"), action=ref.get("action"),
                                 ref=ref.get("ref"), ref_type=ref.get("ref_type"), comment_kind=ref.get("comment_kind"),
                                 **{field: ref.get(field) for field in ("path", "job", "step_index", "line")},
                                 measured_comment_identity_state=measured_state, comment_identity_state=identity_state,
                                 identity_join_status=join_status, identity_evidence=evidence,
                                 configuration_coverage=ref.get("configuration_coverage"), payload=payload)
    entries = state["configuration"]["entries"]
    for entry in entries or [{"tool": None, "path": None, "parse_state": "no_local_configuration_observed",
                              "inheritance_unknown": True}]:
        yield "configurations", dict(repository_id=identity, snapshot_sha=commit,
                                     **{key: entry.get(key) for key in ("tool", "path", "parse_state", "inheritance_unknown")},
                                     payload=entry)


def _event_row(identity, index, event):
    return "lineage_events", dict(event_id=identifier(identity, index, event["commit"], event["event"]),
                                 repository_id=identity,
                                 **{key: event.get(key) for key in ("commit", "previous_commit", "event")},
                                 before_lineages=event_lineages(event.get("before")),
                                 after_lineages=event_lineages(event.get("after")), payload=event)


def _check_rows(detail):
    identity = detail["summary"]["repository_id"]
    yield "checks", dict(repository_id=identity, check_type="reference_instance_conservation",
                         passed=detail["lineages"]["conservation"]["passed"], payload=detail["lineages"]["conservation"])
    yield "checks", dict(repository_id=identity, check_type="offline_raw_source_replay",
                         passed=detail["replay"]["passed"], payload=detail["replay"])
    yield "checks", dict(repository_id=identity, check_type="configuration_modification_coverage",
                         passed=detail["timeline"]["configuration_commit_coverage_passed"], payload=detail["timeline"])
    yield "checks", dict(repository_id=identity, check_type="measurement_quality", passed=None, payload=detail["lineages"]["quality"])


def measurement_rows(detail, evidence_index=None, audit=None):
    audit = audit if audit is not None else Counter()
    identity = detail["summary"]["repository_id"]
    if "_measurement_path" in detail:
        lineage_map, ordinal = detail["_lineage_map"], 0
        for prefix, value in sections(detail["_measurement_path"], {"snapshots.item", "lineages.events.item"}):
            if prefix == "snapshots.item":
                yield from _snapshot_rows(identity, value, lineage_map, evidence_index, audit)
            else:
                yield _event_row(identity, ordinal, value)
                ordinal += 1
    else:
        lineage_map = {}
        for reference in detail["lineages"]["references"]:
            _index_lineage(lineage_map, reference)
        for state in detail["snapshots"]:
            yield from _snapshot_rows(identity, state, lineage_map, evidence_index, audit)
        for ordinal, event in enumerate(detail["lineages"]["events"]):
            yield _event_row(identity, ordinal, event)
    yield from _check_rows(detail)


def measurement_file_rows(path, evidence_index=None, audit=None):
    yield from measurement_rows(measurement_header(path), evidence_index, audit)


def _path_map(hashes, prefix, numeric=False):
    return {name: expected for name, expected in hashes.items()
            if Path(name).parent.as_posix() == prefix and Path(name).suffix == ".json"
            and (not numeric or Path(name).stem.isdigit())}


def select_batch(root):
    """Validate declared inputs before opening any output; never widen a batch."""
    risk_path, join_path = root / "reports/riskset_summary.json", root / "reports/opportunity_join_summary.json"
    risk = json.loads(risk_path.read_text(encoding="utf-8")) if risk_path.exists() else None
    join = json.loads(join_path.read_text(encoding="utf-8")) if join_path.exists() else None
    manifests = {}

    def merge(hashes):
        for name, expected in hashes.items():
            if name in manifests and manifests[name] != expected:
                raise ValueError("Conflicting batch input hashes: " + name)
            if not (root / name).is_file() or digest(root / name) != expected:
                raise ValueError("Changed or missing batch input: " + name)
            manifests[name] = expected

    for summary in (risk, join):
        if summary is not None and (summary.get("measurement_batch_directory") != BATCH_DIRECTORY
                                    or summary.get("measurement_rules_version") != MEASUREMENT_RULES_VERSION):
            raise ValueError("Derived summary belongs to a different measurement batch")
    if risk is not None and risk.get("rules_version") != RISKSET_RULES_VERSION:
        raise ValueError("Riskset summary belongs to a different analysis rules version")
    if join is not None and join.get("rule_version") != EVIDENCE_DIRECTORY:
        raise ValueError("Opportunity join belongs to a different analysis rules version")
    prefix = "data/derived/" + BATCH_DIRECTORY
    if risk is not None:
        if risk.get("inputs_unchanged") is not True:
            raise ValueError("Riskset inputs changed during its generation")
        merge(risk["source_sha256"])
        merge({"reports/" + name: expected for name, expected in risk.get("output_sha256", {}).items()})
        manifests[risk_path.relative_to(root).as_posix()] = digest(risk_path)
        measurements = _path_map(risk["source_sha256"], prefix, numeric=True)
        baselines = _path_map(risk["source_sha256"], "data/derived/cohort-v1/baselines")
    else:
        measurements = {path.relative_to(root).as_posix(): digest(path)
                        for path in sorted((root / prefix).glob("*.json")) if path.stem.isdigit()}
        baselines = {path.relative_to(root).as_posix(): digest(path)
                     for path in sorted((root / "data/derived/cohort-v1/baselines").glob("*.json"))}
        merge({**measurements, **baselines})
    evidence_path = None
    if join is not None:
        if risk is None or join.get("inputs_unchanged") is not True:
            raise ValueError("Opportunity join needs the same unchanged riskset batch")
        join_inputs = join["input_sha256"]
        if (measurements != _path_map(join_inputs, prefix, numeric=True)
                or baselines != _path_map(join_inputs, "data/derived/cohort-v1/baselines")
                or join_inputs.get("reports/riskset_summary.json") != digest(risk_path)):
            raise ValueError("Riskset and opportunity join use different frozen inputs")
        merge(join_inputs)
        for descriptor in join["details"].values():
            merge({descriptor["path"]: descriptor["sha256"]})
        descriptor = join["details"]["reference_evidence"]
        if descriptor["path"] != "data/derived/" + EVIDENCE_DIRECTORY + "/reference_evidence.jsonl":
            raise ValueError("Reference identity evidence belongs to a different join batch")
        evidence_path = root / descriptor["path"]
        manifests[join_path.relative_to(root).as_posix()] = digest(join_path)
        upstreams = _path_map(join_inputs, "data/derived/upstream-opportunities")
    else:
        upstreams = {path.relative_to(root).as_posix(): digest(path)
                     for path in sorted((root / "data/derived/upstream-opportunities").glob("*.json"))}
        merge(upstreams)
    current_measurements = {path.relative_to(root).as_posix() for path in (root / prefix).glob("*.json") if path.stem.isdigit()}
    return dict(measurements=measurements, baselines=baselines, upstreams=upstreams, input_sha256=manifests,
                riskset_summary=risk, join_summary=join, evidence_path=evidence_path,
                excluded_later_measurement_files=sorted(current_measurements - measurements.keys()))


def read_bound(path, root, manifests):
    content = path.read_bytes()
    name = path.relative_to(root).as_posix()
    if hashlib.sha256(content).hexdigest() != manifests[name]:
        raise ValueError("Batch input changed during export: " + name)
    return content


def verify_bound(path, root, manifests):
    name = path.relative_to(root).as_posix()
    if digest(path) != manifests[name]:
        raise ValueError("Batch input changed during export: " + name)


def export(root):
    batch = select_batch(root)
    io_source = Path(__file__).with_name("measurement_io.py").resolve()
    io_hash = digest(io_source)
    io_name = io_source.relative_to(root.resolve()).as_posix() if io_source.is_relative_to(root.resolve()) else io_source.as_posix()
    evidence = None
    if batch["evidence_path"] is not None:
        verify_bound(batch["evidence_path"], root, batch["input_sha256"])
        with batch["evidence_path"].open(encoding="utf-8") as stream:
            evidence = index_evidence(json.loads(line) for line in stream if line.strip())
    output = root / "data/derived" / RULES_VERSION
    output.mkdir(parents=True, exist_ok=True)
    counts = {name: 0 for name in FIELDS}
    manifests, expected_ids, measured_ids, incomplete = dict(batch["input_sha256"]), set(), set(), []
    if io_name in manifests and manifests[io_name] != io_hash:
        raise ValueError("Measurement IO source changed after batch validation")
    manifests[io_name] = io_hash
    audit = Counter({key: 0 for key in ("full_sha_observations", "identity_matched_observations", "identity_missing_observations",
                                      "lineage_locator_conflicts", "invalid_reference_locators", "identity_locator_conflicts",
                                      "identity_lineage_conflicts", "invalid_evidence_locators", "duplicate_evidence_locator_keys",
                                      "unused_evidence_rows", "identity_state_none", "identity_state_unverified",
                                      "identity_state_verified_consistent", "identity_state_verified_conflict")})
    with ExitStack() as stack:
        writers = {}
        for name, fields in FIELDS.items():
            stream = stack.enter_context((output / (name + ".csv")).open("w", encoding="utf-8", newline=""))
            writers[name] = csv.DictWriter(stream, fieldnames=fields)
            writers[name].writeheader()

        def write(name, row):
            writers[name].writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                                   if isinstance(value, (dict, list)) else value for key, value in row.items()})
            counts[name] += 1

        baseline_paths = [root / name for name in sorted(batch["baselines"])]
        for path in baseline_paths:
            record = json.loads(read_bound(path, root, manifests))["summary"]
            identity = str(record["repository_id"]) if record.get("repository_id") else None
            if record["status"] == "confirmed":
                expected_ids.add(identity)
            write("repositories", dict(repository_id=identity, candidate_name=record["repository"],
                                       **{key: record.get(key) for key in ("canonical_repository", "status", "baseline_commit", "baseline_tree")},
                                       payload=record))
            for source in json.loads(record["source_frames"]):
                write("sources", dict(candidate_name=record["repository"], repository_id=identity, source_frame=source,
                                      source_family=record["source_family"], payload={"baseline_record": path.relative_to(root).as_posix(),
                                                                                "baseline_record_sha256": manifests[path.relative_to(root).as_posix()]}))
        for path in [root / name for name in sorted(batch["measurements"])]:
            verify_bound(path, root, manifests)
            detail = measurement_header(path)
            record = detail["summary"]
            if str(record["repository_id"]) not in expected_ids:
                continue
            if record["status"] != "measured" or record.get("measurement_rules_version") != MEASUREMENT_RULES_VERSION:
                incomplete.append({"repository_id": record["repository_id"], "status": record["status"]})
                continue
            measured_ids.add(str(record["repository_id"]))
            for name, row in measurement_rows(detail, evidence, audit):
                write(name, row)
        for path in [root / name for name in sorted(batch["upstreams"])]:
            payload = json.loads(read_bound(path, root, manifests))
            rows = [*payload["opportunities"],
                    *[row for row in payload["release_records"]
                      if row["evidence_grade"] == "C"]]
            for row in rows:
                write("opportunities", opportunity_row(row))
    audit["invalid_evidence_locators"] = len(evidence["invalid"]) if evidence else 0
    audit["duplicate_evidence_locator_keys"] = sum(len(rows) > 1 for rows in evidence["rows"].values()) if evidence else 0
    audit["unused_evidence_rows"] = sum(len(rows) for key, rows in evidence["rows"].items() if key not in evidence["used"]) if evidence else 0
    audit["evidence_rows_input"] = sum(len(rows) for rows in evidence["rows"].values()) + len(evidence["invalid"]) if evidence else 0
    inputs_unchanged = (all((root / name).is_file() and digest(root / name) == expected for name, expected in manifests.items())
                        and digest(io_source) == io_hash)
    join = batch["join_summary"]
    declared_join_counts_equal = (join is not None and join.get("full_sha_reference_observations") == audit["evidence_rows_input"]
                                  and join.get("measured_repository_ids") == len(measured_ids))
    association_passed = (evidence is not None and audit["identity_missing_observations"] == 0
                          and declared_join_counts_equal
                          and not any(audit[key] for key in ("lineage_locator_conflicts", "invalid_reference_locators",
                              "identity_locator_conflicts", "identity_lineage_conflicts", "invalid_evidence_locators",
                              "duplicate_evidence_locator_keys", "unused_evidence_rows")))
    cohort_complete = len(baseline_paths) == 580 and expected_ids == measured_ids
    integrity = (inputs_unchanged and association_passed and cohort_complete and not incomplete
                 and batch["riskset_summary"] is not None
                 and batch["riskset_summary"].get("coverage", {}).get("all_confirmed_measurements_usable") is True)
    report = {"rule_version": RULES_VERSION, "measurement_io_version": IO_RULES_VERSION, "measurement_batch_directory": BATCH_DIRECTORY,
              "measurement_rules_version": MEASUREMENT_RULES_VERSION,
              "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "table_count": len(FIELDS), "table_rows": counts, "table_fields": FIELDS,
              "candidate_names_available": len(baseline_paths), "confirmed_ids_available": len(expected_ids),
              "measured_ids_exported": len(measured_ids), "missing_measurement_ids": sorted(expected_ids - measured_ids),
              "incomplete_measurements": incomplete,
              "all_confirmed_measurement_ids_exported": cohort_complete,
              "complete_cohort_measurement_export": integrity, "integrity_gate_passed": integrity,
              "scope": "complete_bound_cohort" if integrity else "provisional_bound_batch" if batch["riskset_summary"] else "provisional_available_measurements",
              "identity_join_available": evidence is not None, "identity_mapping_complete": association_passed,
              "declared_join_counts_equal": declared_join_counts_equal,
              "bound_input_integrity_passed": inputs_unchanged and batch["riskset_summary"] is not None and batch["join_summary"] is not None,
              "identity_association_counts": dict(audit), "inputs_unchanged": inputs_unchanged,
              "excluded_later_measurement_files": batch["excluded_later_measurement_files"],
              "tables": {name: {"path": (output / (name + ".csv")).relative_to(root).as_posix(),
                                "sha256": digest(output / (name + ".csv"))} for name in FIELDS},
              "input_sha256": manifests, "implementation_sha256": digest(Path(__file__)),
              "opportunity_normalizer": {"implementation": "pintrace.opportunity_join._opportunity",
                                         "source_sha256": digest(Path(__file__).with_name("opportunity_join.py"))},
              "rq3_entry_scope": {
                  "riskset_rules_version": batch["riskset_summary"].get("rules_version") if batch["riskset_summary"] else None,
                  "opportunity_join_rules_version": join.get("rule_version") if join else None,
                  "riskset_episode_kind_counts": batch["riskset_summary"].get("risksets", {}).get("episode_kind_counts") if batch["riskset_summary"] else None,
                  "fixed_entry_diagnostics": batch["riskset_summary"].get("fixed_entry_diagnostics") if batch["riskset_summary"] else None,
                  "repository_first_observed_target_full_sha": batch["riskset_summary"].get("repository_first_observed_target_full_sha") if batch["riskset_summary"] else None,
                  "opportunity_episode_kind_coverage": join.get("target_rq3_by_episode_kind") if join else None,
                  "opportunity_entry_mode_coverage": join.get("target_rq3_by_entry_mode") if join else None,
                  "opportunity_match_evidence": join.get("details", {}).get("opportunity_matches") if join else None},
              "notes": ["Each row preserves typed raw fields in a JSON payload; empty scalar cells mean null, never zero.",
                        "Repository rows are candidate names; statistical repository identity is repository_id.",
                        "PR table is empty until actual PR collection; absence of rows is not absence of PR activity.",
                        "Opportunity rows are upstream evidence, not all automatically applicable to each downstream reference.",
                        "evidence_record_id hashes each complete raw evidence record; normalized opportunity_id uses the joiner's sole canonical implementation.",
                        "Current-only and unresolved evidence has no formal opportunity_id; multiple evidence rows can share one canonical opportunity.",
                        "Baseline-fixed, observed-pin and born-fixed opportunity links retain separate episode-kind and entry-mode denominators; unknown entries are diagnostics only.",
                        "Reference payload retains measured comment identity; the exported identity state uses separately preserved join evidence.",
                        "Verified comment conflicts are findings; duplicate or conflicting reference locators fail association integrity.",
                        "Absent join evidence never upgrades an unverified measured identity; lineage_id is not a reference join key."]}
    save_detail(output / "manifest.json", report)
    save_detail(root / "reports/table_manifest.json", report)
    print(json.dumps({key: report[key] for key in ("table_count", "table_rows", "confirmed_ids_available", "measured_ids_exported", "complete_cohort_measurement_export")}, indent=2))
    return report


def main():
    export(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
