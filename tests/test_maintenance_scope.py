import copy
import hashlib
import json

import pytest

from pintrace.maintenance_scope import _counts, export_scope, select_entries


def row(**changes):
    value = dict(repository_id="1", lineage_id="lineage", reference_target_segment_start_commit="commit",
                 opportunity_target="new-sha", opportunity_id="op-normal", applicable=True, evidence_grade="B",
                 evidence_type="observed_tag_introduction", available_after_or_at=1, available_by=2,
                 selected_for_delay_analysis=False, target_delay_model_candidate=False,
                 outcome="right_censored_no_observed_sha_change", outcome_timestamp=10, adopted_sha=None,
                 rq3_target_episode_links=[dict(episode_id="ep", episode_kind="post_anchor_observed_pin", entry_mode="pin")],
                 comparison_focal_episode_links=[])
    return dict(value, **changes)


def test_filter_before_dedup_restores_normal_evidence_without_mutating_raw_flags():
    original = [row(evidence_type="observed_binding_change", selected_for_delay_analysis=True, opportunity_id="op-retarget"), row()]
    before = copy.deepcopy(original)
    selected, = select_entries(original)
    assert selected["opportunity_id"] == "op-normal"
    assert selected["selected_for_delay_analysis"] is False and selected["target_delay_model_candidate"] is False
    assert selected["maintenance_target_delay_model_candidate"] is True and selected["maintenance_pin_delay_model_candidate"] is True
    assert original == before


@pytest.mark.parametrize("changes", [{"applicable": False}, {"evidence_grade": "C"}, {"evidence_type": "observed_binding_change"},
                                    {"evidence_type": "observed_tag_reintroduction"}, {"evidence_type": "historical_release_event"}])
def test_nonmaintenance_evidence_is_excluded(changes):
    assert select_entries([row(**changes)]) == []


def test_evidence_priority_and_ties_do_not_depend_on_outcome_or_input_order():
    strongest = row(evidence_grade="A", evidence_type="immutable_release", opportunity_id="op-a", available_by=9)
    weak = row(outcome="adopted_target", adopted_sha="new-sha", available_by=3)
    assert select_entries([weak, strongest])[0]["opportunity_id"] == "op-a"
    assert select_entries([strongest, weak])[0]["opportunity_id"] == "op-a"
    early = row(opportunity_id="op-z", available_by=2)
    tie = row(opportunity_id="op-a", available_by=2)
    assert select_entries([early, tie])[0]["opportunity_id"] == "op-a"


def test_legal_episode_kinds_and_comparison_links_rebuild_separate_flags():
    links = [dict(episode_id="baseline", episode_kind="baseline_fixed", entry_mode="baseline_fixed"),
             dict(episode_id="pin", episode_kind="post_anchor_observed_pin", entry_mode="pin"),
             dict(episode_id="born", episode_kind="post_anchor_born_fixed", entry_mode="copied_reference"),
             dict(episode_id="bad", episode_kind="post_anchor_observed_pin", entry_mode="unknown")]
    valid = dict(focal_episode_id="focal", focal_episode_kind="post_anchor_observed_pin", focal_entry_mode="pin", comparison_layer="same_repository_baseline_fixed")
    selected, = select_entries([row(rq3_target_episode_links=links, comparison_focal_episode_links=[valid, dict(valid, focal_entry_mode="unknown")])])
    assert len(selected["maintenance_target_episode_links"]) == 3
    assert selected["maintenance_comparison_focal_episode_links"] == [valid]
    assert all(selected["maintenance_" + name + "_delay_model_candidate"] for name in ("baseline", "pin", "born_fixed", "comparison"))
    assert _counts([selected], kind="baseline_fixed")["episode_ids"] == 1
    assert _counts([selected])["repository_ids"] == 1


def test_input_hash_mismatch_refuses_output(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    matches = tmp_path / "matches.jsonl"
    matches.write_text(json.dumps(row()) + "\n")
    risk = reports / "riskset_summary.json"
    risk.write_text("{}")
    summary = dict(rule_version="opportunity-join-v5", inputs_unchanged=True,
                   input_sha256={"reports/riskset_summary.json": hashlib.sha256(risk.read_bytes()).hexdigest()},
                   details={"opportunity_matches": dict(path="matches.jsonl", sha256="0" * 64)})
    (reports / "opportunity_join_summary.json").write_text(json.dumps(summary))
    with pytest.raises(ValueError, match="missing or changed"):
        export_scope(tmp_path)
    assert not (tmp_path / "data").exists()
    assert not (reports / "maintenance_scope_summary.json").exists()
