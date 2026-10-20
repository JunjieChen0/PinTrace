import copy
import csv
import io
import json
from collections import Counter
from pathlib import Path

import pytest

from pintrace import tables
from pintrace.measurement_io import IO_RULES_VERSION


def detail():
    reference = dict(kind="action", action="owner/action", ref="a" * 40, ref_type="full_sha",
                     path=".github/workflows/ci.yml", job="build", step_index=0, line=7,
                     comment_kind="none", comment_identity_state="none", configuration_coverage={"covered": False},
                     raw_extra={"float": 0.12345678901234568, "negative_zero": -0.0, "big": 2**100, "null": None})
    states = [dict(sha=f"commit-{i}", date=f"2025-03-0{i+1}", complete=True, configuration_complete=True,
                   references=[copy.deepcopy(reference)], files=[{"blob": "b" * 40}], configuration_files=[],
                   configuration={"entries": [] if i == 0 else [{"tool": "dependabot", "path": ".github/dependabot.yml",
                       "parse_state": "parsed", "inheritance_unknown": True, "raw_extra": [0.01, 2**100, False]}]},
                   renames={}, rename_evidence=[], errors=[]) for i in range(2)]
    traced = [dict(reference, snapshot_sha=state["sha"], lineage_id="lineage", lineage_status="matched",
                   unused_lineage_payload={"opaque": "not retained in the locator index"}) for state in states]
    events = [dict(commit="same-commit", previous_commit="previous", event="pin",
                   before=[{"lineage_id": "old"}], after={"lineage_id": "lineage"}, ordinal_payload=i,
                   opaque={"float": 1.25e-17, "big": 2**100}) for i in range(3)]
    return dict(summary={"repository_id": "123"}, snapshots=states,
                lineages=dict(references=traced, events=events, conservation={"passed": True, "intervals": [{"big": 2**100}]},
                              quality={"ambiguous_transition_rate": 0.1}),
                replay={"passed": True, "state_mismatches": []},
                timeline={"configuration_commit_coverage_passed": True, "opaque": [False, 0.125, 2**100]})


def csv_tables(rows):
    streams = {name: io.StringIO(newline="") for name in tables.FIELDS}
    writers = {name: csv.DictWriter(streams[name], fieldnames=fields) for name, fields in tables.FIELDS.items()}
    for writer in writers.values():
        writer.writeheader()
    for name, row in rows:
        writers[name].writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                               if isinstance(value, (dict, list)) else value for key, value in row.items()})
    return {name: stream.getvalue() for name, stream in streams.items()}


@pytest.mark.parametrize("reorder", [False, True])
@pytest.mark.parametrize("duplicate_lineages", [False, True])
def test_streamed_csv_matches_in_memory_rows_payload_ids_and_audit(tmp_path, reorder, duplicate_lineages):
    payload = detail()
    if duplicate_lineages:
        payload["lineages"]["references"].extend([copy.deepcopy(payload["lineages"]["references"][0])] * 3)
    if reorder:
        payload["lineages"] = {key: payload["lineages"][key] for key in ("events", "quality", "references", "conservation")}
        payload = {key: payload[key] for key in ("lineages", "timeline", "snapshots", "replay", "summary")}
    path = tmp_path / "measurement.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    audits = [Counter(), Counter()]
    evidence = [tables.index_evidence([]), tables.index_evidence([])]
    old = list(tables.measurement_rows(payload, evidence[0], audits[0]))
    new = list(tables.measurement_file_rows(path, evidence[1], audits[1]))
    assert csv_tables(new) == csv_tables(old)
    assert audits[0] == audits[1]
    assert evidence[0]["used"] == evidence[1]["used"]
    assert audits[1]["lineage_locator_conflicts"] == int(duplicate_lineages)
    events = [row for name, row in new if name == "lineage_events"]
    assert [row["event_id"] for row in events] == [tables.identifier("123", i, "same-commit", "pin") for i in range(3)]
    configs = [row for name, row in new if name == "configurations"]
    assert configs[0]["payload"]["parse_state"] == "no_local_configuration_observed"
    assert configs[1]["payload"]["raw_extra"] == [0.01, 2**100, False]
    header = tables.measurement_header(path)
    assert "snapshots" not in header and "references" not in header["lineages"] and "events" not in header["lineages"]
    assert all(value is None or set(value) <= {"lineage_id", "lineage_status"} for value in header["_lineage_map"].values())


@pytest.mark.parametrize("fields", [{}, {"lineage_id": None, "lineage_status": None}])
def test_missing_vs_explicit_null_traced_fields_keep_original_payload_semantics(tmp_path, fields):
    payload = detail()
    payload["snapshots"][0]["references"][0].update(lineage_id="snapshot-value", lineage_status="snapshot-status")
    payload["lineages"]["references"][0] = {key: value for key, value in payload["lineages"]["references"][0].items()
                                                if key not in {"lineage_id", "lineage_status"}}
    payload["lineages"]["references"][0].update(fields)
    path = tmp_path / "measurement.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    rows = [row for name, row in tables.measurement_file_rows(path) if name == "references"]
    assert rows[0]["lineage_id"] is None
    assert rows[0]["payload"]["lineage_id"] == fields.get("lineage_id", "snapshot-value")
    assert csv_tables(tables.measurement_file_rows(path)) == csv_tables(tables.measurement_rows(payload))


def test_file_iterator_uses_two_complete_passes_with_bounded_reads(tmp_path, monkeypatch):
    path = tmp_path / "measurement.json"
    path.write_text(json.dumps(detail()), encoding="utf-8")
    original_open, original_bytes, original_text = Path.open, Path.read_bytes, Path.read_text
    reads, passes = [], []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def read(self, size=-1):
            assert 0 <= size <= 1024 * 1024
            reads.append(size)
            return self.stream.read(size)

        def readinto(self, buffer):
            assert len(buffer) <= 1024 * 1024
            reads.append(len(buffer))
            return self.stream.readinto(buffer)

        def readable(self):
            return True

    def limited_open(candidate, *args, **kwargs):
        stream = original_open(candidate, *args, **kwargs)
        return Reader(stream) if candidate == path else stream

    def no_bulk(candidate, original, *args, **kwargs):
        assert candidate != path
        return original(candidate, *args, **kwargs)

    original_sections = tables.sections

    def sections(candidate, prefixes):
        passes.append(set(prefixes))
        yield from original_sections(candidate, prefixes)

    monkeypatch.setattr(Path, "open", limited_open)
    monkeypatch.setattr(Path, "read_bytes", lambda p, *a, **k: no_bulk(p, original_bytes, *a, **k))
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **k: no_bulk(p, original_text, *a, **k))
    monkeypatch.setattr(tables, "sections", sections)
    assert tables.digest(path)
    assert list(tables.measurement_file_rows(path))
    assert len(passes) == 2
    assert "lineages.references.item" in passes[0]
    assert passes[1] == {"snapshots.item", "lineages.events.item"}
    assert "lineages" not in set.union(*passes)
    assert reads


def test_export_manifest_binds_io_version_and_actual_loader_source(tmp_path, monkeypatch):
    monkeypatch.setattr(tables, "select_batch", lambda root: dict(measurements={}, baselines={}, upstreams={},
        input_sha256={}, riskset_summary=None, join_summary=None, evidence_path=None, excluded_later_measurement_files=[]))
    result = tables.export(tmp_path)
    assert result["rule_version"] == "tables-v5"
    assert result["measurement_io_version"] == IO_RULES_VERSION
    source = Path(tables.__file__).with_name("measurement_io.py").resolve()
    assert result["input_sha256"][source.as_posix()] == tables.digest(source)


def test_tmp_root_manifest_contains_only_existing_verified_inputs(tmp_path, monkeypatch):
    fixture = tmp_path / "bound.json"
    fixture.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tables, "select_batch", lambda root: dict(measurements={}, baselines={}, upstreams={},
        input_sha256={"bound.json": tables.digest(fixture)}, riskset_summary=None, join_summary=None,
        evidence_path=None, excluded_later_measurement_files=[]))
    result = tables.export(tmp_path)
    assert result["inputs_unchanged"] is True
    assert len(result["input_sha256"]) == 2
    for name, expected in result["input_sha256"].items():
        assert (tmp_path / name).is_file()
        assert tables.digest(tmp_path / name) == expected
