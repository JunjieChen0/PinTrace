"""Frozen-cohort RQ3 descriptions; inference is registered by repository cluster."""

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .formal_common import add_quantile, add_ratio, require_context, write_json

RULES_VERSION = "formal-rq3-v1"
DAY = 86400
KINDS = ("baseline_fixed", "post_anchor_observed_pin", "post_anchor_born_fixed")
EXITS = ("unpin", "remove", "replace")
UNKNOWN = ("unknown_boundary", "ambiguous_transition")
CENSORS = ("study_window_censored", "observable_ancestry_censored")
GRID = (0, 7, 30, 90, 180, 365)
FIELDS = ("entry_configuration_coverage", "entry_comment_kind", "entry_comment_identity_state")
QUANTILES = (("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9))
INPUTS = ("reports/risksets.csv", "reports/model_eligibility_audit.json", "reports/rq3_scope_counts.json",
          "reports/maintenance_followup_audit.json", "reports/full_replay_report.json",
          "reports/tool_replay_results.json", "reports/opportunity_join_summary.json",
          "reports/maintenance_scope_summary.json", "data/derived/maintenance-scope-v1/opportunity_entries.jsonl")


def number(value):
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError):
            return None
    return result if math.isfinite(result) else None


def _duration(row):
    start, end = number(row.get("entry_timestamp")), number(row.get("exit_timestamp"))
    return (end - start) / DAY if start is not None and end is not None else None


def _duration_eligible(row, clocks):
    duration, followup = _duration(row), number(row.get("observed_followup_seconds"))
    return (str(row["repository_id"]) not in clocks and row.get("clock_order_state") == "nonnegative"
            and duration is not None and duration > 0 and followup is not None and followup > 0
            and followup == number(row["exit_timestamp"]) - number(row["entry_timestamp"])
            and row.get("positive_observed_followup") in (True, "True"))


def _ratio(context, key, rows, population, unit="episode", binary=False):
    return dict(estimate=add_ratio(context, key, rows, population=population, unit=unit, binary=binary), metric_key=key)


def _quantiles(context, key, samples, population, unit="episode"):
    samples = [(str(identity), value) for identity, value in samples if value is not None]
    points, keys = {}, {}
    for name, q in QUANTILES:
        keys[name] = key + "." + name
        points[name] = add_quantile(context, keys[name], samples, q=q, population=population, unit=unit)
    return dict(observations=len(samples), repositories=len({r for r, _ in samples}), quantiles=points,
                minimum=min((v for _, v in samples), default=None), maximum=max((v for _, v in samples), default=None),
                metric_keys=keys)


def _episode_counts(rows):
    by_repo = defaultdict(set)
    for row in rows:
        by_repo[str(row["repository_id"])].add(row["exit_kind"])
    combinations = Counter(tuple(sorted(kinds)) for kinds in by_repo.values())
    return dict(episodes=len(rows), repositories=len(by_repo),
                known_event_episodes=sum(r["exit_kind"] in EXITS for r in rows),
                known_event_repositories=len({r["repository_id"] for r in rows if r["exit_kind"] in EXITS}),
                exit_counts=dict(sorted(Counter(r["exit_kind"] for r in rows).items())),
                exit_repository_counts={kind: sum(kind in values for values in by_repo.values())
                                        for kind in sorted({r["exit_kind"] for r in rows})},
                repository_exit_combinations=[dict(exit_kinds=list(kinds), repositories=count)
                                               for kinds, count in sorted(combinations.items())])


def retention_status(row, days):
    """Known/unknown exits at the horizon occur before retention is assessed."""
    duration, kind = _duration(row), row["exit_kind"]
    if duration is None or duration < 0:
        return "duration_unknown"
    if duration <= days and kind in EXITS:
        return "known_exit"
    if duration <= days and kind in UNKNOWN:
        return "unknown_interruption"
    if duration >= days:
        return "retained"
    return "censored_early" if kind in CENSORS else "unknown_interruption"


def summarize_persistence(episodes, eligible_episode_ids, context, population="formal", prefix="rq3"):
    """Describe supplied episodes; scenario callers own their eligibility mask."""
    ids = set(context["populations"][population])
    episodes = [r for r in episodes if str(r["repository_id"]) in ids]
    eligible_episode_ids = set(eligible_episode_ids)
    duration = [r for r in episodes if r["episode_id"] in eligible_episode_ids]
    result = dict(identity=_episode_counts(episodes), duration=_episode_counts(duration),
                  excluded=_episode_counts([r for r in episodes if r["episode_id"] not in eligible_episode_ids]),
                  by_kind={}, retention=[], trajectories=[], duration_quantiles_days={})
    for kind in KINDS:
        identity_group = [r for r in episodes if r["episode_kind"] == kind]
        group = [r for r in duration if r["episode_kind"] == kind]
        key = f"{prefix}.persistence.{population}.{kind}"
        result["by_kind"][kind] = dict(identity=_episode_counts(identity_group), duration=_episode_counts(group))
        result["duration_quantiles_days"][kind] = _quantiles(
            context, key + ".observed_duration_days", [(r["repository_id"], _duration(r)) for r in group], population)
        for days in (30, 90, 180):
            statuses = [(r, retention_status(r, days)) for r in group]
            counts = Counter(value for _, value in statuses)
            determinate = counts["retained"] + counts["known_exit"]
            row = dict(kind=kind, horizon_days=days, basis="duration_eligible", episodes=len(group),
                       retained=counts["retained"], known_exit=counts["known_exit"],
                       unknown_interruption=counts["unknown_interruption"], censored_early=counts["censored_early"],
                       duration_unknown=counts["duration_unknown"], determinate=determinate)
            row.update(_ratio(context, key + f".retention.{days}.determinate_retained",
                              [(r["repository_id"], int(status == "retained"), 1)
                               for r, status in statuses if status in {"retained", "known_exit"}], population))
            row["determinate_share"] = _ratio(context, key + f".retention.{days}.determinate_share",
                [(r["repository_id"], int(status in {"retained", "known_exit"}), 1) for r, status in statuses], population)
            result["retention"].append(row)
        times = sorted(set(GRID) | {_duration(r) for r in group})
        for days in times:
            for exit_kind in (*EXITS, *UNKNOWN):
                events = [r for r in group if r["exit_kind"] == exit_kind and _duration(r) <= days]
                point_key = key + f".trajectory.{days:g}.{exit_kind}"
                point = dict(kind=kind, days=days, exit_kind=exit_kind, observed_events=len(events),
                             event_repositories=len({r["repository_id"] for r in events}), episodes=len(group),
                             at_risk_before=sum(_duration(r) >= days for r in group),
                             right_censored_by_time=sum(r["exit_kind"] in CENSORS and _duration(r) <= days for r in group),
                             observed_fraction=len(events) / len(group) if group else None, metric_key=None)
                if days in GRID:
                    metric = _ratio(context, point_key,
                        [(r["repository_id"], int(r["exit_kind"] == exit_kind and _duration(r) <= days), 1) for r in group], population)
                    point["observed_fraction"], point["metric_key"] = metric["estimate"], metric["metric_key"]
                result["trajectories"].append(point)
        repositories = defaultdict(list)
        for row in identity_group:
            repositories[str(row["repository_id"])].append(row)
        result["by_kind"][kind]["observed_known_exit_repository_fraction"] = _ratio(context,
            key + ".observed_known_exit_repository_fraction",
            [(identity, int(any(r["exit_kind"] in EXITS for r in rows)), 1) for identity, rows in repositories.items()],
            population, unit="repository")
    result["interpretation"] = ("Observed cumulative fractions keep the initial duration-subset denominator; "
        "they are not censor-adjusted cumulative incidence. Retention proportions condition on a determinate horizon. "
        "Observed episode duration is follow-up, not an estimated uncensored lifetime; baseline pin age can be unknown.")
    return result


def _project_entry(row):
    fields = ("repository_id", "lineage_id", "reference_target_segment_start_commit", "opportunity_target",
              "opportunity_id", "evidence_grade", "evidence_type", "outcome", "outcome_timestamp",
              "available_after_or_at", "available_by", "adopted_sha", "action", "comment_identity_state")
    result = {key: row.get(key) for key in fields}
    result["repository_id"] = str(row["repository_id"])
    for field in ("available_by", "available_after_or_at"):
        result[field + "_timestamp"] = row.get(field)
        result[field] = datetime.fromtimestamp(number(row[field]), timezone.utc).isoformat() if row.get(field) is not None else None
    result["target_ids"] = [link["episode_id"] for link in row["maintenance_target_episode_links"]]
    result["focal_links"] = [(link["focal_episode_id"], link["comparison_layer"])
                             for link in row["maintenance_comparison_focal_episode_links"]]
    return result


def _role_entries(entries, ids, episode_ids):
    result = []
    for entry in entries:
        if entry["repository_id"] not in ids:
            continue
        row = dict(entry, target_ids=[value for value in entry["target_ids"] if value in episode_ids],
                   focal_links=[link for link in entry["focal_links"] if link[0] in episode_ids])
        result.append(row)
    return result


def _entry_counts(rows, role="target"):
    adopted = [r for r in rows if r["outcome"] == "adopted_target"]
    episode_ids = {value for r in rows for value in (r["target_ids"] if role == "target" else [x[0] for x in r["focal_links"]])}
    return dict(entries=len(rows), repositories=len({r["repository_id"] for r in rows}), episodes=len(episode_ids),
                opportunity_ids=len({r["opportunity_id"] for r in rows}),
                actual_target_sha_adoptions=len({(r["repository_id"], r["lineage_id"], r.get("outcome_timestamp"), r.get("adopted_sha")) for r in adopted}),
                adopting_repository_ids=len({r["repository_id"] for r in adopted}),
                outcome_counts=dict(sorted(Counter(r["outcome"] for r in rows).items())))


def delay_bounds(row):
    """Adoption delay only; B upper bound is open and U is not a publication date."""
    if row["outcome"] != "adopted_target":
        return None
    lower, upper, adopted = (number(row.get(key)) for key in ("available_after_or_at", "available_by", "outcome_timestamp"))
    if None in (lower, upper, adopted) or lower > upper or adopted < upper:
        return None
    return dict(lower_days=(adopted - upper) / DAY, upper_days=(adopted - lower) / DAY,
                proxy_days=(adopted - upper) / DAY, upper_inclusive=row["evidence_grade"] == "A")


def _graded_maintenance(rows, context, key, population, role):
    result = dict(counts=_entry_counts(rows, role), by_grade={})
    outcomes = sorted({r["outcome"] for r in rows} | {"adopted_target", "other_sha_change", "right_censored_no_observed_sha_change"})
    for grade in ("A", "B"):
        group = [r for r in rows if r["evidence_grade"] == grade]
        block = dict(counts=_entry_counts(group, role), outcome_proportions={})
        for outcome in outcomes:
            block["outcome_proportions"][outcome] = _ratio(context, key + f".{grade}.outcome.{outcome}",
                [(r["repository_id"], int(r["outcome"] == outcome), 1) for r in group], population, unit="maintenance_entry")
        samples = [(r, delay_bounds(r)) for r in group if r["outcome"] == "adopted_target"]
        valid = [(r, bound) for r, bound in samples if bound is not None]
        block["delay_records"] = [dict(repository_id=r["repository_id"], lineage_id=r["lineage_id"],
            opportunity_id=r["opportunity_id"], reference_target_segment_start_commit=r.get("reference_target_segment_start_commit"),
            available_after_or_at=r["available_after_or_at"], available_by=r["available_by"],
            available_after_or_at_timestamp=r.get("available_after_or_at_timestamp", number(r["available_after_or_at"])),
            available_by_timestamp=r.get("available_by_timestamp", number(r["available_by"])),
            outcome_timestamp=r["outcome_timestamp"], **bound) for r, bound in valid]
        block["adopted_entries_with_unknown_or_invalid_delay"] = len(samples) - len(valid)
        if grade == "A":
            block["delay_quantiles"] = dict(point=_quantiles(context, key + ".A.delay.point",
                [(r["repository_id"], bound["proxy_days"]) for r, bound in valid], population, "adopted_maintenance_entry"))
        else:
            low = _quantiles(context, key + ".B.delay.lower_proxy",
                [(r["repository_id"], bound["lower_days"]) for r, bound in valid], population, "adopted_maintenance_entry")
            high = _quantiles(context, key + ".B.delay.upper",
                [(r["repository_id"], bound["upper_days"]) for r, bound in valid], population, "adopted_maintenance_entry")
            block["delay_quantiles"] = dict(lower=low, upper=high, proxy=low, interval="[T-U,T-L)")
        result["by_grade"][grade] = block
    return result


def _horizon_status(row, days):
    elapsed = None if number(row.get("outcome_timestamp")) is None else (number(row["outcome_timestamp"]) - number(row["available_by"])) / DAY
    if elapsed is None or elapsed < 0:
        return "unknown_outcome_time"
    if elapsed <= days and row["outcome"] in {"adopted_target", "other_sha_change"}:
        return row["outcome"]
    if elapsed >= days:
        return "no_observed_change_through_horizon"
    return "unknown_early_censor_or_interruption"


def _horizons(rows, context, key, population, window_end):
    result = {}
    for days in (30, 90, 180):
        potential = [r for r in rows if window_end - number(r["available_by"]) >= days * DAY]
        censored = [r for r in rows if r["outcome"] == "right_censored_no_observed_sha_change"
                    and number(r.get("outcome_timestamp")) is not None
                    and number(r["outcome_timestamp"]) - number(r["available_by"]) >= days * DAY]
        block = dict(status="bounded_exploration" if days == 30 else "closed_long_maintenance_indicator",
                     calendar_potential=_entry_counts(potential), right_censored_observed_followup=_entry_counts(censored))
        block["calendar_potential"]["by_grade"] = {g: _entry_counts([r for r in potential if r["evidence_grade"] == g]) for g in ("A", "B")}
        block["right_censored_observed_followup"]["by_grade"] = {g: _entry_counts([r for r in censored if r["evidence_grade"] == g]) for g in ("A", "B")}
        if days == 30:
            block["by_grade"] = {}
            for grade in ("A", "B"):
                group = [r for r in potential if r["evidence_grade"] == grade]
                statuses = [(r, _horizon_status(r, days)) for r in group]
                known = [(r, value) for r, value in statuses if not value.startswith("unknown_")]
                block["by_grade"][grade] = dict(entries=len(group), outcome_counts=dict(Counter(v for _, v in statuses)),
                    determinate=len(known), proxy_adoption_fraction_among_determinate=_ratio(context,
                    key + f".30.{grade}.proxy_adoption_among_determinate",
                    [(r["repository_id"], int(value == "adopted_target"), 1) for r, value in known], population, unit="maintenance_entry"))
        result[str(days)] = block
    return result


def summarize_maintenance(entries, context, population="formal", prefix="rq3", window_end=None):
    """Describe compact role-projected entries, clustered by provider repository."""
    ids = set(context["populations"][population])
    owners = {r["episode_id"]: str(r["repository_id"]) for r in context.get("rq3_data", {}).get("episodes", [])}
    entries = _role_entries(entries, ids, {eid for eid, owner in owners.items() if owner in ids})
    if window_end is None:
        window_end = datetime.fromisoformat(context["study"]["window_end"].replace("Z", "+00:00")).timestamp()
    target = [r for r in entries if r["target_ids"]]
    comparison = [r for r in entries if r["focal_links"]]
    key = f"{prefix}.maintenance.{population}"
    return dict(target=_graded_maintenance(target, context, key + ".target", population, "target"),
                comparison=_graded_maintenance(comparison, context, key + ".comparison", population, "comparison"),
                horizons=_horizons(target, context, key + ".target.horizon", population, window_end),
                interpretation="A point delays and B interval/proxy delays remain separate. Entry outcomes are provider observations, not independent paired effects; 30-day results are selected-entry exploration, not failure rates.")


def _condition_tables(episodes, entries, context):
    index = {r["episode_id"]: r for r in episodes}
    result = {}
    for field in FIELDS:
        mechanism = "h1" if field == "entry_configuration_coverage" else "h2"
        categories = sorted({r.get(field) or "unknown" for r in episodes})
        rows = []
        for category in categories:
            group = [r for r in episodes if (r.get(field) or "unknown") == category]
            cell = dict(category=category, **_episode_counts(group))
            cell["observed_known_exit_fraction"] = _ratio(context, f"{mechanism}.entry.{field}.{category}.known_exit",
                [(r["repository_id"], int(r["exit_kind"] in EXITS), 1) for r in group], "formal")
            rows.append(cell)
        result[field] = rows
    cross = {}
    target = [r for r in entries if r["target_ids"]]
    for field in FIELDS:
        mechanism = "h1" if field == "entry_configuration_coverage" else "h2"
        cells = defaultdict(list)
        for entry in target:
            values = sorted({index[value].get(field) or "unknown" for value in entry["target_ids"]})
            category = values[0] if len(values) == 1 else "mixed_episode_entry_conditions"
            cells[(category, entry["evidence_grade"])].append(entry)
        cross[field] = []
        for (category, grade), group in sorted(cells.items()):
            cell = dict(category=category, grade=grade, **_entry_counts(group))
            cell["adoption_fraction"] = _ratio(context, f"{mechanism}.maintenance.{field}.{category}.{grade}.adopted_target",
                [(r["repository_id"], int(r["outcome"] == "adopted_target"), 1) for r in group], "formal", "maintenance_entry")
            cross[field].append(cell)
    return result, cross


def _tool_cases(tool_report):
    rows = []
    for tool, detail in sorted(tool_report["results"].items()):
        for case in detail["results"]:
            official = case["official_result"]
            updated = any(row.get("updates") or row.get("updated_files") for row in official or [])
            status = ("fixture_not_scheduled" if official is None else "official_module_update" if updated
                      else "official_module_disabled" if any(r.get("skipReason") == "disabled" for r in official) else "no_module_update_observed")
            rows.append(dict(tool=tool, version=detail["version"], case_id=case["case_id"], covered=case["covered"],
                             status=status, routing_status=case.get("routing_status"), official_result=official))
    return rows


def _csv(path, records, fields):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for record in records:
            writer.writerow({key: json.dumps(record[key], ensure_ascii=False, sort_keys=True) if isinstance(record.get(key), (list, dict)) else record.get(key) for key in fields})


def run(root, output_dir, context):
    root, output_dir = Path(root).resolve(), Path(output_dir).resolve()
    require_context(root, context, inputs=INPUTS)
    if output_dir == root / "reports":
        raise ValueError("RQ3 writes a new batch before the main orchestrator publishes reports")
    reports = {Path(name).stem: json.loads((root / name).read_text(encoding="utf-8")) for name in INPUTS if name.endswith(".json")}
    with (root / "reports/risksets.csv").open(encoding="utf-8", newline="") as stream:
        episodes = [r for r in csv.DictReader(stream) if r["row_kind"] == "episode"]
    for row in episodes:
        for field in ("entry_timestamp", "exit_timestamp", "observed_followup_seconds", "fixed_age_seconds"):
            row[field] = number(row.get(field))
    entries = []
    with (root / INPUTS[-1]).open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                entries.append(_project_entry(json.loads(line)))
    model, scope = reports["model_eligibility_audit"], reports["rq3_scope_counts"]
    clocks = set(reports["opportunity_join_summary"]["clock_unalignable_repository_ids"])
    eligible = {r["episode_id"] for r in episodes if _duration_eligible(r, clocks)}
    formal_episodes = [r for r in episodes if r["repository_id"] in context["formal_ids"]]
    if {r["episode_id"] for r in formal_episodes} != set(model["formal_episode_ids"]) or eligible & set(model["formal_episode_ids"]) != set(model["eligible_episode_ids"]):
        raise ValueError("Episode identity/duration eligibility differs from the frozen audit")
    context["rq3_data"] = dict(episodes=episodes, maintenance=entries, clocks=clocks,
        eligible_episode_ids=set(model["eligible_episode_ids"]), raw_eligible_episode_ids=eligible,
        window_end=datetime.fromisoformat(context["study"]["window_end"].replace("Z", "+00:00")).timestamp())
    persistence = dict(rule_version=RULES_VERSION, populations={}, comparisons=dict(
        riskset=scope["formal_scope"]["riskset_comparisons"], maintenance=scope["formal_scope"]["maintenance_comparisons"]))
    maintenance = dict(rule_version=RULES_VERSION, populations={}, B_pre_upper_exclusions=scope["formal_scope"]["B_pre_upper_exclusions"],
                       B_pre_upper_diagnostics=scope["formal_scope"]["B_pre_upper_diagnostics"],
                       B_pre_upper_semantics="Frozen evidence-row diagnostics include duplicate evidence and only generated candidates; they are not full-cohort opportunity denominators.",
                       closed_maintenance_horizons=[90, 180], regression_opened=False)
    projected = {}
    for population in ("formal", "raw"):
        ids = context[population + "_ids"]
        selected = [r for r in episodes if r["repository_id"] in ids]
        projected[population] = _role_entries(entries, ids, {r["episode_id"] for r in selected})
        persistence["populations"][population] = summarize_persistence(selected, eligible, context, population)
        maintenance["populations"][population] = summarize_maintenance(projected[population], context, population)
    for name, key in (("identity", "all_formal"), ("duration", "duration_eligible"), ("excluded", "excluded")):
        if any(persistence["populations"]["formal"][name][field] != value for field, value in model["counts"][key].items()):
            raise ValueError("Persistence counts differ from frozen " + key)
    for role in ("target", "comparison"):
        actual = maintenance["populations"]["formal"][role]["counts"]
        expected = scope["formal_scope"]["maintenance_counts"][role]
        mapping = {"opportunity_reference_pairs": "entries", "repository_ids": "repositories", "episode_ids": "episodes"}
        if any(actual[mapping.get(field, field)] != value for field, value in expected.items()):
            raise ValueError("Maintenance counts differ from frozen " + role)
    for grade in ("A", "B"):
        if maintenance["populations"]["formal"]["target"]["by_grade"][grade]["counts"]["entries"] != scope["formal_scope"]["maintenance_by_grade"][grade]["opportunity_reference_pairs"]:
            raise ValueError("A/B target entry counts changed")
    frozen_horizons = reports["maintenance_followup_audit"]["horizons"]
    for days in ("30", "90", "180"):
        for field in ("calendar_potential", "right_censored_observed_followup"):
            actual = maintenance["populations"]["formal"]["horizons"][days][field]
            if any(actual[key] != frozen_horizons[days][field][key] for key in ("entries", "repositories", "episodes")):
                raise ValueError("Maintenance horizon counts changed")
    born = [r for r in formal_episodes if r["episode_kind"] == "post_anchor_born_fixed" and r["exit_kind"] == "unpin"]
    persistence["born_fixed_unpin"] = dict(identity=len(born), duration=sum(r["episode_id"] in eligible for r in born),
        excluded=sum(r["episode_id"] not in eligible for r in born), repository_ids=sorted({r["repository_id"] for r in born}))
    conditions, cross = _condition_tables(formal_episodes, projected["formal"], context)
    h1h2 = dict(rule_version=RULES_VERSION, episode_entry_conditions=dict(identity=conditions, duration={}),
                maintenance_target_cross_tabs=cross, inheritance_unknown_snapshots={},
                tool_replays=_tool_cases(reports["tool_replay_results"]),
                limits=["Entry configuration measures visible local intent, not bot execution or PR creation.",
                        "Unknown/inherited configuration is not absence of automation; raw categories are not upgraded.",
                        "Maintenance condition cells use target episode entry conditions, not provider configurations or conditions at U.",
                        "Historical official modules use a synthetic shared upstream; missing ecosystem routing is a fixture gate, not a hosted scheduler replay."])
    for field in FIELDS:
        group = [r for r in formal_episodes if r["episode_id"] in eligible]
        h1h2["episode_entry_conditions"]["duration"][field] = [dict(category=value, **_episode_counts([r for r in group if (r.get(field) or "unknown") == value])) for value in sorted({r.get(field) or "unknown" for r in group})]
    quality = reports["full_replay_report"]["repository_checks"]
    for population, ids in (("physical", context["raw_ids"]), ("formal", context["formal_ids"])):
        group = [row for row in quality if str(row["repository_id"]) in ids]
        h1h2["inheritance_unknown_snapshots"][population] = dict(
            snapshots=sum(r["quality"]["snapshots"] for r in group),
            inheritance_unknown_snapshots=sum(r["quality"]["inheritance_unknown_snapshots"] for r in group),
            repositories=len(group), unit="snapshot, not independent repository or observed bot execution")
        bootstrap_population = "raw" if population == "physical" else "formal"
        h1h2["inheritance_unknown_snapshots"][population]["snapshot_fraction"] = _ratio(context,
            f"h1.inheritance.{population}.snapshot_fraction",
            [(r["repository_id"], r["quality"]["inheritance_unknown_snapshots"], r["quality"]["snapshots"]) for r in group],
            bootstrap_population, "snapshot")
    names = ("rq3_persistence_summary", "rq3_maintenance_results", "h1_h2_summary")
    filenames = [name + ".json" for name in names] + ["rq3_episode_records.csv", "rq3_maintenance_records.csv"]
    for name in filenames:
        path = output_dir / name
        if path.is_relative_to(root) and path.relative_to(root).as_posix() in context["freeze_manifest"]["input_sha256"]:
            raise ValueError("Output would overwrite a frozen input")
    output_dir.mkdir(parents=True, exist_ok=True)
    episode_records = [dict(row, formal=row["repository_id"] in context["formal_ids"], duration_eligible=row["episode_id"] in eligible, observed_duration_days=_duration(row)) for row in episodes]
    _csv(output_dir / "rq3_episode_records.csv", episode_records,
         ["repository_id", "episode_id", "episode_kind", "entry_timestamp", "exit_timestamp", "exit_kind", "formal", "duration_eligible", "observed_duration_days", *FIELDS])
    maintenance_records = []
    for population, group in projected.items():
        for row in group:
            for role, flag in (("target", row["target_ids"]), ("comparison", row["focal_links"])):
                if flag:
                    maintenance_records.append(dict(row, population=population, role=role, delay=delay_bounds(row)))
    _csv(output_dir / "rq3_maintenance_records.csv", maintenance_records,
         ["population", "role", "repository_id", "lineage_id", "opportunity_id", "evidence_grade", "outcome", "available_after_or_at", "available_by", "outcome_timestamp", "target_ids", "focal_links", "delay"])
    summaries = dict(zip(names, (persistence, maintenance, h1h2)))
    for name, report in summaries.items():
        report["freeze_sha256"] = context["guard"]["freeze_sha256"]
        report["input_sha256"] = {path: context["freeze_manifest"]["input_sha256"][path] for path in INPUTS}
        report["records"] = dict(episodes="rq3_episode_records.csv", maintenance="rq3_maintenance_records.csv")
        write_json(output_dir / (name + ".json"), report)
    require_context(root, context, inputs=INPUTS)
    context["rq3_data"]["projected"] = projected
    return summaries
