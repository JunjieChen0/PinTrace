import copy
import json

import pytest

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
