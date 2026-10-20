import json
import random

import pytest

from pintrace.control_baseline import activity, classify, eligible_controls, reference_profile
from pintrace.control_frame import baseline_workflow_profile, decile_edges, stratified_pool, stratum
from pintrace.control_match import balance, cell, mutable_class, quartile_edges, select, smd

DAY = 86400
ANCHOR = 1_000 * DAY
TARGET = "tj-actions/changed-files"


def reference(action, ref_type="mutable_or_unresolved", kind="action"):
    return dict(kind=kind, action=action, ref_type=ref_type)


def test_activity_counts_only_commits_before_the_anchor_inside_the_window():
    history = [dict(timestamp=ANCHOR + 5 * DAY), dict(timestamp=ANCHOR - DAY), dict(timestamp=ANCHOR - 179 * DAY), dict(timestamp=ANCHOR - 181 * DAY)]
    assert activity(history, ANCHOR, 180) == 2
    assert activity(history, ANCHOR, 365) == 3


def test_reference_profile_ignores_the_target_and_non_action_entries():
    refs = [reference("actions/checkout"), reference("actions/setup-node", "full_sha"), reference(TARGET),
            reference(None, kind="local"), reference("org/repo/.github/workflows/x.yml", kind="reusable_workflow")]
    result = reference_profile(refs)
    assert result == dict(action_references=2, mutable_references=1, mutable_share=0.5, target_references=1)


def test_classify_separates_parse_failure_target_presence_and_empty_baselines():
    assert classify(dict(complete=False, references=[])) == "parse_incomplete"
    assert classify(dict(complete=True, references=[reference(TARGET)])) == "target_present"
    assert classify(dict(complete=True, references=[])) == "no_action_references"
    assert classify(dict(complete=True, references=[reference("actions/checkout")])) == "control_eligible"


def test_baseline_profile_keeps_only_the_latest_live_state_per_history():
    rows = [dict(repository="A/b", uid="u1", committed_date=str(ANCHOR - 10 * DAY), commit_hash="c2", file_hash="a" * 64, file_path="p", valid_workflow="True"),
            dict(repository="A/b", uid="u1", committed_date=str(ANCHOR - 50 * DAY), commit_hash="c1", file_hash="b" * 64, file_path="p", valid_workflow="True"),
            dict(repository="A/b", uid="u2", committed_date=str(ANCHOR - 20 * DAY), commit_hash="c3", file_hash="", file_path="", valid_workflow="True"),
            dict(repository="A/b", uid="u1", committed_date=str(ANCHOR + DAY), commit_hash="c9", file_hash="c" * 64, file_path="p", valid_workflow="True")]
    live, recent = baseline_workflow_profile(rows, ANCHOR, 180)
    assert live == {"a/b": 1}
    assert recent == {"a/b": 3}


def test_stratified_pool_is_deterministic_and_reports_shortfalls_without_padding():
    roster = {f"r{i}": dict(commits=str(i)) for i in range(100)}
    edges = decile_edges(list(range(100)))
    assert len(edges) == 9 and stratum(0, edges) == 0 and stratum(99, edges) == 9
    names = sorted(roster)
    first = stratified_pool(names, roster, edges, 50, random.Random("s"))
    again = stratified_pool(names, roster, edges, 50, random.Random("s"))
    assert first == again
    pool, available, shortfall = first
    assert len(pool) == 50 and sum(available.values()) == 100 and shortfall == {}
    thin, _, short = stratified_pool(names[:12], roster, edges, 50, random.Random("s"))
    assert len(thin) <= 12 and short


def test_mutable_class_and_cell_use_the_treated_quartile_edges():
    assert [mutable_class(v) for v in (None, 0, 1, 0.4)] == ["none", "all_pinned", "all_mutable", "mixed"]
    edges = quartile_edges([1, 2, 3, 4, 5, 6, 7, 8])
    row = dict(git_commits_180d=7, mutable_share=1.0, owner_type="Organization")
    assert cell(row, edges) == (3, "all_mutable", "Organization")


def test_selection_reports_a_shortfall_for_cells_with_no_eligible_control():
    treated = [row(i, 100, 0.0) for i in range(10)]
    pool = [row(1000 + i, 100, 1.0) for i in range(50)]
    chosen, _, _, shortfall = select(treated, pool, 10)
    assert chosen == [] and sum(shortfall.values()) == 10


def test_smd_is_zero_for_identical_groups_and_scales_by_the_pooled_deviation():
    assert smd([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == 0.0
    assert smd([5.0, 5.0], [5.0, 5.0]) == 0.0
    # means 3 and 1, both sample variances 2, pooled deviation sqrt(2)
    assert smd([2.0, 4.0], [0.0, 2.0]) == pytest.approx(2.0 / 2.0 ** 0.5)
    assert smd([0.0, 2.0], [2.0, 4.0]) == pytest.approx(-2.0 / 2.0 ** 0.5)


def row(identity, commits, share, owner="Organization"):
    return dict(repository_id=str(identity), git_commits_180d=commits, git_commits_365d=commits * 2, mutable_share=share,
                owner_type=owner, action_references=4, baseline_workflow_files=2)


def test_selection_matches_cell_proportions_and_balance_improves_over_the_raw_pool():
    treated = [row(i, 50 + i % 7 * 20, 1.0 if i % 3 else 0.0) for i in range(60)]
    # Full cross of attributes, so every treated cell has candidates. Most of the raw pool is far less active than the
    # treated cohort and a minority overlaps its range, which gives the raw pool a large imbalance on activity.
    activities = [10 + k for k in range(8)] * 20 + [50 + k * 20 for k in range(7)] * 6
    pool = [row(1000 + n, commits, share, owner)
            for n, (commits, share, owner) in enumerate((c, s, o) for c in activities for s in (1.0, 0.0) for o in ("Organization", "User"))]
    chosen, edges, wanted, shortfall = select(treated, pool, 30)
    assert shortfall == {}
    assert len(chosen) <= 30 and len({r["repository_id"] for r in chosen}) == len(chosen)
    assert select(treated, pool, 30)[0] == chosen
    after, before = balance(treated, chosen), balance(treated, pool)
    assert abs(before["log1p_commits_180d"]) > 0.5
    assert abs(after["log1p_commits_180d"]) < 0.1 and abs(after["organization"]) < 0.1
    assert all(cell(r, edges) in wanted for r in chosen)


def write_baseline(root, name, identity, status="control_eligible", commit="c1"):
    directory = root / "data/derived/control-v1/baselines"
    directory.mkdir(parents=True, exist_ok=True)
    summary = dict(repository=name, repository_id=identity, status=status, baseline_commit=commit)
    (directory / (name.replace("/", "__") + ".json")).write_text(json.dumps(dict(summary=summary, baseline_snapshot=None)), encoding="utf-8")


def test_each_selected_id_yields_one_record_even_when_a_renamed_repository_has_two_names(tmp_path):
    write_baseline(tmp_path, "Old/name", "7", commit="c1")
    write_baseline(tmp_path, "new/Name", "7", commit="c1")
    write_baseline(tmp_path, "solo/repo", "8")
    write_baseline(tmp_path, "other/repo", "9")
    write_baseline(tmp_path, "bad/repo", "10", status="parse_incomplete")
    records = eligible_controls(tmp_path, [7, "8", "10"])
    assert [r["repository_id"] for r in records] == ["7", "8"]
    first = records[0]
    assert first["repository"] == "new/Name" and first["candidate_aliases"] == ["new/Name", "Old/name"]
    assert first["alias_baseline_conflict"] is False and records[1]["alias_baseline_conflict"] is False
    assert first["cohort_record_path"].endswith("new__Name.json") and len(first["cohort_record_sha256"]) == 64


def test_alias_records_with_different_baseline_commits_are_flagged(tmp_path):
    write_baseline(tmp_path, "a/one", "5", commit="c1")
    write_baseline(tmp_path, "b/two", "5", commit="c2")
    [record] = eligible_controls(tmp_path, ["5"])
    assert record["alias_baseline_conflict"] is True
