import json

import pytest
import yaml

from pintrace.configuration import CONFIG_PATHS, coverage_for, enrich_workflow, parse_configuration
from pintrace.workflow import classify_uses, parse_workflow

SHA = "0123456789abcdef0123456789abcdef01234567"
PATH = ".github/workflows/ci.yml"


def workflow(step):
    return "jobs:\n  build:\n    steps:\n" + step


def reference():
    return dict(classify_uses("actions/checkout@" + SHA), path=PATH)


def dependabot(extra="", directory="/", ecosystem="github-actions"):
    return {".github/dependabot.yml": "version: 2\nupdates:\n  - package-ecosystem: " + ecosystem +
            "\n    directory: '" + directory + "'\n    schedule: {interval: weekly}\n" + extra}


@pytest.mark.parametrize("comment,kind,version", [
    ("", "none", None),
    (" # v4.2.1", "semantic_version", "v4.2.1"),
    (" # 4.2.1 # explanatory words", "semantic_version", "4.2.1"),
    (" # version: v4.2.1", "semantic_version", "v4.2.1"),
    (" # security patch", "other", None),
    (" # ratchet:actions/checkout@v4", "ratchet", "v4"),
    (" # ratchet:exclude", "ratchet", None),
])
def test_real_uses_comments_are_distinguished_and_identity_unverified(comment, kind, version):
    text = workflow("      - uses: actions/checkout@" + SHA + comment + "\n")
    result = enrich_workflow(text, PATH)
    item = result["references"][0]
    assert (item["comment_kind"], item["comment_version"]) == (kind, version)
    assert item["comment_identity_state"] == ("unverified" if version else "none")
    assert item["step_context"] is None
    assert not result["errors"]
    assert all(item[k] == value for k, value in parse_workflow(text, PATH)["references"][0].items())


def test_hash_in_quoted_string_is_not_a_comment():
    text = workflow("      - name: 'checkout # v9'\n        uses: 'actions/checkout@v4#literal'\n")
    item = enrich_workflow(text, PATH)["references"][0]
    assert item["ref"] == "v4#literal"
    assert item["comment_kind"] == "none"
    assert item["comment_text"] is None
    assert item["step_name"] == "checkout # v9"


def test_script_and_prior_line_comments_do_not_become_reference_comments():
    text = workflow("      # uses: actions/checkout@" + SHA + " # v9\n"
                    "      - run: 'echo uses: actions/checkout@v4 # v8'\n"
                    "      - uses: actions/checkout@" + SHA + "\n"
                    "        name: 'checkout # v7'\n")
    result = enrich_workflow(text, PATH)
    assert len(result["references"]) == 1
    assert result["references"][0]["comment_kind"] == "none"


def test_flow_mapping_comment_and_multiple_references_on_one_line():
    text = workflow("      - {uses: actions/checkout@" + SHA + "} # v4\n")
    assert enrich_workflow(text, PATH)["references"][0]["comment_version"] == "v4"
    ambiguous = "jobs: {build: {steps: [{uses: actions/checkout@" + SHA + "}, {uses: a/b@v1}]}} # v9\n"
    for item in enrich_workflow(ambiguous, PATH)["references"]:
        assert item["comment_attachment_state"] == "ambiguous"
        assert item["comment_version"] is None


def test_comment_after_another_flow_field_is_not_assigned_to_uses():
    text = workflow("      - {uses: actions/checkout@" + SHA + ", name: checkout} # v4\n")
    item = enrich_workflow(text, PATH)["references"][0]
    assert item["comment_attachment_state"] == "ambiguous"
    assert item["comment_version"] is None


@pytest.mark.parametrize("scalar", [">-", "|-"])
def test_multiline_uses_retains_reference_without_guessing_comment_owner(scalar):
    text = workflow("      - uses: " + scalar + " # v4\n          actions/checkout@" + SHA + "\n")
    item = enrich_workflow(text, PATH)["references"][0]
    assert item["ref_type"] == "full_sha"
    assert item["comment_attachment_state"] == "multiline_ambiguous"
    assert item["comment_version"] is None


def test_aliases_keep_counts_but_do_not_reuse_anchor_comment_or_context():
    text = workflow("      - &checkout {name: Checkout, uses: actions/checkout@" + SHA + "} # v4\n"
                    "      - *checkout # v9\n")
    result = enrich_workflow(text, PATH)
    assert len(result["references"]) == 2
    for item in result["references"]:
        assert item["comment_attachment_state"] == "ambiguous"
        assert item["comment_version"] is None
        assert item["step_context"] is None


def test_context_survives_pinning_quoting_field_reorder_comment_and_file_moves():
    before = workflow("      - name: Checkout\n        uses: actions/checkout@v4\n        with: {fetch-depth: 0}\n")
    after = workflow("      - with: {fetch-depth: 0}\n        uses: actions/checkout@" + SHA + " # v4\n        name: 'Checkout'\n")
    old = enrich_workflow(before, PATH)["references"][0]
    new = enrich_workflow(after, ".github/workflows/renamed.yml")["references"][0]
    assert old["step_context"] == new["step_context"]
    assert old["step_context"] is not None


@pytest.mark.parametrize("text", ["jobs: [broken", "", "# only comment\n"])
def test_invalid_and_empty_workflows_preserve_original_states(text):
    result = enrich_workflow(text, PATH)
    original = parse_workflow(text, PATH)
    assert result["errors"] == original["errors"]
    assert result["references"] == original["references"]
    assert result.get("inactive_empty_document") == original.get("inactive_empty_document")


def test_no_local_configuration_is_not_evidence_of_no_bot():
    result = coverage_for(reference(), parse_configuration({}))
    assert result["status"] == "no_local_config_observed"
    assert result["inheritance_unknown"]
    assert result["global_configuration_unknown"]


def test_dependabot_root_ecosystem_coverage():
    config = parse_configuration(dependabot(), branch="main")
    result = coverage_for(reference(), config)
    assert result["status"] == "covered"
    assert result["by_tool"]["dependabot"]["status"] == "covered"
    assert result["scope"] == "local_configuration_intent"
    assert config["entries"][0]["data"]["updates"][0]["directory"] == "/"


@pytest.mark.parametrize("extra,expected", [
    ("    open-pull-requests-limit: 0\n", "explicitly_excluded_or_paused"),
    ("    ignore:\n      - dependency-name: actions/*\n", "explicitly_excluded_or_paused"),
    ("    ignore:\n      - dependency-name: owner/repo\n", "covered"),
    ("    ignore:\n      - dependency-name: actions/checkout\n        versions: ['4.*']\n", "unknown"),
    ("    allow:\n      - dependency-name: owner/repo\n", "explicitly_excluded_or_paused"),
    ("    allow:\n      - dependency-type: direct\n", "covered"),
    ("    target-branch: release\n", "explicitly_excluded_or_paused"),
    ("    exclude-paths: ['.github/**']\n", "unknown"),
])
def test_dependabot_pause_ignore_allow_branch_and_unsupported_path_rules(extra, expected):
    assert coverage_for(reference(), parse_configuration(dependabot(extra), branch="main"))["status"] == expected


def test_other_dependabot_ecosystem_and_wrong_directory_do_not_cover_actions():
    assert coverage_for(reference(), parse_configuration(dependabot(ecosystem="npm")))["status"] == "explicitly_excluded_or_paused"
    assert coverage_for(reference(), parse_configuration(dependabot(directory="/.github/workflows")))["status"] == "unknown"
    assert coverage_for(reference(), parse_configuration(dependabot("    target-branch: release\n")))["status"] == "unknown"


@pytest.mark.parametrize("text", ["version: 2\nupdates: [broken", "version: 2\nversion: 1\nupdates: []\n",
                                 "version: 2\nupdates: []\nbase: &b {x: 1}\nother: {<<: *b}\n"])
def test_invalid_dependabot_configuration_is_unknown(text):
    config = parse_configuration({".github/dependabot.yaml": text})
    assert config["errors"]
    assert coverage_for(reference(), config)["status"] == "unknown"


def test_subaction_dependency_name_uncertainty_is_preserved():
    item = dict(classify_uses("github/codeql-action/init@v3"), path=PATH)
    extra = "    ignore:\n      - dependency-name: github/codeql-action\n"
    assert coverage_for(item, parse_configuration(dependabot(extra)))["status"] == "unknown"


@pytest.mark.parametrize("data,expected", [
    ({}, "covered"),
    ({"enabled": False}, "explicitly_excluded_or_paused"),
    ({"enabledManagers": ["npm"]}, "explicitly_excluded_or_paused"),
    ({"enabledManagers": ["github-actions"]}, "covered"),
    ({"github-actions": {"enabled": False}}, "explicitly_excluded_or_paused"),
    ({"ignoreDeps": ["actions/checkout"]}, "explicitly_excluded_or_paused"),
    ({"ignorePaths": [".github/workflows/**"]}, "explicitly_excluded_or_paused"),
    ({"includePaths": [".github/workflows/**"]}, "covered"),
    ({"includePaths": ["docs/**"]}, "explicitly_excluded_or_paused"),
    ({"ignorePaths": ["**/*.yml"]}, "unknown"),
    ({"baseBranchPatterns": ["release/*"]}, "unknown"),
    ({"github-actions": {"enabled": []}}, "unknown"),
    ({"github-actions": {"enabled": 1}}, "unknown"),
    ({"packageRules": False}, "unknown"),
    ({"extends": ["config:recommended"]}, "unknown"),
    ({"packageRules": [{"matchPackageNames": ["actions/checkout"], "enabled": False}]}, "unknown"),
])
def test_renovate_local_coverage_rules(data, expected):
    config = parse_configuration({"renovate.json": json.dumps(data)}, branch="main")
    assert coverage_for(reference(), config)["status"] == expected


def test_json5_is_parsed_without_custom_parser_and_external_extends_stays_unknown():
    config = parse_configuration({".github/renovate.json5": "{ // comment\n extends: ['github:org/presets'],}"})
    entry = config["entries"][0]
    assert entry["parse_state"] == "parsed"
    assert entry["inheritance_unknown"]
    assert coverage_for(reference(), config)["status"] == "unknown"
    assert coverage_for(reference(), config)["inheritance_unknown"]


def test_comment_json_is_not_silently_assumed_compatible_with_every_historical_tool():
    config = parse_configuration({"renovate.json": '{ // comment\n "enabled": true}'})
    assert config["entries"][0]["parse_state"] == "parsed"
    assert config["entries"][0]["syntax_compatibility_unknown"]
    assert coverage_for(reference(), config)["status"] == "unknown"


def test_package_json_renovate_section_is_an_entry_but_plain_package_is_not():
    plain = parse_configuration({"package.json": '{"name": "sample"}'})
    assert plain["entries"] == []
    config = parse_configuration({"package.json": '{"name":"sample","renovate":{"enabled":false}}'})
    assert config["entries"][0]["data"] == {"enabled": False}
    assert coverage_for(reference(), config)["status"] == "explicitly_excluded_or_paused"
    assert "package.json" in CONFIG_PATHS


@pytest.mark.parametrize("files", [
    {"renovate.json": '{"enabled":false,"enabled":true}'},
    {"package.json": '{"renovate":{"enabled":false},"renovate":{"enabled":true}}'},
    {"renovate.json5": "[broken"},
    {"renovate.json": "{}", ".github/renovate.json": '{"enabled":false}'},
    {".github/dependabot.yml": "version: 2\nupdates: []", ".github/dependabot.yaml": "version: 2\nupdates: []"},
])
def test_invalid_or_multiple_entries_are_never_silently_resolved(files):
    assert coverage_for(reference(), parse_configuration(files))["status"] == "unknown"


def test_covered_dependabot_and_unresolved_renovate_are_reported_separately():
    config = parse_configuration({**dependabot(), "renovate.json": '{"extends":["local>org/presets"]}'})
    result = coverage_for(reference(), config)
    assert result["status"] == "covered"
    assert result["by_tool"]["renovate"]["status"] == "unknown"
    assert result["inheritance_unknown"]


def test_unknown_dependabot_schedule_does_not_claim_working_coverage():
    files = dependabot()
    files[".github/dependabot.yml"] = files[".github/dependabot.yml"].replace("weekly", "unsupported")
    assert coverage_for(reference(), parse_configuration(files))["status"] == "unknown"


def test_partial_workflow_references_still_have_explicit_unknown_annotation_fields():
    text = "jobs:\n  valid:\n    steps:\n      - uses: actions/checkout@" + SHA + " # v4\n  invalid: []\n"
    result = enrich_workflow(text, PATH)
    assert result["errors"]
    item = result["references"][0]
    assert item["comment_attachment_state"] == "ambiguous"
    assert item["context_state"] == "workflow_parse_incomplete"
    assert item["comment_version"] is None


def test_invalid_dependabot_allow_type_and_fractional_pr_limit_are_unknown():
    malformed = dependabot("    allow:\n      - dependency-type: {}\n")
    assert coverage_for(reference(), parse_configuration(malformed))["status"] == "unknown"
    fractional = dependabot("    open-pull-requests-limit: 0.0\n")
    assert coverage_for(reference(), parse_configuration(fractional))["status"] == "unknown"


@pytest.mark.skipif(not hasattr(yaml, "CSafeLoader"), reason="These tab separators are accepted by the C base parser")
@pytest.mark.parametrize("text,expected_ref", [
    ("on:\n  push:\t\n" + workflow("      - uses: actions/checkout@" + SHA + " # v4\n"), SHA),
    (workflow("      - uses: actions/checkout@" + SHA + " # v4\n        if: github.ref\t== 'refs/heads/main'\n"), SHA),
    (workflow("      - uses:\tactions/checkout@" + SHA + " # v4\n"), SHA),
    (workflow("      - uses: actions/checkout@" + SHA + "\t # v4\n"), SHA),
    (workflow("      - name: 'message # not a comment\ttext'\n        uses: 'actions/checkout@v4#literal'\t # v4\n"), "v4#literal"),
], ids=["mapping_trailing_tab", "plain_expression_inline_tab", "uses_separator_tab", "before_comment_tab", "quoted_hash_and_tab"])
def test_tab_scanning_agrees_with_base_parser_and_preserves_reference_fields(text, expected_ref):
    base = parse_workflow(text, PATH)
    assert base["errors"] == []
    assert len(base["references"]) == 1
    enriched = enrich_workflow(text, PATH)
    assert enriched["errors"] == []
    item = enriched["references"][0]
    assert all(item[key] == value for key, value in base["references"][0].items())
    assert item["ref"] == expected_ref
    assert item["comment_kind"] == "semantic_version"
    assert item["comment_text"] == "v4"
    assert item["comment_version"] == "v4"
    assert item["comment_attachment_state"] == "attached"


def test_indentation_tab_rejected_by_base_parser_remains_unknown():
    text = "jobs:\n\tbuild:\n    steps:\n      - uses: actions/checkout@" + SHA + "\n"
    base = parse_workflow(text, PATH)
    assert base["errors"]
    enriched = enrich_workflow(text, PATH)
    assert enriched["errors"] == base["errors"]
    assert enriched["references"] == base["references"] == []
