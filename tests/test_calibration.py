import hashlib

from pintrace.calibration import replay_interval, replay_snapshot, select_examples
from pintrace.workflow import action_counts, parse_workflow


def snapshot(sha, complete=True):
    return {"sha": sha, "complete": complete}


def detail(repo, snapshots, events):
    return {"summary": {"repository": repo}, "snapshots": snapshots, "events": events}


def event(before, after):
    return {"previous_commit": before, "commit": after, "event": "action_added", "action": "owner/action"}


def test_calibration_excludes_gaps_and_nonadjacent_event_pairs():
    result, _ = select_examples([detail("a/repo", [snapshot("a"), snapshot("b", False), snapshot("c")],
                                      [event("a", "c")])], "seed")
    assert result == []


def test_selection_is_reproducible_when_input_order_changes():
    details = [detail(f"r/repo{i}", [snapshot("a"), snapshot("b")], [event("a", "b")]) for i in range(12)]
    first, _ = select_examples(details, "seed")
    second, _ = select_examples(list(reversed(details)), "seed")
    assert [item["repository"] for item in first] == [item["repository"] for item in second]
    assert len(first) == 8


def test_no_reported_event_is_only_an_automatic_stratum():
    result, _ = select_examples([detail("a/repo", [snapshot("a"), snapshot("b")], [])], "seed")
    assert result[0]["hint"] == "no_reported_event"
    assert "labels" not in result[0]


def cached_snapshot(directory, text):
    content = text.encode()
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    (directory / blob).write_bytes(content)
    references = parse_workflow(text, "ci.yml")["references"]
    return {"complete": True, "references": references, "counts": action_counts(references),
            "files": [{"path": "ci.yml", "blob": blob, "kind": "blob", "mode": "100644"}]}


def test_replay_checks_original_blob_identity_before_parsing(tmp_path):
    saved = cached_snapshot(tmp_path, "jobs: {a: {steps: [{uses: 'a/b@v1'}]}}")
    (tmp_path / saved["files"][0]["blob"]).write_text("changed content", encoding="utf-8")
    result = replay_snapshot(saved, tmp_path)
    assert result["status"] == "failed"
    assert result["problems"][0]["reason"] == "blob_identity_mismatch"


def test_missing_blob_is_unknown_instead_of_zero_references(tmp_path):
    saved = {"files": [{"path": "ci.yml", "blob": "a" * 40, "kind": "blob", "mode": "100644"}]}
    assert replay_snapshot(saved, tmp_path)["status"] == "unknown"


def test_replay_detects_missing_saved_event(tmp_path):
    before = cached_snapshot(tmp_path, "jobs: {a: {steps: [{uses: 'a/b@v1'}]}}")
    after = cached_snapshot(tmp_path, "jobs: {a: {steps: []}}")
    result = replay_interval({"before": before, "after": after}, [], tmp_path)
    assert result["status"] == "failed"
    assert result["events_match_saved_output"] is False
    expected = [{"action": "a/b", "event": "action_removed",
                 "before": {"mutable_or_unresolved": 1}, "after": {}}]
    assert replay_interval({"before": before, "after": after}, expected, tmp_path)["status"] == "passed"
