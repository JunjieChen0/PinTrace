from datetime import datetime, timezone

from pintrace.configuration import enrich_workflow
from pintrace.formal_robustness import DAY, maintenance_segment, shifted_target_episodes, truncate_episodes
from pintrace.lineage import reconstruct
from pintrace.pilot import timestamp


ANCHOR = timestamp("2025-03-14T00:00:00Z")
TARGET = "tj-actions/changed-files"


def test_excluded_incident_day_exit_stays_an_unknown_boundary():
    episode = dict(entry_timestamp=ANCHOR, exit_timestamp=ANCHOR + 100,
                   episode_kind="baseline_fixed", exit_kind="unpin")
    rows, reasons = truncate_episodes([episode], ANCHOR, ANCHOR + 20 * DAY, exclude_event_day=True)
    assert rows[0]["exit_kind"] == "unknown_boundary"
    assert rows[0]["exit_timestamp"] == ANCHOR + 100
    assert reasons["exit_on_excluded_event_day"] == 1


def test_landmark_does_not_restore_previously_exited_periods():
    episodes = [dict(entry_timestamp=ANCHOR, exit_timestamp=ANCHOR + 2 * DAY,
                     episode_kind="baseline_fixed", exit_kind="remove"),
                dict(entry_timestamp=ANCHOR, exit_timestamp=ANCHOR + 50 * DAY,
                     episode_kind="baseline_fixed", exit_kind="unpin")]
    rows, reasons = truncate_episodes(episodes, ANCHOR + 7 * DAY, ANCHOR + 30 * DAY)
    assert len(rows) == 1 and reasons["outside_segment"] == 1
    assert rows[0]["calendar_segment_delayed_entry"]
    assert rows[0]["exit_kind"] == "study_window_censored"
    assert rows[0]["observed_followup_seconds"] == 23 * DAY


def test_shifted_anchor_reconstructs_real_baseline_instead_of_relabeling_old_period():
    snapshots = []
    for index, offset, ref in [(0, -2, "v1"), (1, 0.5, "a" * 40), (2, 2, "a" * 40)]:
        text = f"jobs:\n  build:\n    steps:\n      - id: changed\n        uses: {TARGET}@{ref}\n"
        point = ANCHOR + offset * DAY
        snapshots.append(dict(sha=f"sha-{index}", timestamp=point,
                              date=datetime.fromtimestamp(point, timezone.utc).isoformat(), complete=True,
                              references=enrich_workflow(text, ".github/workflows/ci.yml")["references"]))
    detail = dict(summary=dict(repository_id="1", repository="owner/repo", canonical_repository="owner/repo",
                               status="measured", measurement_rules_version="measurement-v3", observed_ancestry_covers_end=True,
                               baseline_commit="sha-0", endpoint_commit="sha-2"), snapshots=snapshots,
                  timeline=dict(endpoint_commit="sha-2"), replay={"passed": True}, lineages=reconstruct("owner/repo", snapshots))
    study = dict(target_action=TARGET, event_anchor="2025-03-14T00:00:00Z", window_end="2026-03-14T23:59:59Z")
    original, _ = shifted_target_episodes([detail], {"1"}, study, ANCHOR)
    shifted, _ = shifted_target_episodes([detail], {"1"}, study, ANCHOR + DAY)
    assert len(original) == len(shifted) == 1
    assert original[0]["episode_kind"] == "post_anchor_observed_pin"
    assert shifted[0]["episode_kind"] == "baseline_fixed"
    assert shifted[0]["entry_timestamp"] == ANCHOR + DAY


def test_maintenance_calendar_stratum_censors_future_adoption():
    episode = dict(episode_id="old", repository_id="1", lineage_id="ref", entry_timestamp=ANCHOR,
                   exit_timestamp=ANCHOR + 300 * DAY, entry_commit="pin", episode_kind="post_anchor_observed_pin")
    entry = dict(available_by_timestamp=ANCHOR + 10 * DAY, available_by=ANCHOR + 10 * DAY,
                 target_ids=["old"], focal_links=[], outcome="adopted_target", outcome_timestamp=ANCHOR + 100 * DAY)
    row = maintenance_segment([entry], [episode], [episode], ANCHOR, ANCHOR + 30 * DAY)[0]
    assert row["outcome"] == "right_censored_no_observed_sha_change"
    assert row["outcome_timestamp"] == ANCHOR + 30 * DAY


def test_anchor_sensitivity_relinks_target_and_drops_no_longer_post_anchor_focal():
    old = dict(episode_id="old-pin", repository_id="1", lineage_id="ref", entry_timestamp=ANCHOR,
               exit_timestamp=ANCHOR + 300 * DAY, entry_commit="pin", episode_kind="post_anchor_observed_pin")
    shifted = dict(old, episode_id="shifted-baseline", episode_kind="baseline_fixed", entry_timestamp=ANCHOR + DAY)
    entry = dict(available_by_timestamp=ANCHOR + 10 * DAY, available_by=ANCHOR + 10 * DAY,
                 target_ids=["old-pin"], focal_links=[("old-pin", "same_commit_co_pinned_supplement")],
                 outcome="adopted_target", outcome_timestamp=ANCHOR + 20 * DAY)
    row = maintenance_segment([entry], [old], [shifted], ANCHOR + DAY, ANCHOR + 300 * DAY, remap_anchor=True)[0]
    assert row["target_ids"] == ["shifted-baseline"]
    assert row["focal_links"] == []
