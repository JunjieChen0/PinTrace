"""Sampling and coverage estimates (pintrace/coverage_audit.py) on synthetic data."""

import gzip
import hashlib
import json
from datetime import datetime, timezone

import pytest

from pintrace import coverage_audit as ca
from pintrace import coverage_crawl as cc
from test_coverage_crawl import Session, page


ANCHOR = datetime(2025, 3, 14, tzinfo=timezone.utc)


def crawl(directory, pages, monkeypatch, limit=None):
    """A real crawl directory written by coverage_crawl from synthetic pages."""
    monkeypatch.setattr(cc.time, "sleep", lambda seconds: None)
    cc.crawl(directory, delay=1, limit=limit, session=Session(pages))
    return directory


def three_pages():
    return [page(["A/x", "b/y"], "Q1"), page(["a/X"], "Q2"), page(["c/z"])]


def rewrite_page(directory, index, **fields):
    lines = (directory / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    lines[index] = json.dumps(dict(json.loads(lines[index]), **fields))
    (directory / "pages.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_dependents_dedupes_case_insensitively_from_a_verified_crawl(tmp_path, monkeypatch):
    assert ca.read_dependents(crawl(tmp_path, three_pages(), monkeypatch)) == ["A/x", "b/y", "c/z"]


def test_verify_crawl_rejects_an_incomplete_crawl(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not complete"):
        ca.verify_crawl(crawl(tmp_path, three_pages(), monkeypatch, limit=2))


def test_verify_crawl_rejects_a_tampered_raw_page(tmp_path, monkeypatch):
    directory = crawl(tmp_path, three_pages(), monkeypatch)
    (directory / "raw" / "00001.html.gz").write_bytes(gzip.compress(page(["e/v"], "Q2").encode("utf-8")))
    with pytest.raises(ValueError, match="SHA-256"):
        ca.verify_crawl(directory)


def test_verify_crawl_rejects_a_broken_link_chain(tmp_path, monkeypatch):
    directory = crawl(tmp_path, three_pages(), monkeypatch)
    url = json.loads((directory / "pages.jsonl").read_text(encoding="utf-8").splitlines()[2])["url"]
    rewrite_page(directory, 2, url=url + "&skipped=1")
    with pytest.raises(ValueError, match="Next link"):
        ca.verify_crawl(directory)


def test_verify_crawl_rejects_a_list_without_the_end_marker(tmp_path, monkeypatch):
    directory = crawl(tmp_path, three_pages(), monkeypatch)
    raw = page(["c/z"]).replace("<button disabled>Next</button>", "").encode("utf-8")
    (directory / "raw" / "00002.html.gz").write_bytes(gzip.compress(raw))
    rewrite_page(directory, 2, sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match="end marker"):
        ca.verify_crawl(directory)


def test_verify_crawl_rejects_rows_not_in_the_raw_pages(tmp_path, monkeypatch):
    directory = crawl(tmp_path, three_pages(), monkeypatch)
    with (directory / "dependents.csv").open("a", encoding="utf-8") as stream:
        stream.write("2,1,extra/repo\n")
    with pytest.raises(ValueError, match="re-parsed"):
        ca.verify_crawl(directory)


def test_identity_check_applies_the_cohort_conditions():
    good = dict(repository_id="1", canonical_repository="o/r", created_at="2020-01-01T00:00:00Z")
    assert ca.identity_check(good, ANCHOR) == []
    assert ca.identity_check(dict(good, created_at="2025-03-14T00:00:00Z"), ANCHOR) == ["current_entity_created_not_before_anchor"]
    assert ca.identity_check(dict(good, created_at="2020-01-01T00:00:00"), ANCHOR) == ["current_created_invalid"]
    assert ca.identity_check(dict(good, repository_id=None, created_at=None), ANCHOR) == [
        "current_metadata_identity_invalid", "current_created_invalid"]


def test_apply_identity_moves_a_late_fork_out_of_confirmed_without_mutating_input():
    rows = [dict(status="confirmed", repository_id="1", canonical_repository="o/r", created_at="2025-06-01T00:00:00Z",
                 current_fork=True),
            dict(status="not_at_baseline", created_at="2025-06-01T00:00:00Z")]
    output = ca.apply_identity(rows, ANCHOR)
    assert output[0]["status"] == "historical_identity_unknown" and rows[0]["status"] == "confirmed"
    assert output[1] is rows[1]


def test_missing_bounds_count_unknown_rows_as_ineligible_and_as_eligible():
    sample = ([dict(status="confirmed")] * 30 + [dict(status="not_at_baseline")] * 60
              + [dict(status="collection_failed")] * 6 + [dict(status="historical_identity_unknown")] * 4)
    result = ca.estimate(listed=1_000, frame_listed=0, formal_listed=50, sample_rows=sample)
    bounds = result["missing_bounds"]
    assert bounds["planned"] == 100 and bounds["unknown"] == 10
    assert bounds["rate_low"] == 0.30 and bounds["rate_high"] == 0.40
    assert bounds["coverage_low"] == pytest.approx(50 / 450) and bounds["coverage_high"] == pytest.approx(50 / 350)
    assert result["pre_incident_rate"]["total"] == 90


def test_sample_excludes_the_frame_and_does_not_depend_on_list_order():
    names = [f"o/r{i}" for i in range(50)]
    first = ca.draw_sample(names, {"o/r1", "o/r2"}, size=10, seed="s")
    assert len(first) == 10 and not {"o/r1", "o/r2"} & set(first)
    assert ca.draw_sample(list(reversed(names)), {"o/r1", "o/r2"}, size=10, seed="s") == first
    assert len(ca.draw_sample(names, set(), size=500, seed="s")) == 50


def test_wilson_matches_a_known_interval():
    interval = ca.wilson(10, 100)
    assert interval["estimate"] == 0.1
    assert round(interval["low"], 4) == 0.0552 and round(interval["high"], 4) == 0.1744
    assert ca.wilson(0, 0) is None


def test_estimate_scales_the_sample_rate_to_dependents_outside_the_frame():
    sample = ([dict(status="confirmed")] * 10 + [dict(status="not_at_baseline")] * 85
              + [dict(status="no_pre_event_history")] * 5 + [dict(status="inaccessible_unknown")] * 3)
    result = ca.estimate(listed=10_100, frame_listed=100, formal_listed=90, sample_rows=sample)
    assert result["pre_incident_rate"]["total"] == 100 and result["dependents_outside_frame"] == 10_000
    assert result["estimated_pre_incident_outside"]["estimate"] == pytest.approx(1000)
    coverage = result["formal_coverage"]
    assert coverage["estimate"] == pytest.approx(90 / 1090)
    assert coverage["low"] < coverage["estimate"] < coverage["high"]


def test_non_fork_estimate_keeps_forks_in_the_denominator():
    sample = ([dict(status="confirmed", current_fork=True)] * 4 + [dict(status="confirmed", current_fork=False)] * 6
              + [dict(status="not_at_baseline")] * 10)
    result = ca.estimate(listed=1_000, frame_listed=0, formal_listed=10, sample_rows=sample, eligible=ca.not_fork)
    assert result["pre_incident_rate"]["successes"] == 6 and result["pre_incident_rate"]["total"] == 20


def test_standardized_differences_match_hand_computed_values():
    assert ca.smd_continuous([1, 2, 3], [2, 3, 4]) == pytest.approx(-1.0)
    assert ca.smd_continuous([1], [2, 3]) is None and ca.smd_continuous([1, 1], [1, 1]) is None
    assert ca.smd_proportion(0.5, 0.5) == 0 and ca.smd_proportion(0.8, 0.2) == pytest.approx(0.6 / 0.4)
    cohort = [dict(git_commits_180d=v, owner_type="Organization") for v in (0, 1, 2)]
    sample = [dict(git_commits_180d=v, owner_type="User") for v in (0, 0, 1)]
    output = ca.standardized_differences(cohort, sample)
    assert output["git_commits_180d"]["transform"] == "log1p" and output["git_commits_180d"]["smd"] > 0
    assert output["organization_share"]["smd"] is None  # both shares are 0 or 1, so the pooled SD is 0


def project(tmp_path, monkeypatch):
    """A synthetic project root holding every input the summary binds."""
    root = tmp_path / "root"
    for path in (*ca.FIXED_INPUTS, *ca.IMPLEMENTATION):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x\n", encoding="utf-8")
    directory = crawl(root / ca.OUTPUT / "crawl-1", three_pages(), monkeypatch)
    (root / ca.OUTPUT / "sample.csv").write_text("position,repository\n0,b/y\n", encoding="utf-8")
    (root / ca.OUTPUT / "sample_manifest.json").write_text(json.dumps(dict(
        crawl=directory.relative_to(root).as_posix(), size=1, sample_sha256=ca.digest(root / ca.OUTPUT / "sample.csv"),
        dependents_sha256=ca.digest(directory / "dependents.csv"))), encoding="utf-8")
    ca.save_detail(ca.output_path(root, "b/y"), dict(summary=dict(repository="b/y", status="not_at_baseline")))
    (root / "data/derived/cohort-v1/baselines").mkdir(parents=True)
    report = dict(crawl=dict(directory=directory.relative_to(root).as_posix()), input_sha256=ca.bound_inputs(root, directory),
                  implementation_sha256={p: ca.digest(root / p) for p in ca.IMPLEMENTATION})
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports/coverage_audit.json").write_text(json.dumps(report), encoding="utf-8")
    return root, directory


def test_check_sample_manifest_rejects_a_changed_sample(tmp_path, monkeypatch):
    root, directory = project(tmp_path, monkeypatch)
    ca.check_sample_manifest(root, directory)
    (root / ca.OUTPUT / "sample.csv").write_text("position,repository\n0,c/z\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sample manifest"):
        ca.check_sample_manifest(root, directory)


def test_run_check_passes_then_names_a_changed_eligibility_record(tmp_path, monkeypatch):
    root, _ = project(tmp_path, monkeypatch)
    assert ca.run_check(root)["ok"]
    ca.save_detail(ca.output_path(root, "b/y"), dict(summary=dict(repository="b/y", status="confirmed")))
    result = ca.run_check(root)
    assert not result["ok"] and result["differences"] == [ca.output_path(root, "b/y").relative_to(root).as_posix()]


def test_describe_reports_quartiles_and_shares():
    rows = [dict(git_commits_180d=v, baseline_workflow_files=1, action_references=2, mutable_share=0.5,
                 owner_type="Organization" if v % 2 else "User",
                 target_full_sha=1 if v == 4 else 0, target_mutable=0 if v == 4 else 1)
            for v in (1, 2, 3, 4)]
    output = ca.describe(rows)
    assert output["git_commits_180d"]["median"] == 2.5
    assert output["organization_share"]["successes"] == 2 and output["target_all_sha_share"]["successes"] == 1
    assert json.dumps(output)
