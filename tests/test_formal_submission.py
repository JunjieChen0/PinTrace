import json

import pytest

from pintrace.formal_submission import freeze


def test_submission_freeze_rejects_incomplete_or_extraction_only_run(tmp_path):
    for incomplete in ({"completed": False}, {"completed": True, "extract_only": True}):
        (tmp_path / "run.json").write_text(json.dumps(incomplete), encoding="utf-8")
        with pytest.raises(ValueError, match="completed full"):
            freeze(tmp_path, "run.json")


def test_submission_freeze_never_overwrites_an_existing_evidence_manifest(tmp_path):
    directory = tmp_path / "reports"
    directory.mkdir()
    target = directory / "submission_freeze_manifest.json"
    target.write_text("existing evidence", encoding="utf-8")
    with pytest.raises(ValueError, match="not be overwritten"):
        freeze(tmp_path, "irrelevant.json")
    assert target.read_text(encoding="utf-8") == "existing evidence"
