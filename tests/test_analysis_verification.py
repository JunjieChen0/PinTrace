from copy import deepcopy
import csv
import json
from pathlib import Path

import pytest

import scripts.verify_analysis as audit
import pintrace.maintenance_scope as maintenance
from pintrace.measurement import BATCH_DIRECTORY
from pintrace.opportunity_join import EPISODE_ENTRY_MODES, _opportunity


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=sorted(set().union(*(row.keys() for row in rows))) if rows else ["id"])
        writer.writeheader()
        writer.writerows(rows)


def fixture(root):
    measured = "data/derived/" + BATCH_DIRECTORY + "/1.json"
    write_json(root / measured, {"fixture": "measurement identity is checked by the separate full replay gate"})
    flow = [{"repository": "repo/" + str(number), "repository_id": "1" if number == 0 else "",
             "status": "confirmed" if number == 0 else "not_at_baseline"} for number in range(580)]
    write_csv(root / "reports/cohort_flow.csv", flow)
    common = {name: audit.digest(root / name) for name in (measured, "reports/cohort_flow.csv")}
    episodes = [dict(row_kind="episode", episode_id="ep-" + kind, episode_kind=kind,
                     entry_mode=mode, source_entry_event=mode, entry_mode_state="known_local_entry",
                     repository_id="1", lineage_id="line-" + kind)
                for kind, mode in (("baseline_fixed", "baseline_fixed"), ("post_anchor_observed_pin", "pin"),
                                   ("post_anchor_born_fixed", "copied_reference"))]
    diagnostic = dict(row_kind="fixed_entry_diagnostic", entry_mode="unknown_boundary", lineage_id="unknown")
    comparison = dict(row_kind="comparison", repository_id="1", eligible=True, eligible_lineage_ids='["comparator"]',
                      focal_episode_id="ep-post_anchor_born_fixed", comparison_layer="same_repository_baseline_fixed")
    write_csv(root / "reports/risksets.csv", episodes + [diagnostic, comparison])
    risk = dict(rules_version=audit.RISKSET_RULES_VERSION,
                measurement_batch_directory=BATCH_DIRECTORY, measurement_rules_version=BATCH_DIRECTORY,
                coverage={"all_confirmed_measurements_usable": True, "confirmed_repository_ids": 1, "measured_repository_ids": 1},
                source_sha256=common, output_sha256={"risksets.csv": audit.digest(root / "reports/risksets.csv")},
                risksets={"episode_kind_counts": {row["episode_kind"]: 1 for row in episodes}},
                fixed_entry_diagnostics={"reference_rows": 1})
    write_json(root / "reports/riskset_summary.json", risk)
    replay = dict(input_sha256=common, gate_passed=True, verified_ids=["1"], scope={"confirmed_repository_ids": ["1"]})
    write_json(root / "reports/full_replay_report.json", replay)
    record = dict(repository="owner/action", tag="v1.1.0", target="a" * 40, evidence_grade="A",
                  evidence_type="immutable_release", immutable=True, published_at="2025-03-16T00:00:00Z",
                  available_at="2025-03-16T00:00:00Z", source_url="https://example.org/immutable-release")
    opportunity = _opportunity(record)
    canonical = opportunity["opportunity_id"]
    table_names = ("repositories", "snapshots", "references", "lineage_events", "opportunities", "configurations", "pr_events", "checks", "sources")
    tables, table_rows = {}, {}
    for name in table_names:
        path = "data/derived/tables-v4/" + name + ".csv"
        content = [{"opportunity_id": canonical, "payload": json.dumps(record)}] if name == "opportunities" else []
        write_csv(root / path, content)
        tables[name] = dict(path=path, sha256=audit.digest(root / path))
        table_rows[name] = len(content)
    matches = []
    for episode in episodes:
        link = {key: episode[key] for key in ("episode_id", "episode_kind", "entry_mode", "source_entry_event")}
        matches.append(dict(repository_id="1", lineage_id=episode["lineage_id"], opportunity_id=canonical,
            evidence_type="immutable_release",
            upstream_repository=record["repository"], opportunity_tag=record["tag"], evidence_grade="A",
            available_after_or_at=opportunity["lower"], available_by=opportunity["upper"],
            reference_target_segment_start_commit="initial-" + episode["episode_kind"], opportunity_target="a" * 40,
            applicable=True, selected_for_delay_analysis=True, target_delay_model_candidate=True,
            target_pin_delay_model_candidate=episode["episode_kind"] == "post_anchor_observed_pin",
            target_born_fixed_delay_model_candidate=episode["episode_kind"] == "post_anchor_born_fixed",
            target_baseline_delay_model_candidate=episode["episode_kind"] == "baseline_fixed",
            rq3_target_episode_ids=[episode["episode_id"]], rq3_target_episode_links=[link],
            comparison_focal_episode_ids=[], comparison_focal_episode_links=[],
            outcome="adopted_target", outcome_timestamp=1, adopted_sha="a" * 40))
    unknown = dict(matches[0], lineage_id="unknown", rq3_target_episode_ids=[], rq3_target_episode_links=[],
                   target_delay_model_candidate=False, target_baseline_delay_model_candidate=False)
    comparator = dict(unknown, lineage_id="comparator", comparison_focal_episode_ids=["ep-post_anchor_born_fixed"],
        comparison_focal_episode_links=[dict(focal_episode_id="ep-post_anchor_born_fixed", focal_episode_kind="post_anchor_born_fixed",
                                             focal_entry_mode="copied_reference", comparison_layer="same_repository_baseline_fixed")])
    matches += [unknown, comparator]
    join = dict(rule_version=audit.JOIN_RULES_VERSION,
                measurement_batch_directory=BATCH_DIRECTORY, measurement_rules_version=BATCH_DIRECTORY,
                riskset_export_coverage=deepcopy(risk["coverage"]), inputs_unchanged=True,
                input_sha256=common, details={}, exported_target_riskset_episodes=3, target_rq3_episode_candidates=3,
                target_rq3_repository_candidates=1, target_rq3_opportunity_pairs=3, actual_target_sha_adoptions=3)
    for field, values in (("episode_kind", EPISODE_ENTRY_MODES), ("entry_mode", set().union(*EPISODE_ENTRY_MODES.values()))):
        join["target_rq3_by_" + field] = {}
        for value in values:
            amount = sum(episode[field] == value for episode in episodes)
            join["target_rq3_by_" + field][value] = dict(exported_episodes=amount, candidate_episodes=amount,
                candidate_episode_fraction=1.0 if amount else None, candidate_repository_ids=1 if amount else 0,
                selected_opportunity_reference_pairs=amount, actual_target_sha_adoptions=amount)
    manifest = dict(rule_version=audit.TABLE_RULES_VERSION,
                    tables=tables, table_rows=table_rows, input_sha256=common, integrity_gate_passed=True)
    state = dict(root=root, risk=risk, replay=replay, join=join, manifest=manifest, matches=matches)
    source = root / "pintrace/maintenance_scope.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(Path(maintenance.__file__).read_bytes())
    refresh(state)
    return state


def refresh(state):
    root = state["root"]
    write_json(root / "reports/riskset_summary.json", state["risk"])
    write_json(root / "reports/full_replay_report.json", state["replay"])
    path = root / "data/derived/opportunity-join-v4/opportunity_matches.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in state["matches"]), encoding="utf-8")
    state["join"]["details"]["opportunity_matches"] = dict(path=path.relative_to(root).as_posix(), sha256=audit.digest(path))
    state["join"]["input_sha256"] = dict(state["risk"]["source_sha256"], **{
        name: audit.digest(root / name) for name in ("reports/riskset_summary.json", "reports/risksets.csv")})
    write_json(root / "reports/opportunity_join_summary.json", state["join"])
    state["manifest"]["input_sha256"] = dict(state["join"]["input_sha256"], **{
        "reports/opportunity_join_summary.json": audit.digest(root / "reports/opportunity_join_summary.json"),
        path.relative_to(root).as_posix(): audit.digest(path)})
    state["manifest"]["rq3_entry_scope"] = dict(opportunity_episode_kind_coverage=deepcopy(state["join"]["target_rq3_by_episode_kind"]),
                                               opportunity_entry_mode_coverage=deepcopy(state["join"]["target_rq3_by_entry_mode"]))
    write_json(root / "reports/table_manifest.json", state["manifest"])
    if state["join"]["rule_version"] == audit.JOIN_RULES_VERSION:
        maintenance.export_scope(root)


def test_valid_bound_artifacts_pass_with_separate_strata_and_unknown_diagnostic(tmp_path):
    fixture(tmp_path)
    result = audit.verify(tmp_path)
    assert result["gate_passed"] and all(row["passed"] for row in result["checks"].values())
    assert result["counts"]["episodes"] == 3 and result["counts"]["fixed_entry_diagnostic_rows"] == 1
    assert json.loads((tmp_path / "reports/analysis_verification.json").read_text())["gate_passed"]


@pytest.mark.parametrize("corruption", ["opportunity", "opportunity_target", "opportunity_time", "episode", "kind", "focal_mode", "old_id_list"])
def test_semantically_bad_links_fail_even_after_join_output_hash_is_updated(tmp_path, corruption):
    state = fixture(tmp_path)
    if corruption == "opportunity":
        state["matches"][0]["opportunity_id"] = "op-nonexistent"
    elif corruption == "opportunity_target":
        state["matches"][0]["opportunity_target"] = "b" * 40
    elif corruption == "opportunity_time":
        state["matches"][0]["available_by"] += 1
    elif corruption == "episode":
        state["matches"][0]["rq3_target_episode_ids"] = ["ep-nonexistent"]
        state["matches"][0]["rq3_target_episode_links"][0]["episode_id"] = "ep-nonexistent"
    elif corruption == "kind":
        state["matches"][0]["rq3_target_episode_links"][0]["episode_kind"] = "post_anchor_born_fixed"
    elif corruption == "focal_mode":
        state["matches"][-1]["comparison_focal_episode_links"][0]["focal_entry_mode"] = "pin"
    else:
        state["matches"][0]["rq3_target_episode_ids"] = []
    refresh(state)
    result = audit.verify(tmp_path)
    assert result["checks"]["input_hashes"]["passed"]
    assert not result["gate_passed"] and not result["checks"]["opportunity_links"]["passed"]


@pytest.mark.parametrize("corruption", ["diagnostic_id", "duplicate_episode", "illegal_entry_mode"])
def test_episode_integrity_is_independent_of_updated_riskset_file_hash(tmp_path, corruption):
    state = fixture(tmp_path)
    with (tmp_path / "reports/risksets.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if corruption == "diagnostic_id":
        rows[3]["episode_id"] = "ep-unknown"
    elif corruption == "duplicate_episode":
        rows.append(deepcopy(rows[0]))
    else:
        rows[2]["entry_mode"] = "unknown_boundary"
    write_csv(tmp_path / "reports/risksets.csv", rows)
    state["risk"]["output_sha256"]["risksets.csv"] = audit.digest(tmp_path / "reports/risksets.csv")
    refresh(state)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["episodes"]["passed"]


@pytest.mark.parametrize("corruption", ["merged_denominator", "duplicate_candidate_flag", "pending_replay", "different_measurement_batch"])
def test_summary_selection_and_complete_measurement_set_are_recomputed(tmp_path, corruption):
    state = fixture(tmp_path)
    if corruption == "merged_denominator":
        state["join"]["target_rq3_by_episode_kind"]["post_anchor_observed_pin"]["exported_episodes"] = 2
    elif corruption == "duplicate_candidate_flag":
        state["matches"].append(dict(state["matches"][1], selected_for_delay_analysis=False))
    elif corruption == "pending_replay":
        state["replay"].update(gate_passed=False, verified_ids=[])
    else:
        name = "data/derived/" + BATCH_DIRECTORY + "/2.json"
        write_json(tmp_path / name, {})
        state["replay"]["input_sha256"] = dict(state["replay"]["input_sha256"], **{name: audit.digest(tmp_path / name)})
    refresh(state)
    result = audit.verify(tmp_path)
    key = "summary_counts" if corruption in {"merged_denominator", "duplicate_candidate_flag"} else "measurement_batch"
    assert not result["gate_passed"] and not result["checks"][key]["passed"]


def test_missing_inputs_produce_a_failed_report_instead_of_passing(tmp_path):
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["readable_inputs"]["passed"]


def test_hash_change_during_reading_closes_gate(monkeypatch, tmp_path):
    fixture(tmp_path)
    original, observed = audit.digest, []

    def changed(path):
        if path == tmp_path / "data/derived" / BATCH_DIRECTORY / "1.json":
            observed.append(path)
            if len(observed) > 1:
                return "0" * 64
        return original(path)

    monkeypatch.setattr(audit, "digest", changed)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["inputs_unchanged"]["passed"]


@pytest.mark.parametrize("kind", ["current_only", "unresolved"])
def test_current_only_and_unresolved_raw_evidence_remain_explicit_without_formal_ids(tmp_path, kind):
    state = fixture(tmp_path)
    descriptor = state["manifest"]["tables"]["opportunities"]
    path = tmp_path / descriptor["path"]
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    record = dict(evidence_grade="C", evidence_type="current_release_listing") if kind == "current_only" else dict(
        evidence_grade="A", evidence_type="immutable_release", immutable=True, repository="owner/action",
        tag="v2.0.0", target="b" * 40, published_at="invalid date", available_at="invalid date", source_url="https://example.org/source")
    rows.append({"opportunity_id": "", "payload": json.dumps(record)})
    write_csv(path, rows)
    descriptor["sha256"] = audit.digest(path)
    state["manifest"]["table_rows"]["opportunities"] = len(rows)
    refresh(state)
    result = audit.verify(tmp_path)
    assert result["gate_passed"]
    key = "current_only_evidence_rows" if kind == "current_only" else "unresolved_evidence_rows"
    assert result["counts"][key] == 1


@pytest.mark.parametrize("report,field,old_version", [("risk", "rules_version", "risksets-v3"),
    ("join", "rule_version", "opportunity-join-v3"), ("manifest", "rule_version", "tables-v3")])
def test_old_analysis_rule_versions_fail_after_all_input_hashes_are_updated(tmp_path, report, field, old_version):
    state = fixture(tmp_path)
    state[report][field] = old_version
    refresh(state)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["input_hashes"]["passed"]


@pytest.mark.parametrize("update_hash", [False, True])
def test_changed_table_bytes_or_row_count_cannot_keep_the_gate_green(tmp_path, update_hash):
    state = fixture(tmp_path)
    descriptor = state["manifest"]["tables"]["references"]
    path = tmp_path / descriptor["path"]
    path.write_text("id\nextra-row\n", encoding="utf-8")
    if update_hash:
        descriptor["sha256"] = audit.digest(path)
    refresh(state)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"]
    assert not result["checks"]["table_files" if update_hash else "input_hashes"]["passed"]


def test_multiple_selected_evidence_rows_fail_even_when_summary_counts_are_synchronized(tmp_path):
    state = fixture(tmp_path)
    state["matches"].append(deepcopy(state["matches"][1]))
    state["join"]["target_rq3_opportunity_pairs"] += 1
    state["join"]["target_rq3_by_episode_kind"]["post_anchor_observed_pin"]["selected_opportunity_reference_pairs"] += 1
    state["join"]["target_rq3_by_entry_mode"]["pin"]["selected_opportunity_reference_pairs"] += 1
    refresh(state)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"]
    assert "multiple selected" in " ".join(result["checks"]["summary_counts"]["examples"])


def test_initial_replay_without_input_map_does_not_skip_fk_or_stratified_checks(tmp_path):
    state = fixture(tmp_path)
    state["replay"].pop("input_sha256")
    state["replay"].update(gate_passed=False, verified_ids=[])
    state["matches"][0]["opportunity_id"] = "op-missing-while-replay-is-pending"
    state["join"]["target_rq3_by_episode_kind"]["post_anchor_observed_pin"]["exported_episodes"] = 2
    refresh(state)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and "readable_inputs" not in result["checks"]
    assert result["counts"]["episodes"] == 3
    for name in ("opportunity_links", "summary_counts", "measurement_batch"):
        assert not result["checks"][name]["passed"] and result["checks"][name]["status"] == "checked"
    assert result["checks"]["measurement_batch"]["measurement_files_by_report"] == [1, 1, 1, 0]


def test_large_lineage_event_json_field_is_preserved_and_csv_limit_restored(tmp_path, monkeypatch):
    state = fixture(tmp_path)
    descriptor = state["manifest"]["tables"]["lineage_events"]
    path = tmp_path / descriptor["path"]
    payload = {"event": "ambiguous_transition", "evidence": "x,\n\"quoted\"" * 17000}
    text = json.dumps(payload)
    assert len(text) > 165239
    write_csv(path, [{"event_id": "large-event", "payload": text}])
    descriptor["sha256"] = audit.digest(path)
    state["manifest"]["table_rows"]["lineage_events"] = 1
    refresh(state)
    original, observed = csv.DictReader, []

    def parsed_rows(*args, **kwargs):
        for row in original(*args, **kwargs):
            if row.get("event_id") == "large-event":
                observed.append(json.loads(row["payload"]))
            yield row

    monkeypatch.setattr(audit.csv, "DictReader", parsed_rows)
    previous_limit = csv.field_size_limit()
    result = audit.verify(tmp_path)
    assert result["gate_passed"] and result["rules_version"] == "analysis-verification-v3"
    assert result["counts"]["lineage_events_rows"] == 1 and observed == [payload]
    assert csv.field_size_limit() == previous_limit


def test_csv_parser_error_is_reported_and_previous_field_limit_is_restored(tmp_path, monkeypatch):
    fixture(tmp_path)
    previous_limit = csv.field_size_limit()

    def invalid_rows(*args, **kwargs):
        assert csv.field_size_limit() == 2**31 - 1
        raise csv.Error("synthetic malformed CSV")

    monkeypatch.setattr(audit.csv, "DictReader", invalid_rows)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"]
    assert "synthetic malformed CSV" in result["checks"]["readable_inputs"]["examples"][0]
    assert csv.field_size_limit() == previous_limit


def add_opportunity(state, record):
    descriptor = state["manifest"]["tables"]["opportunities"]
    path = state["root"] / descriptor["path"]
    with path.open(encoding="utf-8", newline="") as stream:
        content = list(csv.DictReader(stream))
    opportunity = _opportunity(record)
    content.append({"opportunity_id": opportunity["opportunity_id"], "payload": json.dumps(record)})
    write_csv(path, content)
    descriptor["sha256"] = audit.digest(path)
    state["manifest"]["table_rows"]["opportunities"] = len(content)
    return opportunity


def match_evidence(template, opportunity, selected):
    row = dict(template, opportunity_id=opportunity["opportunity_id"], upstream_repository=opportunity["repository"],
               opportunity_tag=opportunity["tag"], opportunity_target=opportunity["target"], evidence_grade=opportunity["evidence_grade"],
               evidence_type=opportunity["evidence_type"], available_after_or_at=opportunity["lower"], available_by=opportunity["upper"],
               selected_for_delay_analysis=selected)
    row["target_delay_model_candidate"] = selected and bool(row["rq3_target_episode_links"])
    for flag, kind in (("target_baseline_delay_model_candidate", "baseline_fixed"), ("target_pin_delay_model_candidate", "post_anchor_observed_pin"),
                       ("target_born_fixed_delay_model_candidate", "post_anchor_born_fixed")):
        row[flag] = selected and any(link["episode_kind"] == kind for link in row["rq3_target_episode_links"])
    return row


def retarget_case(root):
    state = fixture(root)
    normal = dict(repository="owner/action", tag="v1.1.0", target="a" * 40, evidence_grade="B", evidence_type="observed_tag_introduction",
                  available_after="2025-03-15T00:00:00Z", available_by="2025-03-16T00:00:00Z", lower_snapshot="c" * 40,
                  upper_snapshot="d" * 40, lower_url="https://example.org/lower", upper_url="https://example.org/upper",
                  lower_complete=True, upper_complete=True, lower_snapshot_tags_complete=True, upper_snapshot_tags_complete=True)
    retarget = dict(normal, tag="v1", evidence_type="observed_binding_change", previous_target="b" * 40,
                    available_after="2025-03-14T00:00:00Z", available_by="2025-03-15T00:00:00Z")
    normal_match = match_evidence(state["matches"][0], add_opportunity(state, normal), False)
    retarget_match = match_evidence(state["matches"][0], add_opportunity(state, retarget), True)
    state["matches"][0] = normal_match
    state["matches"].append(retarget_match)
    refresh(state)
    return state, normal_match, retarget_match


def scope_rows(state):
    summary = json.loads((state["root"] / "reports/maintenance_scope_summary.json").read_text(encoding="utf-8"))
    path = state["root"] / summary["details"]["opportunity_entries"]["path"]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def replace_scope_rows(state, content):
    path = state["root"] / "reports/maintenance_scope_summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    output = state["root"] / summary["details"]["opportunity_entries"]["path"]
    output.write_text("".join(json.dumps(row) + "\n" for row in content), encoding="utf-8")
    summary["details"]["opportunity_entries"]["sha256"] = audit.digest(output)
    write_json(path, summary)


def test_filter_before_dedup_restores_normal_maintenance_flags_from_links(tmp_path):
    state, normal, retarget = retarget_case(tmp_path)
    selected = next(row for row in scope_rows(state) if row["lineage_id"] == normal["lineage_id"])
    assert retarget["available_by"] < normal["available_by"]
    assert not selected["selected_for_delay_analysis"] and not selected["target_delay_model_candidate"]
    assert selected["maintenance_selected"] and selected["maintenance_baseline_delay_model_candidate"]
    assert selected["opportunity_id"] == normal["opportunity_id"]
    result = audit.verify(tmp_path)
    assert result["gate_passed"] and result["checks"]["maintenance_scope"]["normal_applicable_keys"] == 5


@pytest.mark.parametrize("corruption", ["observed_binding_change", "observed_tag_reintroduction", "historical_release_event", "target_link", "maintenance_flag"])
def test_synchronized_view_hash_cannot_hide_excluded_types_or_altered_links_and_flags(tmp_path, corruption):
    state = fixture(tmp_path)
    content = scope_rows(state)
    target = next(row for row in content if row["maintenance_target_episode_links"])
    if corruption == "target_link":
        target["maintenance_target_episode_links"] = []
    elif corruption == "maintenance_flag":
        target["maintenance_target_delay_model_candidate"] = False
    else:
        content[0]["evidence_type"] = corruption
    replace_scope_rows(state, content)
    result = audit.verify(tmp_path)
    assert result["checks"]["input_hashes"]["passed"]
    assert not result["gate_passed"] and not result["checks"]["maintenance_scope"]["passed"]


def test_better_normal_evidence_cannot_be_replaced_with_a_later_normal_entry(tmp_path):
    state = fixture(tmp_path)
    record = dict(repository="owner/action", tag="v1.2.0", target="a" * 40, evidence_grade="A", evidence_type="immutable_release",
                  immutable=True, available_at="2025-03-17T00:00:00Z", published_at="2025-03-17T00:00:00Z", source_url="https://example.org/later")
    worse = match_evidence(state["matches"][0], add_opportunity(state, record), False)
    state["matches"].append(worse)
    refresh(state)
    content = scope_rows(state)
    content = [maintenance.select_entries([worse])[0] if row["lineage_id"] == worse["lineage_id"] else row for row in content]
    replace_scope_rows(state, content)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["maintenance_scope"]["passed"]
    assert all(result["checks"][name]["passed"] for name in ("input_hashes", "opportunity_links", "summary_counts"))


def test_normal_alternative_suppressed_by_raw_dedup_must_not_be_omitted_from_view(tmp_path):
    state, normal, _ = retarget_case(tmp_path)
    replace_scope_rows(state, [row for row in scope_rows(state) if row["lineage_id"] != normal["lineage_id"]])
    result = audit.verify(tmp_path)
    assert not result["gate_passed"]
    assert "coverage is incomplete" in " ".join(result["checks"]["maintenance_scope"]["examples"])


def test_retarget_without_normal_replacement_stays_diagnostic_and_cannot_create_an_entry(tmp_path):
    state, normal, retarget = retarget_case(tmp_path)
    old_normal_entry = next(row for row in scope_rows(state) if row["lineage_id"] == normal["lineage_id"])
    state["matches"] = [row for row in state["matches"] if row is not normal]
    refresh(state)
    assert audit.verify(tmp_path)["gate_passed"]
    selected = scope_rows(state)
    assert all(row["lineage_id"] != normal["lineage_id"] for row in selected)
    selected.append(dict(old_normal_entry, **retarget))
    replace_scope_rows(state, selected)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["maintenance_scope"]["passed"]


@pytest.mark.parametrize("corruption", ["join_batch", "denominator", "target_counts"])
def test_maintenance_summary_cannot_change_join_binding_or_formal_counts(tmp_path, corruption):
    fixture(tmp_path)
    path = tmp_path / "reports/maintenance_scope_summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "join_batch":
        summary["join_summary_sha256"] = "0" * 64
    elif corruption == "denominator":
        summary["target_by_episode_kind"]["post_anchor_observed_pin"]["exported_episodes"] += 1
    else:
        summary["counts"]["target"]["repository_ids"] += 1
    write_json(path, summary)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["maintenance_scope"]["passed"]


def test_partial_replay_can_pass_maintenance_semantics_without_opening_overall_gate(tmp_path):
    state = fixture(tmp_path)
    state["risk"]["coverage"]["all_confirmed_measurements_usable"] = False
    state["join"]["riskset_export_coverage"] = deepcopy(state["risk"]["coverage"])
    state["replay"].update(gate_passed=False, verified_ids=[])
    refresh(state)
    summary = json.loads((tmp_path / "reports/maintenance_scope_summary.json").read_text(encoding="utf-8"))
    assert summary["source_coverage_complete"] is False
    result = audit.verify(tmp_path)
    assert result["checks"]["maintenance_scope"]["passed"]
    assert not result["gate_passed"] and not result["checks"]["measurement_batch"]["passed"]


def test_missing_maintenance_output_keeps_required_gate_closed_after_original_checks(tmp_path):
    fixture(tmp_path)
    path = tmp_path / "reports/maintenance_scope_summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["details"]["opportunity_entries"] = {"path": "data/derived/maintenance-scope-v1/pending.jsonl", "sha256": "0" * 64}
    write_json(path, summary)
    result = audit.verify(tmp_path)
    assert not result["gate_passed"] and not result["checks"]["maintenance_scope"]["passed"]
    assert all(result["checks"][name]["passed"] for name in ("opportunity_links", "summary_counts", "measurement_batch"))
