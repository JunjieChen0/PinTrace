import pytest

from pintrace import validity_golden as golden


@pytest.fixture(scope="module")
def evaluated():
    rows, digest = golden.evaluate()
    return rows, digest


def test_every_golden_scenario_and_parser_case_passes(evaluated):
    rows, _ = evaluated
    assert [row["name"] for row in rows if not row["passed"]] == []
    assert {row["kind"] for row in rows} == {"scenario", "parser"}
    assert len(rows) >= 60


def test_all_required_categories_have_scenarios(evaluated):
    rows, _ = evaluated
    present = {row["category"] for row in rows}
    assert set(golden.CATEGORIES) <= present


def test_scenario_names_are_unique_so_a_report_row_identifies_one_case(evaluated):
    names = [row["name"] for row in evaluated[0]]
    assert len(names) == len(set(names))


def test_evaluation_is_deterministic(evaluated):
    again_rows, again_digest = golden.evaluate()
    assert (again_rows, again_digest) == evaluated


def test_positive_controls_exist_so_a_rule_set_that_never_pins_cannot_pass():
    pins = [item for item in golden.scenarios() if item["name"].startswith("positive_control") and "pin" in item["events"]]
    assert pins, "a real pin must be part of the suite"


@pytest.mark.parametrize("name", sorted(golden.MUTANTS))
def test_each_deliberately_wrong_rule_is_caught(name):
    with golden.MUTANTS[name]():
        rows, _ = golden.evaluate()
    assert any(not row["passed"] for row in rows), f"mutant {name} survived the suite"


def test_the_judge_rejects_a_fabricated_pin_even_when_the_ledger_balances():
    item = next(i for i in golden.scenarios() if i["name"] == "comment_added_to_a_sha_is_not_an_event")
    honest = golden.replay(item["snapshots"])
    assert golden.judge(item, honest) == []
    forged = dict(honest, events=[dict(event="pin", commit="commit-2", previous_commit="commit-1")])
    problems = golden.judge(item, forged)
    assert any("fabricated pin" in problem for problem in problems) and any("events" in problem for problem in problems)


def test_the_judge_reports_a_ledger_that_does_not_balance():
    item = golden.scenarios()[0]
    result = golden.replay(item["snapshots"])
    broken = dict(result, conservation=dict(result["conservation"], passed=False, known_entries=3))
    problems = golden.judge(item, broken)
    assert any("does not balance" in problem for problem in problems) and any("known_entries" in problem for problem in problems)


def test_parser_judge_flags_a_rejection_that_never_happened_and_a_wrong_reference_set():
    swallow = lambda text, path: dict(golden.configuration.enrich_workflow(text, path), errors=[])
    assert golden.judge_parser("x", "jobs: [broken", dict(rejected=True)) == []
    assert golden.judge_parser("x", "jobs: [broken", dict(rejected=True), parser=swallow) == ["not rejected"]
    wrong = golden.judge_parser("x", "jobs:\n  b:\n    steps:\n      - uses: a/b@v1\n", dict(refs=[("a/b", "full_sha")]))
    assert wrong and "references" in wrong[0]
    assert golden.judge_parser("x", "", dict(inactive=True)) == []
    assert golden.judge_parser("x", "jobs: {}\n", dict(inactive=True))  # a real empty-jobs file is not an inactive document


def test_a_hash_in_a_comment_never_changes_a_reference():
    text = "jobs:\n  b:\n    steps:\n      - uses: a/b@v1 # " + "a" * 40 + "\n"
    refs = golden.configuration.enrich_workflow(text, golden.PATH)["references"]
    assert [(r["ref"], r["ref_type"]) for r in refs] == [("v1", "mutable_or_unresolved")]
