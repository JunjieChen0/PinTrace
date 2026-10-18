"""Golden structural scenarios, not manually inferred research labels."""

import hashlib
import json

import pytest

from pintrace.lineage import RULES_VERSION, reconstruct
from pintrace.configuration import enrich_workflow
from pintrace.workflow import classify_uses


SHA_A, SHA_B = "a" * 40, "b" * 40


def ref(action="actions/checkout", version="v4", **fields):
    return dict(classify_uses(action + "@" + version), path="ci.yml", job="build",
                step_id=None, step_index=0, **fields)


def snap(number, refs=(), complete=True, **fields):
    return dict(sha=f"commit-{number}", timestamp=number, date=str(number),
                complete=complete, references=list(refs), **fields)


def run(*snapshots):
    output = reconstruct("owner/repo", list(snapshots))
    assert output["conservation"]["passed"]
    assert output["conservation"]["balance"] == 0
    assert all(row["balance"] == 0 for row in output["conservation"]["intervals"])
    json.dumps(output)
    return output


def kinds(output):
    return [event["event"] for event in output["events"]]


@pytest.mark.parametrize("old,new,event", [
    ("v4", SHA_A, "pin"), (SHA_A, "v4", "unpin"),
    (SHA_A, SHA_B, "sha_target_change"), ("v4", "v5", "mutable_update"),
])
def test_supported_single_reference_version_changes(old, new, event):
    result = run(snap(1, [ref(version=old)]), snap(2, [ref(version=new)]))
    assert kinds(result) == [event]
    assert result["references"][0]["lineage_id"] == result["references"][1]["lineage_id"]
    assert result["conservation"]["known_entries"] == 0


def test_reorder_does_not_change_lineages_or_create_events():
    first = [ref(step_context="checkout-linux"), ref(step_context="checkout-macos")]
    second = [dict(first[1], step_index=0, line=99), dict(first[0], step_index=1, line=3)]
    result = run(snap(1, first), snap(2, second))
    assert not result["events"]
    assert result["references"][0]["lineage_id"] == result["references"][3]["lineage_id"]
    assert result["references"][1]["lineage_id"] == result["references"][2]["lineage_id"]


def test_rename_comment_and_line_changes_preserve_reference():
    before = ref()
    after = dict(before, path="release.yml", comment="v4", line=21)
    result = run(snap(1, [before]), snap(2, [after], renames={"ci.yml": "release.yml"}))
    assert kinds(result) == []
    assert result["references"][0]["lineage_id"] == result["references"][1]["lineage_id"]


def test_missing_rename_evidence_is_unknown_instead_of_new_and_removed():
    result = run(snap(1, [ref()]), snap(2, [dict(ref(), path="release.yml")]))
    assert kinds(result) == ["ambiguous_transition"]
    assert result["conservation"]["unknown_entries"] == result["conservation"]["unknown_exits"] == 1
    assert result["conservation"]["known_entries"] == result["conservation"]["known_exits"] == 0


def test_action_identity_first_observed_copy_and_reintroduction_are_distinct():
    original = ref(step_context="original")
    copied = ref(step_context="copy")
    result = run(snap(1), snap(2, [original]), snap(3, [original, copied]),
                 snap(4), snap(5, [original]))
    assert kinds(result) == ["first_observed_action", "copied_reference", "remove", "remove", "reintroduced_action"]
    assert result["quality"]["observed_action_groups"] == 1
    assert result["conservation"]["known_entries"] == 3
    assert result["conservation"]["known_exits"] == 2


def test_two_first_introduced_references_are_one_identity_group_and_one_copy():
    result = run(snap(1), snap(2, [ref(step_context="a"), ref(step_context="b")]))
    assert kinds(result) == ["first_observed_action", "copied_reference"]
    assert result["quality"]["observed_action_groups"] == 1
    assert result["conservation"]["known_entries"] == 2


def test_ambiguous_many_to_many_rewrite_does_not_invent_pinning():
    before = [ref(version="v3"), ref(version="v4")]
    after = [ref(version=SHA_A), ref(version=SHA_B)]
    result = run(snap(1, before), snap(2, after))
    assert kinds(result) == ["ambiguous_transition"]
    assert result["events"][0]["lineage_status"] == "lineage_ambiguous"
    assert result["quality"]["ambiguous_transition_rate"] == 1
    assert result["conservation"]["unknown_entries"] == result["conservation"]["unknown_exits"] == 2


def test_identical_duplicate_reorder_does_not_guess_instance_identity():
    result = run(snap(1, [ref(), ref()]), snap(2, [ref(), ref()]))
    assert kinds(result) == ["ambiguous_transition"]
    assert not any(kind in kinds(result) for kind in ("pin", "first_observed_action", "remove"))


def test_explicit_ids_disambiguate_many_references():
    a, b = dict(ref(), step_id="a"), dict(ref(), step_id="b")
    result = run(snap(1, [a, b]), snap(2, [dict(b, ref=SHA_A, ref_type="full_sha"), a]))
    assert kinds(result) == ["pin"]
    assert result["events"][0]["matching_rule"] == "unique_step_id"


def test_same_step_position_does_not_override_conflicting_context():
    before = ref(step_context="build")
    after = ref(version=SHA_A, step_context="deployment")
    result = run(snap(1, [before]), snap(2, [after]))
    assert kinds(result) == ["ambiguous_transition"]


def test_changed_explicit_step_id_does_not_override_same_context():
    before = dict(ref(step_context="same"), step_id="old")
    after = dict(ref(version=SHA_A, step_context="same"), step_id="new")
    assert kinds(run(snap(1, [before]), snap(2, [after]))) == ["ambiguous_transition"]


@pytest.mark.parametrize("anchor", ["step_id", "step_context"])
def test_replacement_has_two_sided_instance_accounting(anchor):
    before = dict(ref(), **{anchor: "stable"})
    after = dict(ref("new/action"), **{anchor: "stable"})
    result = run(snap(1, [before]), snap(2, [after]))
    assert kinds(result) == ["replace"]
    assert result["events"][0]["entry_kind"] == "first_observed_action"
    assert result["conservation"]["known_entries"] == result["conservation"]["known_exits"] == 1
    assert result["references"][0]["lineage_id"] != result["references"][1]["lineage_id"]


def test_unanchored_action_replacement_is_ambiguous_not_position_based():
    result = run(snap(1, [ref()]), snap(2, [ref("new/action")]))
    assert kinds(result) == ["ambiguous_transition"]


def test_partial_yaml_snapshot_is_not_zero_and_does_not_bridge_gap():
    result = run(snap(1, [ref()]), snap(2, [], complete=False), snap(3, [ref(version=SHA_A)]))
    assert kinds(result) == ["unknown_boundary", "unknown_boundary"]
    assert result["conservation"]["unknown_exits"] == result["conservation"]["unknown_entries"] == 1
    assert result["conservation"]["known_exits"] == result["conservation"]["known_entries"] == 0
    assert result["quality"]["history_has_unknown"]


def test_partial_reference_rows_are_evidence_without_lineage():
    result = run(snap(1, [ref()], complete=False), snap(2, [ref(version=SHA_A)]))
    assert result["references"][0]["lineage_id"] is None
    assert result["references"][0]["lineage_status"] == "snapshot_incomplete"
    assert kinds(result) == ["unknown_boundary"]
    assert result["conservation"]["initial_instances"] == 0


def test_real_empty_workflow_is_distinct_from_an_unparsed_empty_marker():
    empty = run(snap(1, [ref()]), snap(2))
    unknown = run(snap(1, [ref()]), snap(2, complete=False))
    assert kinds(empty) == ["remove"]
    assert kinds(unknown) == ["unknown_boundary"]
    assert not unknown["conservation"]["endpoint_known"]


def test_first_parent_order_is_preserved_despite_clock_inversion():
    result = run(snap(20, [ref()]), snap(10, [ref(version=SHA_A)]))
    assert kinds(result) == ["pin"]
    assert result["events"][0]["previous_commit"] == "commit-20"
    assert result["events"][0]["commit"] == "commit-10"
    assert result["quality"]["matching_uses_timestamps"] is False


def test_mixed_reference_changes_balance_known_and_unknown_boundaries():
    first = [dict(ref(), step_id="keep"), dict(ref("old/action"), step_id="replace"),
             dict(ref("gone/action"), path="gone.yml"), ref("many/action"), ref("many/action")]
    second = [dict(ref(version=SHA_A), step_id="keep"), dict(ref("new/action"), step_id="replace"),
              ref("many/action", SHA_A), ref("many/action", SHA_B), dict(ref("fresh/action"), job="other")]
    result = run(snap(1, first), snap(2, second))
    assert sorted(kinds(result)) == sorted(["pin", "replace", "remove", "first_observed_action", "ambiguous_transition"])
    ledger = result["conservation"]
    assert ledger["initial_instances"] == ledger["final_instances"] == 5
    assert ledger["known_entries"] == ledger["known_exits"] == 2
    assert ledger["unknown_entries"] == ledger["unknown_exits"] == 2


def test_non_action_references_are_outside_the_measured_denominator():
    local = dict(classify_uses("./local"), path="ci.yml", job="build")
    result = run(snap(1, [local]), snap(2, [local]))
    assert result["references"] == result["events"] == []
    assert result["conservation"]["initial_instances"] == 0


def test_empty_input_and_rule_version_are_explicit():
    result = run()
    assert result["rules_version"] == RULES_VERSION
    assert not result["conservation"]["endpoint_known"]
    assert result["quality"]["ambiguous_transition_rate"] is None


def test_replay_is_exactly_deterministic():
    inputs = [snap(1, [ref()]), snap(2, [ref(version=SHA_A)]), snap(3), snap(4, [ref()])]
    assert reconstruct("owner/repo", inputs) == reconstruct("owner/repo", inputs)


def source_snapshot(number, text, path=".github/workflows/ci.yml", **fields):
    raw = text.encode("utf-8")
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    parsed = enrich_workflow(text, path)
    assert not parsed["errors"]
    return snap(number, parsed["references"], files=[dict(path=path, blob=blob, mode="100644", kind="blob")], **fields)


@pytest.mark.parametrize("aliased", [False, True])
def test_identical_blob_keeps_repeated_reference_ids_across_config_only_change(aliased):
    steps = (f"      - &checkout {{uses: actions/checkout@{SHA_A}}}\n      - *checkout\n" if aliased else
             f"      - uses: actions/checkout@{SHA_A}\n      - uses: actions/checkout@{SHA_A}\n")
    text = "jobs:\n  build:\n    steps:\n" + steps
    before = source_snapshot(1, text, configuration_files=[])
    after = source_snapshot(2, text, configuration_files=[dict(path=".github/dependabot.yml", blob="changed-config")])
    assert before["files"] == after["files"]
    assert all(reference["step_context"] is None for reference in before["references"])
    result = run(before, after)
    assert kinds(result) == []
    assert [item["lineage_id"] for item in result["references"][:2]] == [item["lineage_id"] for item in result["references"][2:]]
    assert result["conservation"]["unknown_entries"] == result["conservation"]["unknown_exits"] == 0
    assert result["quality"]["matching_rule_identical_workflow_blob_node"] == 2
    assert result["quality"]["matching_uses_unverified_step_position"] is False


def test_identical_blob_node_anchor_respects_confirmed_file_rename():
    text = f"jobs:\n  build:\n    steps:\n      - uses: actions/checkout@{SHA_A}\n      - uses: actions/checkout@{SHA_A}\n"
    before = source_snapshot(1, text, path=".github/workflows/old.yml")
    after = source_snapshot(2, text, path=".github/workflows/new.yml", renames={".github/workflows/old.yml": ".github/workflows/new.yml"})
    result = run(before, after)
    assert kinds(result) == []
    assert result["quality"]["matching_rule_identical_workflow_blob_node"] == 2


@pytest.mark.parametrize("missing", ["before", "after", "both"])
def test_node_position_without_two_sided_blob_evidence_stays_ambiguous(missing):
    text = f"jobs:\n  build:\n    steps:\n      - uses: actions/checkout@{SHA_A}\n      - uses: actions/checkout@{SHA_A}\n"
    before, after = source_snapshot(1, text), source_snapshot(2, text)
    for side in ("before", "after"):
        if missing in (side, "both"):
            (before if side == "before" else after).pop("files")
    assert kinds(run(before, after)) == ["ambiguous_transition"]


def test_reorder_in_changed_blob_without_identity_anchors_stays_ambiguous():
    template = "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@{}\n      - uses: actions/checkout@{}\n"
    before = source_snapshot(1, template.format(SHA_A, SHA_B))
    after = source_snapshot(2, template.format(SHA_B, SHA_A))
    assert before["files"][0]["blob"] != after["files"][0]["blob"]
    assert kinds(run(before, after)) == ["ambiguous_transition"]


def test_identical_blob_without_file_identity_evidence_is_not_a_position_match():
    text = f"jobs:\n  build:\n    steps:\n      - uses: actions/checkout@{SHA_A}\n      - uses: actions/checkout@{SHA_A}\n"
    result = run(source_snapshot(1, text, path=".github/workflows/old.yml"),
                 source_snapshot(2, text, path=".github/workflows/new.yml"))
    assert kinds(result) == ["ambiguous_transition"]


def test_unchanged_file_node_anchors_survive_other_workflow_content_changes():
    repeated = f"jobs:\n  build:\n    steps:\n      - uses: actions/checkout@{SHA_A}\n      - uses: actions/checkout@{SHA_A}\n"
    single = "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@{}\n"
    before = source_snapshot(1, repeated)
    after = source_snapshot(2, repeated)
    old_single = source_snapshot(1, single.format("v4"), ".github/workflows/other.yml")
    new_single = source_snapshot(2, single.format(SHA_B), ".github/workflows/other.yml")
    for target, additional in ((before, old_single), (after, new_single)):
        target["references"].extend(additional["references"])
        target["files"].extend(additional["files"])
    result = run(before, after)
    assert kinds(result) == ["pin"]
    assert result["quality"]["matching_rule_identical_workflow_blob_node"] == 2
    assert result["conservation"]["unknown_entries"] == result["conservation"]["unknown_exits"] == 0
