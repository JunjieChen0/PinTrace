"""Source-text golden scenarios exercise annotations together with lineages."""

from itertools import permutations

import pytest
import yaml

from pintrace.configuration import coverage_for, enrich_workflow, parse_configuration
from pintrace.lineage import reconstruct

SHA = "a" * 40
PATH = ".github/workflows/ci.yml"
EMPTY = "jobs:\n  build:\n    steps: []\n"
BOT = {".github/dependabot.yml": "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    schedule: {interval: weekly}\n"}


def workflow(*steps):
    return "jobs:\n  build:\n    steps:\n" + "".join(steps)


def step(action="actions/checkout", version="v4", identifier=None, name=None, comment=""):
    return ("      - " + ("id: " + identifier + "\n        " if identifier else "") +
            "uses: " + action + "@" + version + comment + "\n" +
            ("        name: " + name + "\n" if name else ""))


def state(number, text, files=None, path=PATH, **extra):
    # None represents an unresolved source marker; '' is a real verified blob.
    parsed = enrich_workflow(text, path) if text is not None else {"references": [], "errors": ["unresolved_source_marker"]}
    config = parse_configuration(files or {}, branch="main")
    refs = [dict(ref, configuration_coverage=coverage_for(ref, config)) for ref in parsed["references"]]
    return dict(sha=f"commit-{number}", date=f"date-{number}", timestamp=number,
                complete=not parsed["errors"], configuration=config,
                configuration_complete=not config["errors"], references=refs, **extra)


def run(*snapshots):
    result = reconstruct("owner/repo", list(snapshots))
    assert result["conservation"]["passed"]
    assert all(interval["passed"] for interval in result["conservation"]["intervals"])
    return result


def events(result):
    return [item["event"] for item in result["events"]]


@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_source_step_reorder_and_comment_edits_never_create_pins_or_entries(order):
    before = [step(version=SHA, identifier=f"checkout-{i}", name=f"Checkout-{i}") for i in range(3)]
    after = [step(version=SHA, identifier=f"checkout-{i}", name=f"Checkout-{i}", comment=" # v4") for i in order]
    result = run(state(1, workflow(*before)), state(2, workflow(*after)))
    assert events(result) == []
    assert result["conservation"]["known_entries"] == 0
    assert result["conservation"]["known_exits"] == 0
    assert result["conservation"]["unknown_entries"] == 0
    assert all(ref["comment_identity_state"] == "unverified" for ref in result["references"][3:])
    first = {ref["step_id"]: ref["lineage_id"] for ref in result["references"][:3]}
    assert all(first[ref["step_id"]] == ref["lineage_id"] for ref in result["references"][3:])


def test_comment_only_change_to_bare_sha_preserves_same_lineage():
    original = workflow(step(version=SHA))
    commented = workflow(step(version=SHA, comment=" # ratchet:actions/checkout@v4"))
    result = run(state(1, original), state(2, commented))
    assert not result["events"]
    assert result["references"][0]["step_context"] is None
    assert result["references"][0]["lineage_id"] == result["references"][1]["lineage_id"]
    assert result["references"][1]["comment_kind"] == "ratchet"


def test_source_removal_then_return_is_reintroduction_not_first_addition():
    original = workflow(step(identifier="checkout"))
    result = run(state(1, original), state(2, EMPTY), state(3, original))
    assert events(result) == ["remove", "reintroduced_action"]
    assert result["conservation"]["known_entries"] == 1
    assert result["conservation"]["known_exits"] == 1


def test_copying_a_step_is_distinct_from_first_observed_action_identity():
    one = workflow(step(identifier="one"))
    two = workflow(step(identifier="one"), step(identifier="two"))
    result = run(state(1, EMPTY), state(2, one), state(3, two))
    assert events(result) == ["first_observed_action", "copied_reference"]
    assert result["quality"]["observed_action_groups"] == 1
    assert result["conservation"]["known_entries"] == 2


@pytest.mark.parametrize("gap", [None, "jobs: [broken"])
def test_unknown_source_marker_or_illegal_yaml_does_not_bridge_tag_to_sha(gap):
    result = run(state(1, workflow(step())), state(2, gap), state(3, workflow(step(version=SHA))))
    assert events(result) == ["unknown_boundary", "unknown_boundary"]
    assert result["conservation"]["unknown_exits"] == 1
    assert result["conservation"]["unknown_entries"] == 1
    assert result["conservation"]["known_entries"] == 0
    assert result["conservation"]["known_exits"] == 0
    assert result["references"][0]["lineage_id"] != result["references"][-1]["lineage_id"]


def test_real_empty_blob_is_inactive_and_distinct_from_missing_source_marker():
    verified = run(state(1, workflow(step())), state(2, ""))
    unresolved = run(state(1, workflow(step())), state(2, None))
    assert events(verified) == ["remove"]
    assert events(unresolved) == ["unknown_boundary"]
    assert verified["conservation"]["endpoint_known"]
    assert not unresolved["conservation"]["endpoint_known"]


def test_configuration_only_change_updates_state_without_future_backfill_or_reference_events():
    text = workflow(step(version=SHA))
    result = run(state(1, text), state(2, text, files=BOT), state(3, text, files=BOT))
    assert events(result) == []
    assert [ref["configuration_coverage"]["status"] for ref in result["references"]] == [
        "no_local_config_observed", "covered", "covered"]
    assert len({ref["lineage_id"] for ref in result["references"]}) == 1


def test_renovate_inherited_configuration_changes_annotation_without_claiming_no_bot():
    text = workflow(step(version=SHA))
    files = {".github/renovate.json": '{"extends":["github:org/renovate-config"]}'}
    result = run(state(1, text), state(2, text, files=files))
    assert not result["events"]
    coverage = result["references"][-1]["configuration_coverage"]
    assert coverage["status"] == "unknown"
    assert coverage["inheritance_unknown"]


def test_git_rename_and_source_layout_changes_preserve_identity():
    before = workflow(step(identifier="checkout", version=SHA))
    after = "jobs: {build: {steps: [{id: checkout, uses: actions/checkout@" + SHA + "}]}}\n"
    result = run(state(1, before), state(2, after, path=".github/workflows/release.yml",
                                          renames={PATH: ".github/workflows/release.yml"}))
    assert not result["events"]
    assert result["references"][0]["lineage_id"] == result["references"][1]["lineage_id"]


def test_unanchored_different_action_is_ambiguous_even_at_identical_step_position():
    result = run(state(1, workflow(step())), state(2, workflow(step(action="owner/replacement"))))
    assert events(result) == ["ambiguous_transition"]
    assert result["conservation"]["unknown_exits"] == result["conservation"]["unknown_entries"] == 1


def test_rewrite_with_duplicate_actions_is_not_two_invented_pin_events():
    before = workflow(step(version="v3"), step(version="v4"))
    after = workflow(step(version=SHA, comment=" # v3"), step(version="b" * 40, comment=" # v4"))
    result = run(state(1, before), state(2, after))
    assert events(result) == ["ambiguous_transition"]
    assert result["conservation"]["unknown_entries"] == result["conservation"]["unknown_exits"] == 2


def test_topological_order_survives_committer_clock_inversion():
    result = run(state(20, workflow(step())), state(10, workflow(step(version=SHA))))
    assert events(result) == ["pin"]
    assert result["events"][0]["previous_commit"] == "commit-20"
    assert result["events"][0]["commit"] == "commit-10"


def test_source_replay_is_exactly_reproducible():
    snapshots = [state(1, workflow(step())), state(2, workflow(step(version=SHA)), files=BOT),
                 state(3, EMPTY), state(4, workflow(step(version=SHA, comment=" # v4")), files=BOT)]
    assert reconstruct("owner/repo", snapshots) == reconstruct("owner/repo", snapshots)


@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"), reason="The C base parser accepts inline expression tabs")
def test_inline_tab_accepted_by_base_parser_does_not_create_a_false_unknown_boundary():
    before = workflow(step()) + "\n"
    before = before.replace("    steps:\n", "    if: github.ref\t== 'refs/heads/main'\n    steps:\n")
    after = before.replace("actions/checkout@v4", "actions/checkout@" + SHA)
    result = run(state(1, before), state(2, after))
    assert events(result) == ["pin"]
    assert result["conservation"]["unknown_entries"] == result["conservation"]["unknown_exits"] == 0


def test_invalid_indentation_tab_is_unknown_and_does_not_bridge_a_pin_transition():
    broken = "jobs:\n\tbuild:\n    steps:\n      - uses: actions/checkout@v4\n"
    result = run(state(1, workflow(step())), state(2, broken), state(3, workflow(step(version=SHA))))
    assert events(result) == ["unknown_boundary", "unknown_boundary"]
    assert result["conservation"]["unknown_entries"] == result["conservation"]["unknown_exits"] == 1
