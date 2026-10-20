import copy
import hashlib
import json

import pytest

import pintrace.measurement as measurement
from pintrace.lineage import reconstruct
import pintrace.verify_measurement as verify
from pintrace.workflow import classify_uses


def reference():
    return dict(classify_uses("actions/checkout@" + "a" * 40), path=".github/workflows/ci.yml", job="build",
                step_id=None, step_index=0, comment_kind="semantic_version", comment_identity_state="unverified",
                comment_attachment_state="attached", configuration_coverage={"status": "no_local_config_observed"},
                configuration_complete=True, configuration_snapshot_sha="one")


def state(sha="one", refs=None, complete=True):
    return dict(sha=sha, timestamp=1, date="1970-01-01T00:00:01Z", complete=complete,
                references=[reference()] if refs is None else refs, configuration_complete=True,
                configuration={"inheritance_unknown": True}, files=[], configuration_files=[], renames={})


def scope_fixture():
    rows = [dict(repository="one/repo", repository_id="1", status="confirmed"),
            dict(repository="gone/repo", status="inaccessible_unknown", http_status=404)]
    measurements = [dict(repository_id="1", status="measured")]
    return [row["repository"] for row in rows], rows, measurements


def test_scope_requires_equal_full_success_set_and_keeps_inaccessible_names():
    names, rows, measurements = scope_fixture()
    output = verify.audit_scope(names, rows, rows, measurements)
    assert output["passed"]
    assert output["candidate_names"] == 2
    assert output["confirmed_repository_ids"] == output["measured_repository_ids"] == ["1"]
    assert not output["identity_or_access_code_failures"]


@pytest.mark.parametrize("change", ["missing_measurement", "extra_measurement", "missing_candidate", "no_error_code", "stale_ledger"])
def test_partial_stale_and_out_of_scope_results_cannot_pass(change):
    names, rows, measurements = scope_fixture()
    flow = copy.deepcopy(rows)
    if change == "missing_measurement":
        measurements = []
    elif change == "extra_measurement":
        measurements.append(dict(repository_id="extra", status="measured"))
    elif change == "missing_candidate":
        rows = rows[:1]
    elif change == "no_error_code":
        rows[1].pop("http_status")
    elif change == "stale_ledger":
        flow[0]["status"] = "pending"
    assert not verify.audit_scope(names, flow, rows, measurements)["passed"]


def test_duplicate_names_and_duplicate_measurement_outputs_fail():
    names, rows, measurements = scope_fixture()
    assert not verify.audit_scope(names + [names[0]], rows, rows, measurements)["passed"]
    assert not verify.audit_scope(names, rows + [rows[0]], rows, measurements)["passed"]
    assert not verify.audit_scope(names, rows, rows, measurements + measurements)["passed"]


def test_independent_conservation_check_handles_ambiguous_and_unknown_boundaries():
    snapshots = [state(), state("two", [], False), state("three"), state("four", [])]
    lineages = reconstruct("owner/repo", snapshots)
    checked = verify.audit_conservation(lineages, snapshots)
    assert checked["passed"]
    assert checked["unknown_entries"] == checked["unknown_exits"] == 1
    assert checked["known_exits"] == 1


@pytest.mark.parametrize("corruption", ["global", "interval", "topology", "continued", "replacement"])
def test_stored_pass_flags_do_not_hide_inconsistent_instance_ledger(corruption):
    snapshots = [state(), state("two", [])]
    lineages = reconstruct("owner/repo", snapshots)
    if corruption == "global":
        lineages["conservation"]["known_exits"] = 0
    elif corruption == "interval":
        lineages["conservation"]["intervals"][0]["known_exits"] = 0
    elif corruption == "topology":
        lineages["conservation"]["intervals"][0]["previous_commit"] = "wrong"
    elif corruption == "continued":
        lineages["conservation"]["intervals"][0]["continued_instances"] = 10
    elif corruption == "replacement":
        lineages["conservation"]["intervals"][0]["replacement_entries"] = 1
    assert not verify.audit_conservation(lineages, snapshots)["passed"]


def test_sha_annotations_must_be_present_and_come_from_same_snapshot():
    snapshot = state()
    assert verify.audit_reference_fields([snapshot])["passed"]
    snapshot["references"][0]["configuration_snapshot_sha"] = "future"
    assert not verify.audit_reference_fields([snapshot])["passed"]
    snapshot["references"][0].pop("comment_kind")
    output = verify.audit_reference_fields([snapshot])
    assert "comment_kind" in output["missing_fields"][0]["missing"]


def test_incomplete_configuration_cannot_be_reported_covered():
    snapshot = state()
    snapshot["configuration_complete"] = False
    snapshot["references"][0]["configuration_coverage"] = {"status": "covered"}
    assert not verify.audit_reference_fields([snapshot])["passed"]
    snapshot["references"][0]["configuration_coverage"] = {"status": "unknown"}
    assert verify.audit_reference_fields([snapshot])["passed"]


def verify_fixture(monkeypatch, tmp_path):
    snapshot = state()
    record = dict(repository_id="1", repository="owner/repo", canonical_repository="owner/repo",
                  baseline_commit="one", observed_head="one", git_directory="repo.git", cohort_record_sha256="cohort")
    implementation = {"pintrace/measurement.py": "current"}
    detail = dict(summary=dict(record), snapshots=[snapshot], lineages=reconstruct("owner/repo", [snapshot]),
                  implementation_sha256=implementation)
    path = tmp_path / "data/derived" / verify.BATCH_DIRECTORY / "1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(detail), encoding="utf-8")
    monkeypatch.setattr(verify, "_source_state", lambda *args: "objects-unchanged")
    monkeypatch.setattr(verify, "fixed_history", lambda *args: [dict(sha="one", timestamp=1)])
    monkeypatch.setattr(verify, "modification_commits", lambda *args: {"one"})
    monkeypatch.setattr(verify, "select_points", lambda *args: ([dict(sha="one", timestamp=1)],
                                                             dict(required_configuration_commits=["one"], configuration_only_commits=[])))
    monkeypatch.setattr(verify, "_tree_objects", lambda *args: ([], []))
    monkeypatch.setattr(verify, "replay_detail", lambda *args: dict(passed=True))
    return path, record, implementation, detail


def test_repository_verification_checks_scope_tree_annotations_and_replay(monkeypatch, tmp_path):
    path, record, implementation, _ = verify_fixture(monkeypatch, tmp_path)
    result = verify.verify_one(tmp_path, path, record, implementation, {})
    assert result["passed"]
    assert all(result["checks"].values())
    assert result["quality"]["snapshots"] == 1


@pytest.mark.parametrize("corruption", ["implementation", "cohort", "tree", "replay", "scope"])
def test_any_frozen_input_or_replay_disagreement_closes_gate(monkeypatch, tmp_path, corruption):
    path, record, implementation, detail = verify_fixture(monkeypatch, tmp_path)
    if corruption == "implementation":
        detail["implementation_sha256"] = {"different": "hash"}
    elif corruption == "cohort":
        detail["summary"]["cohort_record_sha256"] = "different"
    elif corruption == "tree":
        monkeypatch.setattr(verify, "_tree_objects", lambda *args: ([dict(path="extra.yml", mode="100644", kind="blob", blob="extra")], []))
    elif corruption == "replay":
        monkeypatch.setattr(verify, "replay_detail", lambda *args: dict(passed=False))
    elif corruption == "scope":
        monkeypatch.setattr(verify, "select_points", lambda *args: ([dict(sha="extra", timestamp=2)],
                                                                 dict(required_configuration_commits=["extra"], configuration_only_commits=["extra"])))
    path.write_text(json.dumps(detail), encoding="utf-8")
    assert not verify.verify_one(tmp_path, path, record, implementation, {})["passed"]


def test_streamed_detail_equals_json_loads_for_every_section(monkeypatch, tmp_path):
    path, _, _, detail = verify_fixture(monkeypatch, tmp_path)
    whole = verify.load_detail(path)
    monkeypatch.setattr(verify, "LARGE_FILE_BYTES", 0)
    streamed = verify.load_detail(path)
    assert streamed == whole == json.loads(path.read_text(encoding="utf-8"))
    assert set(streamed) <= verify.DETAIL_KEYS and streamed["snapshots"] == detail["snapshots"]


def test_streamed_detail_without_a_summary_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "1.json"
    path.write_text(json.dumps({"snapshots": []}), encoding="utf-8")
    monkeypatch.setattr(verify, "LARGE_FILE_BYTES", 0)
    with pytest.raises(KeyError):
        verify.load_detail(path)


def test_summary_is_read_without_the_rest_of_the_file(tmp_path):
    path = tmp_path / "1.json"
    path.write_text('{"summary": {"repository_id": "1", "status": "measured"}, "snapshots": [', encoding="utf-8")
    assert verify.read_summary(path) == {"repository_id": "1", "status": "measured"}
    path.write_text('{"snapshots": []}', encoding="utf-8")
    with pytest.raises(KeyError):
        verify.read_summary(path)


def test_heavy_files_are_separated_from_the_worker_pool_and_ordered_by_size(monkeypatch, tmp_path):
    sizes = {"a": 10, "b": 5000, "c": 200, "d": 9000}
    paths = []
    for name, size in sizes.items():
        path = tmp_path / (name + ".json")
        path.write_bytes(b"x" * size)
        paths.append(path)
    monkeypatch.setattr(verify, "HEAVY_FILE_BYTES", 1000)
    ordinary, heavy = verify.split_by_weight(paths)
    assert [p.stem for p in ordinary] == ["a", "c"] and [p.stem for p in heavy] == ["b", "d"]


def test_streamed_repository_verification_reaches_the_same_result_as_the_whole_file_read(monkeypatch, tmp_path):
    path, record, implementation, _ = verify_fixture(monkeypatch, tmp_path)
    whole = verify.verify_one(tmp_path, path, record, implementation, {})
    (tmp_path / "data/derived" / verify.VERIFICATION_DIRECTORY / "1.json").unlink()
    monkeypatch.setattr(verify, "LARGE_FILE_BYTES", 0)
    streamed = verify.verify_one(tmp_path, path, record, implementation, {})
    assert not streamed["cache_used"]
    for key in ("passed", "checks", "quality", "errors", "conservation", "reference_fields", "cache_signature"):
        assert streamed[key] == whole[key], key


WORKFLOW_TEXT = "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@" + "a" * 40 + " # v4\n"


class BlobClient:
    def __init__(self, root, blob, content):
        self.root, self.blob_id, self.content = root, blob, content

    def blob(self, repository, commit, path, blob):
        target = self.root / "data/cache/blobs" / self.blob_id
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content)
        return self.content


def real_fixture(monkeypatch, tmp_path):
    """Two real snapshots read through measurement.read_state, with the lineage rebuilt from them and written to disk."""
    content = WORKFLOW_TEXT.encode("utf-8")
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    entry = dict(path=".github/workflows/ci.yml", blob=blob, mode="100644", kind="blob")
    monkeypatch.setattr(measurement, "workflow_files", lambda *args: [dict(entry)])
    monkeypatch.setattr(measurement, "configuration_files", lambda *args: [])
    client = BlobClient(tmp_path, blob, content)
    states = []
    for sha, stamp in (("one", 100), ("two", 200)):
        state = measurement.read_state(client, tmp_path, "owner/repo", dict(sha=sha, timestamp=stamp), "main", {})
        state.update(renames={}, rename_evidence=[])
        states.append(state)
    record = dict(repository_id="1", repository="owner/repo", canonical_repository="owner/repo", observed_default_branch="main",
                  baseline_commit="one", observed_head="two", git_directory="repo.git", cohort_record_sha256="cohort")
    implementation = {"pintrace/measurement.py": "current"}
    detail = dict(summary=dict(record), snapshots=states, implementation_sha256=implementation, timeline={"endpoint_commit": "two"},
                  lineages=reconstruct("owner/repo", states), replay={"passed": True})
    path = tmp_path / "data/derived" / verify.BATCH_DIRECTORY / "1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(detail, indent=2), encoding="utf-8")
    points = [dict(sha="one", timestamp=100), dict(sha="two", timestamp=200)]
    monkeypatch.setattr(verify, "_source_state", lambda *args: "objects-unchanged")
    monkeypatch.setattr(verify, "_source_state_of", lambda *args: "objects-unchanged")
    monkeypatch.setattr(verify, "fixed_history", lambda *args: [dict(sha="two", timestamp=200), dict(sha="one", timestamp=100)])
    monkeypatch.setattr(verify, "modification_commits", lambda *args: {"one", "two"})
    monkeypatch.setattr(verify, "select_points", lambda *args: (points, dict(required_configuration_commits=["one"], configuration_only_commits=[])))
    monkeypatch.setattr(verify, "_tree_objects", lambda *args: ([dict(entry)], []))
    return path, record, implementation, detail


def run_both_ways(monkeypatch, tmp_path, path, record, implementation):
    results = {}
    for mode, limit in (("whole", 2**60), ("streamed", 0)):
        output = tmp_path / "data/derived" / verify.VERIFICATION_DIRECTORY / "1.json"
        if output.exists():
            output.unlink()
        monkeypatch.setattr(verify, "STREAM_VERIFY_BYTES", limit)
        results[mode] = verify.verify_one(tmp_path, path, record, implementation, {})
    return results["whole"], results["streamed"]


COMPARED = ("passed", "checks", "quality", "errors", "conservation", "reference_fields", "replay", "cache_signature",
            "tree_mismatches", "configuration_only_commits", "missing_configuration_commits")


def test_snapshot_by_snapshot_verification_gives_the_same_row_as_the_whole_file_read(monkeypatch, tmp_path):
    path, record, implementation, _ = real_fixture(monkeypatch, tmp_path)
    whole, streamed = run_both_ways(monkeypatch, tmp_path, path, record, implementation)
    assert whole["passed"] and streamed["passed"]
    for key in COMPARED:
        assert streamed[key] == whole[key], key


@pytest.mark.parametrize("corruption", ["stored_reference", "stored_event", "stored_conservation", "stored_quality", "extra_lineage_key",
                                        "state_reference", "implementation", "tree", "missing_blob"])
def test_both_verification_paths_close_the_gate_for_the_same_corruption(monkeypatch, tmp_path, corruption):
    path, record, implementation, detail = real_fixture(monkeypatch, tmp_path)
    if corruption == "stored_reference":
        detail["lineages"]["references"][0]["ref"] = "changed"
    elif corruption == "stored_event":
        detail["lineages"]["events"].append(dict(event="remove", commit="two"))
    elif corruption == "stored_conservation":
        detail["lineages"]["conservation"]["known_exits"] = 5
    elif corruption == "stored_quality":
        detail["lineages"]["quality"]["history_has_unknown"] = True
    elif corruption == "extra_lineage_key":
        detail["lineages"]["unexpected"] = 1
    elif corruption == "state_reference":
        detail["snapshots"][1]["references"][0]["ref"] = "changed"
    elif corruption == "implementation":
        detail["implementation_sha256"] = {"different": "hash"}
    elif corruption == "tree":
        monkeypatch.setattr(verify, "_tree_objects", lambda *args: ([dict(path="extra.yml", mode="100644", kind="blob", blob="x")], []))
    elif corruption == "missing_blob":
        (tmp_path / "data/cache/blobs" / detail["snapshots"][0]["files"][0]["blob"]).write_bytes(b"corrupted")
    path.write_text(json.dumps(detail, indent=2), encoding="utf-8")
    whole, streamed = run_both_ways(monkeypatch, tmp_path, path, record, implementation)
    assert not whole["passed"] and not streamed["passed"]
    assert streamed["checks"] == whole["checks"]


def test_streamed_verification_reports_a_file_without_summary_as_a_failed_row(monkeypatch, tmp_path):
    path, record, implementation, detail = real_fixture(monkeypatch, tmp_path)
    detail.pop("summary")
    path.write_text(json.dumps(detail, indent=2), encoding="utf-8")
    whole, streamed = run_both_ways(monkeypatch, tmp_path, path, record, implementation)
    assert not whole["passed"] and not streamed["passed"] and streamed["errors"]


def test_cache_requires_same_measurement_implementation_cohort_and_source_state(monkeypatch, tmp_path):
    path, record, implementation, _ = verify_fixture(monkeypatch, tmp_path)
    assert verify.verify_one(tmp_path, path, record, implementation, {})["passed"]
    calls = []

    def replay(*args):
        calls.append(True)
        return dict(passed=True)

    monkeypatch.setattr(verify, "replay_detail", replay)
    cached = verify.verify_one(tmp_path, path, record, implementation, {})
    assert cached["cache_used"] and not calls
    monkeypatch.setattr(verify, "_source_state", lambda *args: "objects-modified")
    refreshed = verify.verify_one(tmp_path, path, record, implementation, {})
    assert not refreshed["cache_used"] and calls


def test_invalid_measurement_json_becomes_failed_check_not_process_crash(monkeypatch, tmp_path):
    path, record, implementation, _ = verify_fixture(monkeypatch, tmp_path)
    path.write_text("{ incomplete", encoding="utf-8")
    output = verify.verify_one(tmp_path, path, record, implementation, {})
    assert not output["passed"] and output["errors"]


def test_global_report_stays_pending_until_every_confirmed_id_verified():
    names, rows, measurements = scope_fixture()
    scope = verify.audit_scope(names, rows, rows, measurements)
    assert not verify.make_report(scope, [], expected_names=2)["gate_passed"]
    check = dict(repository_id="1", passed=True, quality={"snapshots": 2, "workflow_unknown_snapshots": 1,
                                                          "configuration_unknown_snapshots": 0, "complete_adjacent_intervals": 1,
                                                          "ambiguous_intervals": 1})
    report = verify.make_report(scope, [check], expected_names=2,
                                baseline_verification=dict(baseline_freeze_ready=True))
    assert report["gate_passed"]
    assert report["workflow_unknown_snapshot_rate"] == 0.5
    assert report["ambiguous_transition_rate"] == 1
    assert not verify.make_report(scope, [check], expected_names=580)["gate_passed"]


def test_missing_or_failed_baseline_verification_blocks_full_freeze(tmp_path):
    assert not verify.load_baseline_verification(tmp_path)["baseline_freeze_ready"]
    baseline = tmp_path / "data/derived/cohort-v1/baselines/one.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "data/derived/cohort-v1/verified_baseline_manifest.json"
    manifest.write_text(json.dumps({baseline.relative_to(tmp_path).as_posix(): verify.digest(baseline)}), encoding="utf-8")
    implementation = tmp_path / "pintrace/verify_cohort.py"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("# fixed verifier", encoding="utf-8")
    tie_implementation = tmp_path / "pintrace/ties.py"
    tie_implementation.write_text("# fixed ties", encoding="utf-8")
    tie_source = tmp_path / "data/derived/cohort-v1/tie_source_records.json"
    tie_source.write_text("{}", encoding="utf-8")
    path = tmp_path / "reports/cohort_verification.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(baseline_freeze_ready=True, completed_names=1,
                                   source_sha256={"implementation": verify.digest(implementation)})), encoding="utf-8")
    (tmp_path / "reports/tie_resolution_summary.json").write_text(json.dumps(dict(
        total=6, resolved=6, tie_unresolved=0, source_records_sha256=verify.digest(tie_source),
        implementation_sha256=verify.digest(tie_implementation))), encoding="utf-8")
    result = verify.load_baseline_verification(tmp_path)
    assert result["baseline_freeze_ready"] and result["report_sha256"] == verify.digest(path)
    baseline.write_text("changed", encoding="utf-8")
    assert not verify.load_baseline_verification(tmp_path)["baseline_freeze_ready"]
    names, rows, measurements = scope_fixture()
    scope = verify.audit_scope(names, rows, rows, measurements)
    check = dict(repository_id="1", passed=True, quality={})
    assert not verify.make_report(scope, [check], expected_names=2)["gate_passed"]
    assert not verify.make_report(scope, [check], expected_names=2,
                                  baseline_verification=dict(baseline_freeze_ready=False))["gate_passed"]
