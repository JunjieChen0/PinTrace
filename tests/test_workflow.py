import pytest

from pintrace.workflow import action_counts, changes, classify_uses, parse_workflow

SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize("value,kind,ref_type", [
    ("actions/checkout@" + SHA, "action", "full_sha"),
    ("actions/checkout@" + SHA.upper(), "action", "full_sha"),
    ("actions/checkout@abcdef0", "action", "mutable_or_unresolved"),
    ("actions/checkout@v4", "action", "mutable_or_unresolved"),
    ("actions/checkout@main", "action", "mutable_or_unresolved"),
    ("./local/action", "local", None),
    ("docker://alpine:3", "docker", None),
    ("actions/checkout@${{ matrix.ref }}", "dynamic", None),
    ("actions/checkout", "unsupported", None),
])
def test_reference_categories(value, kind, ref_type):
    result = classify_uses(value)
    assert (result["kind"], result["ref_type"]) == (kind, ref_type)


def test_comments_scripts_and_reusable_workflows_are_not_direct_actions():
    text = """on: push
jobs:
  reusable:
    uses: org/repo/.github/workflows/build.yml@main
  build:
    runs-on: ubuntu-latest
    steps:
      # uses: tj-actions/changed-files@v45
      - run: 'echo "uses: tj-actions/changed-files@v45"'
      - uses: Actions/Checkout@v4
      - uses: owner/repo/subdir@v1
"""
    result = parse_workflow(text, ".github/workflows/main.yml")
    assert not result["errors"]
    assert action_counts(result["references"]) == {
        "actions/checkout": {"mutable_or_unresolved": 1},
        "owner/repo/subdir": {"mutable_or_unresolved": 1},
    }
    assert result["references"][0]["kind"] == "reusable_workflow"


def test_duplicate_uses_and_merge_keys_are_missing_data_not_clean_workflows():
    duplicate = "jobs:\n  build:\n    steps:\n      - uses: x/y@v1\n        uses: a/b@v2\n"
    merged = "defaults: &base\n  uses: x/y@v1\njobs:\n  build:\n    steps:\n      - <<: *base\n"
    for text in [duplicate, merged, "jobs: [broken"]:
        result = parse_workflow(text, "ci.yml")
        assert result["errors"]
        assert result["references"] == []


def test_yaml_anchors_count_each_step_and_do_not_loop():
    text = "jobs:\n  test:\n    steps:\n      - &checkout {uses: actions/checkout@v4}\n      - *checkout\n"
    parsed = parse_workflow(text, "ci.yml")
    assert not parsed["errors"]
    assert action_counts(parsed["references"])["actions/checkout"]["mutable_or_unresolved"] == 2


def test_deleting_unpinned_dependency_is_not_pinning_adoption():
    before = [classify_uses("actions/checkout@" + SHA), classify_uses("tj-actions/changed-files@v45")]
    after = before[:1]
    events = changes(before, after)
    assert events == [{"action": "tj-actions/changed-files", "event": "action_removed",
                       "before": {"mutable_or_unresolved": 1}, "after": {}}]


def test_reorder_or_move_does_not_create_adoption_or_removal():
    before = [classify_uses("actions/checkout@" + SHA), classify_uses("owner/repo@v1")]
    after = [dict(ref, path="renamed.yml", step_index=99) for ref in reversed(before)]
    assert changes(before, after) == []


def test_tag_update_and_sha_update_are_observable_without_count_changes():
    assert changes([classify_uses("tj-actions/changed-files@v45")],
                   [classify_uses("tj-actions/changed-files@v46")])[0]["event"] == "mutable_refs_changed"
    assert changes([classify_uses("owner/repo@" + SHA)],
                   [classify_uses("owner/repo@" + "a" * 40)])[0]["event"] == "sha_targets_changed"


def test_sha_letter_case_does_not_create_an_update():
    assert changes([classify_uses("owner/repo@" + SHA)],
                   [classify_uses("owner/repo@" + SHA.upper())]) == []


@pytest.mark.parametrize("text", ["", "  \n", "# uses: tj-actions/changed-files@v45\n"])
def test_empty_and_comment_only_files_have_no_active_references(text):
    result = parse_workflow(text, "empty.yml")
    assert result["inactive_empty_document"]
    assert result["references"] == []
    assert result["errors"] == []

