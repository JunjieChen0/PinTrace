import json
import os
import subprocess

from pintrace import baseline
from pintrace.baseline import baseline_status, pre_event_point, summarize
from pintrace.github import GitHub
from pintrace.pilot import timestamp
from pintrace.workflow import TARGET


def test_baseline_excludes_anchor_and_respects_ancestry_over_timestamp_sort():
    history = [{"sha": "head", "timestamp": 200}, {"sha": "at-anchor", "timestamp": 100},
               {"sha": "nearest-ancestor", "timestamp": 80}, {"sha": "older-ancestor", "timestamp": 90}]
    assert pre_event_point(history, 100)["sha"] == "nearest-ancestor"
    assert pre_event_point(history, 50) is None


def test_incomplete_baseline_is_not_reported_as_absence():
    assert baseline_status({"complete": False, "counts": {}}) == "parse_incomplete"
    assert baseline_status({"complete": True, "counts": {}}) == "not_at_baseline"
    assert baseline_status({"complete": True, "counts": {TARGET: {"full_sha": 1}}}) == "confirmed"


def test_confirmed_repository_names_are_deduplicated_by_stable_id():
    records = [{"repository": "old/name", "repository_id": "123", "status": "confirmed"},
               {"repository": "new/name", "repository_id": "123", "status": "confirmed"},
               {"repository": "other/repo", "repository_id": "456", "status": "not_at_baseline"}]
    result = summarize(records)
    assert result["confirmed_unique_repository_ids"] == 1
    assert result["confirmed_identity_aliases"] == [["old/name", "new/name"]]


def test_collector_reads_pre_event_tree_instead_of_current_workflow(tmp_path, monkeypatch):
    directory = tmp_path / "git"
    directory.mkdir()

    def command(*args, date=None):
        env = dict(os.environ)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        return subprocess.run(["git", "-C", str(directory), "-c", "user.name=Test",
                               "-c", "user.email=test@example.invalid", *args],
                              capture_output=True, check=True, env=env).stdout.strip().decode()

    command("init", "-b", "main")
    workflow = directory / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    content = b"jobs: {a: {steps: [{uses: 'tj-actions/changed-files@v45'}]}}\n"
    workflow.write_bytes(content)
    command("add", ".github")
    command("commit", "-m", "before", date="2025-03-13T12:00:00Z")
    expected = command("rev-parse", "HEAD")
    blob = command("rev-parse", "HEAD:.github/workflows/ci.yml")
    cache = tmp_path / "data/cache/blobs"
    cache.mkdir(parents=True)
    (cache / blob).write_bytes(content)
    workflow.write_text("jobs: {a: {steps: [{uses: 'actions/checkout@v4'}]}}\n", encoding="utf-8")
    command("add", ".github")
    command("commit", "-m", "after", date="2025-03-14T12:00:00Z")
    monkeypatch.setattr(baseline, "clone", lambda repository, destination: directory)
    monkeypatch.setattr(GitHub, "api", lambda self, endpoint: {
        "full_name": "renamed/project", "id": 123, "default_branch": "main",
        "archived": False, "fork": False, "private": False})
    row = {"repository": "owner/project", "source_frames": '["zenodo:18414913"]',
           "source_family": "zenodo:10259013"}
    result = baseline.collect(row, tmp_path, timestamp("2025-03-14T00:00:00Z"), "unused-test-token")
    assert result["status"] == "confirmed"
    assert result["repository_id"] == "123"
    assert result["canonical_repository"] == "renamed/project"
    assert result["baseline_commit"] == expected
    assert result["target_mutable"] == 1
    saved = json.loads((tmp_path / "data/derived/baseline-jss-extra-v1/owner__project.json")
                       .read_text(encoding="utf-8"))
    assert saved["baseline_snapshot"]["references"][0]["action"] == TARGET
