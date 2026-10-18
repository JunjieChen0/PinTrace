"""Recompute the S1--S4 acceptance gates from actual batch artifacts."""

from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path
import re

from .formal_common import require_context, sha256, write_json

RULES_VERSION = "formal-acceptance-v1"
SUMMARIES = ("rq1_summary", "rq2_summary", "rq3_persistence_summary", "rq3_maintenance_results",
             "h1_h2_summary", "robustness_checks", "uncertainty_intervals")
SCENARIOS = {"formal_main", "raw_main", "exclude_pilot", "anchor_minus_one_day", "anchor_plus_one_day",
             "exclude_incident_day", "after_first_seven_days", "after_first_thirty_days", "before_sha_policy",
             "sha_policy_before_immutable", "after_immutable_releases"}
CHAPTERS = ("Introduction", "Background and Related Work", "Data, Scope, and Measurement",
            "Direct Incident-Linked Response", "Extension Beyond the Incident Action",
            "Persistence and Opportunity-Conditioned Maintenance", "Configuration and Version-Information Evidence",
            "Uncertainty and Robustness", "Discussion", "Threats to Validity and Method Revisions", "Conclusion", "References")
HISTORICAL_SOURCES = {"maintenance_scope": "reports/maintenance_scope_summary.json",
                      "development_checkpoint": "data/derived/analysis-v4/checkpoints/152-20261018T113552Z/reports/table_manifest.json"}


def equivalent(actual, expected):
    """Compare recomputed values, allowing the documented metric wrapper."""
    if isinstance(actual, dict) and "estimate" in actual and not isinstance(expected, dict):
        actual = actual["estimate"]
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and equivalent(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(equivalent(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, (int, float)) and not isinstance(expected, bool) and isinstance(actual, (int, float)):
        return math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)
    return actual == expected


def csv_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def csv_value(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else "" if value is None else str(value)


def csv_matches(path, expected):
    expected = list(expected)
    actual = csv_rows(path)
    fields = {key for row in expected for key in row} or {"repository_id"}
    return len(actual) == len(expected) and all(
        set(row) == fields and all(row[key] == csv_value(wanted.get(key)) for key in fields)
        for row, wanted in zip(actual, expected))


class Evidence:
    def __init__(self, root):
        self.root, self.checks, self.inputs = Path(root).resolve(), [], {}

    def bind(self, path):
        path = Path(path).resolve()
        name = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else str(path)
        digest = sha256(path)
        if name in self.inputs and self.inputs[name] != digest:
            raise ValueError("Evidence changed during acceptance: " + name)
        self.inputs[name] = digest
        return dict(path=name, sha256=digest)

    def load(self, path):
        self.bind(path)
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def check(self, section, name, passed, paths, details=None):
        self.checks.append(dict(section=section, requirement=name, passed=bool(passed),
                                evidence=[self.bind(path) for path in paths], details=details))


def metric_sources_match(output, metrics, evidence):
    registry = evidence.load(output / "estimand_registry.json")
    actual = {row["key"]: row for row in registry["metrics"]}
    return all(row["key"] in actual and equivalent(actual[row["key"]], row) for row in metrics)


def rq12_point_links(rq1, rq2):
    """Map the documented RQ12 scalar fields to their registered estimands."""
    links = []
    for report in (rq1, rq2):
        if "metric_prefix" not in report:
            continue
        prefix = report["metric_prefix"]
        def add(suffix, value):
            links.append((prefix + suffix, value.get("estimate") if isinstance(value, dict) else value))
        if report is rq1:
            for field, suffix in (("first_response_proportions", ".first_response."),
                ("full_window_combination_proportions", ".combination."), ("full_window_behavior_repository_proportions", ".behavior.")):
                for category, value in report[field].items():
                    add(suffix + category, value)
            for day, cell in report["horizons"].items():
                for field, suffix in (("observed_response_lower", "lower"), ("response_upper_bound", "upper"), ("complete_case_response", "complete")):
                    add(f".horizon.{day}.{suffix}", cell[field])
            for day, cell in report["target_state_horizons"].items():
                for category, value in cell["proportions"].items():
                    add(f".target_state.{day}.{category}", value)
            for q, value in report["response_time_days_quantiles"].items():
                add(".response_days." + q, value)
        else:
            existing, new = report["existing"], report["new_actions"]
            for field in ("repository_equal", "reference_weighted"):
                for category, value in existing[field].items():
                    add(".existing." + field + "." + category, value)
            for day, cell in existing["horizons"].items():
                for field, suffix in (("observed_pin_cumulative", "pin.lower"), ("upper_bound", "pin.upper"), ("repository_equal_observed_pin", "pin.repository_equal")):
                    add(f".existing.horizon.{day}.{suffix}", cell[field])
                for field, suffix in (("reference_weighted_outcomes", "reference_weighted"), ("repository_equal_outcomes", "repository_equal")):
                    for category, value in cell[field].items():
                        add(f".existing.horizon.{day}.{suffix}.{category}", value)
            for q, value in existing["first_pin_days_quantiles"].items():
                add(".existing.first_pin_days." + q, value)
            for category, value in new["repository_equal"].items():
                add(".new.repository_equal." + category, value)
            for category, value in new["reference_weighted"].items():
                weighting = "reference_weighted" if category in {"full_sha_known_groups", "all_full_sha_group_reference_weighted"} else "observed_reference_weighted"
                add(".new." + weighting + "." + category, value)
    return links


def point_links(documents):
    links = []
    def walk(value):
        if isinstance(value, dict):
            if value.get("metric_key"):
                links.append((value["metric_key"], value.get("estimate", value.get("observed_fraction"))))
            for name, key in value.get("metric_keys", {}).items():
                links.append((key, value["quantiles"][name]))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    for name in SUMMARIES[:-1]:
        walk(documents[name])
    for population in ("formal", "raw"):
        links.extend(rq12_point_links(documents["rq1_summary"].get(population, {}), documents["rq2_summary"].get(population, {})))
    for scenario in documents["robustness_checks"]["scenarios"].values():
        links.extend(rq12_point_links(scenario["rq1"], scenario["rq2"]))
    return links


def check_rq12(root, output, context, evidence, documents):
    from .formal_rq12 import _record_line, describe_repository, summarize

    paths = [output / (name + ".json") for name in ("rq1_summary", "rq2_summary")]
    cache = output / "rq12_repository_records.jsonl"
    receipt_path = output / "rq12_record_cache_receipt.json"
    receipt = evidence.load(receipt_path)
    records = context["rq12_records"]
    digest = hashlib.sha256()
    for record in records:
        digest.update(_record_line(record).encode("utf-8"))
    evidence.check("S1", "source-bound actual compact cache", digest.hexdigest() == sha256(cache) == receipt["sha256"]
        and receipt["freeze_sha256"] == context["guard"]["freeze_sha256"]
        and receipt["implementation_sha256"] == sha256(root / "pintrace/formal_rq12.py"), [cache, receipt_path, *paths])
    rows = {population: [describe_repository(record) for record in records if record["repository_id"] in context[population + "_ids"]]
            for population in ("formal", "raw")}
    for key, filename in (("rq1", "rq1_repository_results.csv"), ("references", "rq2_existing_reference_results.csv"), ("new_actions", "rq2_new_action_results.csv")):
        expected = [dict(population=population, **row) for population, group in rows.items()
                    for result in group for row in ([result[key]] if key == "rq1" else result[key])]
        evidence.check("S1", "actual row reconstruction: " + key, csv_matches(output / filename, expected), [output / filename, cache])
    born = {(row["repository_id"], row["lineage_id"]) for row in csv_rows(root / "reports/risksets.csv")
            if row["row_kind"] == "episode" and row["episode_kind"] == "post_anchor_born_fixed" and row["repository_id"] in context["formal_ids"]}
    for population, group in rows.items():
        ids = context[population + "_ids"]
        local = dict(metrics=[], populations=context["populations"])
        recalculated = summarize(records, ids, context=local, population=population)
        rq1, rq2 = documents["rq1_summary"][population], documents["rq2_summary"][population]
        evidence.check("S1", population + " RQ1 partitions, horizon risk/censoring and response quantiles",
            equivalent(rq1, recalculated["rq1"]) and sum(rq1["first_response_counts"].values()) == len(ids)
            and sum(rq1["full_window_combination_counts"].values()) == len(ids)
            and all(sum(row["counts"].values()) == row["denominator"] == len(ids) for row in rq1["horizons"].values())
            and set(rq1["horizons"]) == {"7", "30", "90", "180", "365"}, paths + [cache])
        evidence.check("S1", population + " RQ2 fixed denominator and paired weighting", equivalent(rq2, recalculated["rq2"]), paths + [cache])
        primary = [row for value in group for key in ("references", "new_actions") for row in value[key]]
        contaminated = [row for row in primary if row["action"] == context["study"]["target_action"]
                        or (row["repository_id"], row.get("lineage_id")) in born
                        or any((row["repository_id"], lid) in born for lid in row.get("lineage_ids", []))]
        evidence.check("S1", population + " target and target-born isolation", not contaminated,
                       paths + [root / "reports/risksets.csv"], dict(contaminated_rows=len(contaminated), formal_target_born_lineages=len(born)))
        new = [row for value in group for row in value["new_actions"]]
        repositories = {row["repository_id"] for row in new}
        evidence.check("S1", population + " new Action denominator excludes no-new repositories",
            rq2["new_actions"]["repositories_with_new_groups"] == len(repositories)
            and rq2["new_actions"]["no_new_group_repositories"] == len(ids - repositories)
            and all(not row["excluded_copy_or_reintroduction"] for row in new), paths + [cache])
        evidence.check("S1", population + " bootstrap clusters match actual repository/reference observations",
            metric_sources_match(output, local["metrics"], evidence), paths + [cache, output / "estimand_registry.json"])
    evidence.check("S1", "frozen formal/raw and target-born cohort sizes",
        (len(context["formal_ids"]), len(context["raw_ids"]), len(born)) == (455, 457, 65),
        [root / "reports/freeze_manifest.json", root / "reports/risksets.csv"])


def check_rq3(root, output, context, evidence, documents):
    from .formal_rq3 import (DAY, FIELDS, INPUTS, _condition_tables, _duration_eligible, _entry_counts, _episode_counts, _project_entry,
                             _role_entries, _tool_cases, number, summarize_maintenance, summarize_persistence)
    from .formal_common import add_ratio

    require_context(root, context, inputs=INPUTS)
    paths = [output / (name + ".json") for name in ("rq3_persistence_summary", "rq3_maintenance_results", "h1_h2_summary")]
    episodes = [row for row in csv_rows(root / "reports/risksets.csv") if row["row_kind"] == "episode"]
    clocks = set(evidence.load(root / "reports/opportunity_join_summary.json")["clock_unalignable_repository_ids"])
    eligible = {row["episode_id"] for row in episodes if _duration_eligible(row, clocks)}
    maintenance_path = root / INPUTS[-1]
    with maintenance_path.open(encoding="utf-8") as stream:
        entries = [_project_entry(json.loads(line)) for line in stream if line.strip()]
    evidence.bind(maintenance_path)
    scope = evidence.load(root / "reports/rq3_scope_counts.json")["formal_scope"]
    end = number(context["study"]["window_end"])
    local = dict(metrics=[], populations=context["populations"], rq3_data=dict(episodes=episodes), study=context["study"])
    for population in ("formal", "raw"):
        ids = context[population + "_ids"]
        selected = [row for row in episodes if row["repository_id"] in ids]
        duration = [row for row in selected if row["episode_id"] in eligible]
        persistence = documents["rq3_persistence_summary"]["populations"][population]
        evidence.check("S2", population + " identity/duration exit tables from frozen episodes",
            equivalent(persistence["identity"], _episode_counts(selected)) and equivalent(persistence["duration"], _episode_counts(duration))
            and all(equivalent(persistence["by_kind"][kind][layer], _episode_counts([r for r in source if r["episode_kind"] == kind]))
                    for kind in persistence["by_kind"] for layer, source in (("identity", selected), ("duration", duration))), paths + [root / "reports/risksets.csv"])
        recalculated = summarize_persistence(selected, eligible, local, population)
        evidence.check("S2", population + " complete duration quantiles, retention and competing/unknown trajectories",
            equivalent(persistence, recalculated), paths + [root / "reports/risksets.csv"])
        projected = _role_entries(entries, ids, {r["episode_id"] for r in selected})
        recalculated_maintenance = summarize_maintenance(projected, local, population)
        evidence.check("S2", population + " complete maintenance outcome/conditional-delay/horizon tables",
            equivalent(documents["rq3_maintenance_results"]["populations"][population], recalculated_maintenance), paths + [maintenance_path])
        for role, field in (("target", "target_ids"), ("comparison", "focal_links")):
            source = [row for row in projected if row[field]]
            block = documents["rq3_maintenance_results"]["populations"][population][role]
            evidence.check("S2", population + " " + role + " maintenance entries/outcomes",
                equivalent(block["counts"], _entry_counts(source, role)), paths + [maintenance_path])
            for grade in ("A", "B"):
                group = [r for r in source if r["evidence_grade"] == grade]
                result = block["by_grade"][grade]
                wanted = []
                for row in group:
                    l, u, t = (number(row[key]) for key in ("available_after_or_at", "available_by", "outcome_timestamp"))
                    if row["outcome"] == "adopted_target" and None not in (l, u, t) and l <= u <= t:
                        wanted.append(dict(repository_id=row["repository_id"], lineage_id=row["lineage_id"], opportunity_id=row["opportunity_id"],
                            lower_days=(t-u)/DAY, upper_days=(t-l)/DAY, proxy_days=(t-u)/DAY, upper_inclusive=grade == "A"))
                evidence.check("S2", population + " " + role + " grade " + grade + " conditional delays",
                    result["counts"]["entries"] == len(group) and result["counts"]["outcome_counts"] == dict(Counter(r["outcome"] for r in group))
                    and equivalent(result["delay_records"], wanted)
                    and (all(r["lower_days"] == r["upper_days"] for r in wanted) if grade == "A" else result["delay_quantiles"]["interval"] == "[T-U,T-L)"), paths + [maintenance_path])
        if population == "formal":
            target = [r for r in projected if r["target_ids"]]
            for day in (30, 90, 180):
                horizon = documents["rq3_maintenance_results"]["populations"][population]["horizons"][str(day)]
                potential = [r for r in target if end - number(r["available_by"]) >= day*DAY]
                censored = [r for r in target if r["outcome"] == "right_censored_no_observed_sha_change" and number(r["outcome_timestamp"]) - number(r["available_by"]) >= day*DAY]
                evidence.check("S2", str(day) + "-day actual opportunity bounds",
                    horizon["calendar_potential"]["entries"] == len(potential)
                    and horizon["right_censored_observed_followup"]["entries"] == len(censored)
                    and ((len(potential), len(censored)) == (84, 7) if day == 30 else len(potential) == len(censored) == 0), paths + [maintenance_path])
    p, m, h = (documents[name] for name in ("rq3_persistence_summary", "rq3_maintenance_results", "h1_h2_summary"))
    f = p["populations"]["formal"]
    target = m["populations"]["formal"]["target"]
    born = [r for r in episodes if r["repository_id"] in context["formal_ids"] and r["episode_kind"] == "post_anchor_born_fixed" and r["exit_kind"] == "unpin"]
    evidence.check("S2", "frozen headline scope and all three born-fixed unpins retained",
        (f["identity"]["episodes"], f["duration"]["episodes"], target["counts"]["entries"], m["populations"]["formal"]["comparison"]["counts"]["entries"],
         target["by_grade"]["A"]["counts"]["entries"], target["by_grade"]["B"]["counts"]["entries"]) == (307, 297, 285, 689, 239, 46)
        and len(born) == 3 and {r["repository_id"] for r in born} == {"58132544"}
        and not any(r["episode_id"] in eligible for r in born) and p["born_fixed_unpin"] == dict(identity=3, duration=0, excluded=3, repository_ids=["58132544"]), paths)
    evidence.check("S2", "three comparison layers, pre-U diagnostics and closed long maintenance",
        p["comparisons"] == dict(riskset=scope["riskset_comparisons"], maintenance=scope["maintenance_comparisons"])
        and m["B_pre_upper_exclusions"] == scope["B_pre_upper_exclusions"] and m["B_pre_upper_diagnostics"] == scope["B_pre_upper_diagnostics"]
        and m["closed_maintenance_horizons"] == [90, 180] and m["regression_opened"] is False, paths + [root / "reports/rq3_scope_counts.json"])
    formal = [r for r in episodes if r["repository_id"] in context["formal_ids"]]
    conditions, cross = _condition_tables(formal, _role_entries(entries, context["formal_ids"], {r["episode_id"] for r in formal}), local)
    condition_ok = all(dict(Counter((r.get(field) or "unknown") for r in source)) == {r["category"]:r["episodes"] for r in h["episode_entry_conditions"][layer][field]}
        for field in FIELDS for layer, source in (("identity", formal), ("duration", [r for r in formal if r["episode_id"] in eligible])))
    condition_ok &= equivalent(h["episode_entry_conditions"]["identity"], conditions) and equivalent(h["maintenance_target_cross_tabs"], cross)
    episode_index = {r["episode_id"]: r for r in formal}
    formal_entries = _role_entries(entries, context["formal_ids"], set(episode_index))
    for field in FIELDS:
        groups = defaultdict(list)
        for row in formal_entries:
            if not row["target_ids"]:
                continue
            categories = {episode_index[eid].get(field) or "unknown" for eid in row["target_ids"]}
            category = next(iter(categories)) if len(categories) == 1 else "mixed_episode_entry_conditions"
            groups[(category, row["evidence_grade"])].append(row)
        expected = [dict(category=category, grade=grade, entries=len(group),
            episodes=len({eid for r in group for eid in r["target_ids"]}), outcome_counts=dict(Counter(r["outcome"] for r in group)))
            for (category, grade), group in sorted(groups.items())]
        condition_ok &= equivalent(h["maintenance_target_cross_tabs"][field], expected)
    quality = evidence.load(root / "reports/full_replay_report.json")["repository_checks"]
    inherited = True
    for label, ids in (("physical", context["raw_ids"]), ("formal", context["formal_ids"])):
        group = [r for r in quality if str(r["repository_id"]) in ids]
        point = add_ratio(local, f"h1.inheritance.{label}.snapshot_fraction",
            [(r["repository_id"], r["quality"]["inheritance_unknown_snapshots"], r["quality"]["snapshots"]) for r in group],
            population="raw" if label == "physical" else "formal", unit="snapshot")
        inherited &= equivalent(h["inheritance_unknown_snapshots"][label], dict(snapshots=sum(r["quality"]["snapshots"] for r in group),
            inheritance_unknown_snapshots=sum(r["quality"]["inheritance_unknown_snapshots"] for r in group), repositories=len(group), snapshot_fraction=point))
    tool_path = root / "reports/tool_replay_results.json"
    evidence.check("S2", "entry-local H1/H2 categories, inheritance unknown and real historical-tool outputs",
        condition_ok and inherited and h["inheritance_unknown_snapshots"]["physical"]["inheritance_unknown_snapshots"] == 35904
        and h["tool_replays"] == _tool_cases(evidence.load(tool_path)) and len(h["tool_replays"]) == 12, paths + [tool_path, root / "reports/full_replay_report.json"])
    evidence.check("S2", "RQ3/H1/H2 bootstrap clusters match frozen episode and maintenance observations",
        metric_sources_match(output, local["metrics"], evidence), paths + [maintenance_path, output / "estimand_registry.json"])


def check_bootstrap(output, context, evidence, documents):
    import numpy as np
    from scipy.stats import beta
    from .formal_robustness import pilot_ids

    registry_path = output / "estimand_registry.json"
    registry = evidence.load(registry_path)
    paths = [output / (name + ".json") for name in ("uncertainty_round_one", "uncertainty_round_two", "uncertainty_intervals", "bootstrap_reproducibility")]
    first, _, uncertainty, receipt = [evidence.load(path) for path in paths]
    evidence.check("S3", "actual two-round bootstrap bytes, seed and percentile method",
        sha256(paths[0]) == sha256(paths[1]) == sha256(paths[2]) == receipt["round_one_sha256"] == receipt["round_two_sha256"]
        and (first["seed"], first["replicates"], first["confidence_level"], first["interval"]) == (20261018, 2000, .95, "percentile")
        and receipt["seed"] == 20261018 and receipt["replicates"] == 2000 and receipt["metrics"] == len(first["metrics"]), paths)
    metrics = registry["metrics"]
    keys = [metric["key"] for metric in metrics]
    evidence.check("S3", "unique estimand registry and actual cluster sufficient statistics",
        len(keys) == len(set(keys)) and set(keys) == set(uncertainty["metrics"]) and equivalent(registry["populations"], context["populations"]), paths + [registry_path])
    valid = True
    for metric in metrics:
        result = uncertainty["metrics"][metric["key"]]
        clusters = metric["clusters"]
        values = [v for group in clusters.values() for v in group]
        if metric["kind"] == "ratio":
            denominator = sum(group[1] for group in clusters.values())
            estimate = sum(group[0] for group in clusters.values()) / denominator if denominator else None
        else:
            estimate = float(np.quantile(values, metric["q"], method="linear")) if values else None
        valid &= equivalent(metric["estimate"], estimate) and equivalent(result["estimate"], estimate)
        valid &= set(clusters) <= set(registry["populations"][metric["population"]]) and result["n_clusters"] == len(clusters)
        valid &= result["population_clusters"] == len(registry["populations"][metric["population"]]) and result["requested_replicates"] == 2000
        valid &= result["status"] in {"no_observations", "sparse", "estimated", "zero_degenerate", "constant_degenerate"}
        valid &= 0 <= result["valid_replicates"] <= 2000
        if result["valid_replicates"]:
            valid &= result["lower"] is not None and result["upper"] is not None and result["lower"] <= result["upper"]
        else:
            valid &= result["lower"] is result["upper"] is None
        if result["status"] in {"zero_degenerate", "constant_degenerate"}:
            valid &= equivalent(result["lower"], result["upper"]) and equivalent(result["lower"], estimate)
        if result["status"] == "no_observations":
            valid &= not clusters and estimate is None
        if metric.get("binary") and clusters:
            successes, n = int(sum(row[0] for row in clusters.values())), len(clusters)
            valid &= all(d == 1 and v in (0, 1) for v, d in clusters.values()) and equivalent(result["exact_binomial"], dict(successes=successes, denominator=n,
                lower=0. if successes == 0 else float(beta.ppf(.025, successes, n-successes+1)),
                upper=1. if successes == n else float(beta.ppf(.975, successes+1, n-successes))))
    evidence.check("S3", "recomputed point estimates, risk clusters, degeneracy and exact binary intervals", valid, paths + [registry_path])
    links = point_links(documents)
    evidence.check("S3", "all published point/quantile metric links equal uncertainty points",
        bool(links) and all(key in uncertainty["metrics"] and equivalent(value, uncertainty["metrics"][key]["estimate"]) for key, value in links)
        and {key for key, _ in links} == set(keys), [output / (name + ".json") for name in SUMMARIES] + [registry_path])
    root = evidence.root
    pilots = pilot_ids(root)
    robustness = documents["robustness_checks"]
    matrix = robustness["scenarios"]
    evidence.check("S3", "eleven actual sensitivity outputs and resolved pilot exclusion",
        set(matrix) == SCENARIOS and len(pilots) == 50 and len(pilots & context["formal_ids"]) == 49
        and matrix["formal_main"]["repositories"] == 455 and matrix["raw_main"]["repositories"] == 457
        and matrix["exclude_pilot"]["repositories"] == 406
        and all(set(row) >= {"rq1", "rq2", "rq3_persistence", "rq3_maintenance", "settings"}
            and row["rq1"]["repositories"] == row["repositories"] == row["rq2"]["repositories"] for row in matrix.values()),
        [output / "robustness_checks.json", root / "reports/pilot_candidates.csv", root / "data/derived/tables-v4/repositories.csv"])
    if "rq12_records" in context:
        from .formal_rq12 import summarize
        valid, reconstructed = True, []
        for row in matrix.values():
            local = dict(metrics=[], populations=context["populations"])
            expected = summarize(context["rq12_records"], set(context["populations"][row["population"]]),
                                 row["settings"], context=local, population=row["population"])
            valid &= equivalent(row["rq1"], expected["rq1"]) and equivalent(row["rq2"], expected["rq2"])
            reconstructed.extend(local["metrics"])
        evidence.check("S3", "all sensitivity RQ12 scalars and joint repository clusters recompute from actual cache",
            valid and metric_sources_match(output, reconstructed, evidence),
            [output / "robustness_checks.json", output / "rq12_repository_records.jsonl", registry_path])
    from .formal_rq3 import DAY, number
    anchor, end = number(context["study"]["event_anchor"]), number(context["study"]["window_end"])
    policy, immutable = number("2025-08-15T00:00:00Z"), number("2025-10-28T00:00:00Z")
    settings = {"formal_main": dict(anchor=anchor, end=end), "raw_main": dict(anchor=anchor, end=end),
        "exclude_pilot": dict(anchor=anchor, end=end), "anchor_minus_one_day": dict(anchor=anchor-DAY),
        "anchor_plus_one_day": dict(anchor=anchor+DAY), "exclude_incident_day": dict(exclude_event_day=True),
        "after_first_seven_days": dict(start_after_days=7), "after_first_thirty_days": dict(start_after_days=30),
        "before_sha_policy": dict(period_start=anchor, period_end=policy-1),
        "sha_policy_before_immutable": dict(period_start=policy, period_end=immutable-1),
        "after_immutable_releases": dict(period_start=immutable, period_end=end)}
    evidence.check("S3", "actual anchor shifts, landmarks and both policy calendar boundaries",
        set(matrix) == SCENARIOS and all(equivalent(matrix[name]["settings"], expected) for name, expected in settings.items()),
        [output / "robustness_checks.json", root / "study.toml"])


def check_manuscript(root, output, context, evidence, documents):
    from .formal_manuscript import audit_artifacts, audit_text

    figure_path, map_path, audit_path = (output / (name + ".json") for name in ("figure_manifest", "claim_evidence_map", "manuscript_audit"))
    manifest, mapping, prior = [evidence.load(path) for path in (figure_path, map_path, audit_path)]
    sources = {name: dict(path=output / (name + ".json"), sha256=sha256(output / (name + ".json")), data=documents[name]) for name in SUMMARIES}
    sources["figure_manifest"] = dict(path=figure_path, sha256=sha256(figure_path), data=manifest)
    for name, path in (("freeze_manifest", root / "reports/freeze_manifest.json"), ("followup", root / "reports/maintenance_followup_audit.json"), ("source_catalog", output / "source_catalog.json")):
        sources[name] = dict(path=path, sha256=sha256(path), data=evidence.load(path))
    frozen_inputs = context["freeze_manifest"].get("input_sha256", {})
    for name, relative in HISTORICAL_SOURCES.items():
        path = root / relative
        sources[name] = dict(path=path, sha256=sha256(path), data=evidence.load(path))
    evidence.check("S4", "historical source aliases match original freeze or bind additional actual sources",
        all(sources[name]["sha256"] == frozen_inputs[relative] for name, relative in HISTORICAL_SOURCES.items() if relative in frozen_inputs),
        [root / relative for relative in HISTORICAL_SOURCES.values()],
        {name:dict(path=relative, sha256=sources[name]["sha256"], original_freeze_bound=relative in frozen_inputs)
         for name, relative in HISTORICAL_SOURCES.items()})
    document = output / "manuscript.md"
    text = document.read_text(encoding="utf-8")
    text_result, artifact_result = audit_text(text, mapping["claims"], sources), audit_artifacts(root, manifest, sources)
    for artifact in manifest["artifacts"]:
        evidence.bind(root / artifact["path"])
        evidence.bind(root / artifact["data_path"])
    evidence.check("S3", "all real figure/table bytes and CSV/Markdown cells resolve to current JSON", artifact_result["passed"],
                   [figure_path, *[root / artifact["path"] for artifact in manifest["artifacts"]]], artifact_result)
    source_bindings = [*manifest["input_sha256"].items(), *mapping["input_sha256"].items()]
    evidence.check("S4", "actual numeric-token and contextual closed-claim re-audit", text_result["passed"] and text_result["registered_claims"] > 0,
                   [document, map_path, audit_path], text_result)
    evidence.check("S4", "current source hashes and original manuscript audit bindings",
        all(sha256(root / path) == digest for path, digest in source_bindings)
        and mapping["implementation_sha256"] == sha256(root / "pintrace/formal_manuscript.py")
        and mapping["freeze_sha256"] == context["guard"]["freeze_sha256"]
        and prior["manuscript_sha256"] == sha256(document) and prior["claim_map_sha256"] == sha256(map_path)
        and prior["closed_analyses"] == context["freeze_manifest"]["closed_models"], [document, map_path, audit_path, figure_path])
    clean = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    sections = {match.group(1): match.group(2) for match in re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", clean, re.M | re.S)}
    revision = re.search(r"### Revisions after the development checkpoint\n(.*?)(?=\n## |\Z)", clean, re.S)
    evidence.check("S4", "complete substantive chapters and post-checkpoint identity/maintenance disclosure",
        all(len(sections.get(chapter, "").strip()) >= 60 for chapter in CHAPTERS)
        and bool(revision) and all(word in revision.group(1).lower() for word in ("152", "identity", "maintenance"))
        and not re.search(r"^\s*(?:TODO|TBD)\b", clean, re.M), [document, output / "source_catalog.json"])
    published = [(output / (name + ".json"), root / "reports" / (name + ".json")) for name in (*SUMMARIES, "figure_manifest", "claim_evidence_map", "manuscript_audit", "source_catalog", "bootstrap_reproducibility")]
    published.append((document, root / "paper/manuscript.md"))
    evidence.check("S4", "published paper and reports equal staged artifacts", all(sha256(a) == sha256(b) for a, b in published), [path for pair in published for path in pair])


def run(root, output_dir, context):
    root, output = Path(root).resolve(), Path(output_dir).resolve()
    require_context(root, context, inputs=("study.toml", "reports/risksets.csv"))
    target = output / "formal_acceptance.json"
    if target.is_relative_to(root) and target.relative_to(root).as_posix() in context["freeze_manifest"]["input_sha256"]:
        raise ValueError("Acceptance would overwrite a frozen input")
    evidence = Evidence(root)
    evidence.bind(root / "reports/freeze_manifest.json")
    documents = {}
    try:
        documents = {name: evidence.load(output / (name + ".json")) for name in SUMMARIES}
    except (OSError, ValueError) as error:
        for section in ("S1", "S2", "S3", "S4"):
            evidence.check(section, "all required actual statistical reports exist", False, [], repr(error))
    if len(documents) == len(SUMMARIES):
        for section, action in (("S1", check_rq12), ("S2", check_rq3), ("S3", check_bootstrap), ("S4", check_manuscript)):
            try:
                action(root, output, context, evidence, documents) if section != "S3" else action(output, context, evidence, documents)
            except (OSError, KeyError, TypeError, ValueError, IndexError) as error:
                evidence.check(section, "required actual evidence resolves", False, [], repr(error))
    evidence.check("S4", "all bound evidence remains unchanged", all(sha256(root / path) == digest for path, digest in evidence.inputs.items()), [])
    sources = {"pintrace/" + name + ".py": sha256(root / "pintrace" / (name + ".py")) for name in
               ("formal_acceptance", "formal_common", "formal_stats", "formal_rq12", "formal_rq3", "formal_robustness", "formal_figures", "formal_manuscript")}
    report = dict(rule_version=RULES_VERSION, acceptance_passed=all(row["passed"] for row in evidence.checks),
        requirements={section: dict(passed=any(r["section"] == section for r in evidence.checks) and all(r["passed"] for r in evidence.checks if r["section"] == section),
            checks=sum(r["section"] == section for r in evidence.checks)) for section in ("S1", "S2", "S3", "S4")},
        checks=evidence.checks, input_sha256=evidence.inputs, implementation_sha256=sources,
        additional_input_sha256={name:evidence.inputs[name] for name in HISTORICAL_SOURCES.values()
            if name in evidence.inputs and name not in context["freeze_manifest"]["input_sha256"]},
        freeze_sha256=context["guard"]["freeze_sha256"],
        limits=["Programmatic artifact acceptance is not external review or validation of latent behavior.", "The coordinating runner separately rehashes every frozen input before and after production."])
    report["acceptance_passed"] &= all(row["passed"] for row in report["requirements"].values())
    write_json(target, report)
    require_context(root, context)
    if not report["acceptance_passed"]:
        raise ValueError("S1--S4 formal acceptance failed; see " + str(target))
    return report
