from copy import deepcopy
from datetime import datetime, timezone
import hashlib

from pintrace.configuration import enrich_workflow
from pintrace.lineage import reconstruct
from pintrace.measurement import RULES_VERSION as MEASUREMENT_RULES_VERSION
from pintrace.risksets import build_risksets, compact_measurement

TARGET = "tj-actions/changed-files"
SHA, OTHER_SHA = "a" * 40, "b" * 40
ANCHOR = 1741910400
DAY = 86400
STUDY = dict(target_action=TARGET, event_anchor="2025-03-14T00:00:00Z",
             window_end="2026-03-14T23:59:59Z")


def text(*items):
    return "jobs:\n  build:\n    steps:\n" + "".join(
        "      - id: " + identity + "\n        uses: " + action + "@" + ref + "\n"
        for identity, action, ref in items)


def snap(number, items=(), offset=0, complete=True):
    content = text(*items) if items else "jobs:\n  build:\n    steps: []\n"
    parsed = enrich_workflow(content, ".github/workflows/ci.yml")
    clock = ANCHOR + offset * DAY
    return dict(sha=f"commit-{number}", timestamp=clock,
                date=datetime.fromtimestamp(clock, timezone.utc).isoformat(),
                complete=complete, references=parsed["references"])


def pair(identity, snapshots, baseline=0, covers_end=True):
    summary = dict(repository_id=str(identity), repository=f"owner/repo-{identity}",
                   canonical_repository=f"owner/repo-{identity}", baseline_commit=snapshots[baseline]["sha"])
    raw = dict(summary=dict(summary, status="confirmed"), baseline_snapshot=deepcopy(snapshots[baseline]))
    measured = dict(summary=dict(summary, status="measured", observed_ancestry_covers_end=covers_end,
                                 endpoint_commit=snapshots[-1]["sha"], measurement_rules_version=MEASUREMENT_RULES_VERSION),
                    snapshots=snapshots, timeline=dict(endpoint_commit=snapshots[-1]["sha"]),
                    replay={"passed": True}, lineages=reconstruct(summary["canonical_repository"], snapshots))
    return raw, measured


def result(*pairs, flow=None):
    return build_risksets([item[0] for item in pairs], [item[1] for item in pairs if item[1] is not None], STUDY, flow)


def episodes(output, kind=None):
    return [row for row in output["risksets"] if row["row_kind"] == "episode"
            and (kind is None or row["episode_kind"] == kind)]


def comparisons(output, layer):
    return [row for row in output["risksets"] if row["row_kind"] == "comparison" and row["comparison_layer"] == layer]


def test_baseline_target_states_are_repository_counts_and_reference_counts_stay_separate():
    pinned = pair(1, [snap(1, [("a", TARGET, SHA), ("b", TARGET, SHA)], offset=-1)])
    mixed = pair(2, [snap(1, [("a", TARGET, SHA), ("b", TARGET, "v45")], offset=-1)])
    mutable = pair(3, [snap(1, [("a", TARGET, "v45")], offset=-1)])
    output = result(pinned, mixed, mutable)
    assert output["summary"]["baseline_target_repository_states"] == {"all_full_sha": 1, "mixed": 1, "none_full_sha": 1}
    risk = output["summary"]["risksets"]
    assert risk["baseline_fixed_target_reference_episodes"] == 3
    assert risk["baseline_fixed_target_repository_ids"] == 2
    assert all(row["fixed_age_state"] == "left_censored_unknown" for row in episodes(output))
    assert all(row["entry_timestamp"] == ANCHOR for row in episodes(output))


def test_repin_is_another_episode_not_another_first_repository_adoption():
    mutable = [("target", TARGET, "v45")]
    pinned = [("target", TARGET, SHA)]
    raw, measured = pair(1, [snap(1, mutable, -1), snap(2, pinned, 1), snap(3, mutable, 2), snap(4, pinned, 3)])
    output = result((raw, measured))
    found = episodes(output, "post_anchor_observed_pin")
    assert len(found) == 2
    assert [row["pin_ordinal_in_observed_lineage"] for row in found] == [1, 2]
    assert [row["repeated_observed_lineage_pin"] for row in found] == [False, True]
    assert [row["repository_action_pinning_history"] for row in found] == ["first_observed_fixed_commit", "already_observed_fixed_earlier"]
    assert found[0]["exit_kind"] == "unpin"
    assert found[0]["exit_timestamp"] == ANCHOR + 2 * DAY
    assert output["summary"]["risksets"]["post_anchor_pin_repository_action_groups"] == 1


def test_baseline_fixed_unpin_then_repin_is_repeat_even_without_an_earlier_observed_pin_transition():
    raw, measured = pair(1, [snap(1, [("t", TARGET, SHA)], -1),
                              snap(2, [("t", TARGET, "v45")], 1), snap(3, [("t", TARGET, SHA)], 2)])
    output = result((raw, measured))
    episode = episodes(output, "post_anchor_observed_pin")[0]
    assert episode["pin_ordinal_in_observed_lineage"] == 1
    assert episode["repeated_observed_lineage_pin"]
    assert episode["repository_action_pinning_history"] == "already_observed_fixed_earlier"


def test_duplicate_steps_are_reference_episodes_but_one_repository_action_group():
    before = [("a", TARGET, "v45"), ("b", TARGET, "v45")]
    after = [("a", TARGET, SHA), ("b", TARGET, SHA)]
    output = result(pair(1, [snap(1, before, -1), snap(2, after, 1)]))
    risk = output["summary"]["risksets"]
    assert risk["post_anchor_pin_reference_episodes"] == 2
    assert risk["post_anchor_pin_repository_action_groups"] == 1
    assert risk["post_anchor_pin_commit_groups"] == 1


def test_primary_comparator_can_exit_after_entry_without_being_removed_from_pairing():
    before = [("t", TARGET, "v45"), ("a", "actions/checkout", SHA), ("b", "actions/checkout", SHA)]
    entry = [("t", TARGET, SHA), ("a", "actions/checkout", SHA), ("b", "actions/checkout", SHA)]
    after = [("t", TARGET, SHA)]
    output = result(pair(1, [snap(1, before, -1), snap(2, entry, 1), snap(3, after, 2)]))
    rows = comparisons(output, "same_repository_baseline_fixed")
    assert len(rows) == 1
    assert rows[0]["eligible"]
    assert rows[0]["eligible_reference_count"] == 2
    primary = output["summary"]["comparison_layers"]["same_repository_baseline_fixed"]
    assert primary["comparator_repository_action_groups"] == 1
    assert primary["focal_repository_ids"] == 1


def test_primary_comparator_removed_before_entry_has_an_explicit_reason():
    initial = [("t", TARGET, "v45"), ("c", "actions/checkout", SHA)]
    output = result(pair(1, [snap(1, initial, -1), snap(2, [("t", TARGET, "v45")], 1),
                               snap(3, [("t", TARGET, SHA)], 2)]))
    rows = comparisons(output, "same_repository_baseline_fixed")
    assert not rows[0]["eligible"]
    assert rows[0]["eligibility_reason"] == "baseline_reference_not_surviving_at_entry"


def test_primary_comparator_that_unpinned_and_repinned_is_not_continuously_baseline_fixed():
    original = [("t", TARGET, "v45"), ("c", "actions/checkout", SHA)]
    reverted = [("t", TARGET, "v45"), ("c", "actions/checkout", "v4")]
    entry = [("t", TARGET, SHA), ("c", "actions/checkout", SHA)]
    output = result(pair(1, [snap(1, original, -1), snap(2, reverted, 1), snap(3, entry, 2)]))
    primary = comparisons(output, "same_repository_baseline_fixed")[0]
    supplement = comparisons(output, "same_commit_co_pinned_supplement")[0]
    assert not primary["eligible"]
    assert primary["eligibility_reason"] == "baseline_reference_unpinned_before_entry"
    assert supplement["eligible"]


def test_regular_selection_uses_repository_frequencies_not_repeated_step_counts_or_postincident_outcomes():
    many_steps = [(f"c{i}", "actions/checkout", "v4") for i in range(30)]
    first = pair(1, [snap(1, [("t", TARGET, "v45"), *many_steps], -1)])
    second = pair(2, [snap(1, [("t", TARGET, "v45"), ("n", "actions/setup-node", "v4")], -1)])
    third = pair(3, [snap(1, [("t", TARGET, "v45"), ("n", "actions/setup-node", "v4")], -1)])
    output = result(first, second, third)
    selected = output["summary"]["regular_action_selection"]["actions"]
    assert selected[0] == "actions/setup-node"
    frequencies = {row["action"]: row for row in output["upstream_frequencies"]}
    assert frequencies["actions/checkout"]["baseline_repository_count"] == 1
    assert frequencies["actions/checkout"]["baseline_reference_count"] == 30
    altered = deepcopy(first)
    altered[1]["summary"]["status"] = "measurement_failed"
    assert result(altered, second, third)["summary"]["regular_action_selection"]["actions"] == selected


def test_unmeasured_baseline_contributes_frequencies_and_keeps_scope_provisional():
    raw, _ = pair(1, [snap(1, [("t", TARGET, "v45"), ("c", "actions/checkout", SHA)], -1)])
    output = result((raw, None), flow=[{"status": "confirmed", "repository_id": "1"}])
    assert output["summary"]["scope"] == "provisional_available_measurements"
    assert output["summary"]["coverage"]["confirmed_repository_ids"] == 1
    assert output["summary"]["coverage"]["measured_repository_ids"] == 0
    assert output["summary"]["coverage"]["not_yet_usable_repository_ids"] == ["1"]
    assert next(row for row in output["upstream_frequencies"] if row["action"] == "actions/checkout")["opportunity_required"]


def test_unfinished_candidate_flow_prevents_final_scope_even_when_current_measurements_cover_known_ids():
    measured = pair(1, [snap(1, [("t", TARGET, "v45")], -1)])
    output = result(measured, flow=[{"status": "confirmed", "repository_id": "1"}, {"status": "pending"}])
    assert output["summary"]["coverage"]["measured_fraction"] == 1
    assert output["summary"]["scope"] == "provisional_available_measurements"
    complete = result(measured, flow=[{"status": "confirmed", "repository_id": "1"}, {"status": "inaccessible_unknown"}])
    assert complete["summary"]["scope"] == "cohort_complete_descriptive_summary"
    assert not complete["summary"]["frozen_for_formal_models"]


def test_stable_identity_aliases_do_not_double_count_baselines():
    raw, measured = pair(1, [snap(1, [("t", TARGET, SHA)], -1)])
    alias = deepcopy(raw)
    alias["summary"]["repository"] = "previous-owner/renamed"
    output = build_risksets([raw, alias], [measured], STUDY)
    assert output["summary"]["coverage"]["confirmed_repository_ids"] == 1
    assert output["summary"]["baseline_target_repository_states"] == {"all_full_sha": 1}


def test_conflicting_stable_identity_baselines_are_kept_unknown():
    raw, measured = pair(1, [snap(1, [("t", TARGET, SHA)], -1)])
    alias = deepcopy(raw)
    alias["baseline_snapshot"]["sha"] = "different-baseline"
    output = build_risksets([raw, alias], [measured], STUDY)
    assert output["summary"]["coverage"]["baseline_alias_conflict_ids"] == ["1"]
    assert output["summary"]["coverage"]["measured_repository_ids"] == 0
    assert output["risksets"][0]["measurement_status"] == "alias_baseline_conflict"


def test_unknown_boundary_censors_episode_and_does_not_restore_it_from_future_sha():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, [("t", TARGET, SHA)], 1),
                               snap(3, (), 2, complete=False), snap(4, [("t", TARGET, SHA)], 3)]))
    found = episodes(output, "post_anchor_observed_pin")
    assert len(found) == 1
    assert found[0]["exit_kind"] == "unknown_boundary"
    assert found[0]["exit_timestamp"] == ANCHOR + 2 * DAY


def test_first_pin_after_an_earlier_gap_is_not_called_first_adoption():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, (), 1, complete=False),
                               snap(3, [("t", TARGET, "v45")], 2), snap(4, [("t", TARGET, SHA)], 3)]))
    found = episodes(output, "post_anchor_observed_pin")
    assert len(found) == 1
    assert found[0]["earlier_unresolved_history"]
    assert found[0]["repository_action_pinning_history"] == "earlier_history_unknown"


def test_cross_repository_supplement_uses_same_action_and_delayed_entry_not_endpoint_survival():
    focal = pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, [("t", TARGET, SHA)], 1)])
    provider = pair(2, [snap(1, [("t", TARGET, SHA)], -1), snap(2, [("t", TARGET, SHA)], 1), snap(3, (), 2)])
    output = result(focal, provider)
    rows = comparisons(output, "cross_repository_same_action_supplement")
    assert len(rows) == 1
    assert rows[0]["eligible"]
    assert rows[0]["repository_id"] == "2"
    assert rows[0]["action"] == TARGET
    assert output["summary"]["primary_pairing"]["supplement_display_enabled"]


def test_cross_repository_clock_inversion_is_not_silently_aligned_by_sorting_dates():
    focal = pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, [("t", TARGET, SHA)], 3)])
    provider = pair(2, [snap(1, [("t", TARGET, SHA)], -1), snap(2, [("t", TARGET, SHA)], 2), snap(3, [("t", TARGET, SHA)], 1)])
    row = comparisons(result(focal, provider), "cross_repository_same_action_supplement")[0]
    assert not row["eligible"]
    assert row["eligibility_reason"] == "cross_repository_clock_alignment_unknown"


def test_new_sha_target_in_same_pinned_episode_is_not_another_pin_episode():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, [("t", TARGET, SHA)], 1),
                               snap(3, [("t", TARGET, OTHER_SHA)], 2)]))
    assert len(episodes(output, "post_anchor_observed_pin")) == 1
    frequency = next(row for row in output["upstream_frequencies"] if row["action"] == TARGET)
    assert frequency["observed_sha_targets"] == [SHA, OTHER_SHA]


def test_zero_or_unknown_followup_is_not_counted_as_effective_followup():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, [("t", TARGET, SHA)], 1)], covers_end=False))
    episode = episodes(output)[0]
    assert episode["observed_followup_seconds"] == 0
    assert not episode["positive_observed_followup"]
    assert output["summary"]["risksets"]["post_anchor_pin_positive_followup_repository_ids"] == 0


def test_baseline_reference_mismatch_cannot_be_reported_as_full_measurement_coverage():
    raw, measured = pair(1, [snap(1, [("t", TARGET, "v45")], -1)])
    measured["lineages"]["references"][0]["ref"] = "v46"
    output = result((raw, measured))
    assert output["summary"]["coverage"]["measurement_baseline_mismatch_ids"] == ["1"]
    assert output["summary"]["coverage"]["measured_repository_ids"] == 0


def test_all_opportunity_fields_remain_unjoined_and_no_delay_model_is_claimed():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, [("t", TARGET, SHA)], 1)]))
    assert all(row["opportunity_status"] == "pending_join" for row in output["risksets"])
    assert output["summary"]["risksets"]["delay_model_eligible_episodes"] == 0
    assert not episodes(output)[0]["model_eligible"]


def test_missing_confirmed_identity_details_cannot_create_a_false_100_percent_denominator():
    measured = pair(1, [snap(1, [("t", TARGET, "v45")], -1)])
    output = result(measured, flow=[{"status": "confirmed", "repository_id": "1"},
                                   {"status": "confirmed", "repository_id": "2"}])
    assert output["summary"]["coverage"]["measured_fraction"] == 1
    assert not output["summary"]["coverage"]["flow_confirmed_ids_match_details"]
    assert output["summary"]["scope"] == "provisional_available_measurements"


def test_candidate_name_substitution_is_not_accepted_as_complete_flow():
    raw, measured = pair(1, [snap(1, [("t", TARGET, "v45")], -1)])
    flow = [{"repository": "replacement/repository", "repository_id": "1", "status": "confirmed"}]
    output = build_risksets([raw], [measured], STUDY, flow, candidate_names=["owner/repo-1"])
    assert not output["summary"]["coverage"]["candidate_names_match_source"]
    assert output["summary"]["scope"] == "provisional_available_measurements"


def test_old_measurement_rule_files_are_not_mixed_into_the_active_batch():
    raw, measured = pair(1, [snap(1, [("t", TARGET, "v45")], -1)])
    measured["summary"]["measurement_rules_version"] = "measurement-obsolete"
    output = result((raw, measured))
    assert output["summary"]["coverage"]["measured_repository_ids"] == 0
    assert output["risksets"][0]["measurement_status"] == "measurement_rules_version_mismatch"


def test_unchanged_duplicate_workflow_during_configuration_only_commits_does_not_censor_episodes():
    content = "jobs:\n  build:\n    steps:\n      - uses: " + TARGET + "@" + SHA + "\n      - uses: " + TARGET + "@" + SHA + "\n"
    data = content.encode()
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    parsed = enrich_workflow(content, ".github/workflows/ci.yml")
    states = [dict(snap(number, offset=offset), references=deepcopy(parsed["references"]),
                   files=[dict(path=".github/workflows/ci.yml", blob=blob, mode="100644", kind="blob")])
              for number, offset in ((1, -1), (2, 1), (3, 2))]
    output = result(pair(1, states))
    found = episodes(output, "baseline_fixed")
    assert len(found) == 2
    assert all(row["exit_kind"] == "study_window_censored" for row in found)
    assert output["summary"]["risksets"]["baseline_exit_kind_counts"] == {"study_window_censored": 2}


def test_compact_analysis_view_is_exactly_equivalent_to_full_measurement_for_risksets():
    initial = [("t", TARGET, "v45"), ("c", "actions/checkout", SHA)]
    pinned = [("t", TARGET, SHA), ("c", "actions/checkout", SHA)]
    raw, measured = pair(1, [snap(1, initial, -1), snap(2, pinned, 1), snap(3, (), 2, complete=False)])
    assert build_risksets([raw], [measured], STUDY) == build_risksets([raw], [compact_measurement(measured)], STUDY)


def test_retained_tag_plus_new_sha_is_born_fixed_without_a_pin_transition():
    initial = [("t", TARGET, "v45"), ("c", "actions/checkout", SHA)]
    added = [*initial, ("new", TARGET, SHA)]
    output = result(pair(1, [snap(1, initial, -1), snap(2, added, 1)]))
    assert episodes(output, "post_anchor_observed_pin") == []
    born = episodes(output, "post_anchor_born_fixed")
    assert len(born) == 1
    assert born[0]["source_entry_event"] == born[0]["entry_mode"] == "copied_reference"
    assert born[0]["entry_time_basis"] == "observed_fixed_entry_commit"
    assert born[0]["pin_ordinal_in_observed_lineage"] == 0
    assert not born[0]["repeated_observed_lineage_pin"]
    repository = next(row for row in output["risksets"] if row["row_kind"] == "repository")
    assert repository["first_observed_target_full_sha_status"] == "observed_post_anchor"
    assert repository["first_observed_target_full_sha_source_modes"] == ["copied_reference"]
    assert output["summary"]["repository_first_observed_target_full_sha"]["observed_repository_ids"] == 1
    assert output["summary"]["risksets"]["post_anchor_pin_reference_episodes"] == 0
    assert output["summary"]["comparison_layers"]["same_repository_baseline_fixed"]["pair_rows"] == 0
    comparison = comparisons(output, "same_repository_baseline_fixed")[0]
    assert comparison["focal_episode_kind"] == "post_anchor_born_fixed"
    assert comparison["eligible"]


def test_true_pin_remains_main_cohort_and_first_observed_source_is_pin():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1),
                             snap(2, [("t", TARGET, SHA)], 1)]))
    assert len(episodes(output, "post_anchor_observed_pin")) == 1
    assert episodes(output, "post_anchor_born_fixed") == []
    repository = next(row for row in output["risksets"] if row["row_kind"] == "repository")
    assert repository["first_observed_target_full_sha_source_modes"] == ["pin"]


def test_baseline_sha_then_copy_is_born_fixed_but_not_repository_first_observed_sha():
    output = result(pair(1, [snap(1, [("t", TARGET, SHA)], -1),
                             snap(2, [("t", TARGET, SHA), ("n", TARGET, SHA)], 1)]))
    born = episodes(output, "post_anchor_born_fixed")
    assert len(born) == 1
    assert born[0]["repository_action_pinning_history"] == "already_observed_fixed_earlier"
    assert output["summary"]["repository_first_observed_target_full_sha"]["observed_repository_ids"] == 0
    repository = next(row for row in output["risksets"] if row["row_kind"] == "repository")
    assert repository["first_observed_target_full_sha_status"] == "baseline_already_fixed"


def test_reintroduced_and_replacement_sha_are_distinct_known_fixed_entries():
    reintroduced = pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, (), 1),
                            snap(3, [("new", TARGET, SHA)], 2)])
    replacement = pair(2, [snap(1, [("t", TARGET, "v45"), ("swap", "actions/checkout", "v4")], -1),
                           snap(2, [("t", TARGET, "v45"), ("swap", TARGET, SHA)], 1)])
    output = result(reintroduced, replacement)
    assert {row["source_entry_event"] for row in episodes(output, "post_anchor_born_fixed")} == {"reintroduced_action", "replace"}
    assert episodes(output, "post_anchor_observed_pin") == []
    assert output["summary"]["repository_first_observed_target_full_sha"]["source_mode_repository_counts"] == {"reintroduced_action": 1, "replace": 1}


def test_unknown_entry_after_gap_is_diagnostic_without_a_born_fixed_episode():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1), snap(2, (), 1, complete=False),
                             snap(3, [("t", TARGET, SHA)], 2)]))
    assert episodes(output) == []
    diagnostic = [row for row in output["risksets"] if row["row_kind"] == "fixed_entry_diagnostic"]
    assert len(diagnostic) == 1
    assert diagnostic[0]["entry_mode_state"] == "unknown"
    assert diagnostic[0]["source_entry_event"] == "unknown_boundary"
    assert "episode_id" not in diagnostic[0]
    first = output["summary"]["repository_first_observed_target_full_sha"]
    assert first["observed_repository_ids"] == first["earlier_history_unknown_repository_ids"] == 1
    assert first["source_mode_repository_counts"] == {"unknown_boundary": 1}


def test_multiple_copies_in_one_commit_are_distinct_episodes_but_one_repository_observation():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1),
                             snap(2, [("t", TARGET, "v45"), ("a", TARGET, SHA), ("b", TARGET, SHA)], 1)]))
    assert len(episodes(output, "post_anchor_born_fixed")) == 2
    assert output["summary"]["risksets"]["post_anchor_born_fixed_commit_groups"] == 1
    first = output["summary"]["repository_first_observed_target_full_sha"]
    assert first["observed_repository_ids"] == 1
    assert first["source_mode_repository_counts"] == {"copied_reference": 1}


def test_born_fixed_uses_entry_configuration_has_zero_age_and_stops_at_unpin():
    initial = [("t", TARGET, "v45")]
    entered = [*initial, ("n", TARGET, SHA)]
    states = [snap(1, initial, -1), snap(2, entered, 1),
              snap(3, [*initial, ("n", TARGET, "v46")], 3)]
    states[1]["references"][1]["configuration_coverage"] = {"status": "covered"}
    output = result(pair(1, states))
    born = episodes(output, "post_anchor_born_fixed")[0]
    assert born["fixed_age_state"] == "observed_within_window"
    assert born["fixed_age_seconds"] == 0
    assert born["entry_configuration_coverage"] == "covered"
    assert born["exit_kind"] == "unpin"
    assert born["observed_followup_seconds"] == 2 * DAY


def test_first_observed_commit_preserves_pin_and_copy_source_mode_set():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1),
                             snap(2, [("t", TARGET, SHA), ("n", TARGET, SHA)], 1)]))
    assert len(episodes(output, "post_anchor_observed_pin")) == 1
    assert len(episodes(output, "post_anchor_born_fixed")) == 1
    first = output["summary"]["repository_first_observed_target_full_sha"]
    assert first["observed_repository_ids"] == 1
    assert first["source_mode_repository_counts"] == {"copied_reference": 1, "pin": 1}
    assert first["source_mode_set_repository_counts"] == {"copied_reference+pin": 1}


def test_ambiguous_sha_entry_is_only_a_diagnostic_and_first_observed_mode():
    states = [snap(1, [("a", TARGET, "v45"), ("b", TARGET, "v45")], -1),
              snap(2, [("a", TARGET, SHA)], 1)]
    for state in states:
        for ref in state["references"]:
            ref.update(step_id=None, step_context=None)
    output = result(pair(1, states))
    assert episodes(output) == []
    diagnostic = next(row for row in output["risksets"] if row["row_kind"] == "fixed_entry_diagnostic")
    assert diagnostic["source_entry_event"] == "ambiguous_transition"
    assert output["summary"]["fixed_entry_diagnostics"] == dict(reference_rows=1, repository_ids=1,
        source_event_counts={"ambiguous_transition": 1})
    first = output["summary"]["repository_first_observed_target_full_sha"]
    assert first["source_mode_repository_counts"] == {"ambiguous_transition": 1}
    assert first["earlier_history_unknown_repository_ids"] == 1


def test_born_fixed_all_comparison_layers_are_separate_from_main_pin_comparisons():
    focal = pair(1, [snap(1, [("t", TARGET, "v45"), ("p", "actions/setup-python", SHA),
                              ("c", "actions/checkout", "v4")], -1),
                     snap(2, [("t", TARGET, "v45"), ("p", "actions/setup-python", SHA),
                              ("c", "actions/checkout", SHA), ("n", TARGET, SHA)], 1)])
    provider = pair(2, [snap(1, [("t", TARGET, SHA)], -1), snap(2, (), 2)])
    output = result(focal, provider)
    layers = output["summary"]["comparison_layers_by_episode_kind"]["post_anchor_born_fixed"]
    assert all(layer["pair_rows"] == 1 for layer in layers.values())
    assert all(layer["pair_rows"] == 0 for layer in output["summary"]["comparison_layers"].values())
    assert all(row["focal_episode_kind"] == "post_anchor_born_fixed"
               for row in output["risksets"] if row["row_kind"] == "comparison")


def test_born_fixed_unknown_boundary_ends_episode_without_restoring_future_sha():
    before = [("t", TARGET, "v45")]
    fixed = [*before, ("n", TARGET, SHA)]
    output = result(pair(1, [snap(1, before, -1), snap(2, fixed, 1),
                             snap(3, (), 2, complete=False), snap(4, fixed, 3)]))
    born = episodes(output, "post_anchor_born_fixed")
    assert len(born) == 1
    assert born[0]["exit_kind"] == "unknown_boundary"
    assert born[0]["observed_followup_seconds"] == DAY
    assert output["summary"]["fixed_entry_diagnostics"]["reference_rows"] == 1


def test_known_local_entry_after_earlier_gap_retains_unknown_history():
    mutable = [("t", TARGET, "v45")]
    output = result(pair(1, [snap(1, mutable, -1), snap(2, (), 1, complete=False),
                             snap(3, mutable, 2), snap(4, [*mutable, ("n", TARGET, SHA)], 3)]))
    born = episodes(output, "post_anchor_born_fixed")[0]
    assert born["entry_mode_state"] == "known_local_entry"
    assert born["earlier_unresolved_history"]
    assert born["repository_action_pinning_history"] == "earlier_history_unknown"
    assert born["fixed_age_seconds"] == 0
    assert output["summary"]["repository_first_observed_target_full_sha"]["earlier_history_unknown_repository_ids"] == 1


def test_missing_measurement_is_unknown_in_repository_first_observed_denominator():
    raw, _ = pair(1, [snap(1, [("t", TARGET, "v45")], -1)])
    output = result((raw, None))
    first = output["summary"]["repository_first_observed_target_full_sha"]
    assert first["baseline_none_confirmed_repository_ids"] == 1
    assert first["baseline_none_usable_repository_ids"] == first["observed_repository_ids"] == first["not_observed_repository_ids"] == 0
    assert first["measurement_unknown_repository_ids"] == 1


def test_compact_view_preserves_born_fixed_and_first_observed_classification():
    raw, measured = pair(1, [snap(1, [("t", TARGET, "v45")], -1),
                             snap(2, [("t", TARGET, "v45"), ("n", TARGET, SHA)], 1)])
    assert build_risksets([raw], [measured], STUDY) == build_risksets([raw], [compact_measurement(measured)], STUDY)


def test_not_observed_sha_reports_unknown_snapshots_without_claiming_no_adoption():
    output = result(pair(1, [snap(1, [("t", TARGET, "v45")], -1),
                             snap(2, (), 1, complete=False)], covers_end=False))
    repository = next(row for row in output["risksets"] if row["row_kind"] == "repository")
    assert repository["first_observed_target_full_sha_status"] == "not_observed_in_complete_post_anchor_snapshots"
    assert repository["post_anchor_complete_snapshot_count"] == 0
    assert repository["post_anchor_incomplete_snapshot_count"] == 1
    assert repository["first_observed_target_full_sha_history_unknown"]
    assert output["summary"]["repository_first_observed_target_full_sha"]["not_observed_with_unknown_snapshot_repository_ids"] == 1


def test_not_observed_sha_distinguishes_no_followup_from_unchanged_ancestry_coverage():
    states = [snap(1, [("t", TARGET, "v45")], -1)]
    no_followup = result(pair(1, states, covers_end=False))
    covered = result(pair(2, states, covers_end=True))
    unknown_row = next(row for row in no_followup["risksets"] if row["row_kind"] == "repository")
    covered_row = next(row for row in covered["risksets"] if row["row_kind"] == "repository")
    assert unknown_row["post_anchor_observed_followup_seconds"] is None
    assert covered_row["post_anchor_observed_followup_seconds"] > 0
    assert no_followup["summary"]["repository_first_observed_target_full_sha"]["not_observed_without_positive_followup_repository_ids"] == 1
    assert covered["summary"]["repository_first_observed_target_full_sha"]["not_observed_without_positive_followup_repository_ids"] == 0
