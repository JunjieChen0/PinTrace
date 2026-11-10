"""Pure parts of the denominator audit (pintrace/denominator_audit.py) on synthetic records."""

import pytest

from pintrace import denominator_audit as da

DAY = 86400
ANCHOR = 1_000 * DAY
END = ANCHOR + 549 * DAY


def snapshot(day, refs=4, full=1, target=0, complete=True):
    return dict(timestamp=ANCHOR + day * DAY, complete=complete, action_references=refs + target,
                full_sha_references=full, target_references=target, target_full_sha=0, sha=f"c{day}")


def record(rid, snapshots, covers_end=True, aligned=True):
    return dict(repository_id=rid, anchor=ANCHOR, window_end=END, observed_ancestry_covers_end=covers_end,
                endpoint_index=len(snapshots) - 1, snapshots=snapshots, clock_aligned=aligned)


ACTIVE = record("1", [snapshot(-10), snapshot(540)])
QUIET = record("2", [snapshot(-10, full=0), snapshot(100, full=0)], covers_end=False)
EMPTY = record("3", [snapshot(-10, refs=0, full=0, target=2)])
PARTIAL = record("4", [snapshot(-10, complete=False), snapshot(560)])


def test_stock_status_names_each_reason():
    assert da.stock_status(ACTIVE, 540) == "counted"
    assert da.stock_status(QUIET, 540) == "no_default_branch_commit_after_last_observed"
    assert da.stock_status(QUIET, 90) == "counted"
    assert da.stock_status(EMPTY, 0) == "no_other_action_references"
    assert da.stock_status(PARTIAL, 0) == "no_complete_snapshot"


def test_unaligned_calendar_is_excluded_from_the_stock():
    unaligned = record("5", [snapshot(-10), snapshot(540)], aligned=False)
    assert da.stock_status(unaligned, 540) == "excluded_clock_unaligned"
    series = da.pr.stock_series([ACTIVE, unaligned], {"1", "5"})
    assert series["excluded_clock_unaligned"] == 1
    assert next(row for row in series["series"] if row["offset_days"] == 540)["repositories"] == 1


def test_attrition_counts_only_the_formal_ids():
    attrition = da.stock_attrition([ACTIVE, QUIET, EMPTY], {"1", "2"}, offsets=(540,))
    assert attrition == {"540": {"counted": 1, "no_default_branch_commit_after_last_observed": 1}}


def test_carry_forward_keeps_the_last_state_without_changing_the_input():
    carried = da.carried_forward(QUIET)
    assert carried["observed_ancestry_covers_end"] is True and QUIET["observed_ancestry_covers_end"] is False
    assert da.stock_status(carried, 540) == "counted"


def test_carry_forward_is_refused_when_a_clone_predates_the_window_end():
    with pytest.raises(ValueError, match="not determinate"):
        da.carry_forward_stock([ACTIVE, QUIET], {"1", "2"}, {"1": END + 1, "2": END - 1}, END)


def maintenance(a=3, b=1):
    return dict(populations=dict(formal=dict(target=dict(
        counts=dict(episodes=2, repositories=1, entries=a + b, opportunity_ids=1),
        by_grade=dict(A=dict(counts=dict(entries=a)), B=dict(counts=dict(entries=b)))))))


def test_table5_requires_every_dimension_to_reach_the_grade_totals():
    rows = [dict(category="covered", grade="A", entries=2, repositories=1),
            dict(category="unknown", grade="A", entries=1, repositories=1),
            dict(category="covered", grade="B", entries=1, repositories=1)]
    tabs = {field: rows for field in da.CROSS_TABS}
    assert da.table5(tabs, maintenance())["entry_comment_kind"]["totals"] == {"A": 3, "B": 1}
    with pytest.raises(ValueError, match="sums to"):
        da.table5({**tabs, "entry_comment_kind": rows[:2]}, maintenance())


def test_periods_check_that_kinds_sum_to_the_total():
    persistence = dict(populations=dict(formal=dict(
        identity=dict(episodes=3, repositories=2),
        by_kind=dict(baseline_fixed=dict(identity=dict(episodes=2, repositories=1)),
                     post_anchor_observed_pin=dict(identity=dict(episodes=1, repositories=1))))))
    routes = dict(changed_entries=5, entries=7, distinct_changes=dict(changes=4, by_role=dict(target=1, comparison=3)))
    result = da.periods(persistence, maintenance(), routes)
    assert result["maintenance_target"]["periods_without_graded_opportunity"] == 1
    persistence["populations"]["formal"]["identity"]["episodes"] = 4
    with pytest.raises(ValueError, match="do not sum"):
        da.periods(persistence, maintenance(), routes)
