"""Prespecified population, anchor and calendar-window sensitivity analyses."""

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

from .formal_common import require_context, sha256, write_json
from .pilot import timestamp
from .risksets import build_risksets


DAY = 86400


def pilot_ids(root):
    root = Path(root)
    with (root / "reports/pilot_candidates.csv").open(encoding="utf-8-sig", newline="") as stream:
        names = [row["repository"].lower() for row in csv.DictReader(stream)]
    with (root / "data/derived/tables-v4/repositories.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    lookup = {}
    for row in rows:
        for key in ("candidate_name", "canonical_repository"):
            lookup.setdefault(row[key].lower(), set()).add(row["repository_id"])
    if len(names) != 50 or any(len(lookup.get(name, set())) != 1 for name in names):
        raise ValueError("The original fifty pilot names do not map uniquely to stable identities")
    return {next(iter(lookup[name])) for name in names}


def shifted_target_episodes(details, ids, study, anchor, *, use_recorded_baseline=False):
    """Reuse the frozen pure rules on target-only projections at the actual new baseline."""
    baselines, measurements = [], []
    shifted = dict(study, event_anchor=datetime.fromtimestamp(anchor, timezone.utc).isoformat())
    for detail in details:
        identity = str(detail["summary"]["repository_id"])
        if identity not in ids:
            continue
        snapshots = detail["snapshots"]
        if use_recorded_baseline:
            choices = [index for index, state in enumerate(snapshots) if state["sha"] == detail["summary"]["baseline_commit"]]
        else:
            if any(a["timestamp"] > b["timestamp"] for a, b in zip(snapshots, snapshots[1:])):
                continue
            choices = [index for index, state in enumerate(snapshots) if state["timestamp"] < anchor]
        if not choices:
            continue
        point = choices[-1]
        baseline = snapshots[point]
        refs = [ref for ref in detail["lineages"]["references"] if ref["snapshot_sha"] == baseline["sha"]]
        if not baseline["complete"] or not refs:
            continue
        summary = dict(repository_id=identity, repository=detail["summary"]["canonical_repository"],
                       canonical_repository=detail["summary"]["canonical_repository"], status="confirmed",
                       baseline_commit=baseline["sha"])
        baselines.append(dict(summary=summary, baseline_snapshot=dict(baseline, references=refs)))
        measurements.append(detail)
    result = build_risksets(baselines, measurements, shifted)
    return [row for row in result["risksets"] if row["row_kind"] == "episode"], dict(
        eligible_repository_ids=[str(detail["summary"]["repository_id"]) for detail in baselines],
        excluded_from_shifted_dependency_riskset=len(ids) - len(baselines),
        rule="Original cohort retained for RQ1; RQ3 fixed-period entry requires actual dependency presence at shifted baseline")


def truncate_episodes(episodes, start, end, *, exclude_event_day=False):
    """Describe periods overlapping a calendar segment; never turn early exits into zeroes."""
    output, excluded = [], {"outside_segment": 0, "entry_on_excluded_event_day": 0, "exit_on_excluded_event_day": 0}
    incident = timestamp("2025-03-14T00:00:00Z")
    for source in episodes:
        entry, exit_time = source["entry_timestamp"], source["exit_timestamp"]
        if exit_time < start or entry > end:
            excluded["outside_segment"] += 1
            continue
        row = dict(source)
        if exclude_event_day:
            if row["episode_kind"] != "baseline_fixed" and incident <= entry < incident + DAY:
                excluded["entry_on_excluded_event_day"] += 1
                continue
            if incident <= exit_time < incident + DAY and row["exit_kind"] in {"unpin", "remove", "replace"}:
                row["exit_kind"] = "unknown_boundary"
                excluded["exit_on_excluded_event_day"] += 1
        row["entry_timestamp"] = max(start, entry)
        row["exit_timestamp"] = min(end, exit_time)
        row["observed_followup_seconds"] = max(0, row["exit_timestamp"] - row["entry_timestamp"])
        row["positive_observed_followup"] = row["observed_followup_seconds"] > 0
        row["calendar_segment_delayed_entry"] = entry < start
        if exit_time > end:
            row["exit_kind"] = "study_window_censored"
        output.append(row)
    return output, excluded


def maintenance_segment(entries, original_episodes, selected_episodes, start, end, *, remap_anchor=False):
    """Align role identities and censor outcomes at the calendar segment end."""
    original = {row["episode_id"]: row for row in original_episodes}
    selected = {row["episode_id"]: row for row in selected_episodes}
    by_lineage = {}
    for row in selected_episodes:
        by_lineage.setdefault((str(row["repository_id"]), row["lineage_id"]), []).append(row)
    output = []
    for entry in entries:
        upper = float(entry.get("available_by_timestamp", entry["available_by"]))
        if not start <= upper <= end:
            continue
        row = dict(entry)
        target_ids, focal_links = [], []
        for identity in entry["target_ids"]:
            source = original[identity]
            candidates = by_lineage.get((str(source["repository_id"]), source["lineage_id"]), [])
            matches = [value for value in candidates if value["entry_timestamp"] <= upper <= value["exit_timestamp"]
                       and (remap_anchor or value["episode_id"] == identity)]
            if len(matches) == 1:
                target_ids.append(matches[0]["episode_id"])
        for identity, layer in entry["focal_links"]:
            if identity in selected:
                focal_links.append((identity, layer))
            elif remap_anchor:
                source = original[identity]
                candidates = by_lineage.get((str(source["repository_id"]), source["lineage_id"]), [])
                matches = [value for value in candidates if value["entry_commit"] == source["entry_commit"]
                           and value["episode_kind"] != "baseline_fixed"]
                if len(matches) == 1:
                    focal_links.append((matches[0]["episode_id"], layer))
        row.update(target_ids=sorted(set(target_ids)), focal_links=sorted(set(focal_links)))
        outcome_time = row.get("outcome_timestamp")
        if outcome_time is not None and float(outcome_time) > end:
            row.update(outcome="right_censored_no_observed_sha_change", outcome_timestamp=end,
                       calendar_segment_censor=True)
        output.append(row)
    return output


def run(root, output_dir, context):
    from .formal_rq12 import summarize
    from .formal_rq3 import number, summarize_maintenance, summarize_persistence

    root, output_dir = Path(root), Path(output_dir)
    require_context(root, context, inputs=("study.toml", "data/derived/tables-v4/repositories.csv"))
    study = context["study"]
    anchor, end = timestamp(study["event_anchor"]), timestamp(study["window_end"])
    pilots = pilot_ids(root)
    records, rq3 = context["rq12_records"], context["rq3_data"]
    details = context.get("rq12_target_measurements")
    if isinstance(details, (str, Path)):
        with Path(details).open(encoding="utf-8") as stream:
            details = [json.loads(line) for line in stream if line.strip()]
    if details is None:
        details = [record["target_measurement"] for record in records]
    projected, _ = shifted_target_episodes(details, context["raw_ids"], study, anchor, use_recorded_baseline=True)
    fields = ("repository_id", "lineage_id", "episode_kind", "entry_commit", "entry_timestamp", "entry_mode", "source_entry_event",
              "exit_commit", "exit_timestamp", "exit_kind", "entry_comment_kind", "entry_comment_identity_state", "entry_configuration_coverage")
    actual = {row["episode_id"]: tuple(row.get(field) for field in fields) for row in projected}
    frozen = {row["episode_id"]: tuple(row.get(field) for field in fields) for row in rq3["episodes"]}
    if actual != frozen:
        raise ValueError("Target-only projection does not reproduce every original fixed period")
    policy = timestamp("2025-08-15T00:00:00Z")
    immutable = timestamp("2025-10-28T00:00:00Z")
    scenarios = [
        dict(label="formal_main", population="formal"),
        dict(label="raw_main", population="raw"),
        dict(label="exclude_pilot", population="formal_exclude_pilot", excluded_ids=pilots),
        dict(label="anchor_minus_one_day", population="formal", anchor=anchor - DAY),
        dict(label="anchor_plus_one_day", population="formal", anchor=anchor + DAY),
        dict(label="exclude_incident_day", population="formal", exclude_event_day=True),
        dict(label="after_first_seven_days", population="formal", start_after_days=7),
        dict(label="after_first_thirty_days", population="formal", start_after_days=30),
        dict(label="before_sha_policy", population="formal", period_start=anchor, period_end=policy - 1),
        dict(label="sha_policy_before_immutable", population="formal", period_start=policy, period_end=immutable - 1),
        dict(label="after_immutable_releases", population="formal", period_start=immutable, period_end=end),
    ]
    context["populations"]["formal_exclude_pilot"] = sorted(context["formal_ids"] - pilots)
    results = {}
    for scenario in scenarios:
        name, population = scenario["label"], scenario["population"]
        ids = set(context["populations"][population])
        settings = dict(anchor=anchor, end=end, **{key: value for key, value in scenario.items() if key not in {"population", "excluded_ids", "anchor"}})
        if "anchor" in scenario:
            settings["anchor"] = scenario["anchor"]
        response = summarize(records, ids, settings, context=context, population=population)
        selected = [row for row in rq3["episodes"] if str(row["repository_id"]) in ids]
        shifts = None
        if "anchor" in scenario:
            selected, shifts = shifted_target_episodes(details, ids, study, scenario["anchor"])
        start = scenario.get("period_start", scenario.get("anchor", anchor) + scenario.get("start_after_days", 0) * DAY)
        stop = scenario.get("period_end", end)
        if name in {"formal_main", "raw_main", "exclude_pilot"}:
            exclusions = {"outside_segment": 0, "entry_on_excluded_event_day": 0, "exit_on_excluded_event_day": 0}
        else:
            selected, exclusions = truncate_episodes(selected, start, stop, exclude_event_day=scenario.get("exclude_event_day", False))
        eligible = {row["episode_id"] for row in selected if str(row["repository_id"]) not in set(rq3["clocks"])
                    and row["observed_followup_seconds"] is not None and row["observed_followup_seconds"] > 0}
        persistence = summarize_persistence(selected, eligible, context, population=population, prefix="robust." + name)
        entries = [row for row in rq3["maintenance"] if str(row["repository_id"]) in ids]
        if scenario.get("exclude_event_day"):
            entries = [row for row in entries if not anchor <= number(row["available_by"]) < anchor + DAY]
        numeric_entries = [dict(row, available_by_timestamp=number(row["available_by"]),
                                outcome_timestamp=number(row.get("outcome_timestamp"))) for row in entries]
        entries = maintenance_segment(numeric_entries, rq3["episodes"], selected, start, stop, remap_anchor="anchor" in scenario)
        maintenance_context = dict(context, rq3_data=dict(rq3, episodes=selected))
        maintenance = summarize_maintenance(entries, maintenance_context, population=population, prefix="robust." + name, window_end=stop)
        results[name] = dict(settings={key: sorted(value) if isinstance(value, set) else value for key, value in settings.items()},
                             population=population, repositories=len(ids), rq1=response["rq1"], rq2=response["rq2"],
                             rq3_persistence=persistence, rq3_maintenance=maintenance,
                             period_exclusions=exclusions, shifted_baseline=shifts,
                             interpretation="Calendar strata and conditional landmark descriptions; no policy or incident causal effect")
    result = dict(rule_version="formal-robustness-v1", freeze_sha256=context["guard"]["freeze_sha256"],
                  additional_input_sha256={"reports/pilot_candidates.csv": sha256(root / "reports/pilot_candidates.csv")},
                  pilot=dict(candidate_names=50, uniquely_resolved_ids=len(pilots), formal_intersection=len(pilots & context["formal_ids"]),
                             raw_intersection=len(pilots & context["raw_ids"]), remaining_formal=len(context["formal_ids"] - pilots)),
                  scenarios=results, checks=dict(all_eleven_scenarios_generated=len(results) == 11,
                    pilot_mapping_complete=len(pilots) == 50, formal_main_denominator=results["formal_main"]["repositories"] == 455,
                    raw_main_denominator=results["raw_main"]["repositories"] == 457,
                    complete_target_projection_matches_frozen_episodes=actual == frozen),
                  target_projection_verification=dict(episodes=len(projected), compared_fields=list(fields), all_original_episodes_match=True),
                  limits=["Calendar segment estimates condition on the original observable cohort, not platform adoption.",
                          "Shifted-anchor target fixed periods are reconstructed with the frozen pure rules; regular-action reference layers are main-cohort descriptions only.",
                          "Opportunities remain limited to their actual A/B observation dates; empty early calendar strata do not imply no upstream releases."])
    if not all(result["checks"].values()):
        raise ValueError("Robustness matrix did not close")
    write_json(output_dir / "robustness_checks.json", result)
    return result
