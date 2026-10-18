from pintrace.cohort import collect, summarize, write_reports


def test_ledger_retains_pending_and_inaccessible_names(tmp_path):
    (tmp_path / "reports").mkdir()
    candidates = [{"repository": "a/repo"}, {"repository": "missing/repo"}, {"repository": "pending/repo"}]
    records = [{"repository": "a/repo", "repository_id": "12", "status": "confirmed"},
               {"repository": "missing/repo", "http_status": 404, "status": "inaccessible_unknown"}]
    summary = write_reports(tmp_path, records, candidates)
    assert summary["planned_names"] == 3
    assert summary["identity_or_inaccessible_code"] == 2
    assert "pending/repo" in (tmp_path / "reports/cohort_flow.csv").read_text()
    assert summary["confirmed_unique_repository_ids"] == 1


def test_identity_aliases_do_not_inflate_confirmed_sample():
    records = [{"repository": name, "repository_id": "123", "status": "confirmed"}
               for name in ("old/name", "new/name")]
    assert summarize(records, 2)["confirmed_unique_repository_ids"] == 1


def test_preserved_inaccessible_result_is_not_replaced_or_counted_as_absence(tmp_path):
    import json

    source = tmp_path / "data/derived/baseline-jss-extra-v1/missing__repo.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"summary": {"status": "unavailable", "error": "HTTPError: 404"},
                                  "baseline_snapshot": None}), encoding="utf-8")
    original = source.read_bytes()
    row = {"repository": "missing/repo", "source_frames": '["zenodo:18414913"]', "source_family": "same"}
    result = collect(row, tmp_path, {"event_anchor": "2025-03-14T00:00:00Z"}, "unused")
    assert result["status"] == "inaccessible_unknown"
    assert result["http_status"] == 404
    assert source.read_bytes() == original
