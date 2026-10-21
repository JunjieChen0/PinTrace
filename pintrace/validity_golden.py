"""Golden scenarios, parser adversarial cases and mutation checks for the measurement rules.

Three layers, each answering a different question:

* Golden scenarios replay small synthetic source histories through the same path the measurement uses (workflow
  parsing, configuration coverage, lineage reconstruction) and assert the exact event sequence and the instance ledger.
  Every scenario states how many ``pin`` events are legitimate; any other ``pin`` is a fabricated one. Positive controls
  (a real pin must still be seen) keep the suite from passing a rule set that never pins.
* Adversarial cases are workflow texts built to fool the parser: references that sit in comments or strings, hash-like
  refs of the wrong length, duplicate keys, merge keys, dynamic refs, non-string values, invalid or multi-document
  YAML. Each must either parse to exactly the stated references or be rejected (reported as an error, which makes the
  snapshot incomplete instead of a clean empty workflow).
* Mutation checks break one rule at a time (short SHAs accepted, position-only matching, timestamp ordering, parse
  errors swallowed, comments scanned as references, ...) and require the suite to fail for each. A suite that still
  passes under a deliberately wrong rule has no power against that error.

All inputs are synthetic and written by the researcher. Passing shows that the rules behave as intended on constructed
cases; it does not show that the rules are right for real histories, which is the job of the audited gold set.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import itertools
import json
from pathlib import Path
import re
import time
from unittest import mock

from . import configuration, lineage, workflow
from .formal_common import sha256, write_json

RULE_VERSION = "validity-golden-v1"
OUTPUT = "data/derived/validity-v1/runs"
SHA, OTHER = "a" * 40, "b" * 40
PATH = ".github/workflows/ci.yml"
EMPTY = "jobs:\n  build:\n    steps: []\n"
DEPENDABOT = {".github/dependabot.yml": "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    schedule: {interval: weekly}\n"}
RENOVATE_EXTENDS = {".github/renovate.json": '{"extends":["github:org/renovate-config"]}'}
CATEGORIES = ("reorder", "rename", "comment_only", "reintroduction", "invalid_yaml", "config_only", "inheritance",
              "first_parent_order", "ambiguous_rewrite")


def job(*steps, name="build"):
    return f"  {name}:\n    steps:\n" + "".join(steps)


def wf(*steps, job_name="build"):
    return "jobs:\n" + job(*steps, name=job_name)


def step(action="actions/checkout", version="v4", identifier=None, name=None, comment=""):
    return ("      - " + ("id: " + identifier + "\n        " if identifier else "") + "uses: " + action + "@" + version + comment + "\n"
            + ("        name: " + name + "\n" if name else ""))


def snap(text, config=None, path=PATH, **extra):
    """One snapshot spec; ``text`` None stands for an unresolved source marker, '' for a verified empty blob."""
    return dict(text=text, config=config or {}, path=path, extra=extra)


def build_state(number, spec):
    text, path = spec["text"], spec["path"]
    parsed = configuration.enrich_workflow(text, path) if text is not None else {"references": [], "errors": ["unresolved_source_marker"]}
    config = configuration.parse_configuration(spec["config"], branch="main")
    references = [dict(ref, configuration_coverage=configuration.coverage_for(ref, config)) for ref in parsed["references"]]
    state = dict(sha=f"commit-{number}", date=f"date-{number}", timestamp=number, complete=not parsed["errors"], configuration=config,
                 configuration_complete=not config["errors"], references=references)
    if text is not None:
        raw = text.encode("utf-8")
        state["files"] = [dict(path=path, blob=hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(), mode="100644", kind="blob")]
    state.update(spec["extra"])
    return state


def replay(specs):
    return lineage.reconstruct("owner/repo", [build_state(number, spec) for number, spec in enumerate(specs, 1)])


def scenario(name, category, snapshots, events, ledger=None, check=None, note=None):
    return dict(name=name, category=category, snapshots=snapshots, events=events, ledger=ledger or {}, check=check, note=note)


def same_lineage(result):
    ids = [ref["lineage_id"] for ref in result["references"]]
    return len(set(ids)) == 1


def scenarios():
    items = []
    for number, order in enumerate(itertools.permutations(range(3))):
        before = [step(version=SHA, identifier=f"checkout-{i}", name=f"Checkout-{i}") for i in range(3)]
        after = [step(version=SHA, identifier=f"checkout-{i}", name=f"Checkout-{i}", comment=" # v4") for i in order]
        items.append(scenario(f"reorder_identified_steps_permutation_{number}", "reorder", [snap(wf(*before)), snap(wf(*after))], [],
                              dict(known_entries=0, known_exits=0, unknown_entries=0, unknown_exits=0),
                              lambda r: len({x["lineage_id"] for x in r["references"]}) == 3))
    items += [
        scenario("reorder_unidentified_steps_in_a_changed_file_stays_unknown", "reorder",
                 [snap(wf(step(version="v4"), step(version="v3"))), snap(wf(step(version="v3"), step(version="v4")))],
                 ["ambiguous_transition"], dict(unknown_entries=2, unknown_exits=2, known_entries=0, known_exits=0),
                 note="no identity anchor and the file bytes changed: nothing may be guessed, in particular no update event"),
        scenario("identical_duplicate_steps_in_an_unchanged_file_keep_identity", "reorder",
                 [snap(wf(step(version=SHA), step(version=SHA))), snap(wf(step(version=SHA), step(version=SHA)), DEPENDABOT)],
                 [], dict(unknown_entries=0, unknown_exits=0),
                 lambda r: r["quality"].get("matching_rule_identical_workflow_blob_node") == 2,
                 note="identical blob at a known file identity proves the node did not move"),
        scenario("step_moved_to_another_job_is_unknown_not_removed_and_added", "reorder",
                 [snap(wf(step(version=SHA))), snap(wf(step(version=SHA), job_name="deploy"))],
                 ["ambiguous_transition"], dict(unknown_entries=1, unknown_exits=1, known_entries=0, known_exits=0)),
        scenario("rename_with_git_evidence_keeps_identity", "rename",
                 [snap(wf(step(identifier="checkout", version=SHA))),
                  snap("jobs: {build: {steps: [{id: checkout, uses: actions/checkout@" + SHA + "}]}}\n", path=".github/workflows/release.yml",
                       renames={PATH: ".github/workflows/release.yml"})],
                 [], dict(known_entries=0, known_exits=0, unknown_entries=0), same_lineage),
        scenario("rename_without_evidence_is_unknown_not_new_and_removed", "rename",
                 [snap(wf(step(version=SHA))), snap(wf(step(version=SHA)), path=".github/workflows/release.yml")],
                 ["ambiguous_transition"], dict(unknown_entries=1, unknown_exits=1, known_entries=0, known_exits=0)),
        scenario("comment_added_to_a_sha_is_not_an_event", "comment_only",
                 [snap(wf(step(version=SHA))), snap(wf(step(version=SHA, comment=" # v4")))], [], dict(known_entries=0, known_exits=0), same_lineage),
        scenario("comment_version_label_change_is_not_an_event", "comment_only",
                 [snap(wf(step(version=SHA, comment=" # v4"))), snap(wf(step(version=SHA, comment=" # v5")))], [], dict(known_entries=0),
                 lambda r: same_lineage(r) and r["references"][-1]["comment_identity_state"] == "unverified"),
        scenario("sha_inside_a_comment_does_not_pin_a_tag", "comment_only",
                 [snap(wf(step(version="v4"))), snap(wf(step(version="v4", comment=" # " + SHA)))], [], dict(known_entries=0),
                 lambda r: all(ref["ref_type"] == "mutable_or_unresolved" for ref in r["references"]),
                 note="a hash written in a comment is not a pin"),
        scenario("positive_control_tag_to_sha_is_a_pin", "comment_only",
                 [snap(wf(step(version="v4"))), snap(wf(step(version=SHA, comment=" # v4")))], ["pin"], dict(known_entries=0, known_exits=0), same_lineage,
                 note="the suite must still see a real pin"),
        scenario("positive_control_sha_to_tag_is_an_unpin", "comment_only",
                 [snap(wf(step(version=SHA))), snap(wf(step(version="v4")))], ["unpin"], dict(known_entries=0), same_lineage),
        scenario("positive_control_sha_to_other_sha", "comment_only",
                 [snap(wf(step(version=SHA))), snap(wf(step(version=OTHER)))], ["sha_target_change"], dict(known_entries=0), same_lineage),
        scenario("sha_letter_case_is_not_an_update", "comment_only",
                 [snap(wf(step(version=SHA))), snap(wf(step(version=SHA.upper())))], [], dict(known_entries=0), same_lineage),
        scenario("hash_of_39_characters_is_a_tag_update_not_a_pin", "comment_only",
                 [snap(wf(step(version="v4"))), snap(wf(step(version="a" * 39)))], ["mutable_update"], dict(known_entries=0),
                 lambda r: r["references"][-1]["ref_type"] == "mutable_or_unresolved"),
        scenario("pin_lookalike_on_a_different_action_is_unknown", "ambiguous_rewrite",
                 [snap(wf(step(version="v4"))), snap(wf(step(action="attacker/checkout", version=SHA)))],
                 ["ambiguous_transition"], dict(unknown_entries=1, unknown_exits=1, known_entries=0, known_exits=0)),
        scenario("removal_then_return_is_reintroduction", "reintroduction",
                 [snap(wf(step(identifier="checkout"))), snap(EMPTY), snap(wf(step(identifier="checkout")))],
                 ["remove", "reintroduced_action"], dict(known_entries=1, known_exits=1)),
        scenario("copying_a_step_is_not_a_first_introduction", "reintroduction",
                 [snap(EMPTY), snap(wf(step(identifier="one"))), snap(wf(step(identifier="one"), step(identifier="two")))],
                 ["first_observed_action", "copied_reference"], dict(known_entries=2), lambda r: r["quality"]["observed_action_groups"] == 1),
    ]
    broken = {"unresolved_marker": None, "unclosed_flow": "jobs: [broken", "tab_indent": "jobs:\n\tbuild:\n    steps:\n      - uses: actions/checkout@v4\n",
              "duplicate_key": "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n        uses: actions/checkout@" + SHA + "\n",
              "merge_key": "d: &b\n  uses: actions/checkout@v4\njobs:\n  build:\n    steps:\n      - <<: *b\n",
              "two_documents": wf(step()) + "---\n" + wf(step())}
    for label, text in broken.items():
        items.append(scenario(f"invalid_yaml_{label}_between_tag_and_sha_does_not_bridge_a_pin", "invalid_yaml",
                              [snap(wf(step())), snap(text), snap(wf(step(version=SHA)))], ["unknown_boundary", "unknown_boundary"],
                              dict(known_entries=0, known_exits=0, unknown_exits=1, unknown_entries=1),
                              lambda r: r["references"][0]["lineage_id"] != r["references"][-1]["lineage_id"]))
    items += [
        scenario("verified_empty_blob_is_a_removal_but_an_unresolved_marker_is_unknown", "invalid_yaml",
                 [snap(wf(step())), snap("")], ["remove"], dict(known_exits=1, unknown_exits=0),
                 lambda r: r["conservation"]["endpoint_known"] is True),
        scenario("configuration_only_change_is_not_a_reference_event", "config_only",
                 [snap(wf(step(version=SHA))), snap(wf(step(version=SHA)), DEPENDABOT), snap(wf(step(version=SHA)), DEPENDABOT)], [],
                 dict(known_entries=0, known_exits=0),
                 lambda r: same_lineage(r) and [x["configuration_coverage"]["status"] for x in r["references"]] == ["no_local_config_observed", "covered", "covered"]),
        scenario("configuration_removed_is_not_a_reference_event", "config_only",
                 [snap(wf(step(version=SHA)), DEPENDABOT), snap(wf(step(version=SHA)))], [], dict(known_entries=0, known_exits=0), same_lineage),
        scenario("inherited_renovate_configuration_is_unknown_not_absent", "inheritance",
                 [snap(wf(step(version=SHA))), snap(wf(step(version=SHA)), RENOVATE_EXTENDS)], [], dict(known_entries=0),
                 lambda r: r["references"][-1]["configuration_coverage"]["status"] == "unknown" and bool(r["references"][-1]["configuration_coverage"]["inheritance_unknown"])),
        scenario("clock_inversion_keeps_first_parent_order", "first_parent_order",
                 [snap(wf(step(version="v4")), timestamp=20), snap(wf(step(version=SHA)), timestamp=10)], ["pin"], dict(known_entries=0),
                 lambda r: r["events"][0]["previous_commit"] == "commit-1" and r["events"][0]["commit"] == "commit-2"),
        scenario("rewrite_of_duplicate_actions_is_not_two_invented_pins", "ambiguous_rewrite",
                 [snap(wf(step(version="v3"), step(version="v4"))), snap(wf(step(version=SHA, comment=" # v3"), step(version=OTHER, comment=" # v4")))],
                 ["ambiguous_transition"], dict(unknown_entries=2, unknown_exits=2, known_entries=0, known_exits=0)),
        scenario("different_action_at_the_same_position_is_unknown", "ambiguous_rewrite",
                 [snap(wf(step())), snap(wf(step(action="owner/replacement")))], ["ambiguous_transition"], dict(unknown_entries=1, unknown_exits=1)),
        scenario("reusable_workflow_pin_is_outside_the_measured_denominator", "ambiguous_rewrite",
                 [snap("jobs:\n  call:\n    uses: org/repo/.github/workflows/x.yml@v1\n"), snap("jobs:\n  call:\n    uses: org/repo/.github/workflows/x.yml@" + SHA + "\n")],
                 [], dict(initial_instances=0, final_instances=0)),
    ]
    return items


def parser_cases():
    """(name, text, expectation). Expectation: refs=[(action, ref_type)], rejected, or inactive."""
    lines = ["x0: &a0 [*l0, *l0, *l0, *l0, *l0, *l0, *l0, *l0, *l0]"]
    lines += [f"x{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 9) + "]" for i in range(1, 7)]
    expansion = "jobs:\n  a:\n    steps:\n      - &l0 {uses: actions/checkout@v4}\n" + "\n".join(lines) + "\n"
    mutable, full = "mutable_or_unresolved", "full_sha"
    steps = lambda body: "jobs:\n  b:\n    steps:\n" + body
    return [
        ("comment_line_is_not_a_reference", steps("      # - uses: tj-actions/changed-files@v45\n      - run: echo hi\n"), dict(refs=[])),
        ("quoted_run_string_is_not_a_reference", steps('      - run: "uses: tj-actions/changed-files@v45"\n'), dict(refs=[])),
        ("block_scalar_run_is_not_a_reference", steps("      - run: |\n          uses: tj-actions/changed-files@v45\n"), dict(refs=[])),
        ("uses_inside_a_name_is_not_a_reference", steps("      - name: 'uses: x/y@v1'\n        run: true\n"), dict(refs=[])),
        ("hash_of_39_characters_is_mutable", steps("      - uses: actions/checkout@" + "a" * 39 + "\n"), dict(refs=[("actions/checkout", mutable)])),
        ("hash_of_41_characters_is_mutable", steps("      - uses: actions/checkout@" + "a" * 41 + "\n"), dict(refs=[("actions/checkout", mutable)])),
        ("hash_of_64_characters_is_mutable", steps("      - uses: actions/checkout@" + "a" * 64 + "\n"), dict(refs=[("actions/checkout", mutable)])),
        ("forty_characters_with_a_non_hex_digit_is_mutable", steps("      - uses: actions/checkout@" + "a" * 39 + "g\n"), dict(refs=[("actions/checkout", mutable)])),
        ("uppercase_sha_is_a_full_sha", steps("      - uses: actions/checkout@" + "A" * 40 + "\n"), dict(refs=[("actions/checkout", full)])),
        ("sha_with_trailing_comment_is_a_full_sha", steps("      - uses: actions/checkout@" + SHA + " # v4\n"), dict(refs=[("actions/checkout", full)])),
        ("quoted_uses_is_read", steps('      - uses: "actions/checkout@v4"\n'), dict(refs=[("actions/checkout", mutable)])),
        ("uppercase_action_name_is_normalized", steps("      - uses: Actions/Checkout@v4\n"), dict(refs=[("actions/checkout", mutable)])),
        ("sub_path_action_keeps_its_path", steps("      - uses: owner/repo/sub/dir@v1\n"), dict(refs=[("owner/repo/sub/dir", mutable)])),
        ("local_and_docker_are_not_actions", steps("      - uses: ./local\n      - uses: docker://alpine:3\n"), dict(refs=[])),
        ("alias_is_counted_once_per_step", steps("      - &c {uses: actions/checkout@v4}\n      - *c\n"), dict(refs=[("actions/checkout", mutable)] * 2)),
        ("duplicate_uses_key_is_rejected", steps("      - uses: x/y@v1\n        uses: a/b@v2\n"), dict(rejected=True)),
        ("merge_key_is_rejected", "d: &b\n  uses: x/y@v1\njobs:\n  b:\n    steps:\n      - <<: *b\n", dict(rejected=True)),
        ("dynamic_ref_is_rejected", steps("      - uses: actions/checkout@${{ matrix.ref }}\n"), dict(rejected=True)),
        ("integer_uses_is_rejected", steps("      - uses: 123\n"), dict(rejected=True)),
        ("boolean_uses_is_rejected", steps("      - uses: true\n"), dict(rejected=True)),
        ("uses_without_a_ref_is_rejected", steps("      - uses: actions/checkout\n"), dict(rejected=True)),
        ("unclosed_flow_is_rejected", "jobs: [broken", dict(rejected=True)),
        ("tab_indentation_is_rejected", "jobs:\n\tbuild:\n    steps:\n", dict(rejected=True)),
        ("two_documents_are_rejected", steps("      - uses: a/b@v1\n") + "---\n" + steps("      - uses: a/b@v2\n"), dict(rejected=True)),
        ("jobs_missing_is_rejected", "name: ci\non: push\n", dict(rejected=True)),
        ("empty_jobs_is_rejected", "jobs: {}\n", dict(rejected=True)),
        ("non_mapping_step_is_rejected", steps("      - just a string\n"), dict(rejected=True)),
        ("empty_file_is_inactive_not_clean", "", dict(inactive=True)),
        ("comment_only_file_is_inactive_not_clean", "# uses: tj-actions/changed-files@v45\n", dict(inactive=True)),
        ("exponential_alias_expansion_finishes", expansion, dict(refs=None, seconds=5)),
    ]


def judge_parser(name, text, expect, parser=None):
    started = time.monotonic()
    result = (parser or configuration.enrich_workflow)(text, PATH)
    elapsed = time.monotonic() - started
    problems = []
    actions = [(ref["action"], ref["ref_type"]) for ref in result["references"] if ref["kind"] == "action"]
    if "refs" in expect:
        if expect["refs"] is not None and (result["errors"] or actions != expect["refs"]):
            problems.append(f"references {actions} errors {result['errors']} != {expect['refs']}")
        if expect.get("seconds") and elapsed > expect["seconds"]:
            problems.append(f"took {elapsed:.1f}s")
    if expect.get("rejected") and not result["errors"]:
        problems.append("not rejected")
    if expect.get("inactive") and not (result.get("inactive_empty_document") and not result["errors"] and not result["references"]):
        problems.append("not reported as an inactive empty document")
    return problems


def judge(item, result):
    problems = []
    conservation = result["conservation"]
    events = [event["event"] for event in result["events"]]
    if not conservation["passed"]:
        problems.append("conservation ledger does not balance")
    if events != item["events"]:
        problems.append(f"events {events} != {item['events']}")
    fabricated = events.count("pin") - item["events"].count("pin")
    if fabricated > 0:
        problems.append(f"{fabricated} fabricated pin event(s)")
    for key, value in item["ledger"].items():
        if conservation[key] != value:
            problems.append(f"ledger {key} {conservation[key]} != {value}")
    if item["check"] is not None and not item["check"](result):
        problems.append("structural check failed")
    return problems


def evaluate():
    """Run every scenario and parser case; return (rows, canonical results digest)."""
    rows, digest = [], hashlib.sha256()
    for item in scenarios():
        try:
            result = replay(item["snapshots"])
            problems = judge(item, result)
            digest.update(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        except Exception as error:  # a rule that raises on a constructed case fails the case
            problems = [f"raised {type(error).__name__}: {error}"]
        rows.append(dict(kind="scenario", name=item["name"], category=item["category"], passed=not problems, problems=problems, note=item["note"]))
    for name, text, expect in parser_cases():
        try:
            problems = judge_parser(name, text, expect)
        except Exception as error:
            problems = [f"raised {type(error).__name__}: {error}"]
        rows.append(dict(kind="parser", name=name, category="parser", passed=not problems, problems=problems, note=None))
    return rows, digest.hexdigest()


# Each mutant is a deliberately wrong rule. The suite must fail under every one of them.
@contextmanager
def short_hash_accepted():
    with mock.patch.object(workflow, "FULL_SHA", re.compile(r"[0-9a-fA-F]{39,40}\Z")):
        yield


@contextmanager
def sha_case_kept():
    original = workflow.classify_uses

    def patched(value, reusable=False):
        result = original(value, reusable)
        if result["ref_type"] == "full_sha":
            result["ref"] = value.rsplit("@", 1)[1]
        return result
    with mock.patch.object(workflow, "classify_uses", patched):
        yield


@contextmanager
def position_only_matching():
    def naive(old, new, renames, old_files=(), new_files=()):
        count = min(len(old), len(new))
        return [(i, i, "position") for i in range(count)], list(range(count, len(old))), list(range(count, len(new))), []
    with mock.patch.object(lineage, "_match", naive):
        yield


@contextmanager
def rename_evidence_ignored():
    original = lineage._match
    with mock.patch.object(lineage, "_match", lambda old, new, renames, old_files=(), new_files=(): original(old, new, {}, old_files, new_files)):
        yield


@contextmanager
def every_change_is_a_pin():
    with mock.patch.object(lineage, "_version_event", lambda old, new: "pin" if old.get("ref") != new.get("ref") else None):
        yield


@contextmanager
def clock_ordering():
    original = lineage.reconstruct
    with mock.patch.object(lineage, "reconstruct", lambda repository, snapshots: original(repository, sorted(snapshots, key=lambda s: s["timestamp"]))):
        yield


@contextmanager
def incomplete_treated_as_complete():
    original = lineage.reconstruct
    with mock.patch.object(lineage, "reconstruct", lambda repository, snapshots: original(repository, [dict(s, complete=True) for s in snapshots])):
        yield


@contextmanager
def parse_errors_swallowed():
    original = workflow.parse_workflow

    def patched(text, path):
        output = original(text, path)
        output["errors"] = []
        return output
    with mock.patch.object(configuration, "parse_workflow", patched):
        yield


@contextmanager
def references_scanned_by_regex():
    def patched(text, path):
        found = re.findall(r"uses:\s*([^\s#]+)", text)
        return {"path": path, "errors": [], "references": [dict(workflow.classify_uses(value), path=path, job="build", step_id=None, step_index=index, line=index + 1)
                                                         for index, value in enumerate(found)]}
    with mock.patch.object(configuration, "parse_workflow", patched):
        yield


@contextmanager
def configuration_change_ends_lineage():
    original = lineage._match

    def patched(old, new, renames, old_files=(), new_files=()):
        # only a same-size interval, so that adding or removing a step cannot trip the mutant by itself
        if len(old) == len(new) and [r.get("configuration_coverage") for r in old] != [r.get("configuration_coverage") for r in new]:
            return [], list(range(len(old))), list(range(len(new))), []
        return original(old, new, renames, old_files, new_files)
    with mock.patch.object(lineage, "_match", patched):
        yield


MUTANTS = {
    "short_hash_accepted_as_full_sha": short_hash_accepted,
    "sha_letter_case_kept": sha_case_kept,
    "position_only_matching": position_only_matching,
    "rename_evidence_ignored": rename_evidence_ignored,
    "every_ref_change_is_a_pin": every_change_is_a_pin,
    "snapshots_ordered_by_commit_clock": clock_ordering,
    "incomplete_snapshot_treated_as_complete": incomplete_treated_as_complete,
    "parse_errors_swallowed": parse_errors_swallowed,
    "references_found_by_text_scan": references_scanned_by_regex,
    "configuration_change_ends_lineage": configuration_change_ends_lineage,
}


def run_mutants():
    output = {}
    for name, mutant in MUTANTS.items():
        with mutant():
            rows, _ = evaluate()
        failed = [row["name"] for row in rows if not row["passed"]]
        output[name] = dict(killed=bool(failed), failing_cases=len(failed), first_failing=failed[:4])
    return output


def run(root):
    root = Path(root).resolve()
    rows, first = evaluate()
    again_rows, second = evaluate()
    mutants = run_mutants()
    categories = {c: dict(scenarios=sum(r["category"] == c for r in rows), passed=sum(r["category"] == c and r["passed"] for r in rows)) for c in CATEGORIES}
    sources = {name: sha256(root / name) for name in ("pintrace/lineage.py", "pintrace/workflow.py", "pintrace/configuration.py")}
    result = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), implementation_sha256=sha256(Path(__file__)),
        measured_rules=dict(lineage=lineage.RULES_VERSION, configuration=configuration.RULE_VERSION), source_sha256=sources,
        cases=dict(scenarios=sum(r["kind"] == "scenario" for r in rows), parser=sum(r["kind"] == "parser" for r in rows),
                   passed=sum(r["passed"] for r in rows), failed=[r["name"] for r in rows if not r["passed"]]),
        categories=categories, rows=rows, replay_digest_sha256=first, mutants=mutants,
        checks=dict(all_cases_pass=all(r["passed"] for r in rows), required_categories_covered=all(v["scenarios"] > 0 for v in categories.values()),
                    replay_deterministic=first == second and rows == again_rows,
                    every_mutant_killed=all(m["killed"] for m in mutants.values()),
                    positive_controls_present=any(r["name"].startswith("positive_control") for r in rows)),
        limits=["Inputs are synthetic and written by the researcher; passing shows the rules behave as intended on constructed cases, not that they are right for real histories.",
                "The mutants are a hand-picked set of single-rule errors, not an exhaustive mutation test.",
                "Adversarial cases cover the parser's rejection paths; a construct that the parser neither accepts nor rejects correctly and that is absent here is untested."])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / OUTPUT / stamp
    write_json(directory / "validity_golden.json", result)
    (root / "reports/validity_golden.json").write_bytes((directory / "validity_golden.json").read_bytes())
    print(json.dumps(dict(completed=True, checks=result["checks"], directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run(Path(__file__).resolve().parents[1])
