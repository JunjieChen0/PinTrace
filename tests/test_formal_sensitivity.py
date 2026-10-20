from datetime import datetime, timezone

import pytest

from pintrace.formal_rq12 import DAY, extract_repository
from pintrace.formal_sensitivity import (CLAIMS, LANDMARKS, METRICS, RERUN, ROBUSTNESS, WINDOW_LABELS, build_matrix, compare, concentration,
                                         cross_support, full_window_check, key_for, rerun_windows, six_months_after, window_end_rows, window_ends)
from pintrace.lineage import reconstruct
from pintrace.workflow import classify_uses

ANCHOR = 20 * DAY
TARGET = "tj-actions/changed-files"
SHA = "a" * 40
FULL_END = ANCHOR + 366 * DAY - 1
STUDY = dict(event_anchor=ANCHOR, window_end=FULL_END, target_action=TARGET)


def e(estimate, lower, upper, valid=2000):
    return dict(estimate=estimate, lower=lower, upper=upper, valid_replicates=valid, requested_replicates=2000)


def test_compare_separates_overlapping_and_consistent_and_never_guesses():
    main = e(.5, .4, .6)
    assert compare(main, e(.52, .45, .58)) == "consistent"
    assert compare(main, e(.3, .2, .45)) == "overlapping"  # intervals meet, but the main point is outside
    assert compare(main, e(.8, .7, .9)) == "separated" and compare(e(.8, .7, .9), main) == "separated"
    assert compare(main, e(None, None, None)) == "undefined" and compare(None, main) == "undefined"
    assert compare(main, e(.5, None, .6)) == "undefined"


def metrics_for(**overrides):
    """Every matrix key present with a common value, so single overrides stand out."""
    metrics = {}
    for template in METRICS.values():
        for label in ("formal_main", *ROBUSTNESS, *LANDMARKS, "before_sha_policy", "sha_policy_before_immutable", "after_immutable_releases"):
            population = {"raw_main": "raw", "exclude_pilot": "formal_exclude_pilot"}.get(label, "formal")
            metrics[key_for(template, label, population)] = e(.5, .4, .6)
    metrics.update(overrides)
    return metrics


def test_matrix_counts_judged_scenarios_and_keeps_landmarks_and_strata_out_of_the_count():
    base = key_for(METRICS["rq1_first_response_pin"], "formal_main", "formal")
    shifted = key_for(METRICS["rq1_first_response_pin"], "after_first_seven_days", "formal")
    raw = key_for(METRICS["rq1_first_response_pin"], "raw_main", "raw")
    matrix = build_matrix(metrics_for(**{shifted: e(.9, .8, .95), raw: e(.9, .8, .95)}))
    row = matrix["rq1_first_response_pin"]
    assert row["main"]["key"] == base and row["main"]["relation"] == "consistent"
    assert row["summary"]["scenarios"] == len(ROBUSTNESS) == 5
    assert row["summary"]["separated"] == 1 and row["summary"]["separated_scenarios"] == ["raw_main"]
    assert row["summary"]["separated_landmarks"] == ["after_first_seven_days"]  # visible, but not in the judged count
    assert row["landmarks"]["after_first_seven_days"]["relation"] == "separated"
    assert set(row["calendar_strata"]) == {"before_sha_policy", "sha_policy_before_immutable", "after_immutable_releases"}


def test_a_missing_scenario_is_undefined_not_consistent():
    metrics = metrics_for()
    del metrics[key_for(METRICS["rq2_new_actions_all_full_sha"], "anchor_plus_one_day", "formal")]
    summary = build_matrix(metrics)["rq2_new_actions_all_full_sha"]["summary"]
    assert summary["undefined"] == 1 and summary["undefined_scenarios"] == ["anchor_plus_one_day"] and summary["consistent"] == 4


def placebo_report(new_verdict="separated_from_placebo", scan=True):
    return {"within_pre_months": {"other": {"first_post_month": {"lower_above_every_pre_month_upper": scan}}},
            "claims": {"new_fixed_share_change_vs_control_first_30d": dict(verdict=new_verdict, marginal=None)}}


def test_claim_statuses_distinguish_missing_design_related_construct_and_unstable_rows():
    claims = cross_support(build_matrix(metrics_for()), placebo_report())
    assert claims["first_response_pin_share"]["status"] == "no_placebo_design"
    assert claims["persistence_180d_observed_pin"]["status"] == "no_placebo_design"
    assert claims["existing_references_pinned_by_180d"]["status"] == "related_placebo_only"
    assert claims["new_actions_fully_pinned_at_introduction"]["status"] == "related_placebo_only"
    assert not any(c["cross_supported"] for c in claims.values()), "no claim here has a same-construct placebo"
    assert all(c["why_not"] for c in claims.values())

    failing = cross_support(build_matrix(metrics_for()), placebo_report(new_verdict="not_distinguishable_from_null", scan=False))
    assert failing["new_actions_fully_pinned_at_introduction"]["status"] == "placebo_does_not_separate"
    assert failing["existing_references_pinned_by_180d"]["status"] == "placebo_does_not_separate"

    unstable = metrics_for(**{key_for(METRICS["rq2_existing_pin_180d_repository_equal"], "exclude_incident_day", "formal"): e(.9, .8, .95)})
    claim = cross_support(build_matrix(unstable), placebo_report())["existing_references_pinned_by_180d"]
    assert claim["status"] == "sensitivity_unstable" and claim["robustness_stable"] is False
    assert any("exclude_incident_day" in reason for reason in claim["why_not"])


def test_same_construct_placebo_with_a_stable_row_is_the_only_route_to_cross_support(monkeypatch):
    spec = dict(CLAIMS["new_actions_fully_pinned_at_introduction"], relation="same_construct")
    monkeypatch.setitem(CLAIMS, "new_actions_fully_pinned_at_introduction", spec)
    claims = cross_support(build_matrix(metrics_for()), placebo_report())
    assert claims["new_actions_fully_pinned_at_introduction"]["status"] == "cross_supported"
    assert claims["new_actions_fully_pinned_at_introduction"]["cross_supported"] is True
    assert claims["new_actions_fully_pinned_at_introduction"]["why_not"] == []


def test_every_claim_points_at_a_matrix_metric_and_states_a_placebo_or_a_reason():
    assert all(spec["metric"] in METRICS for spec in CLAIMS.values())
    assert all(("placebo" in spec and "relation" in spec and "note" in spec) or "reason" in spec for spec in CLAIMS.values())
    assert all(spec["relation"] in {"same_construct", "related_construct"} for spec in CLAIMS.values() if "placebo" in spec)
    assert set(RERUN) <= set(METRICS) and not any(name.startswith("maintenance") or "365d" in name for name in RERUN)


def stamp(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp())


def test_six_months_end_on_the_last_second_of_the_day_and_clip_short_months():
    assert six_months_after(stamp(2025, 3, 14)) == stamp(2025, 9, 14, 23, 59, 59)
    assert six_months_after(stamp(2025, 8, 31)) == stamp(2026, 2, 28, 23, 59, 59)  # no 31 February
    assert six_months_after(stamp(2025, 10, 5)) == stamp(2026, 4, 5, 23, 59, 59)  # crosses the year boundary


def test_window_ends_must_grow_from_the_anchor():
    anchor = stamp(2025, 3, 14)
    ends = window_ends(anchor, stamp(2026, 3, 14, 23, 59, 59), stamp(2026, 9, 14, 23, 59, 59))
    assert list(ends) == list(WINDOW_LABELS) and ends[WINDOW_LABELS[0]] < ends[WINDOW_LABELS[1]] < ends[WINDOW_LABELS[2]]
    with pytest.raises(ValueError, match="increase"):
        window_ends(anchor, stamp(2025, 6, 1), stamp(2026, 9, 14))  # original end earlier than six months


def test_full_window_check_flags_any_drift_from_the_published_estimate():
    published = {key_for(METRICS[name], "formal_main", "formal"): e(.5, .4, .6) for name in RERUN}
    rerun = {WINDOW_LABELS[2]: {name: {k: v for k, v in e(.5, .4, .6).items() if k != "requested_replicates"} for name in RERUN}}
    assert full_window_check(rerun, published) == dict(compared=len(RERUN), mismatched=[], equal=True)
    rerun[WINDOW_LABELS[2]]["rq1_first_response_pin"]["upper"] = .61
    drift = full_window_check(rerun, published)
    assert drift["equal"] is False and drift["mismatched"] == ["rq1_first_response_pin"]
    del rerun[WINDOW_LABELS[2]]["rq1_first_response_pin"]
    assert full_window_check(rerun, published)["mismatched"] == ["rq1_first_response_pin"]  # a metric that vanished is a mismatch
    assert full_window_check({WINDOW_LABELS[2]: {}}, {})["equal"] is False  # nothing compared is not a pass


def test_window_end_rows_keep_the_original_result_and_compare_both():
    name = "rq1_first_response_pin"
    key = key_for(METRICS[name], "formal_main", "formal")
    rows = window_end_rows({"end_6_months_after_anchor": {name: e(.3, .25, .35)}, "end_full_window": {name: e(.5, .4, .6)}},
                           {key: e(.5, .4, .6)}, {key: e(.45, .38, .52)})
    row = rows[name]
    assert row["windows"]["end_full_window"]["relation_to_published_main"] == "consistent"
    assert row["windows"]["end_6_months_after_anchor"]["relation_to_published_main"] == "separated"
    assert row["windows"]["end_6_months_after_anchor"]["relation_to_original_window_published"] == "separated"
    assert row["original_window_published"]["estimate"] == .45
    assert rows["rq3_retention_180d_born_fixed"]["windows"]["end_full_window"]["available"] is False


def ref(version="v1", identifier="target"):
    return dict(classify_uses(TARGET + "@" + version), path="ci.yml", job="test", step_id=identifier, step_index=0)


def state(day, refs=()):
    return dict(sha="commit-" + str(day), timestamp=ANCHOR + day * DAY, complete=True, references=list(refs))


def synthetic_record(identity, *states):
    summary = dict(repository_id=identity, canonical_repository="owner/repo" + identity, status="measured", measurement_rules_version="measurement-v4",
                   baseline_commit=next(s["sha"] for s in reversed(states) if s["timestamp"] < ANCHOR), endpoint_commit=states[-1]["sha"],
                   observed_ancestry_covers_end=True)
    detail = dict(summary=summary, snapshots=list(states), timeline=dict(endpoint_commit=states[-1]["sha"]),
                  lineages=reconstruct("owner/repo" + identity, list(states)), replay=dict(passed=True))
    return extract_repository(detail, STUDY)


def episode(identity, number, exit_day, exit_kind, kind="baseline_fixed"):
    return dict(repository_id=identity, episode_id=f"ep-{identity}-{number}", episode_kind=kind, entry_timestamp=float(ANCHOR),
                exit_timestamp=float(ANCHOR + exit_day * DAY), exit_kind=exit_kind, duration_eligible=True, formal=True)


def test_window_end_rerun_accumulates_with_the_window_and_the_full_window_matches_a_direct_run():
    # repository 1 pins on day 5, repository 2 only on day 300, after the six-month end
    records = [synthetic_record("1", state(-2, [ref()]), state(5, [ref(version=SHA)]), state(20)),
               synthetic_record("2", state(-2, [ref()]), state(300, [ref(version=SHA)]), state(330))]
    episodes = [episode("1", 1, 150, "unpin"), episode("2", 1, 300, "remove"), episode("9", 1, 100, "unpin")]  # repository 9 is outside the population
    ends = {WINDOW_LABELS[0]: ANCHOR + 183 * DAY, WINDOW_LABELS[1]: ANCHOR + 250 * DAY, WINDOW_LABELS[2]: FULL_END}
    rerun, registered = rerun_windows(records, episodes, ["1", "2"], ANCHOR, ends)
    pin = {label: rerun[label]["rq1_first_response_pin"]["estimate"] for label in ends}
    assert pin[WINDOW_LABELS[0]] == pin[WINDOW_LABELS[1]] == pytest.approx(0.5)  # repository 2's pin is not yet observed
    assert pin[WINDOW_LABELS[2]] == pytest.approx(1.0)
    assert all(registered[label]["episodes"] == 2 for label in ends), "repository 9 never enters"
    assert registered[WINDOW_LABELS[2]]["episodes_outside_window"] == 0 and registered[WINDOW_LABELS[0]]["episodes_outside_window"] == 0
    assert registered[WINDOW_LABELS[0]]["end"] == datetime.fromtimestamp(ends[WINDOW_LABELS[0]], timezone.utc).strftime("%Y-%m-%d")
    # the 180-day retention needs a determinate episode: repository 1 exited at day 150 in every window
    retention = rerun[WINDOW_LABELS[2]]["rq3_retention_180d_baseline_fixed"]["estimate"]
    assert retention == pytest.approx(0.5)  # repository 2 is retained past 180 days, repository 1 left at day 150
    # the same call is deterministic
    again, _ = rerun_windows(records, episodes, ["1", "2"], ANCHOR, ends)
    assert again == rerun


def test_a_period_unresolved_at_the_window_end_is_censored_not_counted_as_an_exit():
    records = [synthetic_record("1", state(-2, [ref()]), state(5, [ref(version=SHA)]), state(20))]
    episodes = [episode("1", 1, 300, "remove")]
    ends = {WINDOW_LABELS[0]: ANCHOR + 100 * DAY, WINDOW_LABELS[1]: ANCHOR + 200 * DAY, WINDOW_LABELS[2]: FULL_END}
    rerun, registered = rerun_windows(records, episodes, ["1"], ANCHOR, ends)
    assert registered[WINDOW_LABELS[0]]["duration_eligible"] == 1
    # at day 100 the period has no determinate 180-day status (censored early), so the retention share is undefined there
    assert "rq3_retention_180d_baseline_fixed" not in rerun[WINDOW_LABELS[0]]
    assert rerun[WINDOW_LABELS[1]]["rq3_retention_180d_baseline_fixed"]["estimate"] == pytest.approx(1.0)  # 200 days of follow-up, still fixed
    assert rerun[WINDOW_LABELS[2]]["rq3_retention_180d_baseline_fixed"]["estimate"] == pytest.approx(1.0)  # removed at day 300, after the horizon


def test_concentration_reads_leave_out_ratios_without_adding_intervals():
    extensions = {"interrupted_series": {"concentration": dict(first_month_conversions=100, repositories_with_conversions=20, top10_share=.5,
                                                              pre_conversions=10, pre_top5_share=.8, leave_out={"1": 5.0, "5": 3.0},
                                                              leave_out_pre={"1": 9.0, "5": None})}}
    result = concentration(extensions)
    assert result["all_post_leave_out_ratios_above_one"] is True
    assert result["all_pre_leave_out_ratios_above_one"] is False  # an undefined ratio does not count as above one
    assert result["leave_out_largest_post_contributors"] == {"1": 5.0, "5": 3.0}
    assert "intervals" in result["note"]
