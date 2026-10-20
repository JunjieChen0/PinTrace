from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json

import pytest

import pintrace.opportunity_join as opportunity_join
from pintrace.configuration import enrich_workflow
from pintrace.lineage import reconstruct
from pintrace.opportunity_join import RISKSET_RULES_VERSION, _sources, build_join, comment_identity, measurement_paths_from_manifest, render_report
from pintrace.measurement import BATCH_DIRECTORY, RULES_VERSION as MEASUREMENT_RULES_VERSION
from pintrace.risksets import build_risksets, compact_measurement

TARGET = "owner/action"
SHA, NEXT_SHA = "a" * 40, "b" * 40
ANCHOR, DAY = 1741910400, 86400
STUDY = dict(target_action=TARGET, window_start="2024-09-14T00:00:00Z",
             event_anchor="2025-03-14T00:00:00Z", window_end="2026-03-14T23:59:59Z")


def iso(offset):
    return datetime.fromtimestamp(ANCHOR + DAY * offset, timezone.utc).isoformat()


def snapshot(number, offset, sha=SHA, comment="v4.1.0", complete=True):
    text = "jobs:\n  test:\n    steps:\n      - id: action\n        uses: " + TARGET + "@" + sha
    text += " # " + comment if comment else ""
    parsed = enrich_workflow(text + "\n", ".github/workflows/ci.yml")
    return dict(sha=f"c{number}", timestamp=ANCHOR + DAY * offset, date=iso(offset),
                complete=complete, references=parsed["references"])


def release(tag, sha, offset):
    return dict(repository=TARGET, tag=tag, target=sha, evidence_type="immutable_release",
                immutable=True, published_at=iso(offset), source_url=f"https://example.org/releases/{tag}", evidence_grade="A")


def source_a(new_tag="v4.2.0"):
    old = release("v4.1.0", SHA, -2)
    new = release(new_tag, NEXT_SHA, 2)
    return dict(summary={"repository": TARGET, "errors": []}, observations=[],
                release_records=[old, new], opportunities=[dict(new, available_at=new["published_at"])])


def source_b():
    lower = dict(repository=TARGET, tag="v4", target=SHA, evidence_type="archive_snapshot",
                 evidence_grade="B", visit_date=iso(1), snapshot="c" * 40, source_url="https://example.org/lower")
    upper = dict(lower, target=NEXT_SHA, visit_date=iso(2), snapshot="d" * 40, source_url="https://example.org/upper")
    opportunity = dict(repository=TARGET, tag="v4", target=NEXT_SHA, previous_target=SHA,
        evidence_type="observed_binding_change", evidence_grade="B", available_after=iso(1), available_by=iso(2),
        lower_snapshot=lower["snapshot"], upper_snapshot=upper["snapshot"], lower_url=lower["source_url"], upper_url=upper["source_url"])
    return dict(summary={"repository": TARGET, "errors": []}, observations=[lower, upper],
                release_records=[], opportunities=[opportunity])


def source_new_tag(kind="observed_tag_introduction"):
    source = source_b()
    lower_bindings = {"v4": SHA, "v4.1.0": SHA}
    upper_bindings = {"v4": NEXT_SHA, "v4.1.0": SHA, "v4.2.0": NEXT_SHA}
    source["snapshot_visits"] = [
        dict(snapshot="c" * 40, complete=True, tag_listing_complete=True,
             bindings=lower_bindings, tag_names=list(lower_bindings)),
        dict(snapshot="d" * 40, complete=True, tag_listing_complete=True,
             bindings=upper_bindings, tag_names=list(upper_bindings))]
    source["opportunities"] = [dict(repository=TARGET, tag="v4.2.0", target=NEXT_SHA,
        evidence_type=kind, evidence_grade="B", available_after=iso(1), available_by=iso(2),
        lower_snapshot="c" * 40, upper_snapshot="d" * 40,
        lower_url="https://example.org/lower", upper_url="https://example.org/upper",
        lower_complete=True, upper_complete=True,
        lower_snapshot_tags_complete=True, upper_snapshot_tags_complete=True,
        tag_aliases=["v4.2.0", "4.2.0"], tag_alias_group="4.2.0")]
    return source


def measurements(states):
    summary = dict(repository_id="1", repository="downstream/repo", canonical_repository="downstream/repo",
                   baseline_commit=states[0]["sha"], status="measured", observed_ancestry_covers_end=True,
                   endpoint_commit=states[-1]["sha"], measurement_rules_version=MEASUREMENT_RULES_VERSION)
    detail = dict(summary=summary, snapshots=states, timeline={"endpoint_commit": states[-1]["sha"]},
                  replay={"passed": True}, lineages=reconstruct("downstream/repo", states))
    baseline = dict(summary=dict(summary, status="confirmed"), baseline_snapshot=deepcopy(states[0]))
    return baseline, detail


def run(states, source):
    baseline, detail = measurements(states)
    risk = build_risksets([baseline], [detail], STUDY)
    result = build_join([detail], [source], risk["risksets"], risk["upstream_frequencies"], STUDY, [baseline])
    return result


def test_literal_immutable_tag_identity_is_consistent_or_conflicting_only_after_publication():
    source = _sources([source_a()])[TARGET]
    ref = snapshot(1, -1)["references"][0]
    assert comment_identity(ref, ANCHOR - DAY, source)["comment_identity_state"] == "verified_consistent"
    wrong = dict(ref, ref="e" * 40)
    assert comment_identity(wrong, ANCHOR - DAY, source)["comment_identity_state"] == "verified_conflict"
    assert comment_identity(ref, ANCHOR - 3 * DAY, source)["comment_identity_state"] == "unverified"


def test_major_comment_is_not_replaced_by_an_immutable_full_semver_tag():
    source = _sources([source_a()])[TARGET]
    major = snapshot(1, -1, comment="v4")["references"][0]
    assert comment_identity(major, ANCHOR - DAY, source)["comment_identity_state"] == "unverified"


def test_literal_tag_archive_bindings_remain_unverified_and_expose_discrete_scope():
    source = _sources([source_b()])[TARGET]
    ref = snapshot(1, 1.5, comment="v4")["references"][0]
    result = comment_identity(ref, ANCHOR + 1.5 * DAY, source)
    assert result["comment_identity_state"] == "unverified"
    assert result["archive_evidence_scope"] == "discrete_visits_only_not_persistent_identity"
    assert result["archive_before"]["target"] == SHA
    assert result["archive_after"]["target"] == NEXT_SHA


def test_real_A_opportunity_requires_an_existing_verified_same_major_reference_and_gives_exact_delay():
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source_a())
    match = result["opportunity_matches"][0]
    assert match["applicable"]
    assert match["outcome"] == "adopted_target"
    assert match["update_delay_days"] == {"lower_days": 1, "upper_days": 1}
    assert match["target_delay_model_candidate"]
    assert result["summary"]["target_rq3_episode_candidates"] == 1


def test_A_identity_does_not_infer_a_bare_sha_update_channel():
    result = run([snapshot(1, -1, comment=None), snapshot(2, 3, NEXT_SHA, None)], source_a())
    assert result["upstream_coverage"][0]["historical_binding_best_grade"] == "A"
    assert not result["opportunity_matches"][0]["applicable"]
    assert result["summary"]["upstream_presence_proxy_full_sha_references"] == 1
    assert result["summary"]["applicable_opportunity_reference_pairs"] == 0


def test_different_major_release_is_not_a_same_channel_opportunity():
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v5.0.0")], source_a(new_tag="v5.0.0"))
    assert not result["opportunity_matches"][0]["applicable"]
    assert result["opportunity_matches"][0]["applicability_reason"] == "not_a_later_release_in_verified_same_major_channel"


def test_B_exact_same_tag_and_lower_binding_with_update_after_upper_gives_interval_delay():
    result = run([snapshot(1, -1, comment="v4"), snapshot(2, 3, NEXT_SHA, "v4")], source_b())
    match = result["opportunity_matches"][0]
    assert match["applicable"]
    assert match["comment_identity_state"] == "unverified"
    assert match["update_delay_days"] == {"lower_days": 1, "upper_days": 2}
    assert match["archive_reversion_sensitivity_required"]


def test_B_update_inside_unknown_availability_interval_does_not_invent_nonnegative_latency():
    result = run([snapshot(1, -1, comment="v4"), snapshot(2, 1.5, NEXT_SHA, "v4")], source_b())
    match = result["opportunity_matches"][0]
    assert not match["applicable"]
    assert match["update_delay_days"] is None
    assert match["applicability_reason"] == "downstream_changed_or_censored_before_upper_binding_bound"


def test_B_major_tag_opportunity_does_not_substitute_a_full_version_comment():
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source_b())
    assert not result["opportunity_matches"][0]["applicable"]


def test_B_unknown_gap_before_upper_bound_cannot_supply_an_applicable_opportunity():
    result = run([snapshot(1, -1, comment="v4"), snapshot(2, 1.5, comment="v4", complete=False),
                  snapshot(3, 3, NEXT_SHA, "v4")], source_b())
    match = result["opportunity_matches"][0]
    assert not match["applicable"]
    assert match["outcome"] == "unknown_boundary"
    assert match["rq3_target_episode_ids"] == []


def test_C_current_release_timestamp_and_tag_do_not_create_a_historical_opportunity():
    source = source_a()
    for record in source["release_records"] + source["opportunities"]:
        record.update(immutable=False, evidence_type="current_release_listing")
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["opportunity_matches"] == []
    assert result["upstream_coverage"][0]["historical_binding_best_grade"] == "C"


def test_any_B_binding_is_only_a_proxy_when_no_change_opportunity_exists():
    source = source_b()
    source["opportunities"] = []
    result = run([snapshot(1, -1, comment="v4"), snapshot(2, 3, comment="v4")], source)
    assert result["summary"]["upstream_presence_proxy_full_sha_references"] == 1
    assert result["summary"]["applicable_reference_lineages"] == 0


def test_clock_inversion_keeps_comment_evidence_but_blocks_calendar_opportunity_join():
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0"), snapshot(3, 2.5, NEXT_SHA, "v4.2.0")], source_a())
    assert result["summary"]["clock_unalignable_repository_ids"] == ["1"]
    assert result["summary"]["full_sha_reference_observations"] == 3
    assert not result["opportunity_matches"]


def test_no_observed_sha_change_is_censoring_and_never_labelled_maintenance_failure():
    result = run([snapshot(1, -1), snapshot(2, 4)], source_a())
    match = result["opportunity_matches"][0]
    assert match["applicable"]
    assert match["outcome"] == "right_censored_no_observed_sha_change"
    assert match["update_delay_days"] is None
    assert not result["summary"]["freeze_ready"]


def test_available_time_must_be_the_same_A_binding_timestamp_not_an_unrelated_date():
    source = source_a()
    source["opportunities"][0]["available_at"] = iso(1)
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["opportunity_matches"] == []


def test_join_uses_exact_exported_measurement_set_and_ignores_newly_arriving_files(tmp_path):
    directory = tmp_path / "data/derived" / BATCH_DIRECTORY
    directory.mkdir(parents=True)
    included = directory / "1.json"
    included.write_text("{}", encoding="utf-8")
    (directory / "2.json").write_text("{}", encoding="utf-8")
    relative = included.relative_to(tmp_path).as_posix()
    manifest = dict(rules_version=RISKSET_RULES_VERSION,
                    measurement_batch_directory=BATCH_DIRECTORY, measurement_rules_version=MEASUREMENT_RULES_VERSION,
                    source_sha256={relative: hashlib.sha256(included.read_bytes()).hexdigest()})
    assert measurement_paths_from_manifest(tmp_path, manifest) == [included]
    included.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="changed or missing"):
        measurement_paths_from_manifest(tmp_path, manifest)


def test_join_rejects_a_riskset_manifest_from_another_measurement_batch(tmp_path):
    manifest = dict(measurement_batch_directory="measurement-obsolete", measurement_rules_version=MEASUREMENT_RULES_VERSION, source_sha256={})
    with pytest.raises(ValueError, match="another measurement batch"):
        measurement_paths_from_manifest(tmp_path, manifest)


def test_join_rejects_old_analysis_rules_even_with_current_measurement_inputs(tmp_path):
    manifest = dict(rules_version="risksets-v3", measurement_batch_directory=BATCH_DIRECTORY,
                    measurement_rules_version=MEASUREMENT_RULES_VERSION, source_sha256={})
    with pytest.raises(ValueError, match="analysis rules version"):
        measurement_paths_from_manifest(tmp_path, manifest)


@pytest.mark.parametrize("comment", [None, "v4", "v4.1.0"])
@pytest.mark.parametrize("kind", ["observed_tag_introduction", "observed_tag_reintroduction"])
def test_new_or_reappearing_tag_can_be_an_opportunity_from_complete_lower_code_identity(comment, kind):
    result = run([snapshot(1, -1, comment=comment), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source_new_tag(kind))
    match = result["opportunity_matches"][0]
    assert match["applicable"]
    assert match["archived_lower_code_version"] == (4, 1, 0)
    assert match["update_delay_days"] == {"lower_days": 1, "upper_days": 2}
    assert match["evidence_scope"] == "observed_tag_availability_not_first_ever_publication"
    assert match["comment_identity_state"] in {"none", "unverified"}
    assert match["target_delay_model_candidate"]
    assert len(result["opportunity_matches"]) == 1  # aliases describe one opportunity


def test_incomplete_lower_tag_listing_cannot_prove_new_tag_absence():
    source = source_new_tag()
    source["snapshot_visits"][0]["tag_listing_complete"] = False
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert not result["opportunity_matches"][0]["applicable"]
    assert result["opportunity_matches"][0]["applicability_reason"] == "complete_snapshot_bindings_not_available"


def test_new_tag_record_without_complete_visit_flags_is_not_graded_as_a_B_opportunity():
    source = source_new_tag()
    source["opportunities"][0]["lower_complete"] = False
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["opportunity_matches"] == []


def test_multiple_full_version_bindings_for_bare_sha_keep_channel_identity_unknown():
    source = source_new_tag()
    lower = source["snapshot_visits"][0]
    lower["bindings"]["v4.1.1"] = SHA
    lower["tag_names"].append("v4.1.1")
    result = run([snapshot(1, -1, comment=None), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    match = result["opportunity_matches"][0]
    assert not match["applicable"]
    assert match["applicability_reason"] == "multiple_or_no_archived_full_versions_for_old_sha"


def test_explicit_full_tag_can_resolve_an_alias_version_collision_at_lower_visit_without_verifying_persistent_comment_identity():
    source = source_new_tag()
    source["snapshot_visits"][0]["bindings"]["v4.1.1"] = SHA
    source["snapshot_visits"][0]["tag_names"].append("v4.1.1")
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    match = result["opportunity_matches"][0]
    assert match["applicable"]
    assert match["comment_identity_state"] == "unverified"
    assert match["archived_lower_identity_tags"] == ["v4.1.0"]


def test_cross_major_sha_alias_collision_is_unknown_without_a_literal_full_tag_hint():
    source = source_new_tag()
    lower = source["snapshot_visits"][0]
    lower["bindings"]["v5.0.0"] = SHA
    lower["tag_names"].append("v5.0.0")
    result = run([snapshot(1, -1, comment=None), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["opportunity_matches"][0]["applicability_reason"] == "multiple_archived_major_channels_for_old_sha"


def test_unresolved_numeric_tag_targets_prevent_unique_channel_inference_for_bare_sha():
    source = source_new_tag()
    source["snapshot_visits"][0]["tag_names"].append("v5.0.0")
    result = run([snapshot(1, -1, comment=None), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["opportunity_matches"][0]["applicability_reason"] == "numeric_tag_targets_unresolved_at_lower_visit"


def test_adding_a_comment_to_unchanged_sha_inside_availability_interval_does_not_censor_the_opportunity():
    result = run([snapshot(1, -1, comment=None), snapshot(2, 1.5, comment="v4.1.0"),
                  snapshot(3, 3, NEXT_SHA, "v4.2.0")], source_new_tag())
    match = result["opportunity_matches"][0]
    assert match["applicable"]
    assert match["outcome"] == "adopted_target"
    assert match["update_delay_days"] == {"lower_days": 1, "upper_days": 2}


def test_no_new_version_opportunity_is_created_when_a_numeric_version_target_was_already_available_under_an_alias():
    source = source_new_tag()
    lower = source["snapshot_visits"][0]
    lower["bindings"]["4.2.0"] = NEXT_SHA
    lower["tag_names"].append("4.2.0")
    # Keep only the newly added literal alias in this synthetic bad opportunity.
    source["opportunities"][0]["tag_aliases"] = ["v4.2.0"]
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["opportunity_matches"][0]["applicability_reason"] == "numeric_version_target_already_available_at_lower_visit"


def test_compact_analysis_view_is_exactly_equivalent_to_full_measurement_for_join():
    baseline, measured = measurements([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")])
    risk = build_risksets([baseline], [measured], STUDY)
    full = build_join([measured], [source_new_tag()], risk["risksets"], risk["upstream_frequencies"], STUDY, [baseline])
    projected = build_join([compact_measurement(measured)], [source_new_tag()], risk["risksets"], risk["upstream_frequencies"], STUDY, [baseline])
    assert full == projected


def test_same_update_with_A_release_and_B_archive_evidence_is_one_selected_opportunity_and_adoption():
    source = source_new_tag()
    exact = source_a()
    source["release_records"] = exact["release_records"]
    source["opportunities"].extend(exact["opportunities"])
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["summary"]["applicable_opportunity_reference_pairs"] == 2
    assert result["summary"]["selected_opportunity_reference_pairs"] == 1
    selected = [row for row in result["opportunity_matches"] if row["selected_for_delay_analysis"]]
    assert len(selected) == 1
    assert selected[0]["evidence_grade"] == "A"
    assert result["summary"]["target_rq3_opportunity_pairs"] == 1
    assert result["summary"]["actual_target_sha_adoptions"] == 1


def test_comment_only_snapshot_between_evidence_dates_does_not_create_a_second_target_segment():
    source = source_new_tag()
    exact = source_a()
    exact["release_records"][1]["published_at"] = iso(1.5)
    exact["opportunities"][0]["published_at"] = iso(1.5)
    exact["opportunities"][0]["available_at"] = iso(1.5)
    source["release_records"] = exact["release_records"]
    source["opportunities"].extend(exact["opportunities"])
    result = run([snapshot(1, -1), snapshot(2, 1.75), snapshot(3, 3, NEXT_SHA, "v4.2.0")], source)
    assert result["summary"]["applicable_opportunity_reference_pairs"] == 2
    assert result["summary"]["selected_opportunity_reference_pairs"] == 1
    assert len({row["reference_target_segment_start_commit"] for row in result["opportunity_matches"]}) == 1


def test_reference_evidence_preserves_actual_per_snapshot_locator_fields():
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source_a())
    first = result["reference_evidence"][0]
    assert first["snapshot_sha"] == "c1"
    assert first["path"] == ".github/workflows/ci.yml"
    assert first["job"] == "test"
    assert first["step_id"] == "action"
    assert first["step_index"] == 0
    assert first["line"] == 5
    assert first["ref"] == SHA
    assert result["summary"]["reference_locator_unique"]


def test_missing_lineage_ids_in_partial_snapshot_are_still_uniquely_identified_by_source_occurrence():
    baseline, measured = measurements([snapshot(1, -1), snapshot(2, 3, complete=False)])
    partial = measured["lineages"]["references"][-1]
    assert partial["lineage_id"] is None
    second = deepcopy(partial)
    second["step_index"] = 1
    measured["lineages"]["references"].append(second)
    risk = build_risksets([baseline], [measured], STUDY)
    joined = build_join([measured], [source_a()], risk["risksets"], risk["upstream_frequencies"], STUDY, [baseline])
    rows = [row for row in joined["reference_evidence"] if row["snapshot_sha"] == "c2"]
    assert len(rows) == 2
    assert len({row["reference_observation_key"] for row in rows}) == 2


def test_duplicate_source_locator_is_a_gate_failure_instead_of_silent_lineage_none_match():
    baseline, measured = measurements([snapshot(1, -1)])
    measured["lineages"]["references"].append(deepcopy(measured["lineages"]["references"][0]))
    risk = build_risksets([baseline], [measured], STUDY)
    # Baseline-mismatch is already a separate gate. Supply the valid riskset
    # row explicitly so this test exercises the source-locator check itself.
    risk["risksets"][0]["measurement_status"] = "usable"
    with pytest.raises(ValueError, match="Nonunique per-snapshot"):
        build_join([measured], [source_a()], risk["risksets"], risk["upstream_frequencies"], STUDY, [baseline])


def test_upper_version_aliases_with_different_targets_cannot_be_arbitrarily_selected():
    source = source_new_tag()
    upper = source["snapshot_visits"][1]
    upper["bindings"]["4.2.0"] = "e" * 40
    upper["tag_names"].append("4.2.0")
    source["opportunities"][0]["upper_version_target_conflict"] = True
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert not result["opportunity_matches"][0]["applicable"]
    assert result["opportunity_matches"][0]["applicability_reason"] == "upper_version_alias_targets_conflict"


def test_lower_version_aliases_with_different_targets_are_unknown_even_with_a_literal_comment():
    source = source_new_tag()
    lower = source["snapshot_visits"][0]
    lower["bindings"]["4.1.0"] = "e" * 40
    lower["tag_names"].append("4.1.0")
    result = run([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")], source)
    assert not result["opportunity_matches"][0]["applicable"]
    assert result["opportunity_matches"][0]["applicability_reason"] == "lower_version_alias_targets_conflict"


def multi_snapshot(number, offset, items, complete=True):
    text = "jobs:\n  test:\n    steps:\n" + "".join(
        "      - id: " + identity + "\n        uses: " + action + "@" + target + (" # " + comment if comment else "") + "\n"
        for identity, action, target, comment in items)
    if not items:
        text = "jobs:\n  test:\n    steps: []\n"
    parsed = enrich_workflow(text, ".github/workflows/ci.yml")
    return dict(sha=f"c{number}", timestamp=ANCHOR + DAY * offset, date=iso(offset),
                complete=complete, references=parsed["references"])


@pytest.mark.parametrize("entry_mode", ["copied_reference", "reintroduced_action", "replace"])
def test_known_born_fixed_entries_link_to_opportunities_without_becoming_pin_transitions(entry_mode):
    original = ("original", TARGET, "v4", None)
    added = ("added", TARGET, SHA, "v4.1.0")
    baseline_items = [original]
    if entry_mode == "replace":
        baseline_items.append(("added", "other/action", "v1", None))
    states = [multi_snapshot(1, -1, baseline_items)]
    if entry_mode == "reintroduced_action":
        states.append(multi_snapshot(2, .25, []))
        after = [added]
    else:
        after = [original, added]
    states += [multi_snapshot(3, .5, after),
               multi_snapshot(4, 3, [item if item[0] != "added" else ("added", TARGET, NEXT_SHA, "v4.2.0") for item in after])]
    joined = run(states, source_a())
    match = joined["opportunity_matches"][0]
    assert match["target_delay_model_candidate"] and match["target_born_fixed_delay_model_candidate"]
    assert not match["target_pin_delay_model_candidate"] and not match["target_baseline_delay_model_candidate"]
    assert match["rq3_target_episode_links"] == [dict(episode_id=match["rq3_target_episode_ids"][0],
        episode_kind="post_anchor_born_fixed", entry_mode=entry_mode, source_entry_event=entry_mode)]
    assert joined["summary"]["target_rq3_by_episode_kind"]["post_anchor_observed_pin"]["exported_episodes"] == 0
    assert joined["summary"]["target_rq3_by_episode_kind"]["post_anchor_born_fixed"]["candidate_episodes"] == 1
    assert joined["summary"]["target_rq3_by_entry_mode"][entry_mode]["actual_target_sha_adoptions"] == 1


def test_pin_and_born_fixed_episodes_keep_separate_denominators_in_one_repository():
    states = [multi_snapshot(1, -1, [("original", TARGET, "v4", None)]),
              multi_snapshot(2, .5, [(identity, TARGET, SHA, "v4.1.0") for identity in ("original", "added")]),
              multi_snapshot(3, 3, [(identity, TARGET, NEXT_SHA, "v4.2.0") for identity in ("original", "added")])]
    joined = run(states, source_a())
    by_kind = joined["summary"]["target_rq3_by_episode_kind"]
    for kind in ("post_anchor_observed_pin", "post_anchor_born_fixed"):
        assert by_kind[kind]["exported_episodes"] == by_kind[kind]["candidate_episodes"] == 1
        assert by_kind[kind]["candidate_repository_ids"] == 1
    assert joined["summary"]["target_rq3_episode_candidates"] == 2
    assert joined["summary"]["target_rq3_repository_candidates"] == 1
    assert sum(row["target_pin_delay_model_candidate"] for row in joined["opportunity_matches"]) == 1
    assert sum(row["target_born_fixed_delay_model_candidate"] for row in joined["opportunity_matches"]) == 1


def test_born_fixed_comparator_links_preserve_focal_entry_kind_and_mode():
    regular = ("regular", "actions/checkout", SHA, "v4.1.0")
    original = ("original", TARGET, "v4", None)
    states = [multi_snapshot(1, -1, [original, regular]),
              multi_snapshot(2, .5, [original, regular, ("added", TARGET, SHA, "v4.1.0")]),
              multi_snapshot(3, 3, [original, ("regular", "actions/checkout", NEXT_SHA, "v4.2.0"),
                                       ("added", TARGET, NEXT_SHA, "v4.2.0")])]
    baseline, measured = measurements(states)
    risk = build_risksets([baseline], [measured], STUDY)
    regular_source = source_a()
    regular_source["summary"]["repository"] = "actions/checkout"
    for record in regular_source["release_records"] + regular_source["opportunities"]:
        record["repository"] = "actions/checkout"
    joined = build_join([measured], [source_a(), regular_source], risk["risksets"], risk["upstream_frequencies"], STUDY, [baseline])
    regular_match = next(row for row in joined["opportunity_matches"] if row["action"] == "actions/checkout")
    assert regular_match["applicable"] and not regular_match["target_delay_model_candidate"]
    assert regular_match["rq3_target_episode_links"] == []
    assert len(regular_match["comparison_focal_episode_links"]) == 1
    link = regular_match["comparison_focal_episode_links"][0]
    assert link == dict(focal_episode_id=regular_match["comparison_focal_episode_ids"][0],
        focal_episode_kind="post_anchor_born_fixed", focal_entry_mode="copied_reference",
        comparison_layer="same_repository_baseline_fixed")


def test_duplicate_born_fixed_opportunity_evidence_cannot_keep_a_second_candidate_flag():
    states = [multi_snapshot(1, -1, [("original", TARGET, "v4", None)]),
              multi_snapshot(2, .5, [("original", TARGET, "v4", None), ("added", TARGET, SHA, "v4.1.0")]),
              multi_snapshot(3, 3, [("original", TARGET, "v4", None), ("added", TARGET, NEXT_SHA, "v4.2.0")])]
    source = source_new_tag()
    exact = source_a()
    source["release_records"] = exact["release_records"]
    source["opportunities"].extend(exact["opportunities"])
    joined = run(states, source)
    assert len(joined["opportunity_matches"]) == 2
    duplicate = next(row for row in joined["opportunity_matches"] if not row["selected_for_delay_analysis"])
    assert not duplicate["target_delay_model_candidate"] and not duplicate["target_born_fixed_delay_model_candidate"]
    coverage = joined["summary"]["target_rq3_by_episode_kind"]["post_anchor_born_fixed"]
    assert coverage["selected_opportunity_reference_pairs"] == coverage["actual_target_sha_adoptions"] == 1


def test_unknown_fixed_entry_remains_diagnostic_even_when_an_opportunity_is_applicable():
    states = [multi_snapshot(1, -1, [("original", TARGET, "v4", None)]),
              multi_snapshot(2, .25, [], complete=False),
              multi_snapshot(3, .5, [("added", TARGET, SHA, "v4.1.0")]),
              multi_snapshot(4, 3, [("added", TARGET, NEXT_SHA, "v4.2.0")])]
    joined = run(states, source_a())
    match = joined["opportunity_matches"][0]
    assert match["applicable"]
    assert match["rq3_target_episode_ids"] == match["rq3_target_episode_links"] == []
    assert not any(match[key] for key in ("target_delay_model_candidate", "target_pin_delay_model_candidate",
                                         "target_born_fixed_delay_model_candidate", "target_baseline_delay_model_candidate"))
    assert joined["summary"]["unknown_fixed_entry_diagnostic_rows"] >= 1
    assert joined["summary"]["exported_target_riskset_episodes"] == 0


def test_report_states_B_upper_bound_survival_selection_and_does_not_label_early_change_as_failure():
    joined = run([snapshot(1, -1, comment="v4"), snapshot(2, 1.5, NEXT_SHA, "v4")], source_b())
    report = render_report(joined, {"coverage": {"measured_repository_ids": 1, "confirmed_repository_ids": 1}})
    assert "存活至上界" in report and "不能代表所有固定引用" in report
    assert "早于上界" in report and "不标为维护失败" in report
    assert "转换队列的分母不包含引入即固定" in report


def test_v4_join_writes_new_output_and_preserves_v1_v2_v3_evidence(tmp_path, monkeypatch):
    legacy = [tmp_path / "data/derived" / version / "opportunity_matches.jsonl"
              for version in ("opportunity-join-v1", "opportunity-join-v2", "opportunity-join-v3")]
    for path in legacy:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"legacy evidence\n")
    baseline, measured = measurements([snapshot(1, -1), snapshot(2, 3, NEXT_SHA, "v4.2.0")])
    risk = build_risksets([baseline], [measured], STUDY)
    baseline_path = tmp_path / "data/derived/cohort-v1/baselines/one.json"
    measurement_path = tmp_path / "data/derived" / BATCH_DIRECTORY / "1.json"
    upstream_path = tmp_path / "data/derived/upstream-opportunities/owner__action.json"
    for path, payload in ((baseline_path, baseline), (measurement_path, measured), (upstream_path, source_a())):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    risk["summary"]["source_sha256"] = {path.relative_to(tmp_path).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                        for path in (baseline_path, measurement_path)}
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports/riskset_summary.json").write_text(json.dumps(risk["summary"]), encoding="utf-8")
    for name, rows in (("risksets", risk["risksets"]), ("upstream_frequencies", risk["upstream_frequencies"])):
        with (tmp_path / "reports" / (name + ".csv")).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=sorted(set().union(*(row.keys() for row in rows))))
            writer.writeheader()
            writer.writerows({key: json.dumps(value) if isinstance(value, (list, dict)) else value for key, value in row.items()} for row in rows)
    for name in ("opportunity_join.py", "opportunities.py", "measurement.py", "measurement_io.py"):
        path = tmp_path / "pintrace" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("fixture source", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    (tmp_path / "study.toml").write_text("[study]\n" + "\n".join(key + " = " + json.dumps(value) for key, value in STUDY.items()), encoding="utf-8")
    monkeypatch.setattr(opportunity_join, "__file__", str(tmp_path / "pintrace/opportunity_join.py"))
    opportunity_join.main()
    assert opportunity_join.RULES_VERSION == "opportunity-join-v5"
    output = tmp_path / "data/derived" / opportunity_join.RULES_VERSION / "opportunity_matches.jsonl"
    assert output.exists()
    row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert row["rq3_target_episode_links"][0]["episode_kind"] == "baseline_fixed"
    assert all(path.read_bytes() == b"legacy evidence\n" for path in legacy)
