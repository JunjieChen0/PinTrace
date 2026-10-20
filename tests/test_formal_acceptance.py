import csv
import json
import shutil

import pytest

from pintrace.formal_acceptance import (CHAPTERS, HISTORICAL_SOURCES, SCENARIOS, SUMMARIES, Evidence, check_bootstrap,
                                        check_manuscript, check_rq12, check_rq3, csv_matches, equivalent, point_links, run)
from pintrace.formal_common import (add_ratio, cluster_bootstrap, make_context, sha256,
                                     verify_frozen_inputs, write_json)
from pintrace.formal_figures import Artifacts
from pintrace.formal_manuscript import Writer
from test_formal_rq3 import frozen_fixture as rq3_frozen_fixture


@pytest.fixture
def rq12_fixture(tmp_path):
    from pintrace.formal_rq12 import DAY, _csv, _record_line, describe_repository, summarize
    root, output = tmp_path, tmp_path / "batch"
    output.mkdir()
    (root / "reports").mkdir()
    (root / "pintrace").mkdir()
    (root / "pintrace/formal_rq12.py").write_text("fixture source\n", encoding="utf-8")
    write_json(root / "reports/freeze_manifest.json", dict(status="frozen"))
    ids = {str(i) for i in range(1, 458)}
    records = []
    for identity in sorted(ids):
        reference = dict(action="other/action", lineage_id="other-lineage", kind="action", ref_type="tag")
        group = dict(action="new/action", lineage_ids=["new-lineage"], index=1, timestamp=30*DAY,
                     references=2, full_sha=1, configuration="mixed", identity_state="confirmed_first_observed",
                     excluded_copy_or_reintroduction=False, source_modes=["first_observed_action"])
        snapshots = [dict(sha=str(n), timestamp=t, complete=True, target_references=1, target_full_sha=0)
                     for n, t in enumerate((-DAY, 365*DAY))]
        records.append(dict(repository_id=identity, repository="fixture/" + identity, anchor=0, window_end=365*DAY,
            target_action="tj-actions/changed-files", clock_aligned=True, endpoint_index=1, snapshots=snapshots,
            observed_ancestry_covers_end=True, events=[], first_action_groups=[group] if identity == "1" else [],
            baselines={str(at):dict(index=0, complete=True, references=[reference] if identity == "1" else []) for at in (-DAY, 0, DAY)}))
    cache = output / "rq12_repository_records.jsonl"
    cache.write_text("".join(_record_line(record) for record in records), encoding="utf-8", newline="\n")
    write_json(output / "rq12_record_cache_receipt.json", dict(path=str(cache), sha256=sha256(cache), freeze_sha256="frozen",
        implementation_sha256=sha256(root / "pintrace/formal_rq12.py")))
    context = dict(rq12_records=records, formal_ids=ids - {"456", "457"}, raw_ids=ids,
        freeze_manifest=dict(raw_repository_count=457, formal_repository_count=455,
            formal_episode_kind_counts=dict(post_anchor_born_fixed=dict(episodes=65)),
            input_sha256={"data/derived/tables-v4/repositories.csv": "0" * 64}),
        guard=dict(freeze_sha256="frozen"), study=dict(target_action="tj-actions/changed-files"), metrics=[],
        populations=dict(formal=sorted(ids - {"456", "457"}), raw=sorted(ids)))
    with (root / "reports/risksets.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["repository_id", "lineage_id", "row_kind", "episode_kind"])
        writer.writeheader()
        writer.writerows(dict(repository_id="1", lineage_id="target-" + str(i), row_kind="episode",
                             episode_kind="post_anchor_born_fixed") for i in range(65))
    summaries, by_population = {}, {}
    for population in ("formal", "raw"):
        summaries[population] = summarize(records, context[population + "_ids"], context=context, population=population)
        by_population[population] = [describe_repository(record) for record in records if record["repository_id"] in context[population + "_ids"]]
    documents = {name: {population:summaries[population][key] for population in summaries}
                 for name, key in (("rq1_summary", "rq1"), ("rq2_summary", "rq2"))}
    for name, value in documents.items():
        write_json(output / (name + ".json"), value)
    write_json(output / "estimand_registry.json", dict(populations=context["populations"], metrics=context["metrics"]))
    for key, filename in (("rq1", "rq1_repository_results.csv"), ("references", "rq2_existing_reference_results.csv"), ("new_actions", "rq2_new_action_results.csv")):
        _csv(output / filename, [dict(population=population, **row) for population, descriptions in by_population.items()
            for result in descriptions for row in ([result[key]] if key == "rq1" else result[key])])
    return root, output, context, documents


def test_rq12_reconstructs_actual_csv_horizons_isolation_and_no_new_denominators(rq12_fixture):
    root, output, context, documents = rq12_fixture
    evidence = Evidence(root)
    check_rq12(root, output, context, evidence, documents)
    assert all(row["passed"] for row in evidence.checks), [row["requirement"] for row in evidence.checks if not row["passed"]]
    assert documents["rq2_summary"]["formal"]["new_actions"]["repositories_with_new_groups"] == 1
    assert documents["rq2_summary"]["formal"]["new_actions"]["no_new_group_repositories"] == 454


def test_scalar_rq12_main_and_eleven_scenarios_cover_every_real_registered_point(rq12_fixture):
    from pintrace.formal_rq12 import DAY, summarize
    root, output, context, documents = rq12_fixture
    context["populations"]["formal_exclude_pilot"] = sorted(context["formal_ids"] - {str(i) for i in range(1, 50)})
    matrix = {}
    for name in sorted(SCENARIOS):
        population = "raw" if name == "raw_main" else "formal_exclude_pilot" if name == "exclude_pilot" else "formal"
        settings = dict(label=name, anchor=-DAY if name == "anchor_minus_one_day" else DAY if name == "anchor_plus_one_day" else 0,
            end=365*DAY)
        if name == "after_first_seven_days":
            settings["start_after_days"] = 7
        if name == "after_first_thirty_days":
            settings["start_after_days"] = 30
        if name == "exclude_incident_day":
            settings["exclude_event_day"] = True
        response = summarize(context["rq12_records"], context["populations"][population], settings, context=context, population=population)
        matrix[name] = response
    documents.update({name:{} for name in SUMMARIES[2:-2]})
    documents["robustness_checks"] = dict(scenarios=matrix)
    links = point_links(documents)
    registered = {row["key"]:row["estimate"] for row in context["metrics"]}
    assert {key for key, _ in links} == set(registered)
    assert all(equivalent(value, registered[key]) for key, value in links)
    assert len(registered) > 1000
    documents["rq2_summary"]["formal"]["new_actions"]["repository_equal"]["mixed"] = .2
    assert any(not equivalent(value, registered[key]) for key, value in point_links(documents))


@pytest.mark.parametrize("change,requirement", [("horizon", "RQ1 partitions"), ("weight", "paired weighting"),
    ("csv", "actual row reconstruction"), ("cache", "source-bound actual compact cache")])
def test_rq12_rejects_changed_horizon_weight_csv_and_cache(rq12_fixture, change, requirement):
    root, output, context, documents = rq12_fixture
    if change == "horizon":
        documents["rq1_summary"]["formal"]["horizons"]["30"]["at_risk_just_before"] = 0
    elif change == "weight":
        documents["rq2_summary"]["formal"]["new_actions"]["repository_equal"]["mixed"] = 0
    elif change == "csv":
        path = output / "rq2_new_action_results.csv"
        path.write_text(path.read_text(encoding="utf-8").replace("new/action", "tj-actions/changed-files"), encoding="utf-8")
    else:
        with (output / "rq12_repository_records.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("\n")
    evidence = Evidence(root)
    check_rq12(root, output, context, evidence, documents)
    assert any(not row["passed"] and requirement in row["requirement"] for row in evidence.checks)


@pytest.fixture
def rq3_fixture(rq3_frozen_fixture):
    from pintrace.formal_rq3 import run as run_rq3
    root, output = rq3_frozen_fixture, rq3_frozen_fixture / "batch"
    context = make_context(root, verify_frozen_inputs(root))
    documents = run_rq3(root, output, context)
    write_json(output / "estimand_registry.json", dict(populations=context["populations"], metrics=context["metrics"]))
    return root, output, context, documents


def test_rq3_acceptance_recomputes_all_cells_and_cluster_observations_on_small_fixture(rq3_fixture):
    root, output, context, documents = rq3_fixture
    evidence = Evidence(root)
    check_rq3(root, output, context, evidence, documents)
    exempt = ("frozen headline", "actual opportunity bounds", "inheritance unknown and real")
    assert all(row["passed"] for row in evidence.checks if not any(name in row["requirement"] for name in exempt)), [row["requirement"] for row in evidence.checks if not row["passed"]]


@pytest.mark.parametrize("change,requirement", [("retention", "complete duration"), ("delay", "conditional delays"),
    ("cross", "entry-local H1/H2"), ("clusters", "bootstrap clusters match")])
def test_rq3_acceptance_rejects_missing_retention_wrong_interval_crosscell_and_cluster_assignment(rq3_fixture, change, requirement):
    root, output, context, documents = rq3_fixture
    if change == "retention":
        documents["rq3_persistence_summary"]["populations"]["formal"]["retention"].clear()
    elif change == "delay":
        documents["rq3_maintenance_results"]["populations"]["formal"]["target"]["by_grade"]["B"]["delay_records"][0]["upper_days"] = 999
    elif change == "cross":
        documents["h1_h2_summary"]["maintenance_target_cross_tabs"]["entry_configuration_coverage"][0]["entries"] = 999
    else:
        path = output / "estimand_registry.json"
        registry = json.loads(path.read_text(encoding="utf-8"))
        row = next(row for row in registry["metrics"] if row["clusters"])
        row["clusters"] = {"2":next(iter(row["clusters"].values()))}
        write_json(path, registry)
    evidence = Evidence(root)
    check_rq3(root, output, context, evidence, documents)
    assert any(not row["passed"] and requirement in row["requirement"] for row in evidence.checks)


def test_csv_reconstruction_handles_union_columns_nulls_and_real_tampering(tmp_path):
    path = tmp_path / "rows.csv"
    path.write_text('repository_id,extra,values\n1,,"[1, 2]"\n2,visible,\n', encoding="utf-8")
    rows = [dict(repository_id="1", values=[1, 2]), dict(repository_id="2", extra="visible")]
    assert csv_matches(path, rows)
    assert not csv_matches(path, [*rows, rows[0]])
    assert not csv_matches(path, [dict(rows[0], values=[1, 3]), rows[1]])
    assert not equivalent({"estimate": None, "metric_key": "missing"}, 0)
    assert equivalent({"estimate": .5, "metric_key": "point"}, .5)


def test_evidence_rejects_file_replacement_after_first_binding(tmp_path):
    path = tmp_path / "evidence.json"
    write_json(path, dict(value=1))
    evidence = Evidence(tmp_path)
    evidence.bind(path)
    write_json(path, dict(value=2))
    with pytest.raises(ValueError, match="changed during acceptance"):
        evidence.bind(path)


@pytest.fixture
def bootstrap_fixture(tmp_path, monkeypatch):
    root, output = tmp_path, tmp_path / "batch"
    output.mkdir()
    formal, raw = {str(i) for i in range(1, 456)}, {str(i) for i in range(1, 458)}
    context = dict(freeze_manifest=dict(raw_repository_count=457, formal_repository_count=455,
            formal_episode_kind_counts=dict(post_anchor_born_fixed=dict(episodes=65)),
            input_sha256={"data/derived/tables-v4/repositories.csv": "0" * 64}),
        formal_ids=formal, raw_ids=raw, populations=dict(formal=sorted(formal), raw=sorted(raw),
        formal_exclude_pilot=sorted(formal - {str(i) for i in range(1, 50)})), metrics=[],
        study=dict(event_anchor="2025-03-14T00:00:00Z", window_end="2026-03-03T00:00:00Z"))
    point = add_ratio(context, "rq1.test", [("1", 1, 1), ("2", 0, 1), ("3", 0, 1)], binary=True)
    first = cluster_bootstrap(context["metrics"], context["populations"])
    for name in ("uncertainty_round_one", "uncertainty_round_two", "uncertainty_intervals"):
        write_json(output / (name + ".json"), first)
    write_json(output / "bootstrap_reproducibility.json", dict(seed=20261018, replicates=2000, metrics=1,
        round_one_sha256=sha256(output / "uncertainty_round_one.json"), round_two_sha256=sha256(output / "uncertainty_round_two.json")))
    write_json(output / "estimand_registry.json", dict(populations=context["populations"], metrics=context["metrics"]))
    documents = {name: {} for name in SUMMARIES}
    documents["rq1_summary"] = dict(point=dict(estimate=point, metric_key="rq1.test"))
    documents["uncertainty_intervals"] = first
    documents["robustness_checks"] = dict(scenarios={name: dict(repositories=457 if name == "raw_main" else 406 if name == "exclude_pilot" else 455,
        rq1=dict(repositories=457 if name == "raw_main" else 406 if name == "exclude_pilot" else 455),
        rq2=dict(repositories=457 if name == "raw_main" else 406 if name == "exclude_pilot" else 455),
        rq3_persistence={}, rq3_maintenance={}, settings={}) for name in SCENARIOS})
    from pintrace.formal_rq3 import DAY, number
    anchor, end = number(context["study"]["event_anchor"]), number(context["study"]["window_end"])
    policy, immutable = number("2025-08-15T00:00:00Z"), number("2025-10-28T00:00:00Z")
    settings = {"formal_main":dict(anchor=anchor,end=end),"raw_main":dict(anchor=anchor,end=end),"exclude_pilot":dict(anchor=anchor,end=end),
        "anchor_minus_one_day":dict(anchor=anchor-DAY),"anchor_plus_one_day":dict(anchor=anchor+DAY),"exclude_incident_day":dict(exclude_event_day=True),
        "after_first_seven_days":dict(start_after_days=7),"after_first_thirty_days":dict(start_after_days=30),
        "before_sha_policy":dict(period_start=anchor,period_end=policy-1),"sha_policy_before_immutable":dict(period_start=policy,period_end=immutable-1),
        "after_immutable_releases":dict(period_start=immutable,period_end=end)}
    for name, settings in settings.items():
        documents["robustness_checks"]["scenarios"][name]["settings"] = settings
    (root / "study.toml").write_text("fixture study\n", encoding="utf-8")
    for name, value in documents.items():
        write_json(output / (name + ".json"), value)
    for name in ("reports/pilot_candidates.csv", "data/derived/tables-v4/repositories.csv"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    monkeypatch.setattr("pintrace.formal_robustness.pilot_ids", lambda root, tables: {str(i) for i in range(1, 50)} | {"457"})
    return root, output, context, documents


def test_bootstrap_checks_real_rounds_points_exact_intervals_and_matrix(bootstrap_fixture):
    root, output, context, documents = bootstrap_fixture
    evidence = Evidence(root)
    check_bootstrap(output, context, evidence, documents)
    assert evidence.checks and all(row["passed"] for row in evidence.checks)


@pytest.mark.parametrize("change,requirement", [
    ("round_bytes", "actual two-round"), ("point", "recomputed point"),
    ("exact", "recomputed point"), ("metric_link", "all published point"), ("matrix", "eleven actual")])
def test_bootstrap_detects_changed_bytes_points_binomial_links_and_missing_scenarios(bootstrap_fixture, change, requirement):
    root, output, context, documents = bootstrap_fixture
    if change == "round_bytes":
        with (output / "uncertainty_round_two.json").open("a", encoding="utf-8") as stream:
            stream.write("\n")
    elif change in {"point", "exact"}:
        value = documents["uncertainty_intervals"]
        if change == "point":
            value["metrics"]["rq1.test"]["estimate"] = .9
        else:
            value["metrics"]["rq1.test"]["exact_binomial"]["denominator"] = 455
        write_json(output / "uncertainty_intervals.json", value)
    elif change == "metric_link":
        documents["rq1_summary"]["point"]["estimate"] = .8
    else:
        documents["robustness_checks"]["scenarios"].pop("anchor_plus_one_day")
    evidence = Evidence(root)
    check_bootstrap(output, context, evidence, documents)
    assert any(not row["passed"] and requirement in row["requirement"] for row in evidence.checks)


@pytest.fixture
def manuscript_fixture(tmp_path):
    root, output = tmp_path, tmp_path / "batch"
    output.mkdir()
    (root / "reports").mkdir()
    (root / "pintrace").mkdir()
    (root / "pintrace/formal_manuscript.py").write_text("fixture implementation\n", encoding="utf-8")
    context = dict(guard=dict(freeze_sha256="frozen"), freeze_manifest=dict(closed_models=dict(Cox="closed")))
    docs = {name: dict(count=455) for name in SUMMARIES}
    sources = {}
    for name, value in docs.items():
        path = output / (name + ".json")
        write_json(path, value)
        sources[name] = dict(path=path, sha256=sha256(path), data=value)
    builder = Artifacts(root, {key:value["path"] for key,value in sources.items()})
    builder.table("sample", "Actual source-derived sample", ["unit", "count"], [["formal", builder.cell("rq1_summary", "/count")]])
    figure = dict(artifacts=builder.artifacts, input_sha256={str(v["path"]):v["sha256"] for v in sources.values()})
    write_json(output / "figure_manifest.json", figure)
    for name, path, value in (("figure_manifest", output / "figure_manifest.json", figure),
        ("freeze_manifest", root / "reports/freeze_manifest.json", dict(status="frozen")),
        ("followup", root / "reports/maintenance_followup_audit.json", dict(status="passed")),
        ("source_catalog", output / "source_catalog.json", dict(development_checkpoint_repositories=152))):
        write_json(path, value)
        sources[name] = dict(path=path, sha256=sha256(path), data=value)
    for name, relative in HISTORICAL_SOURCES.items():
        path = root / relative
        write_json(path, dict(count=12 if name == "maintenance_scope" else 152))
        sources[name] = dict(path=path, sha256=sha256(path), data=json.loads(path.read_text(encoding="utf-8")))
    context["freeze_manifest"]["input_sha256"] = {HISTORICAL_SOURCES["maintenance_scope"]:sources["maintenance_scope"]["sha256"]}
    writer = Writer(sources)
    text = "# Empirical study\n" + "\n".join("## " + chapter + "\n" +
        "We describe observed configuration changes with explicit unknown states and bounded interpretation.\n" for chapter in CHAPTERS)
    text += "### Revisions after the development checkpoint\nThe " + writer.value("source_catalog", "/development_checkpoint_repositories")
    text += " checkpoint preceded identity eligibility and maintenance view revisions, which are disclosed as post-hoc changes.\n"
    text += "The observed cohort contains " + writer.value("rq1_summary", "/count") + " repositories.\n"
    text += "The maintenance record count is " + writer.value("maintenance_scope", "/count")
    text += "; the historical table count is " + writer.value("development_checkpoint", "/count") + ".\n"
    (output / "manuscript.md").write_text(text, encoding="utf-8")
    mapping = dict(claims=writer.claims, input_sha256={str(v["path"]):v["sha256"] for v in sources.values()},
        implementation_sha256=sha256(root / "pintrace/formal_manuscript.py"), freeze_sha256="frozen")
    literature = root / "primary-literature.pdf"
    literature.write_bytes(b"%PDF-not-json\x00\xff")
    mapping["input_sha256"][str(literature)] = sha256(literature)
    write_json(output / "claim_evidence_map.json", mapping)
    audit = dict(manuscript_sha256=sha256(output / "manuscript.md"), claim_map_sha256=sha256(output / "claim_evidence_map.json"),
        closed_analyses=context["freeze_manifest"]["closed_models"])
    write_json(output / "manuscript_audit.json", audit)
    write_json(output / "bootstrap_reproducibility.json", dict(passed=True))
    for path in output.glob("*.json"):
        shutil.copyfile(path, root / "reports" / path.name)
    shutil.copyfile(output / "manuscript.md", root / "paper/manuscript.md")
    return root, output, context, docs


def test_manuscript_reaudits_actual_claims_cells_chapters_and_publication(manuscript_fixture):
    root, output, context, docs = manuscript_fixture
    evidence = Evidence(root)
    check_manuscript(root, output, context, evidence, docs)
    assert all(row["passed"] for row in evidence.checks)
    aliases = next(row for row in evidence.checks if row["requirement"].startswith("historical source aliases"))
    assert aliases["details"]["maintenance_scope"]["original_freeze_bound"] is True
    assert aliases["details"]["development_checkpoint"]["original_freeze_bound"] is False
    assert HISTORICAL_SOURCES["development_checkpoint"] in evidence.inputs


def test_manuscript_historical_alias_requires_actual_frozen_maintenance_hash(manuscript_fixture):
    root, output, context, docs = manuscript_fixture
    write_json(root / HISTORICAL_SOURCES["maintenance_scope"], dict(count=999))
    evidence = Evidence(root)
    check_manuscript(root, output, context, evidence, docs)
    assert any(not row["passed"] and row["requirement"].startswith("historical source aliases") for row in evidence.checks)


@pytest.mark.parametrize("change,requirement", [("numeric", "numeric-token"), ("closed", "numeric-token"),
    ("published", "published paper"), ("chapter", "complete substantive"), ("csv_cell", "real figure/table")])
def test_manuscript_rejects_unbound_claims_closed_assertions_publication_and_rebound_bad_csv(manuscript_fixture, change, requirement):
    root, output, context, docs = manuscript_fixture
    if change in {"numeric", "closed", "chapter"}:
        path = output / "manuscript.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace("## Introduction", "## Missing introduction") if change == "chapter" else text + (
            "\nWe found 99 affected repositories.\n" if change == "numeric" else "\nWe fitted Cox regression and estimated a causal effect.\n")
        path.write_text(text, encoding="utf-8")
    elif change == "published":
        (root / "paper/manuscript.md").write_text("Different paper", encoding="utf-8")
    else:
        path = root / "paper/tables/sample.csv"
        path.write_text(path.read_text(encoding="utf-8").replace("455", "999"), encoding="utf-8")
        manifest = json.loads((output / "figure_manifest.json").read_text(encoding="utf-8"))
        for artifact in manifest["artifacts"]:
            artifact["data_sha256"] = sha256(path)
            if artifact["kind"] == "csv_table":
                artifact["sha256"] = sha256(path)
        write_json(output / "figure_manifest.json", manifest)
    evidence = Evidence(root)
    check_manuscript(root, output, context, evidence, docs)
    assert any(not row["passed"] and requirement in row["requirement"] for row in evidence.checks)


def test_run_missing_real_outputs_writes_failure_report_and_raises(tmp_path):
    root, output = tmp_path, tmp_path / "batch"
    (root / "reports").mkdir()
    (root / "pintrace").mkdir()
    (root / "study.toml").write_text('[study]\nwindow_end="2026-03-03T00:00:00Z"\n', encoding="utf-8")
    (root / "reports/risksets.csv").write_text("row_kind\n", encoding="utf-8")
    for name in ("formal_acceptance", "formal_common", "formal_stats", "formal_rq12", "formal_rq3", "formal_robustness", "formal_figures", "formal_manuscript"):
        (root / "pintrace" / (name + ".py")).write_text("fixture source\n", encoding="utf-8")
    write_json(root / "reports/freeze_manifest.json", dict(status="frozen", formal_population=dict(
        formal_eligible_repository_ids=["1"], raw_confirmed_repository_ids=["1"]),
        input_sha256={name:sha256(root/name) for name in ("study.toml", "reports/risksets.csv")}))
    context = make_context(root, verify_frozen_inputs(root))
    with pytest.raises(ValueError, match="formal acceptance failed"):
        run(root, output, context)
    report = json.loads((output / "formal_acceptance.json").read_text(encoding="utf-8"))
    assert report["acceptance_passed"] is False
    assert all(row["passed"] is False for row in report["requirements"].values())
