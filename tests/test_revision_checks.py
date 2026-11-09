"""Window counts of the revision checks (pintrace/revision_checks.py) on a synthetic compact record."""

from pintrace import revision_checks as rc

DAY = 86400
ANCHOR = 1_000 * DAY


def state(t, mutable_other, complete=True):
    return dict(timestamp=t, complete=complete, action_references=1 + mutable_other, full_sha_references=0,
                target_references=1, target_full_sha=0)


def pin(index, t, commit, action="o/x"):
    return dict(event="pin", index=index, timestamp=t, commit=commit, before=dict(ref_type="tag", action=action))


RECORD = dict(anchor=ANCHOR, window_end=ANCHOR + 60 * DAY, observed_ancestry_covers_end=True, endpoint_index=2,
              target_action="t/a",
              snapshots=[state(ANCHOR - 30 * DAY, 3), state(ANCHOR + 10 * DAY, 2), state(ANCHOR + 20 * DAY, 2)],
              events=[pin(1, ANCHOR + 10 * DAY, "c1"), pin(1, ANCHOR + 10 * DAY, "c1"), pin(2, ANCHOR + 20 * DAY, "c2"),
                      pin(2, ANCHOR + 20 * DAY, "c2", action="t/a")])


def test_exposure_is_mutable_other_references_times_months_in_the_window():
    counts = rc.window_counts(RECORD, ANCHOR, ANCHOR + 30 * DAY)
    # 3 references for 10 days, then 2 references for 20 days: (30 + 40) / 30 reference-months
    assert abs(counts["exposure"] - 70 / 30) < 1e-9


def test_conversions_count_events_and_commits_count_once_and_the_target_is_excluded():
    counts = rc.window_counts(RECORD, ANCHOR, ANCHOR + 30 * DAY)
    assert counts["conversions"] == 3 and counts["commits"] == 2 and counts["last"] == ANCHOR + 20 * DAY


def test_an_incomplete_state_neither_exposes_nor_counts():
    record = dict(RECORD, snapshots=[state(ANCHOR - 30 * DAY, 3), state(ANCHOR + 10 * DAY, 2, complete=False),
                                     state(ANCHOR + 20 * DAY, 2)])
    counts = rc.window_counts(record, ANCHOR, ANCHOR + 30 * DAY)
    assert counts["conversions"] == 0 and abs(counts["exposure"] - (30 + 20) / 30) < 1e-9


def test_the_tail_runs_from_day_540_to_the_window_end_inclusive():
    record = dict(RECORD, window_end=ANCHOR + 549 * DAY)
    assert rc.bounds(record, "tail") == (ANCHOR + 540 * DAY, ANCHOR + 549 * DAY + 1)
    assert rc.bounds(record, "m13_18") == (ANCHOR + 360 * DAY, ANCHOR + 540 * DAY)
