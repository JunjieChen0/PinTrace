"""Sampling and coverage estimates (pintrace/coverage_audit.py) on synthetic data."""

import json

import pytest

from pintrace import coverage_audit as ca


def crawl(directory, names, complete=True):
    directory.mkdir(exist_ok=True)
    (directory / "dependents.csv").write_text(
        "page,position,repository\n" + "".join(f"0,{i},{n}\n" for i, n in enumerate(names)), encoding="utf-8")
    if complete:
        (directory / "complete.json").write_text("{}", encoding="utf-8")
    return directory


def test_read_dependents_dedupes_case_insensitively_and_requires_a_complete_crawl(tmp_path):
    assert ca.read_dependents(crawl(tmp_path / "done", ["A/x", "a/X", "b/y"])) == ["A/x", "b/y"]
    with pytest.raises(ValueError):
        ca.read_dependents(crawl(tmp_path / "partial", ["a/x"], complete=False))


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


def test_describe_reports_quartiles_and_shares():
    rows = [dict(git_commits_180d=v, baseline_workflow_files=1, action_references=2, mutable_share=0.5,
                 owner_type="Organization" if v % 2 else "User",
                 target_full_sha=1 if v == 4 else 0, target_mutable=0 if v == 4 else 1)
            for v in (1, 2, 3, 4)]
    output = ca.describe(rows)
    assert output["git_commits_180d"]["median"] == 2.5
    assert output["organization_share"]["successes"] == 2 and output["target_all_sha_share"]["successes"] == 1
    assert json.dumps(output)
