import hashlib
import json

import pytest

import pintrace.measurement as measurement


WORKFLOW = "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@" + "a" * 40 + " # v4\n"
DEPENDABOT = "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    schedule: {interval: weekly}\n"


def source(path, text):
    content = text.encode("utf-8")
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    return dict(path=path, blob=blob, mode="100644", kind="blob"), content


class FakeClient:
    def __init__(self, root, texts):
        self.root, self.texts = root, texts

    def blob(self, repository, commit, path, blob):
        content = self.texts[blob]
        target = self.root / "data/cache/blobs" / blob
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return content


def fixture_state(monkeypatch, root, config_text=None, config_path=".github/dependabot.yml"):
    workflow, raw = source(".github/workflows/ci.yml", WORKFLOW)
    config_sources, contents = [], {workflow["blob"]: raw}
    if config_text is not None:
        entry, raw_config = source(config_path, config_text)
        config_sources.append(entry)
        contents[entry["blob"]] = raw_config
    monkeypatch.setattr(measurement, "workflow_files", lambda *args: [dict(workflow)])
    monkeypatch.setattr(measurement, "configuration_files", lambda *args: [dict(item) for item in config_sources])
    point = dict(sha="frozen-commit", timestamp=100)
    client = FakeClient(root, contents)
    return point, client, workflow, config_sources


def test_select_points_includes_configuration_only_commits_and_clock_inversion():
    history = [dict(sha=f"c{i}", timestamp=clock) for i, clock in
               [(6, 140), (5, 120), (4, 170), (3, 110), (2, 80), (1, 60)]]
    config = dict(window_start="1970-01-01T00:01:40Z", window_end="1970-01-01T00:02:30Z")
    points, audit = measurement.select_points(history, {"c3", "c5"}, {"c4", "c5"}, config, "c3")
    assert [point["sha"] for point in points] == ["c2", "c3", "c4", "c5", "c6"]
    assert audit["configuration_only_commits"] == ["c4"]
    assert audit["included_clock_outside_window"] == ["c4"]
    assert audit["source_clock_inversions"] == 1


def test_select_points_rejects_baseline_outside_fixed_ancestry():
    with pytest.raises(ValueError, match="baseline"):
        measurement.select_points([dict(sha="head", timestamp=0)], set(), set(),
                                  dict(window_start="1970-01-01T00:00:01Z", window_end="1970-01-01T00:00:02Z"), "other")


def test_history_and_path_logs_use_fixed_sha_not_moving_head(monkeypatch):
    calls = []

    def git(directory, *args):
        calls.append(args)
        return b"10 parent\n20 child\n" if args[0] == "rev-list" else b"config-only\n"

    monkeypatch.setattr(measurement, "git", git)
    assert measurement.fixed_history("repo.git", "fixed-sha")[0] == dict(sha="parent", timestamp=10)
    assert measurement.modification_commits("repo.git", "fixed-sha", ("renovate.json",)) == {"config-only"}
    assert all("fixed-sha" in args and "HEAD" not in args for args in calls)


def test_state_attaches_comment_and_historical_local_configuration(monkeypatch, tmp_path):
    point, client, _, _ = fixture_state(monkeypatch, tmp_path, DEPENDABOT)
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    assert state["complete"] and state["configuration_complete"]
    reference = state["references"][0]
    assert reference["comment_kind"] == "semantic_version"
    assert reference["comment_identity_state"] == "unverified"
    assert reference["configuration_coverage"]["by_tool"]["dependabot"]["status"] == "covered"
    assert reference["configuration_snapshot_sha"] == point["sha"]
    assert reference["historical_default_branch_unknown"]


def test_failed_configuration_read_retains_workflow_and_marks_coverage_unknown(monkeypatch, tmp_path):
    point, client, _, configurations = fixture_state(monkeypatch, tmp_path, DEPENDABOT)
    client.texts.pop(configurations[0]["blob"])
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    assert state["complete"] and not state["configuration_complete"]
    assert len(state["references"]) == 1
    assert state["references"][0]["configuration_coverage"]["status"] == "unknown"
    assert state["references"][0]["configuration_coverage"]["reasons"] == ["configuration_tree_incomplete"]


def test_invalid_configuration_is_not_absent_and_does_not_erase_references(monkeypatch, tmp_path):
    point, client, _, _ = fixture_state(monkeypatch, tmp_path, "{ invalid", "renovate.json")
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    assert state["complete"] and not state["configuration_complete"]
    assert state["configuration_errors"]
    assert state["references"][0]["configuration_coverage"]["status"] == "unknown"


def test_inherited_configuration_remains_unknown_not_fetch_failure(monkeypatch, tmp_path):
    point, client, _, _ = fixture_state(monkeypatch, tmp_path, '{"extends": ["config:recommended"]}', "renovate.json")
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    assert state["complete"] and state["configuration_complete"]
    assert state["configuration"]["inheritance_unknown"]
    assert state["references"][0]["configuration_coverage"]["inheritance_unknown"]


def test_corrupt_git_blob_is_not_parsed_as_clean_workflow(monkeypatch, tmp_path):
    point, client, workflow, _ = fixture_state(monkeypatch, tmp_path)
    client.texts[workflow["blob"]] = b""
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    assert not state["complete"] and state["references"] == []
    assert "Git blob identity mismatch" in state["errors"][0]["errors"][0]


def test_unique_blob_rename_avoids_heuristic_diff(monkeypatch):
    def forbidden(*args):
        raise AssertionError("No diff required for unique equal blobs")

    monkeypatch.setattr(measurement, "git", forbidden)
    before = dict(sha="a", files=[dict(path="old.yml", blob="same")])
    after = dict(sha="b", files=[dict(path="new.yml", blob="same")])
    renames, evidence = measurement.workflow_renames("repo.git", before, after)
    assert renames == {"old.yml": "new.yml"}
    assert evidence[0]["method"] == "unique_identical_blob"


def test_changed_content_rename_retains_git_diff_score(monkeypatch):
    monkeypatch.setattr(measurement, "git", lambda *args: b"R090\0old.yml\0new.yml\0")
    before = dict(sha="a", files=[dict(path="old.yml", blob="old")])
    after = dict(sha="b", files=[dict(path="new.yml", blob="new")])
    renames, evidence = measurement.workflow_renames("repo.git", before, after)
    assert renames == {"old.yml": "new.yml"}
    assert evidence[0]["similarity"] == 90


def test_duplicate_identical_file_renames_are_not_forced(monkeypatch):
    monkeypatch.setattr(measurement, "git", lambda *args: b"R100\0a.yml\0c.yml\0R100\0b.yml\0d.yml\0")
    before = dict(sha="a", files=[dict(path=path, blob="same") for path in ("a.yml", "b.yml")])
    after = dict(sha="b", files=[dict(path=path, blob="same") for path in ("c.yml", "d.yml")])
    assert measurement.workflow_renames("repo.git", before, after) == ({}, [])


def test_source_replay_rebuilds_references_configuration_and_lineage(monkeypatch, tmp_path):
    point, client, _, _ = fixture_state(monkeypatch, tmp_path, DEPENDABOT)
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    state.update(renames={}, rename_evidence=[])
    detail = dict(summary=dict(git_directory="repo.git", canonical_repository="owner/repo", observed_default_branch="main"),
                  snapshots=[state], lineages=measurement.reconstruct("owner/repo", [state]))
    replay = measurement.replay_detail(detail, tmp_path)
    assert replay["passed"] and replay["source_states_checked"] == 1
    assert replay["required_blob_objects"] == replay["verified_blob_objects"] == 2
    state["references"][0]["ref"] = "incorrect"
    replay = measurement.replay_detail(detail, tmp_path)
    assert not replay["passed"]
    assert dict(commit=point["sha"], field="references") in replay["state_mismatches"]


def test_missing_cache_blob_blocks_source_replay(monkeypatch, tmp_path):
    point, client, workflow, _ = fixture_state(monkeypatch, tmp_path)
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    state.update(renames={}, rename_evidence=[])
    detail = dict(summary=dict(git_directory="repo.git", canonical_repository="owner/repo", observed_default_branch="main"),
                  snapshots=[state], lineages=measurement.reconstruct("owner/repo", [state]))
    (tmp_path / "data/cache/blobs" / workflow["blob"]).write_bytes(b"corrupted")
    replay = measurement.replay_detail(detail, tmp_path)
    assert not replay["passed"]
    assert replay["missing_or_mismatched_blobs"] == [workflow["blob"]]


def test_unavailable_git_tree_is_reported_as_replay_failure(monkeypatch, tmp_path):
    point, client, _, _ = fixture_state(monkeypatch, tmp_path)
    state = measurement.read_state(client, tmp_path, "owner/repo", point, "main", {})
    state.update(renames={}, rename_evidence=[])
    detail = dict(summary=dict(git_directory="repo.git", canonical_repository="owner/repo", observed_default_branch="main"),
                  snapshots=[state], lineages=measurement.reconstruct("owner/repo", [state]))

    def unavailable(*args):
        raise RuntimeError("Git tree object unavailable")

    monkeypatch.setattr(measurement, "workflow_files", unavailable)
    replay = measurement.replay_detail(detail, tmp_path)
    assert not replay["passed"]
    assert replay["state_mismatches"][0]["field"] == "source_tree_or_rename_read"
    assert replay["source_states_checked"] == 0


def test_confirmed_aliases_are_deduplicated_by_stable_id(monkeypatch, tmp_path):
    directory = tmp_path / "data/derived/cohort-v1/baselines"
    directory.mkdir(parents=True)
    for index, (name, identity, status, baseline) in enumerate([
            ("old/name", "10", "confirmed", "same"), ("new/name", "10", "confirmed", "same"),
            ("else/where", "20", "no_dependency_at_baseline", "other")]):
        (directory / f"{index}.json").write_text(json.dumps(dict(summary=dict(
            repository=name, repository_id=identity, status=status, baseline_commit=baseline))), encoding="utf-8")
    records = measurement.confirmed_records(tmp_path)
    assert len(records) == 1
    assert records[0]["candidate_aliases"] == ["new/name", "old/name"]
    assert not records[0]["alias_baseline_conflict"]


def test_conflicting_alias_baselines_block_measurement_before_any_git_call(monkeypatch, tmp_path):
    monkeypatch.setattr(measurement, "git", lambda *args: pytest.fail("Conflict must stop before Git"))
    record = dict(repository="a/b", repository_id="10", alias_baseline_conflict=True)
    result = measurement.collect(record, tmp_path, {}, "")
    assert result["status"] == "measurement_failed"
    assert "conflicting frozen baselines" in result["error"]


def test_v3_output_preserves_existing_v1_and_v2_records(monkeypatch, tmp_path):
    legacy = [tmp_path / "data/derived" / directory / "10.json" for directory in ("measurement-v1", "measurement-v2")]
    for path in legacy:
        path.parent.mkdir(parents=True)
        path.write_text("legacy " + path.parent.name + " preserved", encoding="utf-8")
    monkeypatch.setattr(measurement, "git", lambda *args: pytest.fail("Alias conflict must stop before Git"))
    result = measurement.collect(dict(repository="a/b", repository_id="10", alias_baseline_conflict=True), tmp_path, {}, "")
    assert result["measurement_rules_version"] == "measurement-v3"
    assert result["lineage_rules_version"] == "lineage-v2"
    assert all(path.read_text(encoding="utf-8") == "legacy " + path.parent.name + " preserved" for path in legacy)
    assert (tmp_path / "data/derived" / measurement.BATCH_DIRECTORY / "10.json").exists()
