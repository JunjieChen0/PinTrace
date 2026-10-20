import csv
import json

import pytest

from pintrace.formal_activity import (annotate, dormancy, join_activity, rate, read_activity, summarize, tier_cuts, tier_of, truthy)
from pintrace.formal_common import sha256, write_json

DAY = 86400


def episode(repository, kind="baseline_fixed", exit_kind="study_window_censored", days=100, formal="True", eligible="True"):
    return dict(repository_id=repository, episode_id=f"ep-{repository}-{kind}-{exit_kind}", episode_kind=kind, exit_kind=exit_kind,
                formal=formal, duration_eligible=eligible, entry_timestamp=str(0.0), exit_timestamp=str(days * DAY))


def activity(commits, dormant90="False", dormant180="False"):
    return {str(i): dict(commits_pre_180d=str(n), no_commit_last_90d_at_end=dormant90, no_commit_last_180d_at_end=dormant180)
            for i, n in commits.items()}


def test_tiers_use_pre_incident_terciles_and_ties_stay_in_the_lower_tier():
    cuts = tier_cuts([1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert cuts == (3, 6)
    assert [tier_of(n, cuts) for n in (1, 3, 4, 6, 7, 9)] == ["low", "low", "mid", "mid", "high", "high"]
    assert [tier_of(n, tier_cuts([1, 2, 2, 2, 2, 9])) for n in (1, 2, 9)] == ["low", "low", "high"]  # ties stay together
    with pytest.raises(ValueError, match="three repositories"):
        tier_cuts([1, 2])


def test_join_keeps_only_formal_duration_eligible_rows_and_fails_on_a_missing_repository():
    rows = [episode("1"), episode("2", formal="False"), episode("3", eligible="False")]
    joined = join_activity(rows, activity({1: 5, 2: 5, 3: 5}))
    assert [r["repository_id"] for r in joined] == ["1"] and joined[0]["duration"] == 100
    with pytest.raises(ValueError, match="No maintenance-activity row for repository 1"):
        join_activity([episode("1")], activity({2: 5}))
    with pytest.raises(ValueError, match="positive observed duration"):
        join_activity([episode("1", days=0)], activity({1: 5}))


def test_rates_bound_unknown_interruptions_and_count_exposure_in_years():
    # one known exit in 365.25 days of exposure is exactly 100 per 100 years
    assert rate(1, 0, 365.25) == pytest.approx(100.0)
    assert rate(1, 1, 365.25) == pytest.approx(200.0)


def build(n_per_tier=4):
    commits, rows = {}, []
    for tier, base in (("low", 1), ("mid", 100), ("high", 1000)):
        for k in range(n_per_tier):
            repository = len(commits) + 1
            commits[repository] = base + k
            # low-activity repositories lose their pin quickly; high ones almost never
            exit_kind = "unpin" if tier == "low" else "study_window_censored" if tier == "high" else ("remove" if k == 0 else "study_window_censored")
            rows.append(episode(str(repository), exit_kind=exit_kind, days=200))
            rows.append(episode(str(repository), kind="post_anchor_born_fixed", exit_kind="ambiguous_transition" if k == 0 else "study_window_censored", days=100))
    return rows, commits


def test_summary_orders_tiers_and_gives_an_upper_bound_at_least_the_known_rate():
    rows, commits = build()
    body, tiers = summarize(join_activity(rows, activity(commits)), activity(commits))
    assert body["repositories_by_tier"] == {"low": 4, "mid": 4, "high": 4}
    low, high = body["rates"]["low.all"], body["rates"]["high.all"]
    assert low["known_exit_rate_per_100_years"]["estimate"] > high["known_exit_rate_per_100_years"]["estimate"] == 0
    for cell in body["rates"].values():
        known, upper = cell["known_exit_rate_per_100_years"], cell["known_or_unknown_exit_rate_per_100_years"]
        assert upper["estimate"] >= known["estimate"]
        assert upper["requested_replicates"] == 2000 and upper["seed"] == 20261018 and upper["interval"] == "percentile"
    assert body["rates"]["high.post_anchor_born_fixed"]["unknown_interruptions"] == 1
    assert set(tiers.values()) == {"low", "mid", "high"}
    ratio = body["rate_ratios"]["low_vs_high.all.known_or_unknown"]
    assert ratio["n_clusters"] == 8 and ratio["estimate"] is not None and ratio["estimate"] > 1


def test_summary_is_reproducible_and_does_not_depend_on_row_order():
    rows, commits = build()
    first, _ = summarize(join_activity(rows, activity(commits)), activity(commits))
    second, _ = summarize(join_activity(list(reversed(rows)), activity(commits)), activity(commits))
    assert first == second


def test_dormancy_counts_both_censoring_kinds_and_reports_unknown_state():
    rows = [episode("1"), episode("2", exit_kind="observable_ancestry_censored"), episode("3"), episode("4", exit_kind="remove")]
    flags = activity({1: 1, 2: 1, 3: 1, 4: 1}, dormant90="False", dormant180="False")
    flags["1"]["no_commit_last_90d_at_end"] = "True"
    flags["2"]["no_commit_last_90d_at_end"] = "True"
    flags["3"]["no_commit_last_180d_at_end"] = ""
    result = dormancy(join_activity(rows, flags), flags)
    assert result["persisting_episodes"] == 3 and result["persisting_repositories"] == 3
    assert result["by_exit_kind"] == {"study_window_censored": dict(episodes=2, dormant_90d=1, dormant_180d=0),
                                      "observable_ancestry_censored": dict(episodes=1, dormant_90d=1, dormant_180d=0)}
    assert result["dormant_90d"]["dormant_episodes"] == 2 and result["dormant_90d"]["episodes_with_known_state"] == 3
    assert result["dormant_90d"]["share"]["estimate"] == pytest.approx(2 / 3)
    assert result["dormant_180d"]["episodes_with_unknown_state"] == 1 and result["dormant_180d"]["episodes_with_known_state"] == 2
    assert truthy("True") is True and truthy("False") is False and truthy("") is None


def test_activity_reader_binds_files_and_populations(tmp_path):
    directory = tmp_path / "data/derived/maintenance-activity-v1"
    directory.mkdir(parents=True)
    with (directory / "activity.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["repository_id", "group", "commits_pre_180d"])
        writer.writeheader()
        writer.writerows([dict(repository_id="1", group="treated", commits_pre_180d="3"), dict(repository_id="9", group="control", commits_pre_180d="4")])
    (directory / "activity_bins.json").write_text("{}", encoding="utf-8")
    write_json(directory / "summary.json", dict(output_sha256={name: sha256(directory / name) for name in ("activity.csv", "activity_bins.json")}))
    rows, _ = read_activity(tmp_path, [1], [9])
    assert set(rows) == {"1"}
    with pytest.raises(ValueError, match="Treated activity rows differ"):
        read_activity(tmp_path, [1, 2], [9])
    with pytest.raises(ValueError, match="Control activity rows differ"):
        read_activity(tmp_path, [1], [9, 10])
    (directory / "activity_bins.json").write_text('{"x": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="changed since it was produced"):
        read_activity(tmp_path, [1], [9])


def test_dormancy_is_not_zero_by_construction_for_a_quiet_repository():
    # a repository silent since before the window end can only contribute ancestry-censored periods
    rows = [episode("1", exit_kind="observable_ancestry_censored"), episode("2")]
    flags = activity({1: 1, 2: 1})
    flags["1"]["no_commit_last_90d_at_end"] = "True"
    result = dormancy(join_activity(rows, flags), flags)
    assert result["dormant_90d"]["share"]["estimate"] == pytest.approx(0.5)


def test_annotate_flags_degenerate_and_partial_intervals():
    base = dict(estimate=0.0, lower=0.0, upper=0.0, valid_replicates=2000, requested_replicates=2000)
    assert annotate(base)["status"] == "zero_degenerate"
    assert annotate(dict(base, estimate=2.0, lower=2.0, upper=2.0))["status"] == "constant_degenerate"
    assert annotate(dict(base, estimate=1.0, lower=0.5, upper=2.0, valid_replicates=1931))["status"] == "partial_replicates"
    assert annotate(dict(base, estimate=1.0, lower=0.5, upper=2.0))["status"] == "estimated"
    assert annotate(dict(base, estimate=None, lower=None, upper=None, valid_replicates=0))["status"] == "undefined"
