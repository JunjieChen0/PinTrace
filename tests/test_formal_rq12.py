"""Statistical boundaries use deterministic source histories, never labels."""
import json

import pytest

from pintrace.formal_rq12 import DAY, describe_repository, extract_path, extract_repository, run, summarize
from pintrace.lineage import reconstruct
from pintrace.workflow import classify_uses

ANCHOR = 20 * DAY
TARGET = "tj-actions/changed-files"
SHA = "a" * 40
STUDY = dict(event_anchor=ANCHOR, window_end=ANCHOR + 366 * DAY - 1, target_action=TARGET)


def ref(action=TARGET, version="v1", identifier="target"):
    return dict(classify_uses(action + "@" + version), path="ci.yml", job="test", step_id=identifier, step_index=0)


def state(day, refs=(), complete=True, name=None):
    return dict(sha=name or "commit-" + str(day), timestamp=ANCHOR + day * DAY,
                complete=complete, references=list(refs))


def detail(*states, identity="1", covers=True, baseline=None):
    baseline = baseline or next(s["sha"] for s in reversed(states) if s["timestamp"] < ANCHOR)
    return dict(summary=dict(repository_id=identity, canonical_repository="owner/repo" + identity,
                            status="measured", measurement_rules_version="measurement-v4",
                            baseline_commit=baseline, endpoint_commit=states[-1]["sha"],
                            observed_ancestry_covers_end=covers),
                snapshots=list(states), timeline=dict(endpoint_commit=states[-1]["sha"]),
                lineages=reconstruct("owner/repo" + identity, list(states)), replay=dict(passed=True))


def record(*states, **options):
    return extract_repository(detail(*states, **options), STUDY)


def summaries(records, **settings):
    return summarize(records, {r["repository_id"] for r in records}, settings)


def test_first_response_and_window_combinations_close_and_are_different():
    first = record(state(-2, [ref()]), state(1, [ref(version=SHA)]), state(20))
    second = record(state(-2, [ref()]), state(10, [ref(version="v2")]), identity="2")
    third = record(state(-2, [ref()]), state(8, [ref()]), identity="3")
    output = summaries([first, second, third])["rq1"]
    assert output["first_response_counts"]["pin"] == 1
    assert output["first_response_counts"]["tag_update"] == 1
    assert output["first_response_counts"]["no_observed_change"] == 1
    assert output["full_window_combination_counts"] == {"no_observed_change": 1, "pin+remove": 1, "tag_update": 1}
    assert all(output["checks"].values())


def test_simultaneous_pin_and_removal_are_not_arbitrarily_ordered():
    both = [ref(identifier="one"), ref(identifier="two")]
    r = record(state(-2, both), state(1, [ref(version=SHA, identifier="one")]))
    output = describe_repository(r)["rq1"]
    assert output["first_response_state"] == "multiple_behaviors"
    assert output["first_commit_behaviors"] == ["pin", "remove"]
    assert output["response_time_days"] == 1


def test_unknown_before_observed_pin_is_not_a_known_first_response():
    r = record(state(-2, [ref()]), state(1, complete=False), state(2, [ref()]), state(3, [ref(version=SHA)]))
    output = describe_repository(r)["rq1"]
    assert output["first_response_state"] == "unknown_boundary"
    assert output["response_time_days"] is None
    assert output["full_window_behaviors"] == ["pin", "unknown_boundary"]
    horizon = summaries([r])["rq1"]["horizons"]["7"]
    assert horizon["observed_response_lower"] == 0
    assert horizon["response_upper_bound"] == 1
    assert horizon["complete_case_response"] is None


def test_many_to_many_ambiguity_is_not_pinning_or_unchanged():
    r = record(state(-2, [ref(identifier=None), ref(identifier=None)]),
               state(1, [ref(version=SHA, identifier=None), ref(version=SHA, identifier=None)]))
    assert describe_repository(r)["rq1"]["first_response_state"] == "ambiguous_boundary"
    assert summaries([r])["rq1"]["first_response_counts"]["pin"] == 0


def test_ancestry_censoring_is_not_zero_response_through_day_365():
    r = record(state(-2, [ref()]), state(10, [ref()]), covers=False)
    output = summaries([r])["rq1"]
    assert output["horizons"]["7"]["counts"] == {"unchanged_observed_through_horizon": 1}
    assert output["horizons"]["30"]["counts"] == {"censored_before_horizon": 1}
    assert output["horizons"]["365"]["complete_case_denominator"] == 0


def test_clock_inversion_keeps_topological_response_but_no_delay():
    r = record(state(-2, [ref()]), state(3, [ref(version="v2")]), state(1, [ref(version=SHA)]))
    output = describe_repository(r)["rq1"]
    assert output["first_response_state"] == "clock_unknown"
    assert output["first_observed_topological_category"] == "tag_update"
    assert output["response_time_days"] is None
    assert summaries([r])["rq1"]["horizons"]["7"]["counts"] == {"clock_unknown": 1}


@pytest.mark.parametrize("before,after,kind", [("v1", SHA, "pin"), (SHA, "v1", "unpin"), (SHA, "b" * 40, "sha_target_change")])
def test_rq1_does_not_drop_already_fixed_reference_changes(before, after, kind):
    r = record(state(-2, [ref(version=before)]), state(1, [ref(version=after)]))
    assert describe_repository(r)["rq1"]["first_response_state"] == kind


def test_existing_fixed_baseline_denominator_competitions_and_weights():
    a = ref("actions/a", identifier="a")
    b = ref("actions/b", identifier="b")
    large = record(state(-2, [ref(), a, b]), state(1, [ref(), ref("actions/a", SHA, "a")]))
    small = record(state(-2, [ref(), a]), state(1, [ref(), ref("actions/a", SHA, "a")]), identity="2")
    output = summaries([large, small])["rq2"]["existing"]
    assert output["baseline_references"] == 3
    assert output["outcome_counts"] == {"pin": 2, "remove": 1}
    assert output["reference_weighted"]["pin"] == pytest.approx(2 / 3)
    assert output["repository_equal"]["pin"] == .75
    assert output["horizons"]["7"]["fixed_baseline_denominator"] == 3


def test_removal_and_replacement_never_count_as_existing_pin():
    a = ref("actions/a", identifier="a")
    r = record(state(-2, [ref(), a]), state(1, [ref(), ref("actions/b", SHA, "a")]))
    output = summaries([r])["rq2"]
    assert output["existing"]["outcome_counts"] == {"replace": 1}
    assert output["existing"]["reference_weighted"]["pin"] == 0
    assert output["new_actions"]["configuration_counts"]["all_full_sha"] == 1


def test_unknown_existing_lineage_cannot_bridge_to_later_sha():
    a = ref("actions/a", identifier="a")
    r = record(state(-2, [ref(), a]), state(1, complete=False), state(2, [ref(), ref("actions/a", SHA, "a")]))
    output = summaries([r])["rq2"]
    assert output["existing"]["outcome_counts"] == {"unknown_boundary": 1}
    assert output["existing"]["horizons"]["7"]["upper_bound"] == 1
    assert output["existing"]["reference_weighted"]["pin"] == 0
    assert output["new_actions"]["groups"] == 0


def test_first_identity_group_counts_all_its_initial_references_once():
    a = ref("actions/a", SHA, "a")
    b = ref("actions/a", "v1", "b")
    r = record(state(-2, [ref()]), state(1, [ref(), a, b]))
    output = summaries([r])["rq2"]["new_actions"]
    assert output["groups"] == 1
    assert output["configuration_counts"]["mixed"] == 1
    assert output["reference_weighted"]["full_sha_known_groups"] == .5
    assert output["repository_equal"]["all_full_sha_known_groups"] == 0


def test_copy_reintroduction_and_target_born_references_are_excluded():
    a = ref("actions/a", identifier="a")
    r = record(state(-2, [ref(), a]), state(1, [ref(), a, ref("actions/a", SHA, "copy"), ref(version=SHA, identifier="target-copy")]),
               state(2, [ref()]), state(3, [ref(), ref("actions/a", SHA, "new")]))
    output = summaries([r])["rq2"]
    assert output["new_actions"]["groups"] == 0
    assert output["checks"]["target_action_primary_rows"] == 0
    assert all(row["action"] != TARGET for row in describe_repository(r)["references"])


def test_first_seen_after_earlier_parse_gap_is_unknown_not_certain_adoption():
    r = record(state(-2, [ref()]), state(1, complete=False), state(2, [ref()]), state(3, [ref(), ref("actions/new", SHA, "new")]))
    output = summaries([r])["rq2"]["new_actions"]
    assert output["configuration_counts"]["unknown"] == 1
    assert output["known_groups"] == 0
    assert output["repository_equal"]["all_full_sha_known_groups"] is None


def test_repositories_without_new_actions_do_not_get_zero_ratios():
    no_new = record(state(-2, [ref()]), state(1, [ref()]))
    new = record(state(-2, [ref()]), state(1, [ref(), ref("actions/new", SHA, "new")]), identity="2")
    output = summaries([no_new, new])["rq2"]["new_actions"]
    assert output["no_new_group_repositories"] == 1
    assert output["repositories_with_new_groups"] == 1
    assert output["repository_equal"]["all_full_sha_known_groups"] == 1


def test_shifted_anchor_uses_actual_changed_baseline_instead_of_relabeling():
    a = ref("actions/a", identifier="a")
    r = record(state(-2, [ref(), a]), state(-.5, [ref(), ref("actions/a", SHA, "a")]), state(.5, [ref(), ref("actions/a", "v2", "a")]), state(2, [ref()]))
    assert summaries([r])["rq2"]["existing"]["baseline_references"] == 0
    assert summaries([r], anchor=ANCHOR - DAY)["rq2"]["existing"]["baseline_references"] == 1
    assert summaries([r], anchor=ANCHOR + DAY)["rq2"]["existing"]["baseline_references"] == 1
    with pytest.raises(ValueError, match="cached"):
        summaries([r], anchor=ANCHOR + 2 * DAY)


def test_excluding_event_day_does_not_turn_early_change_into_unchanged():
    r = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(.5, [ref(version=SHA), ref("actions/a", SHA, "a")]), state(10, [ref(version="v2")]))
    output = summaries([r], exclude_event_day=True)
    assert output["rq1"]["first_response_counts"]["excluded_eventday"] == 1
    assert output["rq2"]["existing"]["outcome_counts"] == {"excluded_eventday": 1}
    assert output["rq1"]["full_window_behavior_repository_counts"] == {"unpin": 1}
    assert output["rq1"]["checks"]["first_response_partition"]


def test_landmark_and_policy_period_keep_prior_terminal_outcomes_separate():
    r = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(1, [ref(version=SHA), ref("actions/a", SHA, "a")]), state(60, [ref(version="v3")]))
    output = summaries([r], start_after_days=30, period_end=ANCHOR + 90 * DAY)
    assert output["rq1"]["first_response_counts"]["before_landmark"] == 1
    assert output["rq2"]["existing"]["outcome_counts"] == {"before_landmark": 1}
    assert output["rq1"]["full_window_behavior_repository_counts"] == {"unpin": 1}


def test_target_projection_preserves_snapshot_identity_comments_and_proofs():
    target = dict(ref(), comment_kind="semantic_version", comment_identity_state="unverified", configuration_coverage=dict(status="covered", inheritance_unknown=True))
    r = record(state(-2, [target, ref("actions/a", identifier="a")]), state(1, [dict(target, ref=SHA, ref_type="full_sha")]))
    projected = r["target_measurement"]
    assert projected["replay"]["passed"] and projected["lineages"]["conservation"]["passed"]
    assert all(x["action"] == TARGET for x in projected["lineages"]["references"])
    assert projected["lineages"]["references"][0]["configuration_coverage"]["inheritance_unknown"]
    assert projected["lineages"]["references"][0]["snapshot_sha"] == "commit--2"
    assert projected["lineages"]["references"][0]["comment_kind"] == "semantic_version"


def test_streaming_extraction_equals_pure_extraction_without_full_snapshot_bodies(tmp_path):
    original = detail(state(-2, [ref()]), state(1, [ref(), ref("actions/a", SHA, "a")]), state(5, [ref(version=SHA)]))
    for snapshot in original["snapshots"]:
        snapshot["configuration"] = {"body": "x" * 200000}
    path = tmp_path / "source.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    streamed = extract_path(path, STUDY)
    assert streamed == extract_repository(original, STUDY)
    assert "body" not in json.dumps(streamed)


def test_missing_or_duplicate_repository_records_are_rejected():
    r = record(state(-2, [ref()]), state(1, [ref()]))
    with pytest.raises(ValueError, match="Missing or duplicate"):
        summarize([r, r], {"1"})
    with pytest.raises(ValueError, match="Missing or duplicate"):
        summarize([r], {"1", "2"})


def test_metric_keys_and_cluster_units_are_complete_and_unique():
    r = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(1, [ref(version=SHA), ref("actions/a", SHA, "a")]))
    context = dict(metrics=[], populations=dict(formal=["1"], raw=["1"]))
    output = summarize([r], {"1"}, context=context)
    keys = [metric["key"] for metric in context["metrics"]]
    assert len(keys) == len(set(keys))
    assert any(key.startswith("rq1.formal.main.horizon.365") for key in keys)
    assert any(key.startswith("rq2.formal.main.existing.horizon.7") for key in keys)
    assert any(metric["binary"] for metric in context["metrics"] if metric["kind"] == "ratio")
    assert output["rq1"]["response_time_days_quantiles"]["0.5"] == 1


def test_run_cannot_bypass_the_shared_frozen_input_guard(tmp_path):
    with pytest.raises(ValueError, match="guard"):
        run(tmp_path, tmp_path / "out", {})
    assert not (tmp_path / "out").exists()


def test_outside_period_horizons_are_null_instead_of_zero():
    r = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(60, [ref()]))
    output = summaries([r], period_start=ANCHOR + 30 * DAY, period_end=ANCHOR + 90 * DAY)
    for days in (7, 180, 365):
        assert output["rq1"]["horizons"][str(days)]["observed_response_lower"] is None
        assert output["rq1"]["horizons"][str(days)]["response_upper_bound"] is None
        assert output["rq2"]["existing"]["horizons"][str(days)]["observed_pin_cumulative"] is None


def test_clock_unknown_new_group_does_not_claim_postincident_fixed_adoption():
    r = record(state(-2, [ref()]), state(2, [ref()]), state(1, [ref(), ref("actions/new", SHA, "new")]))
    output = summaries([r])["rq2"]["new_actions"]
    assert output["configuration_counts"]["unknown"] == 1
    assert output["known_groups"] == 0
    row = describe_repository(r)["new_actions"][0]
    assert row["observed_configuration"] == "all_full_sha"
    assert row["calendar_state"] == "clock_unknown"


def test_tag_update_of_other_action_is_not_target_response():
    r = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(1, [ref(), ref("actions/a", "v2", "a")]))
    assert summaries([r])["rq1"]["first_response_counts"]["no_observed_change"] == 1


def test_later_unknown_does_not_erase_an_already_confirmed_first_response():
    r = record(state(-2, [ref()]), state(1, [ref(version=SHA)]), state(3, complete=False))
    output = describe_repository(r)["rq1"]
    assert output["first_response_state"] == "pin"
    assert output["response_time_days"] == 1
    assert output["full_window_behaviors"] == ["pin", "unknown_boundary"]


def test_complete_empty_configuration_is_removal_instead_of_missing_boundary():
    empty = record(state(-2, [ref()]), state(1))
    missing = record(state(-2, [ref()]), state(1, complete=False), identity="2")
    output = summaries([empty, missing])["rq1"]
    assert output["first_response_counts"]["remove"] == 1
    assert output["first_response_counts"]["unknown_boundary"] == 1


def test_existing_first_pin_survives_later_unpin_and_mutable_updates():
    a = ref("actions/a", identifier="a")
    r = record(state(-2, [ref(), a]), state(1, [ref(), ref("actions/a", "v2", "a")]),
               state(2, [ref(), ref("actions/a", SHA, "a")]), state(3, [ref(), a]))
    output = summaries([r])["rq2"]["existing"]
    assert output["outcome_counts"] == {"pin": 1}
    assert output["first_pin_days_quantiles"]["0.5"] == 2


def test_baseline_fixed_other_action_is_not_an_existing_nonsha_risk_unit():
    r = record(state(-2, [ref(), ref("actions/a", SHA, "a")]), state(1, [ref()]))
    output = summaries([r])["rq2"]["existing"]
    assert output["baseline_references"] == 0
    assert output["repository_equal"]["pin"] is None


def test_all_initial_sha_copies_form_one_known_identity_and_two_references():
    r = record(state(-2, [ref()]), state(1, [ref(), ref("actions/new", SHA, "a"), ref("actions/new", SHA, "b")]))
    output = summaries([r])["rq2"]["new_actions"]
    assert output["groups"] == 1
    assert output["known_groups"] == 1
    assert output["reference_denominator_known_groups"] == 2
    assert output["repository_equal"]["all_full_sha_known_groups"] == 1
    assert output["reference_weighted"]["full_sha_known_groups"] == 1


def test_censor_unknown_and_competing_removal_keep_fixed_reference_denominator():
    censor = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(3, [ref(), ref("actions/a", identifier="a")]), covers=False)
    remove = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(1, [ref()]), identity="2")
    output = summaries([censor, remove])["rq2"]["existing"]["horizons"]["7"]
    assert output["fixed_baseline_denominator"] == 2
    assert output["counts"] == {"censored_before_horizon": 1, "remove": 1}
    assert output["observed_pin_cumulative"] == 0
    assert output["upper_bound"] == .5


def test_raw_and_formal_summaries_apply_the_identity_mask_consistently():
    good = record(state(-2, [ref()]), state(1, [ref()]))
    excluded = record(state(-2, [ref()]), state(1, [ref(version=SHA)]), identity="2")
    formal = summarize([good, excluded], {"1"})
    raw = summarize([good, excluded], {"1", "2"}, population="raw")
    assert formal["rq1"]["repositories"] == 1
    assert formal["rq1"]["first_response_counts"]["pin"] == 0
    assert raw["rq1"]["first_response_counts"]["pin"] == 1


def test_exclusion_ids_preserve_complete_remaining_denominators():
    first = record(state(-2, [ref()]), state(1, [ref()]))
    second = record(state(-2, [ref()]), state(1, [ref()]), identity="2")
    result = summarize([first, second], {"1", "2"}, dict(excluded_ids={"2"}))
    assert result["rq1"]["repositories"] == 1
    assert result["rq1"]["checks"]["first_response_partition"]


def test_streaming_extraction_without_any_reference_or_event(tmp_path):
    original = detail(state(-2), state(1))
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    assert extract_path(path, STUDY) == extract_repository(original, STUDY)


def test_missing_shifted_baseline_is_unknown_not_unchanged_or_zero_reference_repository():
    r = record(state(-.25, [ref(), ref("actions/a", identifier="a")]), state(2, [ref()]))
    result = summaries([r], anchor=ANCHOR - DAY)
    assert result["rq1"]["first_response_counts"]["unknown_boundary"] == 1
    assert result["rq1"]["horizons"]["7"]["counts"] == {"unknown_boundary": 1}
    assert result["rq2"]["existing"]["repositories_with_unknown_baseline"] == 1
    assert result["rq2"]["existing"]["repositories_without_baseline_references"] == 0
    assert result["rq2"]["existing"]["baseline_reference_denominator_complete_for_population"] is False


def test_target_state_at_horizon_distinguishes_partial_fixed_absent_and_unknown():
    mixed = record(state(-2, [ref(identifier="one"), ref(identifier="two")]), state(1, [ref(version=SHA, identifier="one"), ref(identifier="two")]))
    absent = record(state(-2, [ref()]), state(1), identity="2")
    unknown = record(state(-2, [ref()]), state(1, complete=False), identity="3")
    output = summaries([mixed, absent, unknown])["rq1"]["target_state_horizons"]["7"]
    assert output["counts"] == {"absent": 1, "mixed": 1, "unknown": 1}


def test_same_utc_date_different_hours_have_identical_integer_response_days():
    early = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(.1, [ref(version=SHA), ref("actions/a", SHA, "a")]))
    late = record(state(-2, [ref(), ref("actions/a", identifier="a")]), state(.9, [ref(version=SHA), ref("actions/a", SHA, "a")]), identity="2")
    for value in (early, late):
        described = describe_repository(value)
        assert described["rq1"]["response_time_days"] == 0
        assert described["references"][0]["first_pin_days"] == 0
        assert isinstance(described["rq1"]["response_time_days"], int)
    assert summaries([early, late])["rq1"]["response_time_days_quantiles"]["0.5"] == 0


def test_clock_unknown_pin_is_an_identity_diagnostic_and_not_main_extension():
    a = ref("actions/a", identifier="a")
    r = record(state(-2, [ref(), a]), state(3, [ref(), ref("actions/a", SHA, "a")]),
               state(1, [ref(), ref("actions/a", SHA, "a")]))
    row = describe_repository(r)["references"][0]
    assert row["outcome"] == "pin"
    assert row["analysis_outcome"] == "clock_unknown"
    assert row["clock_unknown_reason"] == "nonmonotone_snapshot_calendar"
    output = summaries([r])["rq2"]
    assert output["existing"]["outcome_counts"] == {"clock_unknown": 1}
    assert output["existing"]["baseline_references"] == 1
    assert output["existing"]["reference_weighted"]["pin"] == 0
    assert output["existing"]["repository_equal"]["pin"] == 0
    assert output["calendar_eligibility"]["prior_pin_outcomes_recoded"] == 1
    assert output["calendar_eligibility"]["observed_topological_outcome_counts"] == {"pin": 1}


def test_no_postanchor_followup_is_unknown_with_a_separate_reason():
    r = record(state(-2, [ref(), ref("actions/a", identifier="a")]), covers=False)
    output = summaries([r])["rq2"]
    assert output["existing"]["outcome_counts"] == {"clock_unknown": 1}
    diagnostic = output["calendar_eligibility"]
    assert diagnostic["clock_unalignable_repository_ids"] == []
    assert diagnostic["baseline_references_with_clock_unknown"] == 1
    assert diagnostic["reasons"]["observation_ended_before_anchor"]["references"] == 1
    assert diagnostic["prior_pin_outcomes_recoded"] == 0


def test_unknown_calendar_keeps_fixed_denominator_and_known_pin_weighting():
    a = ref("actions/a", identifier="a")
    unknown = record(state(-2, [ref(), a]), state(2, [ref(), ref("actions/a", SHA, "a")]),
                     state(1, [ref(), ref("actions/a", SHA, "a")]))
    known = record(state(-2, [ref(), a]), state(2, [ref(), ref("actions/a", SHA, "a")]), identity="2")
    output = summaries([unknown, known])["rq2"]["existing"]
    assert output["baseline_references"] == 2
    assert output["outcome_counts"] == {"clock_unknown": 1, "pin": 1}
    assert output["reference_weighted"]["pin"] == .5
    assert output["repository_equal"]["pin"] == .5
    assert output["horizons"]["7"]["upper_bound"] == 1


def test_run_writes_new_batch_products_and_keeps_its_input_frozen(tmp_path, monkeypatch):
    from pintrace.formal_common import sha256, verify_frozen_inputs, make_context
    source = tmp_path / "data/derived/measurement-v3/1.json"
    source.parent.mkdir(parents=True)
    original = detail(state(-2, [ref()]), state(1, [ref(version=SHA)]))
    source.write_text(json.dumps(original), encoding="utf-8")
    (tmp_path / "reports").mkdir()
    (tmp_path / "study.toml").write_text('[study]\nevent_anchor=1728000\nwindow_end=33350399\ntarget_action="tj-actions/changed-files"\n', encoding="utf-8")
    risk = tmp_path / "reports/risksets.csv"
    risk.write_text("row_kind,episode_kind,repository_id,lineage_id\n", encoding="utf-8")
    manifest = dict(status="frozen", formal_population=dict(raw_confirmed_repository_ids=["1"], formal_eligible_repository_ids=["1"]),
                    input_sha256={name: sha256(tmp_path / name) for name in ["study.toml", "reports/risksets.csv", "data/derived/measurement-v3/1.json"]})
    (tmp_path / "reports/freeze_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    context = make_context(tmp_path, verify_frozen_inputs(tmp_path))
    result = run(tmp_path, tmp_path / "new-batch", context)
    assert set(result) == {"rq1_summary", "rq2_summary"}
    assert result["rq1_summary"]["formal"]["first_response_counts"]["pin"] == 1
    assert result["rq1_summary"]["checks"]["formal_and_raw_partitions"]
    # The authority declares no population sizes, so the size checks fail closed instead of passing vacuously.
    assert result["rq1_summary"]["checks"]["formal_population"] is False
    assert result["rq1_summary"]["checks"]["raw_population"] is False
    assert len(context["rq12_records"]) == 1
    assert context["rq12_target_measurements"][0] is context["rq12_records"][0]["target_measurement"]
    assert source.read_text(encoding="utf-8") == json.dumps(original)
    assert (tmp_path / "new-batch/rq2_existing_reference_results.csv").exists()
    receipt = json.loads((tmp_path / "new-batch/rq12_record_cache_receipt.json").read_text(encoding="utf-8"))
    assert receipt == context["rq12_record_cache_receipt"]
    context["metrics"] = []
    monkeypatch.setattr("pintrace.formal_rq12.extract_path", lambda *args: (_ for _ in ()).throw(AssertionError("cache must avoid source scan")))
    repeated = run(tmp_path, tmp_path / "cache-rerun", context)
    assert repeated["rq1_summary"]["formal"] == result["rq1_summary"]["formal"]
    context["rq12_records"][0]["repository"] = "tampered/repository"
    with pytest.raises(ValueError, match="cache receipt"):
        run(tmp_path, tmp_path / "tampered", context)
