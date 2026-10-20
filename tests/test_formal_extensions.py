import pytest

from pintrace.formal_extensions import (BINS, Clustered, compromised_pins, pick_release_tag, adopted_route, advisory_line, concentration, conversion_series,
                                        interrupted_series, new_identity_series, new_identity_summary, ref_line, sha_line,
                                        target_lines_at, trajectory_state, update_concentration, version_key)

DAY = 86400
ANCHOR = 10_000_000
NEW = "a" * 40
OLD = "b" * 40
COMPROMISED = "0e58ed8671d6b60d0890c21b07f8835ace038e67"
INDEX = {NEW: {"v46.0.1", "v46"}, OLD: {"v45.0.7"}}


def state(sha, days, *, other_mutable=0, other_full=0, target_mutable=0, target_full=0, complete=True):
    return dict(sha=sha, timestamp=ANCHOR + days * DAY, complete=complete,
                action_references=other_mutable + other_full + target_mutable + target_full,
                full_sha_references=other_full + target_full, target_references=target_mutable + target_full,
                target_full_sha=target_full)


def ref(action, value, kind):
    return dict(action=action, ref=value, ref_type=kind)


def record(snapshots, events, *, identity="1", end=100, aligned=True, references=()):
    return dict(repository_id=identity, repository="o/r", anchor=ANCHOR, window_end=ANCHOR + end * DAY, target_action="tj-actions/changed-files",
                clock_aligned=aligned, snapshots=snapshots, endpoint_index=len(snapshots) - 1, observed_ancestry_covers_end=True,
                events=events, first_action_groups=[], baselines={str(ANCHOR): dict(complete=True, index=0, references=[])},
                target_measurement=dict(lineages=dict(references=list(references))))


def event(kind, index, days, before, after):
    return dict(event=kind, index=index, timestamp=ANCHOR + days * DAY, commit="c%d" % index, before=before, after=after)


def test_versions_are_classified_against_the_first_patched_release():
    assert version_key("v46.0.1") == (46, 0, 1) and version_key("main") is None
    assert [advisory_line(version_key(v)) for v in ("v46.0.1", "v46.0.0", "v46", "v45", "v45.0.9", "v47.0.5", "main")] == [
        "patched_line", "earlier_line", "patched_line", "earlier_line", "earlier_line", "patched_line", "non_version"]


def test_commits_are_classified_by_every_observed_tag():
    assert sha_line(COMPROMISED, INDEX) == "compromised_commit"
    assert sha_line(NEW, INDEX) == "patched_line_commit" and sha_line(OLD, INDEX) == "earlier_line_commit"
    assert sha_line("c" * 40, INDEX) == "untagged_commit"
    assert sha_line(NEW.upper(), INDEX) == "patched_line_commit"
    assert ref_line(ref("t", "v46", "mutable_or_unresolved"), INDEX) == "patched_line"
    assert ref_line(ref("t", OLD, "full_sha"), INDEX) == "earlier_line_commit"


def test_exposure_and_events_are_binned_around_the_anchor():
    snapshots = [state("s0", -10, other_mutable=3, other_full=1, target_mutable=1),
                 state("s1", 5, other_mutable=2, other_full=2, target_mutable=1)]
    pin = event("pin", 1, 5, ref("actions/checkout", "v4", "mutable_or_unresolved"), ref("actions/checkout", NEW, "full_sha"))
    series = conversion_series(record(snapshots, [pin]))
    assert series["other"][-1]["exposure"] == pytest.approx(3 * 10 / 30)
    assert series["other"][0]["exposure"] == pytest.approx(3 * 5 / 30 + 2 * 25 / 30)
    assert series["other"][0]["pin"] == 1 and series["other"][-1]["pin"] == 0
    assert series["target"][0]["exposure"] == pytest.approx(1 * 5 / 30 + 1 * 25 / 30)


def test_target_events_count_only_for_the_target_and_exclude_existing_pins():
    snapshots = [state("s0", -10, target_mutable=1, other_full=1), state("s1", 3, target_full=1, other_full=1), state("s2", 8, target_mutable=1, other_full=1)]
    target = "tj-actions/changed-files"
    events = [event("pin", 1, 3, ref(target, "v45", "mutable_or_unresolved"), ref(target, OLD, "full_sha")),
              event("sha_target_change", 2, 8, ref(target, OLD, "full_sha"), ref(target, "v46", "mutable_or_unresolved"))]
    series = conversion_series(record(snapshots, events))
    assert series["target"][0]["pin"] == 1 and series["other"][0]["pin"] == 0
    assert series["target"][0]["mutable_update"] == 0 and series["target"][0]["remove"] == 0


def test_transitions_beside_an_incomplete_snapshot_are_not_counted():
    snapshots = [state("s0", -10, other_mutable=2), state("s1", 3, other_mutable=1, other_full=1, complete=False)]
    pin = event("pin", 1, 3, ref("actions/checkout", "v4", "mutable_or_unresolved"), ref("actions/checkout", NEW, "full_sha"))
    series = conversion_series(record(snapshots, [pin]))
    assert series["other"][0]["pin"] == 0
    assert series["other"][0]["exposure"] == pytest.approx(2 * 3 / 30)


def test_clustered_resampling_is_deterministic_and_brackets_the_pooled_ratio():
    ids = [str(i) for i in range(30)]
    clustered = Clustered(ids)
    clustered.add("n", {i: float(k % 3 == 0) for k, i in enumerate(ids)})
    clustered.add("d", {i: 1.0 for i in ids})
    first = clustered.evaluate(lambda s: s["n"] / s["d"], 30)
    again = Clustered(ids)
    again.add("n", {i: float(k % 3 == 0) for k, i in enumerate(ids)})
    again.add("d", {i: 1.0 for i in ids})
    assert first == again.evaluate(lambda s: s["n"] / s["d"], 30)
    assert first["estimate"] == pytest.approx(10 / 30) and first["lower"] <= first["estimate"] <= first["upper"]
    with pytest.raises(ValueError, match="Duplicate"):
        clustered.add("n", {})


def test_interrupted_series_excludes_clock_unaligned_repositories_and_reports_a_pulse():
    def repo(identity, pinned_days, aligned=True):
        snapshots = [state("s0", -150, other_mutable=10), state("s1", pinned_days, other_mutable=5, other_full=5)]
        pin = [event("pin", 1, pinned_days, ref("actions/checkout", "v4", "mutable_or_unresolved"), ref("actions/checkout", NEW, "full_sha"))]
        return record(snapshots, pin * 5, identity=identity, end=400, aligned=aligned)

    records = [repo(str(i), 3) for i in range(12)] + [repo("99", 3, aligned=False)]
    result = interrupted_series(records, {r["repository_id"] for r in records})
    assert result["repositories"] == 12 and result["excluded_clock_unaligned"] == ["99"]
    assert result["periods"]["other"]["post_0_30"]["pin"] == 60 and result["periods"]["other"]["pre"]["pin"] == 0
    assert len(result["placebo"]["other"]) == 6 and result["bin_days"] == 30


def group(action, days, configuration, *, excluded=False, index=1):
    return dict(action=action, timestamp=ANCHOR + days * DAY, configuration=configuration, index=index,
                excluded_copy_or_reintroduction=excluded)


def test_new_identity_series_counts_known_groups_in_pre_and_post_bins():
    item = record([state("s0", -200, other_mutable=1), state("s1", 50, other_full=1)], [])
    item["first_action_groups"] = [
        group("a/x", -40, "all_full_sha"), group("a/y", -35, "all_non_sha"), group("a/z", -35, "unknown"),
        group("a/c", -35, "all_full_sha", excluded=True), group("tj-actions/changed-files", -35, "all_full_sha"),
        group("a/p", 3, "all_full_sha"), group("a/q", 4, "mixed"), group("a/late", 900, "all_full_sha")]
    series = new_identity_series(item)
    assert series[-2] == dict(known=2, fixed=1) and series[-1] == dict(known=0, fixed=0)
    assert series[0] == dict(known=2, fixed=1) and series[1] == dict(known=0, fixed=0)


def test_new_identity_summary_reports_period_shares_and_differences():
    def repo(identity, pre_fixed, post_fixed):
        item = record([state("s0", -200, other_mutable=1), state("s1", 80, other_full=1)], [], identity=identity, end=400)
        item["first_action_groups"] = ([group("a/n%d" % k, -60, "all_full_sha" if k < pre_fixed else "all_non_sha") for k in range(4)]
                                       + [group("a/m%d" % k, 10, "all_full_sha" if k < post_fixed else "all_non_sha") for k in range(4)])
        return item

    included = [repo(str(i), 0, 3) for i in range(12)]
    result = new_identity_summary(included, [r["repository_id"] for r in included])
    assert result["periods"]["pre"]["known"] == 48 and result["periods"]["pre"]["fixed"] == 0
    assert result["periods"]["post_0_30"]["share"]["estimate"] == pytest.approx(0.75)
    assert result["differences"]["post_0_30"]["estimate"] == pytest.approx(0.75)
    assert len(result["bins"]) == len(BINS)


def test_concentration_reports_the_share_of_the_largest_contributors():
    series = {}
    for identity, pins in {"a": 8, "b": 1, "c": 1}.items():
        series[identity] = {"other": {b: dict(exposure=10.0, pin=pins if b == 0 else 1) for b in range(-6, 12)}}
    result = concentration(series)
    assert result["first_month_conversions"] == 10 and result["repositories_with_conversions"] == 3
    assert result["top10_share"] == 1.0 and set(result["leave_out"]) == {"1", "5", "10"}
    assert result["pre_conversions"] == 18 and result["pre_top5_share"] == 1.0
    assert set(result["leave_out_pre"]) == {"1", "5", "10"} and result["leave_out_pre"]["10"] is None


def test_trajectory_states_collapse_form_and_advisory_line():
    refs = lambda lines, full: dict(state="x", lines=lines, references=sum(lines.values()), full_sha=full)
    assert trajectory_state(dict(state="absent")) == "absent"
    assert trajectory_state(dict(state="ancestry_censored")) == "not_observed"
    assert trajectory_state(refs({"patched_line_commit": 2}, 2)) == "pinned_patched"
    assert trajectory_state(refs({"earlier_line_commit": 1, "patched_line_commit": 1}, 2)) == "pinned_mixed_line"
    assert trajectory_state(refs({"earlier_line_commit": 2}, 2)) == "pinned_earlier"
    assert trajectory_state(refs({"patched_line": 1}, 0)) == "mutable_patched"
    assert trajectory_state(refs({"earlier_line": 1}, 0)) == "mutable_earlier"
    assert trajectory_state(refs({"patched_line": 1, "earlier_line": 1}, 1)) == "mixed_form"
    # An untagged or compromised commit cannot be placed on either side of the advisory.
    assert trajectory_state(refs({"untagged_commit": 1}, 1)) == "pinned_unclassified"
    assert trajectory_state(refs({"compromised_commit": 1, "patched_line_commit": 1}, 2)) == "pinned_unclassified"
    assert trajectory_state(refs({"non_version": 1}, 0)) == "mutable_unclassified"


def test_day_zero_uses_the_last_state_before_the_anchor():
    target = "tj-actions/changed-files"
    snapshots = [state("s0", -5, target_mutable=1), state("s1", 0, target_full=1)]
    refs = [dict(ref(target, "v45", "mutable_or_unresolved"), snapshot_sha="s0"), dict(ref(target, OLD, "full_sha"), snapshot_sha="s1")]
    item = record(snapshots, [], references=refs)
    assert trajectory_state(target_lines_at(item, 0, INDEX)) == "mutable_earlier"
    assert trajectory_state(target_lines_at(item, 7, INDEX)) == "pinned_earlier"
    assert target_lines_at(record(snapshots, [], aligned=False), 7, INDEX)["state"] == "clock_unknown"
    assert target_lines_at(item, 365, INDEX)["state"] == "ancestry_censored"


def test_target_reference_rows_must_match_the_snapshot_count():
    item = record([state("s0", -5, target_mutable=2)], [], references=[dict(ref("tj-actions/changed-files", "v45", "mutable_or_unresolved"), snapshot_sha="s0")])
    with pytest.raises(ValueError, match="differ"):
        target_lines_at(item, 7, INDEX)


def test_updater_routes_separate_opportunity_target_later_and_other_releases():
    index = {NEW: {"v4.3.0"}, OLD: {"v4.1.0", "v4"}, "d" * 40: {"v4"}}
    entry = lambda sha: dict(adopted_sha=sha, opportunity_tag="v4.2.0")
    assert adopted_route(entry(NEW), "adopted_target", index) == "opportunity_target"
    assert adopted_route(entry(NEW), "other_sha_change", index) == "later_release"
    assert adopted_route(entry(OLD), "other_sha_change", index) == "other_release"
    assert adopted_route(entry("e" * 40), "other_sha_change", index) == "untagged_commit"
    assert adopted_route(entry("d" * 40), "other_sha_change", index) == "moving_tag_only"
    assert adopted_route(entry(NEW), "right_censored_no_observed_sha_change", index) is None


def test_update_concentration_names_the_dominant_target_per_bin():
    snapshots = [state("s%d" % k, k * 4, other_mutable=3) for k in range(6)]
    mutable = lambda value: ref("actions/checkout", value, "mutable_or_unresolved")
    events = [event("mutable_update", k, k * 4, mutable("v4"), mutable("v5" if k < 4 else "v6")) for k in range(1, 6)]
    events.append(event("mutable_update", 2, 8, mutable("v4"), ref("actions/setup-node", "v4", "mutable_or_unresolved")))
    result = update_concentration([record(snapshots, events, end=40)], {"1"})
    first = next(row for row in result if row["bin"] == 0)
    assert first["updates"] == 6 and first["top_action"] == "actions/checkout" and first["top_ref"] == "v5" and first["top_updates"] == 3
    assert first["top_share"] == pytest.approx(0.5)
    assert next(row for row in result if row["bin"] == -3)["top_share"] is None
    assert update_concentration([record(snapshots, events, end=40)], set()) == [
        dict(bin=b, start_day=b * 30, updates=0, top_action=None, top_ref=None, top_updates=0, top_share=None) for b in BINS]


def test_compromised_pins_report_hours_until_the_reference_changed():
    target = "tj-actions/changed-files"
    snapshots = [state("s%d" % k, k, target_mutable=1) for k in range(4)]
    events = [event("pin", 1, 1, ref(target, "v45", "mutable_or_unresolved"), dict(ref(target, COMPROMISED, "full_sha"), lineage_id="L")),
              event("sha_target_change", 3, 1.5, dict(ref(target, COMPROMISED, "full_sha"), lineage_id="L"), dict(ref(target, NEW, "full_sha"), lineage_id="L"))]
    events[1]["timestamp"] = ANCHOR + int(1.75 * DAY)
    rows = compromised_pins(record(snapshots, events))
    assert len(rows) == 1 and rows[0]["replaced_after_hours"] == pytest.approx(18.0)
    assert compromised_pins(record(snapshots, events[:1]))[0]["replaced_after_hours"] is None
    assert compromised_pins(record(snapshots, [])) == []


def test_pick_release_tag_prefers_the_most_specific_highest_version():
    assert pick_release_tag(["v4", "v4.1.0", "v4.2.0"]) == "v4.2.0"
    assert pick_release_tag(["v4", "main"]) is None
    assert pick_release_tag([]) is None
