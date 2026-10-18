import copy
import csv
import json
from collections import Counter

import pytest

import pintrace.tables as tables
from pintrace.tables import event_lineages, measurement_rows


def test_ambiguous_event_retains_all_input_and_output_lineages():
    assert event_lineages([{"lineage_id": "a"}, {"lineage_id": "b"}, {"lineage_id": None}]) == ["a", "b"]


def test_reference_table_preserves_per_snapshot_annotations_and_lineage():
    reference = {"kind": "action", "action": "a/b", "ref": "a" * 40, "ref_type": "full_sha",
                 "path": "ci.yml", "job": "test", "step_index": 0, "line": 3,
                 "comment_kind": "none", "comment_identity_state": "none",
                 "configuration_coverage": {"status": "unknown"}}
    traced = dict(reference, snapshot_sha="commit", lineage_id="lineage", lineage_status="baseline")
    detail = {"summary": {"repository_id": "123"},
              "snapshots": [{"sha": "commit", "date": "2025-03-01", "complete": True,
                             "configuration_complete": True, "configuration": {"entries": []},
                             "references": [reference]}],
              "lineages": {"references": [traced], "events": [], "conservation": {"passed": True}, "quality": {}},
              "replay": {"passed": True}, "timeline": {"configuration_commit_coverage_passed": True}}
    references = [row for name, row in measurement_rows(detail) if name == "references"]
    assert references[0]["lineage_id"] == "lineage"
    assert references[0]["configuration_coverage"]["status"] == "unknown"


def reference(index=0):
    return dict(kind="action", action="a/b", repository="a/b", ref="a" * 40, ref_type="full_sha",
                path=".github/workflows/ci.yml", job="build", step_index=index, line=index + 3,
                comment_kind="semantic_version", comment_version="v1", comment_identity_state="unverified",
                configuration_coverage={"status": "unknown"})


def detail(refs=None, lineage_ids=None):
    refs = refs if refs is not None else [reference()]
    lineage_ids = lineage_ids if lineage_ids is not None else ["lineage-" + str(i) for i in range(len(refs))]
    return dict(summary=dict(repository_id="123", repository="owner/repo", status="measured",
                             measurement_rules_version=tables.MEASUREMENT_RULES_VERSION),
                snapshots=[dict(sha="commit", date="2025-03-01", complete=True, configuration_complete=True,
                                configuration={"entries": []}, references=refs)],
                lineages=dict(references=[dict(ref, snapshot_sha="commit", lineage_id=identity, lineage_status="matched")
                                         for ref, identity in zip(refs, lineage_ids)], events=[], conservation={"passed": True}, quality={}),
                replay={"passed": True}, timeline={"configuration_commit_coverage_passed": True})


def evidence(ref=None, identity="lineage-0", status="verified_consistent"):
    ref = ref or reference()
    return dict(repository_id="123", snapshot_sha="commit", **{key: ref[key] for key in
                ("path", "job", "step_index", "line", "action", "ref")}, sha=ref["ref"], lineage_id=identity,
                comment_identity_state=status, identity_evidence_grade="A", identity_source_urls=["https://example.org/evidence"],
                identity_reason="literal_tag_immutable_binding_published_by_snapshot")


def reference_rows(payload, evidence_rows):
    index = tables.index_evidence(evidence_rows)
    audit = Counter()
    rows = [row for name, row in tables.measurement_rows(payload, index, audit) if name == "references"]
    return rows, audit, index


def test_complete_locators_distinguish_repeated_references_with_null_lineages():
    first, second = reference(), reference(1)
    rows, audit, _ = reference_rows(detail([first, second], [None, None]), [
        evidence(first, None, "verified_consistent"), evidence(second, None, "verified_conflict")])
    assert [row["lineage_id"] for row in rows] == [None, None]
    assert [row["comment_identity_state"] for row in rows] == ["verified_consistent", "verified_conflict"]
    assert audit["identity_matched_observations"] == 2
    assert audit["identity_locator_conflicts"] == 0


def test_duplicate_evidence_locators_remain_unverified_with_all_conflicts_preserved():
    rows, audit, _ = reference_rows(detail(), [evidence(status="verified_consistent"), evidence(status="verified_conflict")])
    assert rows[0]["comment_identity_state"] == "unverified"
    assert rows[0]["identity_join_status"] == "evidence_locator_conflict"
    assert len(rows[0]["identity_evidence"]["conflicting_rows"]) == 2
    assert audit["identity_locator_conflicts"] == 1


def test_genuine_verified_comment_conflict_is_exported_without_changing_measured_state():
    original = detail()
    saved = copy.deepcopy(original)
    rows, audit, _ = reference_rows(original, [evidence(status="verified_conflict")])
    assert rows[0]["comment_identity_state"] == "verified_conflict"
    assert rows[0]["measured_comment_identity_state"] == "unverified"
    assert rows[0]["payload"]["comment_identity_state"] == "unverified"
    assert rows[0]["identity_evidence"]["identity_evidence_grade"] == "A"
    assert audit["identity_state_verified_conflict"] == 1
    assert original == saved


@pytest.mark.parametrize("changed", ["repository_id", "snapshot_sha", "path", "job", "step_index", "line", "action", "ref"])
def test_every_locator_component_is_required_for_identity_attachment(changed):
    candidate = evidence()
    candidate[changed] = candidate[changed] + 1 if isinstance(candidate[changed], int) else candidate[changed] + "-different"
    rows, audit, _ = reference_rows(detail(), [candidate])
    assert rows[0]["comment_identity_state"] == "unverified"
    assert rows[0]["identity_join_status"] == "missing_evidence"
    assert audit["identity_matched_observations"] == 0


def test_lineage_cross_check_conflict_is_preserved_instead_of_upgraded():
    rows, audit, _ = reference_rows(detail(), [evidence(identity="other-lineage")])
    assert rows[0]["identity_join_status"] == "lineage_identity_conflict"
    assert rows[0]["comment_identity_state"] == "unverified"
    assert rows[0]["identity_evidence"]["lineage_id"] == "other-lineage"
    assert audit["identity_lineage_conflicts"] == 1


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def export_fixture(root, evidence_rows=None, candidates=1, with_join=True, with_risk=True):
    baseline_paths = []
    for number in range(candidates):
        path = root / "data/derived/cohort-v1/baselines" / f"candidate-{number}.json"
        write_json(path, dict(summary=dict(repository=f"candidate/repo-{number}", repository_id="123" if number == 0 else str(number + 1000),
            canonical_repository="owner/repo", status="confirmed" if number == 0 else "not_at_baseline",
            baseline_commit="base", baseline_tree="tree", source_frames='["zenodo:17301952"]', source_family="zenodo:10259013")))
        baseline_paths.append(path)
    measurement_path = root / "data/derived" / tables.BATCH_DIRECTORY / "123.json"
    write_json(measurement_path, detail())
    paths = [measurement_path, *baseline_paths]
    hashes = {path.relative_to(root).as_posix(): tables.digest(path) for path in paths}
    risk_path = root / "reports/riskset_summary.json"
    risk = dict(rules_version=tables.RISKSET_RULES_VERSION,
                measurement_batch_directory=tables.BATCH_DIRECTORY, measurement_rules_version=tables.MEASUREMENT_RULES_VERSION,
                inputs_unchanged=True, source_sha256=hashes, coverage={"all_confirmed_measurements_usable": True})
    if with_risk:
        write_json(risk_path, risk)
    evidence_path = root / "data/derived" / tables.EVIDENCE_DIRECTORY / "reference_evidence.jsonl"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("".join(json.dumps(row) + "\n" for row in ([evidence()] if evidence_rows is None else evidence_rows)), encoding="utf-8")
    join = dict(rule_version=tables.EVIDENCE_DIRECTORY,
                measurement_batch_directory=tables.BATCH_DIRECTORY, measurement_rules_version=tables.MEASUREMENT_RULES_VERSION,
                inputs_unchanged=True, measured_repository_ids=1,
                full_sha_reference_observations=len([evidence()] if evidence_rows is None else evidence_rows),
                input_sha256=dict(hashes), details={"reference_evidence": {
                    "path": evidence_path.relative_to(root).as_posix(), "sha256": tables.digest(evidence_path)}})
    if with_risk:
        join["input_sha256"][risk_path.relative_to(root).as_posix()] = tables.digest(risk_path)
    if with_join:
        write_json(root / "reports/opportunity_join_summary.json", join)
    return dict(measurement_path=measurement_path, risk=risk, risk_path=risk_path, join=join, evidence_path=evidence_path)


def test_bound_export_ignores_measurements_created_after_its_riskset_batch(tmp_path):
    fixture = export_fixture(tmp_path)
    extra = fixture["measurement_path"].with_name("999.json")
    write_json(extra, {"late": "not part of the frozen export batch"})
    result = tables.export(tmp_path)
    assert result["measured_ids_exported"] == 1
    assert extra.relative_to(tmp_path).as_posix() in result["excluded_later_measurement_files"]
    assert extra.relative_to(tmp_path).as_posix() not in result["input_sha256"]
    assert result["bound_input_integrity_passed"] and result["identity_mapping_complete"]
    assert not result["complete_cohort_measurement_export"]


def test_stale_measurement_or_join_output_is_rejected_before_tables_are_opened(tmp_path):
    fixture = export_fixture(tmp_path)
    fixture["measurement_path"].write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Changed or missing batch input"):
        tables.export(tmp_path)
    assert not (tmp_path / "data/derived" / tables.RULES_VERSION).exists()


def test_evidence_file_sha_must_match_join_summary(tmp_path):
    fixture = export_fixture(tmp_path)
    fixture["evidence_path"].write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="Changed or missing batch input"):
        tables.export(tmp_path)


def test_riskset_and_join_measurement_hash_maps_must_be_exactly_equal(tmp_path):
    fixture = export_fixture(tmp_path)
    extra = fixture["measurement_path"].with_name("999.json")
    write_json(extra, {"late": "different scope"})
    fixture["join"]["input_sha256"][extra.relative_to(tmp_path).as_posix()] = tables.digest(extra)
    write_json(tmp_path / "reports/opportunity_join_summary.json", fixture["join"])
    with pytest.raises(ValueError, match="different frozen inputs"):
        tables.export(tmp_path)


def test_join_must_bind_the_exact_riskset_summary_sha(tmp_path):
    fixture = export_fixture(tmp_path)
    fixture["risk"]["new_metadata"] = "later riskset run"
    write_json(fixture["risk_path"], fixture["risk"])
    with pytest.raises(ValueError, match="different frozen inputs"):
        tables.export(tmp_path)


def test_missing_join_is_provisional_and_retains_unverified_identity(tmp_path):
    export_fixture(tmp_path, with_join=False)
    result = tables.export(tmp_path)
    assert not result["identity_join_available"] and not result["integrity_gate_passed"]
    assert result["identity_association_counts"]["identity_missing_observations"] == 1
    path = tmp_path / result["tables"]["references"]["path"]
    with path.open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["comment_identity_state"] == "unverified"
    assert row["identity_join_status"] == "join_not_available"


def test_no_summaries_can_only_export_provisional_available_inputs(tmp_path):
    export_fixture(tmp_path, with_join=False, with_risk=False)
    result = tables.export(tmp_path)
    assert result["scope"] == "provisional_available_measurements"
    assert not result["bound_input_integrity_passed"] and not result["integrity_gate_passed"]


def test_duplicate_evidence_locators_fail_identity_integrity(tmp_path):
    export_fixture(tmp_path, evidence_rows=[evidence(), evidence(status="verified_conflict")])
    result = tables.export(tmp_path)
    assert not result["identity_mapping_complete"]
    assert result["identity_association_counts"]["duplicate_evidence_locator_keys"] == 1


def test_complete_same_batch_nine_tables_keep_valid_conflict_and_preserve_v1_v2_v3(tmp_path):
    legacy = [tmp_path / "data/derived" / directory / "manifest.json" for directory in ("tables-v1", "tables-v2", "tables-v3")]
    for path in legacy:
        path.parent.mkdir(parents=True)
        path.write_text("legacy evidence preserved: " + path.parent.name, encoding="utf-8")
    export_fixture(tmp_path, evidence_rows=[evidence(status="verified_conflict")], candidates=580)
    result = tables.export(tmp_path)
    assert result["table_count"] == 9 and result["integrity_gate_passed"]
    assert result["complete_cohort_measurement_export"] and result["identity_mapping_complete"]
    assert result["identity_association_counts"]["identity_state_verified_conflict"] == 1
    assert all(path.read_text(encoding="utf-8") == "legacy evidence preserved: " + path.parent.name for path in legacy)
    assert (tmp_path / "data/derived" / tables.RULES_VERSION / "manifest.json").exists()
    with (tmp_path / result["tables"]["references"]["path"]).open(encoding="utf-8", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["comment_identity_state"] == "verified_conflict"
    assert row["measured_comment_identity_state"] == "unverified"
    assert json.loads(row["payload"])["comment_identity_state"] == "unverified"


def test_change_after_preflight_cannot_replace_the_frozen_measurement_hash(monkeypatch, tmp_path):
    fixture = export_fixture(tmp_path)
    original = tables.select_batch

    def after_preflight(root):
        batch = original(root)
        updated = detail()
        updated["summary"]["new_metadata"] = "later measurement"
        write_json(fixture["measurement_path"], updated)
        return batch

    monkeypatch.setattr(tables, "select_batch", after_preflight)
    with pytest.raises(ValueError, match="Batch input changed during export"):
        tables.export(tmp_path)


def test_source_change_after_reading_closes_manifest_gate(monkeypatch, tmp_path):
    fixture = export_fixture(tmp_path)
    original = tables.measurement_rows

    def changed_after_rows(*args):
        yield from original(*args)
        updated = detail()
        updated["summary"]["new_metadata"] = "after the exported source was read"
        write_json(fixture["measurement_path"], updated)

    monkeypatch.setattr(tables, "measurement_rows", changed_after_rows)
    result = tables.export(tmp_path)
    assert not result["inputs_unchanged"] and not result["integrity_gate_passed"]
    bound_name = fixture["measurement_path"].relative_to(tmp_path).as_posix()
    assert result["input_sha256"][bound_name] != tables.digest(fixture["measurement_path"])


def test_join_summary_counts_must_reconcile_with_exported_identity_rows(tmp_path):
    fixture = export_fixture(tmp_path)
    fixture["join"]["full_sha_reference_observations"] = 100
    write_json(tmp_path / "reports/opportunity_join_summary.json", fixture["join"])
    result = tables.export(tmp_path)
    assert not result["declared_join_counts_equal"] and not result["identity_mapping_complete"]


def opportunity_evidence(grade):
    common = dict(repository="owner/action", tag="v4.2.0", channel="v4", target="b" * 40, evidence_grade=grade)
    if grade == "A":
        return dict(common, evidence_type="immutable_release", immutable=True, published_at="2025-04-01T00:00:00Z",
                    available_at="2025-04-01T00:00:00Z", source_url="https://example.org/releases/v4.2.0")
    if grade == "B":
        return dict(common, evidence_type="observed_binding_change", previous_target="a" * 40,
                    available_after="2025-04-01T00:00:00Z", available_by="2025-04-02T00:00:00Z",
                    lower_snapshot="c" * 40, upper_snapshot="d" * 40,
                    lower_url="https://example.org/archive/lower", upper_url="https://example.org/archive/upper")
    return dict(common, evidence_type="current_release", published_at="2025-04-01T00:00:00Z", immutable=False)


@pytest.mark.parametrize("grade", ["A", "B"])
def test_a_b_opportunity_ids_exactly_match_join_normalization(grade):
    from pintrace.opportunity_join import _opportunity

    record = opportunity_evidence(grade)
    row = tables.opportunity_row(record)
    assert row["opportunity_id"] == _opportunity(record)["opportunity_id"]
    assert row["opportunity_id"].startswith("op-")
    assert row["evidence_record_id"] == tables.identifier(record)
    assert row["opportunity_identity_state"] == "canonical_opportunity"
    assert row["latency_model"] == "requires_reference_applicability"
    assert row["payload"] == record


def test_current_only_evidence_never_receives_a_formal_opportunity_id():
    record = opportunity_evidence("C")
    row = tables.opportunity_row(record)
    assert row["opportunity_id"] is None
    assert row["evidence_record_id"] == tables.identifier(record)
    assert row["opportunity_identity_state"] == row["latency_model"] == "excluded_current_only"
    assert row["payload"] == record and "latency_model" not in record


@pytest.mark.parametrize("corruption", ["binding_time_mismatch", "invalid_date", "reversed_interval"])
def test_unnormalizable_evidence_keeps_raw_identity_but_no_formal_id(corruption):
    record = opportunity_evidence("B" if corruption == "reversed_interval" else "A")
    if corruption == "binding_time_mismatch":
        record["available_at"] = "2025-04-02T00:00:00Z"
    elif corruption == "invalid_date":
        record["available_at"] = "invalid date"
    else:
        record["available_by"] = "2025-03-31T00:00:00Z"
    row = tables.opportunity_row(record)
    assert row["opportunity_id"] is None
    assert row["evidence_record_id"] == tables.identifier(record)
    assert row["opportunity_identity_state"] == "unresolved_opportunity"
    assert row["latency_model"] == "excluded_unresolved_opportunity"
    assert row["payload"] == record


def test_multiple_evidence_records_preserve_one_canonical_opportunity_connection():
    first = opportunity_evidence("B")
    second = dict(first, lower_url="https://example.org/another-lower-copy", provenance="second evidence record")
    a, b = tables.opportunity_row(first), tables.opportunity_row(second)
    assert a["opportunity_id"] == b["opportunity_id"]
    assert a["evidence_record_id"] != b["evidence_record_id"]
    assert a["payload"] == first and b["payload"] == second


def test_csv_retains_canonical_ids_distinct_evidence_records_and_current_only_exclusion(tmp_path):
    fixture = export_fixture(tmp_path)
    a, b, c = [opportunity_evidence(grade) for grade in ("A", "B", "C")]
    another_a = dict(a, source_url="https://example.org/mirrored-immutable-evidence")
    upstream = tmp_path / "data/derived/upstream-opportunities/owner__action.json"
    write_json(upstream, dict(opportunities=[a, another_a, b], release_records=[c]))
    fixture["join"]["input_sha256"][upstream.relative_to(tmp_path).as_posix()] = tables.digest(upstream)
    write_json(tmp_path / "reports/opportunity_join_summary.json", fixture["join"])
    result = tables.export(tmp_path)
    with (tmp_path / result["tables"]["opportunities"]["path"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    assert rows[0]["opportunity_id"] == rows[1]["opportunity_id"] == tables.normalize_opportunity(a)["opportunity_id"]
    assert rows[0]["evidence_record_id"] != rows[1]["evidence_record_id"]
    assert rows[2]["opportunity_id"] == tables.normalize_opportunity(b)["opportunity_id"]
    assert rows[3]["opportunity_id"] == "" and rows[3]["latency_model"] == "excluded_current_only"
    assert json.loads(rows[3]["payload"]) == c


@pytest.mark.parametrize("summary_key,version_key,version", [
    ("risk", "rules_version", "risksets-v3"), ("join", "rule_version", "opportunity-join-v3")])
def test_old_analysis_summaries_cannot_enter_v4_tables(tmp_path, summary_key, version_key, version):
    fixture = export_fixture(tmp_path)
    fixture[summary_key][version_key] = version
    path = fixture["risk_path"] if summary_key == "risk" else tmp_path / "reports/opportunity_join_summary.json"
    write_json(path, fixture[summary_key])
    with pytest.raises(ValueError, match="analysis rules version"):
        tables.export(tmp_path)
    assert not (tmp_path / "data/derived" / tables.RULES_VERSION).exists()


def test_manifest_preserves_separate_episode_entry_coverage_and_bound_opportunity_links(tmp_path):
    fixture = export_fixture(tmp_path)
    fixture["risk"].update(risksets={"episode_kind_counts": {"baseline_fixed": 4, "post_anchor_observed_pin": 2, "post_anchor_born_fixed": 3}},
        fixed_entry_diagnostics={"reference_rows": 1, "repository_ids": 1},
        repository_first_observed_target_full_sha={"observed_repository_ids": 1})
    write_json(fixture["risk_path"], fixture["risk"])
    fixture["join"]["input_sha256"][fixture["risk_path"].relative_to(tmp_path).as_posix()] = tables.digest(fixture["risk_path"])
    kinds = {"post_anchor_observed_pin": {"exported_episodes": 2, "candidate_episodes": 1},
             "post_anchor_born_fixed": {"exported_episodes": 3, "candidate_episodes": 2}}
    modes = {"pin": {"candidate_episodes": 1}, "copied_reference": {"candidate_episodes": 2}}
    fixture["join"].update(target_rq3_by_episode_kind=kinds, target_rq3_by_entry_mode=modes)
    match_path = tmp_path / "data/derived" / tables.EVIDENCE_DIRECTORY / "opportunity_matches.jsonl"
    payload = {"opportunity_id": "op-canonical", "rq3_target_episode_links": [
        {"episode_id": "ep-born", "episode_kind": "post_anchor_born_fixed", "entry_mode": "copied_reference"}]}
    match_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    descriptor = dict(path=match_path.relative_to(tmp_path).as_posix(), sha256=tables.digest(match_path))
    fixture["join"]["details"]["opportunity_matches"] = descriptor
    write_json(tmp_path / "reports/opportunity_join_summary.json", fixture["join"])
    result = tables.export(tmp_path)
    scope = result["rq3_entry_scope"]
    assert scope["opportunity_episode_kind_coverage"] == kinds
    assert scope["opportunity_entry_mode_coverage"] == modes
    assert scope["riskset_episode_kind_counts"]["post_anchor_observed_pin"] == 2
    assert scope["fixed_entry_diagnostics"]["reference_rows"] == 1
    assert scope["opportunity_match_evidence"] == descriptor
    assert result["input_sha256"][descriptor["path"]] == descriptor["sha256"]
    assert result["table_count"] == 9
