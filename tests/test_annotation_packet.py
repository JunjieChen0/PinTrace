"""Pure helpers of the independent-annotator packet builder (tools/annotation/make_packet.py)."""

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("make_packet", ROOT / "tools/annotation/make_packet.py")
packet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packet)


def test_uid_is_stable_opaque_and_distinct():
    first, again, other = packet.uid("e1/event"), packet.uid("e1/event"), packet.uid("e2/event")
    assert first == again and first != other
    assert re.fullmatch(r"i[0-9a-f]{9}", first)


def test_focus_keeps_only_what_a_reader_sees():
    ref = dict(path="a.yml", line=3, job="j", step_index=0, step_name="s", value="actions/checkout@v4",
               comment_text="v4", kind="action", ref_type="mutable_or_unresolved", lineage_id="rl-1", lineage_status="matched")
    shown = packet.focus(ref)
    assert shown["value"] == "actions/checkout@v4" and shown["comment"] == "v4"
    assert not {"kind", "ref_type", "lineage_id", "lineage_status"} & set(shown)


def test_excerpt_window_marks_the_focus_line_and_rejects_bad_lines():
    lines = [f"l{i}" for i in range(1, 21)]
    window = packet.excerpt(lines, 3, context=2)
    assert window == dict(start=1, focus=3, lines=["l1", "l2", "l3", "l4", "l5"])
    assert packet.excerpt(lines, 20, context=2)["lines"] == ["l18", "l19", "l20"]
    assert packet.excerpt(lines, 0) is None and packet.excerpt(lines, 21) is None and packet.excerpt([], 1) is None


def test_diff_rows_number_old_and_new_lines():
    result = packet.diff_rows(["a", "uses: x@v1", "c"], ["a", "uses: x@v2", "c"], context=1)
    kinds = [(row["kind"], row.get("old"), row.get("new")) for row in result["rows"]]
    assert kinds == [("hunk", None, None), ("same", 1, 1), ("del", 2, None), ("add", None, 2), ("same", 3, 3)]
    assert result["truncated"] == 0
    assert packet.diff_rows(["a"], ["a"]) == dict(rows=[], truncated=0)


def test_long_diff_keeps_the_hunk_with_the_focus_line_first():
    before = [f"line {i}" for i in range(200)]
    after = list(before)
    for i in range(0, 200, 10):  # twenty separate hunks
        after[i] = before[i] + " changed"
    result = packet.diff_rows(before, after, context=1, limit=12, focus_new=[191])
    shown_new = {row.get("new") for row in result["rows"]}
    assert 191 in shown_new
    assert result["truncated"] > 0
    assert sum(1 for row in result["rows"] if row["kind"] != "hunk") + result["truncated"] >= 20


def test_file_pairs_follow_renames_and_one_sided_files():
    before = [dict(path="old.yml")]
    after = [dict(path="new.yml")]
    exists_before, exists_after = {"old.yml"}.__contains__, {"new.yml"}.__contains__
    assert packet.file_pairs(before, after, exists_before, exists_after) == [("old.yml", "new.yml")]
    assert packet.file_pairs([], after, set().__contains__, exists_after) == [(None, "new.yml")]
    assert packet.file_pairs(before, [], exists_before, set().__contains__) == [("old.yml", None)]
    same = [dict(path="w.yml")]
    assert packet.file_pairs(same, same, {"w.yml"}.__contains__, {"w.yml"}.__contains__) == [("w.yml", "w.yml")]


def test_pick_takes_ranked_accepted_entries_and_counts_rejections():
    chosen, skipped = packet.pick(list(range(10)), 3, lambda n: None if n % 2 else "even")
    assert chosen == [1, 3, 5]
    assert skipped == {"even": 3}


def test_orders_differ_between_annotators_but_hold_the_same_items():
    items = [dict(uid=packet.uid(str(i))) for i in range(50)]
    first, second = packet.order(items, "A1"), packet.order(items, "A2")
    assert sorted(i["uid"] for i in first) == sorted(i["uid"] for i in second)
    assert [i["uid"] for i in first] != [i["uid"] for i in second]
    assert packet.order(items, "A1") == first


def test_script_json_cannot_close_the_script_element():
    value = {"text": "</script><script>alert(1)</script><!-- x"}
    encoded = packet.script_json(value)
    assert "</" not in encoded and "<!--" not in encoded
    assert json.loads(encoded) == value


def test_render_hides_sample_membership_and_labels():
    template = (ROOT / "tools/annotation/template.html").read_text(encoding="utf-8")
    item = dict(uid="i000000001", task="classify_reference", repository="o/r", links={}, sample="S1",
                reference=dict(path="a.yml", line=1, value="o/r@v1", comment=None, excerpt=None, url=None))
    page = packet.render(template, "pid", "A1", [item], practice=False)
    data = json.loads(re.search(r'<script id="packet" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert "sample" not in data["items"][0]
    assert "{{" not in page
    assert set(data["tasks"]["classify_change"]["labels"]) >= {"mixed", "no_change", "unknown"}


def test_pool_keeps_the_best_ranked_entries_in_order():
    pool = packet.Pool(3)
    for key in ("d", "a", "e", "c", "b"):
        pool.offer(key, key.upper())
    assert pool.ranked() == ["A", "B", "C"]
    assert pool.count == 5


FREEZE_SPEC = importlib.util.spec_from_file_location("annotation_freeze", ROOT / "tools/annotation/freeze.py")
freeze = importlib.util.module_from_spec(FREEZE_SPEC)
FREEZE_SPEC.loader.exec_module(freeze)


def test_freeze_compare_reports_changed_added_and_removed_files():
    recorded = {"sample": {"a": "1", "b": "2"}}
    current = {"sample": {"a": "1", "b": "3", "c": "4"}, "protocol": {}}
    assert freeze.compare(recorded, current) == ["sample:b", "sample:c"]
    assert freeze.compare(recorded, recorded) == []


def test_freeze_hashes_refuse_a_missing_input(tmp_path):
    (tmp_path / "x.txt").write_text("x", encoding="utf-8")
    assert set(freeze.hashes(tmp_path, {"g": ["x.txt"]})["g"]) == {"x.txt"}
    try:
        freeze.hashes(tmp_path, {"g": ["missing.txt"]})
    except FileNotFoundError as error:
        assert "missing.txt" in str(error)
    else:
        raise AssertionError("a missing freeze input must raise")


def test_later_stages_add_protocol_files_and_keep_the_rest():
    first, second = freeze.groups_for("freeze-1"), freeze.groups_for("freeze-2")
    for group in freeze.FIXED_GROUPS:
        assert first[group] == second[group]
    assert set(first["protocol"]) < set(second["protocol"])
    assert "tools/annotation/practice_explanation.md" in second["protocol"]
