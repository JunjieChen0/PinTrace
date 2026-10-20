import pytest

from pintrace.formal_placebo import (HEADLINE, build, classify, consistency, month_scan, null_value, pair_estimates, state, windows)


def e(estimate, lower, upper, valid=2000):
    return dict(estimate=estimate, lower=lower, upper=upper, valid_replicates=valid, requested_replicates=2000)


def test_null_values_cover_ratios_and_changes_but_not_levels():
    assert null_value("treated.other.pin_ratio.post_0_30") == 1.0
    assert null_value("control.other.converted_share_ratio.post_30_90") == 1.0
    assert null_value("ratio_of_ratios.other.pin.post_0_30") == 1.0
    assert null_value("difference_in_changes.new_fixed_share.post_0_30") == 0.0
    assert null_value("treated.new_fixed_share_change.post_0_30") == 0.0
    assert null_value("treated.other.pin_rate.post_0_30") is None
    assert null_value("treated.other.converted_share.pre") is None


def test_state_needs_both_bounds():
    assert state(e(2, 1.5, 3), 1.0) == "excludes_null"
    assert state(e(.5, .1, .9), 1.0) == "excludes_null"
    assert state(e(1, .5, 2), 1.0) == "includes_null"
    assert state(e(1, None, None), 1.0) == "undefined" and state(None, 1.0) == "undefined"


@pytest.mark.parametrize("real,placebo,verdict", [
    (e(5, 2, 9), e(1, .5, 2), "separated_from_placebo"),
    (e(5, .5, 9), e(1, .5, 2), "not_distinguishable_from_null"),
    (e(5, 2, 9), e(4, 2, 6), "placebo_also_shows_effect"),
    (e(5, 2, 9), e(.2, 0, .6), "real_departure_placebo_opposite"),
    (e(1.2, .5, 2.1), e(3, 2, 4), "placebo_departs_real_null"),
    (e(5, 2, 9), e(None, None, None, 0), "undefined"),
])
def test_verdicts_distinguish_a_reproduced_effect_from_an_opposite_departure(real, placebo, verdict):
    assert classify(real, placebo, 1.0)["verdict"] == verdict


def test_marginal_flag_marks_a_bound_hugging_the_ratio_null_and_skips_differences():
    assert classify(e(2, 1.009, 4.3), e(1, .4, 2.9), 1.0)["marginal"] is True
    assert classify(e(2, 1.5, 4.3), e(1, .4, 2.9), 1.0)["marginal"] is False
    assert classify(e(.4, .1, .96), e(1, .4, 2.9), 1.0)["marginal"] is True  # a decrease measured from the upper bound
    assert classify(e(.2, .05, .4), e(0, -.1, .1), 0.0)["marginal"] is None
    assert classify(e(1, .5, 2), e(1, .4, 2.9), 1.0)["marginal"] is None  # nothing to flag when the real interval holds the null


def test_interval_status_is_carried_into_the_pair():
    pair = classify(e(5, 2, 9), e(.3, 0, 8, valid=1717), 1.0)
    assert pair["interval_status"] == dict(real="estimated", placebo="partial_replicates")


def test_pairing_keeps_placebo_only_windows_visible():
    real = {"treated.other.pin_ratio.post_0_30": e(20, 9, 60), "treated.other.pin_rate.pre": e(.6, .2, 1)}
    placebo = {"treated.other.pin_ratio.post_0_30": e(.1, 0, .5), "treated.other.pin_ratio.post_0_90": e(.6, .1, 2),
               "treated.other.pin_rate.pre": e(.8, .2, 1.6)}
    pairs, unpaired = pair_estimates(real, placebo)
    assert list(pairs) == ["treated.other.pin_ratio.post_0_30"] and unpaired == ["treated.other.pin_ratio.post_0_90"]
    assert pairs["treated.other.pin_ratio.post_0_30"]["verdict"] == "real_departure_placebo_opposite"


def extensions(first_lower=8.0):
    pre_rate = [(-6, .8, 0, 2.3), (-5, .5, 0, 1.4), (-4, 1.1, 0, 2.9), (-3, .1, 0, .3), (-2, 1.4, .2, 3.5), (-1, .04, .01, .09)]
    bins = [dict(bin=b, pin_rate=e(rate, lo, hi)) for b, rate, lo, hi in pre_rate] + [dict(bin=0, pin_rate=dict(e(14.2, first_lower, 21.6), seed=20261018, n_clusters=448, requested_replicates=2000))]
    rows = [dict(bin=b, pin_ratio=e(r, lo, hi), share_ratio=e(1, .2, 2)) for b, r, lo, hi in
            [(-6, 1.3, .05, 6.1), (-5, .76, .03, 3.4), (-4, 1.9, 0, 8.3), (-3, .14, 0, .55), (-2, 2.8, .3, 11), (-1, .05, .01, .12)]]
    return {"interrupted_series": {"placebo": {"other": rows}, "bins": {"other": bins}}}


def test_month_scan_counts_months_off_one_and_checks_the_post_month_against_every_pre_month():
    scan = month_scan(extensions())["other"]
    assert scan["pin_ratio"]["excluding_one"] == 2 and scan["pin_ratio"]["months_excluding_one"] == [-3, -1]
    assert scan["share_ratio"]["excluding_one"] == 0
    post = scan["first_post_month"]
    assert post["lower_above_every_pre_month_upper"] is True and post["highest_pre_month_bin"] == -2
    assert post["estimate_over_highest_pre_month_estimate"] == pytest.approx(14.2 / 1.4)
    assert month_scan(extensions(first_lower=3.0))["other"]["first_post_month"]["lower_above_every_pre_month_upper"] is False


def control_report():
    return {"seed": 20261018, "replicates": 2000, "repositories": {"treated": 448, "control": 299},
            "window": {"start": "2024-09-14T00:00:00Z", "end": "2026-09-14T23:59:59Z"},
            "estimates": {"treated.other.pin_rate.post_0_30": e(14.2, 8, 21.6)},
            "placebo": {"fake_anchor": 1734134400.0, "true_anchor": 1741910400.0, "shift_days": 90,
                        "periods": {"pre": [-3, 0], "post_0_30": [0, 1], "post_30_90": [1, 3], "post_0_90": [0, 3]},
                        "repositories": {"treated": 448, "control": 299}, "estimates": {}}}


def test_windows_report_dates_and_the_shorter_fake_baseline():
    result = windows(control_report())
    assert (result["fake_anchor"], result["true_anchor"], result["shift_days"]) == ("2024-12-14", "2025-03-14", 90)
    assert result["periods"]["pre"] == dict(start="2024-09-15", end="2024-12-14")
    assert result["periods"]["post_30_90"]["end"] == "2025-03-14"
    assert (result["placebo_pre_days"], result["real_pre_days"]) == (90, 180) and result["pre_window_inside_study_window"] is True


def test_cross_report_consistency_catches_each_kind_of_drift():
    assert all(consistency(control_report(), extensions()).values())
    other_seed = control_report() | {"seed": 1}
    assert consistency(other_seed, extensions())["same_seed_and_replicates"] is False
    moved = control_report() | {"repositories": {"treated": 447, "control": 299}}
    assert consistency(moved, extensions())["same_treated_clusters"] is False
    drift = control_report()
    drift["estimates"]["treated.other.pin_rate.post_0_30"] = e(14.3, 8, 21.6)
    assert consistency(drift, extensions())["first_month_rate_agrees_across_reports"] is False


def test_build_maps_every_headline_claim_and_fails_loudly_on_schema_drift():
    control = control_report()
    for key in HEADLINE.values():
        null = null_value(key)
        control["estimates"][key] = e(null + 4 if null else .3, null + 2 if null else .1, null + 8 if null else .5)
        control["placebo"]["estimates"][key] = e(null, null - .5 if null else -.1, null + .9 if null else .1)
    built = build(control, extensions())
    assert set(built["claims"]) == set(HEADLINE) and built["contrasts"]["paired"] == len(HEADLINE)
    assert all(claim["verdict"] == "separated_from_placebo" for claim in built["claims"].values())
    assert built["contrasts"]["verdict_counts"] == {"separated_from_placebo": len(HEADLINE)}
    del control["placebo"]["estimates"][HEADLINE["treated_pin_ratio_first_30d"]]
    with pytest.raises(ValueError, match="no paired placebo estimate"):
        build(control, extensions())
