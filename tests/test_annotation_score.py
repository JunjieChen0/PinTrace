"""The independent-annotation scorer (tools/annotation/score.py) on synthetic answers."""

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools/annotation"))
SPEC = importlib.util.spec_from_file_location("score", ROOT / "tools/annotation/score.py")
score = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(score)

FIELDS = ["packet_id", "annotator", "position", "uid", "task", "answer", "confidence", "note", "updated_at_utc"]
KEY = {
    "i000000001": dict(sample="S1", task="classify_change", pipeline="pin"),
    "i000000002": dict(sample="S1", task="classify_change", pipeline="remove"),
    "i000000003": dict(sample="S2", task="classify_change", pipeline="sha_change"),
    "i000000004": dict(sample="S2", task="classify_reference", pipeline="pinned"),
}
A1 = {"i000000001": "pin", "i000000002": "remove", "i000000003": "sha_change", "i000000004": "pinned"}
A2 = {"i000000001": "pin", "i000000002": "unknown", "i000000003": "sha_change", "i000000004": "mutable"}


def answers(annotator, labels, step=30):
    rows = []
    for position, (item, label) in enumerate(labels.items(), start=1):
        second = position * step
        rows.append(dict(packet_id=f"pintrace-v2-{annotator}", annotator=annotator, position=position, uid=item,
                         task=KEY[item]["task"], answer=label, confidence="high", note="",
                         updated_at_utc=f"2026-11-09T01:{second // 60:02d}:{second % 60:02d}.000Z"))
    return rows


def write(path, rows, fields=FIELDS):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def both(tmp_path):
    first = score.read_answers(write(tmp_path / "a1.csv", answers("A1", A1)), "A1", KEY)
    second = score.read_answers(write(tmp_path / "a2.csv", answers("A2", A2)), "A2", KEY)
    return first, second


def test_kappa_matches_a_hand_worked_table():
    pairs = [("a", "a")] * 20 + [("a", "b")] * 5 + [("b", "a")] * 10 + [("b", "b")] * 15
    assert score.kappa(pairs) == pytest.approx(0.4)
    assert score.kappa([("a", "a")] * 3) is None and score.kappa([]) is None


def test_wilson_interval_matches_the_textbook_value():
    assert score.wilson(8, 10) == [0.4902, 0.9433]
    assert score.wilson(0, 0) is None


def test_valid_answers_are_read(tmp_path):
    first, _ = both(tmp_path)
    assert {item: row["answer"] for item, row in first.items()} == A1


@pytest.mark.parametrize("change, message", [
    (lambda rows: rows[:-1], "unanswered"),
    (lambda rows: rows + [rows[0]], "appears twice"),
    (lambda rows: [dict(rows[0], packet_id="pintrace-v2-A2")] + rows[1:], "is not A1's"),
    (lambda rows: [dict(rows[0], answer="pinned")] + rows[1:], "not a label of its task"),
    (lambda rows: [dict(rows[0], confidence="")] + rows[1:], "confidence"),
    (lambda rows: [dict(rows[0], uid="i0000000ff")] + rows[1:], "not in the sample"),
])
def test_bad_answers_are_rejected(tmp_path, change, message):
    with pytest.raises(ValueError, match=message):
        score.read_answers(write(tmp_path / "a1.csv", change(answers("A1", A1))), "A1", KEY)


def test_timing_flags_answers_entered_together(tmp_path):
    hand = score.read_answers(write(tmp_path / "h.csv", answers("A1", A1, step=30)), "A1", KEY)
    burst = score.read_answers(write(tmp_path / "b.csv", answers("A1", A1, step=0)), "A1", KEY)
    assert score.timing(hand)["median_gap_seconds"] == 30 and score.timing(hand)["gaps_under_2s"] == 0
    assert score.timing(burst)["gaps_under_2s"] == 3


def test_disagreement_sheet_is_blind_to_the_pipeline(tmp_path):
    sheet = score.disagreements(KEY, *both(tmp_path))
    assert [row["uid"] for row in sheet] == ["i000000002", "i000000004"]
    assert not any("pipeline" in field for field in sheet[0])


def test_consensus_must_settle_every_disagreement_and_keep_agreements(tmp_path):
    first, second = both(tmp_path)
    fields = ["uid", "consensus"]
    partial = write(tmp_path / "c1.csv", [dict(uid="i000000002", consensus="remove")], fields)
    with pytest.raises(ValueError, match="i000000004"):
        score.read_consensus(partial, KEY, first, second)
    overriding = write(tmp_path / "c2.csv", [dict(uid="i000000002", consensus="remove"),
                                             dict(uid="i000000004", consensus="pinned"),
                                             dict(uid="i000000001", consensus="unpin")], fields)
    with pytest.raises(ValueError, match="may not change it"):
        score.read_consensus(overriding, KEY, first, second)


def test_score_stops_before_reading_answers_when_the_freeze_moved(tmp_path, monkeypatch):
    monkeypatch.setattr(score.freeze, "verify", lambda stage: ["protocol:tools/annotation/codebook.md"])
    with pytest.raises(ValueError, match="no answer read"):
        score.score({"A2": tmp_path / "missing_a2.csv", "A3": tmp_path / "missing_a3.csv"}, None, private=tmp_path)


def test_score_end_to_end_reports_samples_and_tasks_apart(tmp_path, monkeypatch):
    monkeypatch.setattr(score.freeze, "verify", lambda stage: [])
    (tmp_path / "key.json").write_text(json.dumps(KEY), encoding="utf-8")
    a2, a3 = write(tmp_path / "a2.csv", answers("A2", A1)), write(tmp_path / "a3.csv", answers("A3", A2))
    consensus = write(tmp_path / "c.csv", [dict(uid="i000000002", consensus="unknown"),
                                           dict(uid="i000000004", consensus="pinned")], ["uid", "consensus"])
    report = score.score({"A2": a2, "A3": a3}, consensus, private=tmp_path)
    assert report["annotators"] == ["A2", "A3"]
    assert set(report["agreement"]) == {"S1/classify_change", "S2/classify_change", "S2/classify_reference"}
    assert report["agreement"]["S1/classify_change"]["raw"]["hits"] == 1 and report["disagreements"] == 2
    s1 = report["against_pipeline"]["S1/classify_change"]
    assert s1["accuracy"]["hits"] == 1 and s1["accuracy"]["n"] == 2
    assert s1["accuracy_decided"]["n"] == 1 and s1["human_coverage"]["hits"] == 1
    assert {"human": "unknown", "pipeline": "remove", "n": 1} in s1["confusion"]
    assert (tmp_path / "score/disagreements.csv").is_file() and (tmp_path / "score/score.json").is_file()
    header = (tmp_path / "score/disagreements.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "answer_A2" in header and "answer_A3" in header


def test_score_refuses_anything_but_two_annotators(tmp_path):
    with pytest.raises(ValueError, match="Exactly two"):
        score.score({"A2": tmp_path / "a2.csv"}, None, private=tmp_path)
