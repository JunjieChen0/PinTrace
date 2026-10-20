import json

from pintrace import measurement
from pintrace.measurement_stream import stream_save


VALUE = {
    "summary": {"repository": "owner/répo", "status": "measured", "ratio": 0.1, "empty": [], "nothing": None, "flag": True},
    "snapshots": [{"sha": "a" * 40, "files": [], "references": [{"line": 3, "ref": "v1"}, {"line": 4, "ref": "ü"}]}, {"sha": "b" * 40}],
    "implementation_sha256": {"study.toml": "0" * 64},
}


def test_streamed_bytes_equal_the_ordinary_writer(tmp_path):
    ordinary, streamed = tmp_path / "ordinary.json", tmp_path / "streamed.json"
    measurement.save(ordinary, VALUE)
    stream_save(streamed, VALUE)
    assert streamed.read_bytes() == ordinary.read_bytes()
    assert json.loads(streamed.read_text(encoding="utf-8")) == VALUE


def test_streamed_writer_creates_directories_and_leaves_no_pending_file(tmp_path):
    target = tmp_path / "nested/dir/1.json"
    stream_save(target, VALUE)
    assert target.is_file() and not list(target.parent.glob("*.pending"))


def test_streamed_writer_replaces_a_stale_zero_byte_pending_file(tmp_path):
    target = tmp_path / "1.json"
    target.with_suffix(".pending").write_bytes(b"")
    stream_save(target, VALUE)
    assert json.loads(target.read_text(encoding="utf-8")) == VALUE and not target.with_suffix(".pending").exists()
