import math

import pytest

from pintrace.control_compare import Group, compare, converted_share, fixed_share_change, rate, rate_ratio
from pintrace.formal_extensions import BINS, PERIODS


def cell(exposure=0.0, pin=0, remove=0, mutable_update=0):
    return dict(exposure=exposure, pin=pin, remove=remove, mutable_update=mutable_update)


def series(pre_pins, post_pins, exposure=10.0):
    """One repository: constant exposure in every bin, the given pin counts in the pre bins and in bin 0."""
    other = {b: cell(exposure, pin=(pre_pins if b < 0 else post_pins if b == 0 else 0)) for b in BINS}
    return dict(other=other, target={b: cell() for b in BINS})


def identities(pre_fixed, post_fixed, known=2):
    return {b: dict(known=known if b in (-1, 0) else 0, fixed=pre_fixed if b == -1 else post_fixed if b == 0 else 0) for b in BINS}


def group(name, count, pre_pins, post_pins, pre_fixed=0, post_fixed=0):
    ids = [f"{name}{i}" for i in range(count)]
    return Group(name, {i: series(pre_pins, post_pins) for i in ids}, {i: identities(pre_fixed, post_fixed) for i in ids})


def test_rates_and_ratio_use_pooled_events_over_pooled_exposure():
    g = group("t", 4, pre_pins=1, post_pins=6)
    sums = g.totals()
    # pre: 4 repos x 6 bins x 10 exposure = 240 exposure, 4 repos x 6 bins x 1 pin = 24 pins
    assert rate(sums, "other", "pin", PERIODS["pre"]) == pytest.approx(100 * 24 / 240)
    assert rate(sums, "other", "pin", PERIODS["post_0_30"]) == pytest.approx(100 * 24 / 40)
    assert rate_ratio(sums, "other", "pin", PERIODS["post_0_30"]) == pytest.approx(6.0)


def test_converted_share_counts_repositories_with_at_least_one_pin_in_each_bin():
    g = group("t", 3, pre_pins=0, post_pins=1)
    sums = g.totals()
    assert converted_share(sums, "other", PERIODS["pre"]) == 0.0
    assert converted_share(sums, "other", PERIODS["post_0_30"]) == 1.0


def test_fixed_share_change_compares_new_identities_after_with_before():
    g = group("t", 2, 0, 0, pre_fixed=0, post_fixed=2)
    assert fixed_share_change(g.totals(), PERIODS["post_0_30"]) == pytest.approx(1.0)


def test_ratio_of_ratios_is_one_when_both_groups_move_alike_and_larger_when_treated_moves_more():
    same = compare(group("t", 20, 1, 3), group("c", 20, 1, 3), replicates=200)
    assert same["ratio_of_ratios.other.pin.post_0_30"]["estimate"] == pytest.approx(1.0)
    more = compare(group("t", 20, 1, 6), group("c", 20, 1, 3), replicates=200)
    assert more["ratio_of_ratios.other.pin.post_0_30"]["estimate"] == pytest.approx(2.0)
    assert more["treated.other.pin_ratio.post_0_30"]["estimate"] == pytest.approx(6.0)
    assert more["control.other.pin_ratio.post_0_30"]["estimate"] == pytest.approx(3.0)


def test_identical_repositories_give_degenerate_intervals_at_the_point_estimate():
    result = compare(group("t", 10, 1, 4), group("c", 10, 1, 2), replicates=200)["ratio_of_ratios.other.pin.post_0_30"]
    assert result["lower"] == pytest.approx(result["estimate"]) and result["upper"] == pytest.approx(result["estimate"])
    assert result["valid_replicates"] == 200


def test_intervals_widen_when_repositories_differ_and_resampling_is_reproducible():
    def mixed(name, count):
        ids = [f"{name}{i}" for i in range(count)]
        return Group(name, {i: series(1, 2 + (k % 5)) for k, i in enumerate(ids)}, {i: identities(0, k % 3) for k, i in enumerate(ids)})
    first = compare(mixed("t", 30), mixed("c", 30), replicates=300)
    again = compare(mixed("t", 30), mixed("c", 30), replicates=300)
    assert first == again
    interval = first["treated.other.pin_ratio.post_0_30"]
    assert interval["lower"] < interval["estimate"] < interval["upper"]


def test_undefined_ratios_are_reported_as_missing_not_as_zero():
    empty = Group("c", {"c0": dict(other={b: cell() for b in BINS}, target={b: cell() for b in BINS})}, {"c0": identities(0, 0)})
    result = compare(group("t", 5, 1, 2), empty, replicates=50)
    assert result["control.other.pin_ratio.post_0_30"]["estimate"] is None
    assert result["control.other.pin_ratio.post_0_30"]["valid_replicates"] == 0
    assert not any(math.isnan(v["estimate"]) for v in result.values() if v["estimate"] is not None)


from pintrace.control_compare import PLACEBO_PERIODS, PLACEBO_SHIFT_DAYS, placebo_estimates, shift_record
from pintrace.formal_extensions import conversion_series, new_identity_series

DAY = 86400
TRUE_ANCHOR = 1_000 * DAY


def state(offset, pinned, total=1):
    return dict(timestamp=TRUE_ANCHOR + offset * DAY, complete=True, target_references=0, target_full_sha=0,
                action_references=total, full_sha_references=int(pinned))


def history_two_pins(identity):
    """Two mutable references: one pinned at day -120 and one at day -50, both before the real anchor."""
    ref = lambda kind: dict(ref_type=kind, action="a/b")
    pin = lambda index, day: dict(event="pin", index=index, timestamp=TRUE_ANCHOR + day * DAY, before=ref("mutable_or_unresolved"), after=ref("full_sha"))
    return dict(repository_id=identity, anchor=TRUE_ANCHOR, window_end=TRUE_ANCHOR + 400 * DAY, observed_ancestry_covers_end=True,
                clock_aligned=True, target_action="t/t", endpoint_index=2,
                snapshots=[state(-200, 0, 2), state(-120, 1, 2), state(-50, 2, 2)],
                events=[pin(1, -120), pin(2, -50)], first_action_groups=[])


def history(identity="1"):
    """Mutable at day -200, pinned at day -50 (before the real anchor), unpinned and pinned again at day +20."""
    ref = lambda kind: dict(ref_type=kind, action="a/b")
    return dict(repository_id=identity, anchor=TRUE_ANCHOR, window_end=TRUE_ANCHOR + 400 * DAY, observed_ancestry_covers_end=True,
                clock_aligned=True, target_action="t/t", endpoint_index=3,
                snapshots=[state(-200, False), state(-50, True), state(10, False), state(20, True)],
                events=[dict(event="pin", index=1, timestamp=TRUE_ANCHOR - 50 * DAY, before=ref("mutable_or_unresolved"), after=ref("full_sha")),
                        dict(event="pin", index=3, timestamp=TRUE_ANCHOR + 20 * DAY, before=ref("mutable_or_unresolved"), after=ref("full_sha"))],
                first_action_groups=[])


def test_shifted_record_changes_only_the_anchor_and_the_end_of_observation():
    record = history()
    shifted = shift_record(record, TRUE_ANCHOR - 90 * DAY, TRUE_ANCHOR)
    assert shifted["anchor"] == TRUE_ANCHOR - 90 * DAY and shifted["window_end"] == TRUE_ANCHOR
    assert shifted["snapshots"] is record["snapshots"] and shifted["events"] is record["events"]
    assert record["anchor"] == TRUE_ANCHOR and record["window_end"] == TRUE_ANCHOR + 400 * DAY


def test_nothing_after_the_real_anchor_enters_a_placebo_bin():
    shifted = shift_record(history(), TRUE_ANCHOR - PLACEBO_SHIFT_DAYS * DAY, TRUE_ANCHOR)
    series = conversion_series(shifted)["other"]
    assert sum(cell["pin"] for cell in series.values()) == 1
    assert series[1]["pin"] == 1  # day -50 is 40 days after the fake anchor
    assert all(series[b]["exposure"] == 0 for b in range(3, 12))
    # mutable until day -50, which is 40 days into the fake window: full bin 0, one third of bin 1, none afterwards
    assert series[0]["exposure"] == pytest.approx(1.0) and series[1]["exposure"] == pytest.approx(1 / 3) and series[2]["exposure"] == 0
    assert all(cell["known"] == 0 for cell in new_identity_series(shifted).values())


def test_placebo_estimates_use_only_pre_anchor_data_and_report_their_periods():
    records = [history_two_pins(str(i)) for i in range(1, 7)]
    result = placebo_estimates(records, records, {r["repository_id"] for r in records}, {r["repository_id"] for r in records}, TRUE_ANCHOR, replicates=50)
    assert result["fake_anchor"] == TRUE_ANCHOR - 90 * DAY and result["shift_days"] == 90
    assert result["periods"] == {name: [period.start, period.stop] for name, period in PLACEBO_PERIODS.items()}
    assert result["repositories"] == dict(treated=6, control=6)
    keys = result["estimates"]
    assert "ratio_of_ratios.other.pin.post_30_90" in keys and not any(k.endswith("post_180_360") for k in keys)
    # pre (true days -180..-90): exposure 2 + 2 + 1 = 5 and one pin, rate 20; post_30_90: exposure 1/3 and one pin, rate 300
    assert keys["treated.other.pin_rate.pre"]["estimate"] == pytest.approx(20.0)
    assert keys["treated.other.pin_rate.post_30_90"]["estimate"] == pytest.approx(300.0)
    assert keys["treated.other.pin_ratio.post_30_90"]["estimate"] == pytest.approx(15.0)
    assert keys["control.other.pin_ratio.post_30_90"]["estimate"] == pytest.approx(15.0)
    assert keys["ratio_of_ratios.other.pin.post_30_90"]["estimate"] == pytest.approx(1.0)
    assert keys["ratio_of_ratios.other.pin.post_30_90"]["valid_replicates"] == 50
