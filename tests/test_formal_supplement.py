import hashlib

import pytest

from pintrace.formal_supplement import freeze, superseded_inputs


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_supplement_freeze_never_overwrites_an_existing_manifest(tmp_path):
    (tmp_path / "reports").mkdir()
    target = tmp_path / "reports/supplement_freeze_manifest.json"
    target.write_text("existing evidence", encoding="utf-8")
    with pytest.raises(ValueError, match="not be overwritten"):
        freeze(tmp_path)
    assert target.read_text(encoding="utf-8") == "existing evidence"


def test_superseded_inputs_lists_changed_and_missing_files_only(tmp_path):
    (tmp_path / "kept.txt").write_text("same", encoding="utf-8")
    (tmp_path / "edited.txt").write_text("new", encoding="utf-8")
    recorded = {"kept.txt": digest("same"), "edited.txt": digest("old"), "gone.txt": digest("x")}
    rows = {row["path"]: row for row in superseded_inputs(tmp_path, recorded)}
    assert set(rows) == {"edited.txt", "gone.txt"}
    assert rows["edited.txt"]["recorded_sha256"] == digest("old") and rows["edited.txt"]["current_sha256"] == digest("new")
    assert rows["gone.txt"]["current_sha256"] is None


def test_absolute_recorded_paths_are_compared_as_they_are(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("body", encoding="utf-8")
    other = tmp_path / "root"
    other.mkdir()
    assert superseded_inputs(other, {outside.as_posix(): digest("body")}) == []
