import hashlib
import json
from pathlib import Path

import ijson
import pytest

from pintrace.measurement_io import file_sha256, load_analysis_measurement, sections
from pintrace.risksets import compact_measurement, compact_reference


def write_json(path, detail):
    path.write_text(json.dumps(detail, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def rich_measurement():
    ref = dict(kind="action", action="所有者/action", repository="所有者/action", ref="v1", ref_type="tag",
               path=".github/workflows/ci.yml", job="build", step_id="test", step_index=1, line=2,
               lineage_id="lineage-1", lineage_status="known_entry", snapshot_sha="a", snapshot_timestamp=1.25,
               comment_kind="version", comment_version="1.0", comment_attachment_state="attached",
               comment_action="所有者/action", comment_identity_state="verified",
               configuration_coverage=dict(status="complete", repeated_body="discard"),
               configuration={"large_body": "discard"}, unused={"nested": [1, 2, 3]})
    return dict(lineages=dict(events=[dict(event="replace", before=[ref, None], after=dict(ref, ref="a" * 40),
                                              timestamp=1.5, ordinal=1, retained_metadata={"说明": [False, 2.5]}),
                                          dict(event="remove", before=ref, after=None, timestamp=2.0),
                                          dict(event="unknown_boundary", timestamp=3)],
                              references=[ref, dict(ref, snapshot_sha="b", ref="a" * 40)],
                              conservation=dict(passed=True, large_details=["discard"]), unused=["discard"]),
                snapshots=[dict(sha="a", timestamp=1.25, date="2026-10-18", complete=True,
                                references=[ref], workflows={"ci.yml": "discard"}),
                           dict(sha="b", complete=False, references=[ref]), {}],
                replay=dict(passed=True, cases=["discard"]),
                timeline=dict(endpoint_commit="b", metadata={"中文": [None, True, 0.125]}),
                summary=dict(repository_id=2 ** 100 + 1, repository="所有者/项目", ratio=0.1,
                             integer_float=1.0, tiny=1e-300, negative_zero=-0.0),
                unused=[{"body": "discard"}])


def test_analysis_projection_matches_compact_measurement(tmp_path, rich_measurement):
    path = write_json(tmp_path / "measurement.json", rich_measurement)
    raw = json.loads(path.read_text(encoding="utf-8"))
    loaded = load_analysis_measurement(path, compact_reference)
    assert loaded == compact_measurement(raw)
    assert type(loaded["summary"]["repository_id"]) is int
    assert type(loaded["summary"]["integer_float"]) is float
    assert type(loaded["timeline"]["metadata"]["中文"][1]) is bool
    assert loaded["summary"]["negative_zero"].hex() == raw["summary"]["negative_zero"].hex()
    assert loaded["lineages"]["events"][0]["before"][1] is None
    assert loaded["snapshots"][-1] == {}


@pytest.mark.parametrize("detail", [dict(summary={}),
    dict(summary=None, timeline=None, replay=dict(passed=None),
         lineages=dict(conservation=dict(passed=False), references=[], events=[])),
    dict(summary={}, snapshots=[], replay={}, lineages=dict(conservation={}))])
def test_analysis_projection_preserves_missing_and_null_defaults(tmp_path, detail):
    path = write_json(tmp_path / "measurement.json", detail)
    assert load_analysis_measurement(path, compact_reference) == compact_measurement(detail)


def test_analysis_requires_summary(tmp_path):
    path = write_json(tmp_path / "measurement.json", {"snapshots": []})
    with pytest.raises(KeyError, match="summary"):
        load_analysis_measurement(path, compact_reference)


def test_sections_preserve_document_order_and_native_numeric_types(tmp_path):
    detail = dict(rows=[dict(value=2 ** 100, fraction=1.0), [None, False, 0.5], "中文"],
                  passed=False, ignored=dict(large=[1, 2]), count=2 ** 100 + 1)
    path = write_json(tmp_path / "measurement.json", detail)
    output = list(sections(path, {"count", "rows.item", "passed"}))
    assert output == [("rows.item", detail["rows"][0]), ("rows.item", detail["rows"][1]),
                      ("rows.item", "中文"), ("passed", False), ("count", detail["count"])]
    assert type(output[0][1]["value"]) is int
    assert type(output[0][1]["fraction"]) is float


@pytest.mark.parametrize("content", [b'{"summary": {}', b'{"summary": {}, "ignored": [1, 2}',
    b'{"summary": {}, "ignored": [1, 2', b'{"summary": {}} {"other": 1}',
    b'{"summary": {}, "snapshots": [{"sha": "a"}', b'{"summary": {}, "ignored": "\xff"}'])
def test_analysis_rejects_invalid_json_in_selected_or_skipped_sections(tmp_path, content):
    path = tmp_path / "measurement.json"
    path.write_bytes(content)
    with pytest.raises(ijson.JSONError):
        load_analysis_measurement(path, compact_reference)


def test_sections_yield_before_eof_and_use_bounded_reads(tmp_path, monkeypatch):
    path = write_json(tmp_path / "measurement.json", {"rows": [{"index": n, "body": "x" * 128} for n in range(20000)]})
    source_size = path.stat().st_size
    original_open, streams, sizes = Path.open, [], []

    class BoundedReader:
        def __init__(self, raw):
            self.raw = raw

        def read(self, size=-1):
            assert 0 <= size <= 65536
            sizes.append(size)
            return self.raw.read(size)

        def readinto(self, buffer):
            assert len(buffer) <= 65536
            sizes.append(len(buffer))
            return self.raw.readinto(buffer)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.raw.close()

    def bounded_open(self, *args, **kwargs):
        assert self == path and args == ("rb",)
        stream = BoundedReader(original_open(self, *args, **kwargs))
        streams.append(stream)
        return stream

    monkeypatch.setattr(Path, "open", bounded_open)
    items = sections(path, ["rows.item"])
    assert next(items) == ("rows.item", {"index": 0, "body": "x" * 128})
    assert streams[0].raw.tell() < source_size
    assert sum(1 for _ in items) == 19999
    assert sizes and max(sizes) <= 65536


def test_file_sha256_matches_full_bytes_digest(tmp_path):
    path = tmp_path / "measurement.json"
    content = b"measurement\x00" * 100000
    path.write_bytes(content)
    assert file_sha256(path) == hashlib.sha256(content).hexdigest()
