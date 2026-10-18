"""RQ1/RQ2 descriptions from frozen first-parent measurements.

``extract_repository(detail, study)`` and ``extract_path(path, study)`` produce
the same compact, reusable repository record. No raw bodies/configuration are
retained. Three real baselines (anchor - day, anchor, anchor + day), all compact
Action events, all snapshot metadata/counts, and each first-observed Action
identity group support sensitivity calculations without rereading measurements.

``summarize(records, ids, settings, context=None, population='formal')`` is pure
apart from optional shared metric registration. Settings: label, anchor, end,
exclude_event_day, start_after_days, period_start, period_end, excluded_ids.
Landmarks retain the initial baseline and expose earlier terminal outcomes;
they never reset a previously changed/unknown reference to unchanged. Shifted
anchors require their cached actual baseline. Time inversion is not repaired.
``run(root, output_dir, context)`` requires the shared frozen-input receipt and
writes only the new statistics directory. There is deliberately no CLI.
"""

from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

from .formal_common import add_quantile, add_ratio, require_context, sha256, write_json
from .measurement_io import sections

RULE_VERSION = "formal-rq12-v2"
DAY = 86400
HORIZONS = (7, 30, 90, 180, 365)
FIRST_STATES = ("pin", "tag_update", "remove", "replace", "unpin", "sha_target_change",
                "target_reference_entry", "multiple_behaviors", "unknown_boundary",
                "ambiguous_boundary", "no_observed_change", "excluded_eventday", "before_landmark", "clock_unknown")
TERMINALS = {"pin", "remove", "replace", "unknown_boundary", "ambiguous_transition"}
BOUNDARIES = {"unknown_boundary", "ambiguous_transition"}
REF_FIELDS = ("action", "kind", "repository", "ref", "ref_type", "lineage_id", "lineage_status", "path", "job", "step_id", "step_index", "line",
              "snapshot_sha", "snapshot_timestamp", "snapshot_date", "comment_kind", "comment_version", "comment_attachment_state", "comment_action", "comment_identity_state")


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if isinstance(value, str) else value


def _refs(value):
    return value if isinstance(value, list) else [value] if value else []


def _ref(value):
    if isinstance(value, list):
        return [_ref(item) for item in value]
    if not value:
        return None
    result = {key: value[key] for key in REF_FIELDS if key in value}
    if "configuration_coverage" in value:
        result["configuration_coverage"] = dict(value["configuration_coverage"])
    return result


def _event(value):
    return dict({key: value.get(key) for key in ("event", "commit", "previous_commit", "timestamp", "entry_kind", "identity_history_status")},
                before=_ref(value.get("before")), after=_ref(value.get("after")))


def _extract(summary, snapshots, timeline, references, events, study, proof=None):
    """References may be an iterator; retain only three baseline configurations."""
    indices = {state["sha"]: i for i, state in enumerate(snapshots)}
    if len(indices) != len(snapshots):
        raise ValueError("Repeated snapshot commit")
    clocks = [state["timestamp"] for state in snapshots]
    aligned = all(isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) for t in clocks)
    aligned = aligned and all(a <= b for a, b in zip(clocks, clocks[1:]))
    anchor = _time(study["event_anchor"])
    baselines = {}
    for shift in (-1, 0, 1):
        at = anchor + shift * DAY
        point = indices.get(summary["baseline_commit"]) if shift == 0 else next((i for i in range(len(snapshots) - 1, -1, -1) if aligned and clocks[i] < at), None)
        baselines[str(int(at))] = dict(index=point, complete=point is not None and snapshots[point]["complete"] is True,
                                      references=[], calendar_state="aligned" if aligned else "clock_unknown")
    selected = {value["index"] for value in baselines.values() if value["index"] is not None}
    stored = {i: [] for i in selected}
    first_groups, target_references = {}, []
    for state in snapshots:
        state.update(action_references=0, full_sha_references=0, target_references=0, target_full_sha=0)
    for ref in references:
        if ref.get("kind") != "action":
            continue
        point = indices[ref["snapshot_sha"]]
        state = snapshots[point]
        state["action_references"] += 1
        state["full_sha_references"] += ref.get("ref_type") == "full_sha"
        if ref["action"] == study["target_action"]:
            target_references.append(_ref(ref))
            state["target_references"] += 1
            state["target_full_sha"] += ref.get("ref_type") == "full_sha"
        if point in stored and ref["action"] != study["target_action"] and ref.get("ref_type") != "full_sha":
            stored[point].append(_ref(ref))
        action = ref["action"]
        group = first_groups.get(action)
        if group is None or point < group["index"]:
            group = first_groups[action] = dict(action=action, index=point, commit=state["sha"], timestamp=state["timestamp"], references=0, full_sha=0, lineage_ids=[])
        if point == group["index"]:
            group["references"] += 1
            group["full_sha"] += ref.get("ref_type") == "full_sha"
            group["lineage_ids"].append(ref.get("lineage_id"))
    compact_events = [_event(event) for event in events]
    by_commit = defaultdict(list)
    for event in compact_events:
        if event["commit"] not in indices:
            raise ValueError("Event commit is absent from snapshots")
        event["index"] = indices[event["commit"]]
        by_commit[event["commit"]].append(event)
    for group in first_groups.values():
        evidence = [event for event in by_commit[group["commit"]] if any(ref.get("action") == group["action"] for ref in _refs(event.get("after")))]
        modes = {event.get("entry_kind") if event["event"] == "replace" else event["event"] for event in evidence}
        prior_complete = all(state["complete"] is True for state in snapshots[:group["index"] + 1])
        group["source_modes"] = sorted(mode for mode in modes if mode)
        known = "first_observed_action" in modes and prior_complete and all(event.get("identity_history_status") != "earlier_gap" for event in evidence)
        group["identity_state"] = "confirmed_first_observed" if known else "initial_observation" if group["index"] == 0 else "earlier_history_or_entry_unknown"
        group["excluded_copy_or_reintroduction"] = bool(modes) and modes <= {"copied_reference", "reintroduced_action"}
        group["configuration"] = ("all_full_sha" if group["full_sha"] == group["references"] else "mixed" if group["full_sha"] else "all_non_sha") if known else "unknown"
    for value in baselines.values():
        value["references"] = stored.get(value["index"], [])
    endpoint = indices[timeline.get("endpoint_commit", summary.get("endpoint_commit"))]
    proof = proof or {}
    target_events = [event for event in compact_events if any(ref.get("action") == study["target_action"] for ref in _refs(event.get("before")) + _refs(event.get("after")))]
    return dict(repository_id=str(summary["repository_id"]), repository=summary.get("canonical_repository", summary.get("repository")),
                anchor=anchor, window_end=_time(study["window_end"]), target_action=study["target_action"],
                clock_aligned=aligned, snapshots=snapshots, endpoint_index=endpoint,
                observed_ancestry_covers_end=summary.get("observed_ancestry_covers_end") is True,
                baselines=baselines, events=sorted(compact_events, key=lambda event: event["index"]),
                first_action_groups=sorted(first_groups.values(), key=lambda group: (group["index"], group["action"])),
                target_measurement=dict(summary=dict(summary), snapshots=snapshots, timeline=dict(timeline),
                    replay=dict(passed=proof.get("replay", False)), lineages=dict(conservation=dict(passed=proof.get("conservation", False)), references=target_references, events=target_events)))


def extract_repository(detail, study):
    snapshots = [{key: state[key] for key in ("sha", "timestamp", "complete")} for state in detail["snapshots"]]
    return _extract(detail["summary"], snapshots, detail.get("timeline", {}), iter(detail["lineages"]["references"]), detail["lineages"]["events"], study,
                    dict(replay=detail.get("replay", {}).get("passed", False), conservation=detail["lineages"].get("conservation", {}).get("passed", False)))


def extract_path(path, study):
    """One streaming pass, selecting metadata leaves instead of full snapshots."""
    leaves = {"sha": [], "timestamp": [], "complete": []}
    summary, timeline, events, proof = None, {}, [], {}
    # Lineage references follow snapshots in frozen source files. Consume them
    # lazily in _extract, then continue that same iterator to obtain events.
    prefixes = {"summary", "timeline", "lineages.references.item", "lineages.events.item", "replay.passed", "lineages.conservation.passed"} | {"snapshots.item." + key for key in leaves}
    iterator = iter(sections(path, prefixes))
    pending = None
    for prefix, value in iterator:
        if prefix == "summary":
            summary = value
        elif prefix == "timeline":
            timeline = value
        elif prefix.startswith("snapshots.item."):
            leaves[prefix.rsplit(".", 1)[1]].append(value)
        elif prefix in {"replay.passed", "lineages.conservation.passed"}:
            proof["replay" if prefix == "replay.passed" else "conservation"] = value
        else:
            pending = (prefix, value)
            break
    if summary is None or not leaves["sha"] or len({len(values) for values in leaves.values()}) != 1:
        raise ValueError("Incomplete snapshot metadata")
    snapshots = [dict(zip(leaves, values)) for values in zip(*leaves.values())]

    def references():
        nonlocal pending, timeline
        while pending is not None:
            prefix, value = pending
            if prefix == "lineages.references.item":
                yield value
            elif prefix == "lineages.events.item":
                events.append(_event(value))
            elif prefix == "timeline":
                timeline.update(value)
            elif prefix in {"replay.passed", "lineages.conservation.passed"}:
                proof["replay" if prefix == "replay.passed" else "conservation"] = value
            else:
                raise ValueError("Unexpected frozen measurement section order")
            pending = next(iterator, None)
    refs = references()
    return _extract(summary, snapshots, timeline, refs, events, study, proof)


def _baseline(record, anchor):
    if str(int(anchor)) not in record["baselines"]:
        raise ValueError("Requested anchor is not one of the three cached real baselines")
    return record["baselines"][str(int(anchor))]


def _options(record, settings):
    anchor = _time(settings.get("anchor", record["anchor"]))
    end = min(_time(settings.get("end", record["window_end"])), _time(settings.get("period_end", record["window_end"])))
    start = max(anchor + settings.get("start_after_days", 0) * DAY, _time(settings.get("period_start", anchor)))
    return anchor, start, end


def _selected(record, settings):
    anchor, start, end = _options(record, settings)
    baseline = _baseline(record, anchor)
    point = baseline["index"]
    events = [event for event in record["events"] if point is not None and point < event["index"] <= record["endpoint_index"]]
    observed_end = end if record["observed_ancestry_covers_end"] else min(end, record["snapshots"][record["endpoint_index"]]["timestamp"])
    return baseline, events, anchor, start, end, observed_end


def _target_behaviors(event, target):
    before = any(ref.get("action") == target for ref in _refs(event.get("before")))
    after = any(ref.get("action") == target for ref in _refs(event.get("after")))
    if not before and not after:
        return set()
    kind = event["event"]
    if kind == "mutable_update":
        return {"tag_update"}
    if kind in {"first_observed_action", "copied_reference", "reintroduced_action"}:
        return {"target_reference_entry"}
    return {"ambiguous_boundary" if kind == "ambiguous_transition" else kind}


def _terminal_status(kind, event, record, anchor, start, end, observed_end, settings):
    time = event["timestamp"] if event else observed_end
    within = record["clock_aligned"] and time is not None and anchor <= time <= end
    if event and not within:
        return "clock_unknown", None
    if event and settings.get("exclude_event_day") and record["anchor"] <= time < record["anchor"] + DAY:
        return "excluded_eventday", time
    if event and time < start:
        return "before_landmark", time
    if not record["clock_aligned"] or observed_end < anchor:
        return "clock_unknown", None
    return kind, time


def describe_repository(record, settings=None):
    """Return sufficient row records for the requested calendar/landmark scope."""
    settings = settings or {}
    baseline, events, anchor, start, end, observed_end = _selected(record, settings)
    target, snapshots = record["target_action"], record["snapshots"]
    selected = [event for event in events if not record["clock_aligned"] or event["timestamp"] <= end]
    by_point = defaultdict(set)
    for event in selected:
        by_point[event["index"]].update(_target_behaviors(event, target))
    if baseline["index"] is not None:
        for point in range(baseline["index"] + 1, record["endpoint_index"] + 1):
            state = snapshots[point]
            if state["complete"] is not True and (not record["clock_aligned"] or state["timestamp"] <= end):
                by_point[point].add("unknown_boundary")
    nonempty = [(point, kinds) for point, kinds in sorted(by_point.items()) if kinds]
    first = nonempty[0] if nonempty else None
    kinds = first[1] if first else set()
    category = ("unknown_boundary" if "unknown_boundary" in kinds else "ambiguous_boundary" if "ambiguous_boundary" in kinds
                else next(iter(kinds)) if len(kinds) == 1 else "multiple_behaviors" if kinds else "no_observed_change")
    first_event = dict(timestamp=snapshots[first[0]]["timestamp"]) if first else None
    disposition, time = _terminal_status(category, first_event, record, anchor, start, end, observed_end, settings)
    if not baseline["complete"]:
        category, disposition, time = "unknown_boundary", "unknown_boundary", None
    full_window = set()
    for point, behaviors in nonempty:
        stamp = snapshots[point]["timestamp"]
        if not record["clock_aligned"] or start <= stamp <= end:
            if not settings.get("exclude_event_day") or not record["anchor"] <= stamp < record["anchor"] + DAY:
                full_window.update(behaviors)
    if not baseline["complete"]:
        full_window.add("unknown_boundary")
    if not record["clock_aligned"]:
        full_window.add("clock_unknown")
    known_first = bool(baseline["complete"] and first and not kinds & {"unknown_boundary", "ambiguous_boundary"} and disposition not in {"excluded_eventday", "before_landmark", "clock_unknown"})
    rq1 = dict(repository_id=record["repository_id"], first_response_state=disposition,
               first_observed_topological_category=category,
               timing_state=disposition, terminal_timestamp=time, first_commit=snapshots[first[0]]["sha"] if first else None,
               first_commit_behaviors=sorted(kinds), full_window_behaviors=sorted(full_window),
               full_window_combination="+".join(sorted(full_window)) or "no_observed_change",
               response_time_days=math.floor(time / DAY) - math.floor(anchor / DAY) if known_first else None,
               observed_end=observed_end, analysis_start=start, analysis_end=end, anchor=anchor,
               clock_aligned=record["clock_aligned"], first_known_response=known_first)
    rq1["baseline_unknown_at_entry"] = not baseline["complete"]
    reference_rows = []
    exits = defaultdict(list)
    for event in selected:
        if event["event"] in TERMINALS:
            for before in _refs(event.get("before")):
                exits[before.get("lineage_id")].append(event)
    for ref in baseline["references"] if baseline["complete"] else []:
        relevant = exits[ref.get("lineage_id")]
        event = relevant[0] if relevant else None
        kind = event["event"] if event else "no_observed_change"
        status, stamp = _terminal_status(kind, event, record, anchor, start, end, observed_end, settings)
        calendar_reason = ("nonmonotone_snapshot_calendar" if not record["clock_aligned"] else
                           "observation_ended_before_anchor" if observed_end < anchor else
                           "event_calendar_outside_window") if status == "clock_unknown" else None
        reference_rows.append(dict(repository_id=record["repository_id"], **ref, outcome=kind,
            analysis_outcome=status if status in {"excluded_eventday", "before_landmark", "clock_unknown"} else kind,
            timing_state=status, clock_unknown_reason=calendar_reason, terminal_timestamp=stamp, observed_end=observed_end,
            anchor=anchor, analysis_start=start, analysis_end=end,
            first_pin_days=math.floor(stamp / DAY) - math.floor(anchor / DAY) if kind == "pin" and status == "pin" else None))
    new_rows = []
    for group in record["first_action_groups"]:
        if group["action"] == target or group["excluded_copy_or_reintroduction"] or baseline["index"] is None or not baseline["index"] < group["index"] <= record["endpoint_index"]:
            continue
        if record["clock_aligned"] and not start <= group["timestamp"] <= end:
            continue
        if settings.get("exclude_event_day") and record["anchor"] <= group["timestamp"] < record["anchor"] + DAY:
            continue
        new_rows.append(dict(group, repository_id=record["repository_id"],
            observed_configuration=group["configuration"], configuration=group["configuration"] if record["clock_aligned"] else "unknown",
            calendar_state="aligned" if record["clock_aligned"] else "clock_unknown"))
    return dict(rq1=rq1, references=reference_rows, new_actions=new_rows,
                baseline_complete=baseline["complete"], baseline_index=baseline["index"])


def _ratio(context, key, rows, population, binary=False, unit="repository"):
    rows = list(rows)
    if context is not None:
        return add_ratio(context, key, rows, population=population, binary=binary, unit=unit)
    denominator = sum(d for _, n, d in rows if n is not None and d is not None and d > 0)
    return sum(n for _, n, d in rows if n is not None and d is not None and d > 0) / denominator if denominator else None


def _quantile(context, key, rows, population, q, unit):
    rows = [(identity, value) for identity, value in rows if value is not None]
    if context is not None:
        return add_quantile(context, key, rows, q=q, population=population, unit=unit)
    values = sorted(value for _, value in rows)
    if not values:
        return None
    at = (len(values) - 1) * q
    lo = math.floor(at)
    return values[lo] + (values[math.ceil(at)] - values[lo]) * (at - lo)


def _horizon(row, days, known):
    cutoff = row["anchor"] + days * DAY
    status, stamp = row["timing_state"], row["terminal_timestamp"]
    if cutoff > row["analysis_end"] or cutoff < row["analysis_start"]:
        return "outside_analysis_interval"
    if status in {"clock_unknown", "excluded_eventday", "before_landmark"}:
        return status
    if stamp is None and status in BOUNDARIES | {"ambiguous_boundary"}:
        return "ambiguous_boundary" if status == "ambiguous_transition" else status
    if stamp is not None and stamp <= cutoff:
        if status in known:
            return status
        if status in BOUNDARIES or status == "ambiguous_boundary":
            return "ambiguous_boundary" if status == "ambiguous_transition" else status
    return "censored_before_horizon" if row["observed_end"] < cutoff else "unchanged_observed_through_horizon"


def _target_state_at(record, days, settings):
    baseline, _, anchor, start, end, observed_end = _selected(record, settings)
    cutoff = anchor + days * DAY
    if not start <= cutoff <= end:
        return "outside_analysis_interval"
    if not record["clock_aligned"]:
        return "clock_unknown"
    if not baseline["complete"]:
        return "unknown"
    if observed_end < cutoff:
        return "ancestry_censored"
    state = next((state for state in reversed(record["snapshots"][:record["endpoint_index"] + 1]) if state["timestamp"] <= cutoff), None)
    if state is None or state["complete"] is not True:
        return "unknown"
    total, pinned = state["target_references"], state["target_full_sha"]
    return "absent" if not total else "all_full_sha" if pinned == total else "mixed" if pinned else "all_non_sha"


def _existing_summary(rows, repo_ids, prefix, context, population):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["repository_id"]].append(row)
    outcomes = Counter(row["analysis_outcome"] for row in rows)
    warehouse = lambda kind: [(identity, sum(row["analysis_outcome"] == kind for row in group) / len(group), 1) for identity, group in grouped.items()]
    weighted = lambda kind: [(identity, sum(row["analysis_outcome"] == kind for row in group), len(group)) for identity, group in grouped.items()]
    result = dict(baseline_references=len(rows), repositories_with_baseline_references=len(grouped), repositories_without_baseline_references=len(repo_ids) - len(grouped),
                  outcome_counts=dict(sorted(outcomes.items())), repository_equal={}, reference_weighted={}, horizons={})
    result["window_followup_counts"] = dict(sorted(Counter(
        "clock_unknown" if row["timing_state"] == "clock_unknown" else
        "ancestry_censored" if row["analysis_outcome"] == "no_observed_change" and row["observed_end"] < row["analysis_end"] else
        "no_observed_change_complete" if row["analysis_outcome"] == "no_observed_change" else row["analysis_outcome"] for row in rows).items()))
    for kind in sorted(set(outcomes) | TERMINALS | {"no_observed_change"}):
        result["repository_equal"][kind] = _ratio(context, prefix + ".repository_equal." + kind, warehouse(kind), population)
        result["reference_weighted"][kind] = _ratio(context, prefix + ".reference_weighted." + kind, weighted(kind), population, unit="reference")
    for days in HORIZONS:
        categories = Counter(_horizon(row, days, {"pin", "remove", "replace"}) for row in rows)
        by_repo = {identity: Counter(_horizon(row, days, {"pin", "remove", "replace"}) for row in group) for identity, group in grouped.items()}
        terminal = {"unknown_boundary", "ambiguous_boundary", "clock_unknown", "censored_before_horizon"}
        valid = {identity: len(grouped[identity]) - cats["outside_analysis_interval"] for identity, cats in by_repo.items()}
        lower = _ratio(context, f"{prefix}.horizon.{days}.pin.lower", [(identity, cats["pin"], valid[identity]) for identity, cats in by_repo.items()], population, unit="reference")
        upper = _ratio(context, f"{prefix}.horizon.{days}.pin.upper", [(identity, cats["pin"] + sum(cats[k] for k in terminal), valid[identity]) for identity, cats in by_repo.items()], population, unit="reference")
        equal = _ratio(context, f"{prefix}.horizon.{days}.pin.repository_equal", [(identity, cats["pin"] / valid[identity], 1) for identity, cats in by_repo.items() if valid[identity]], population)
        risk = sum(row["timing_state"] not in {"clock_unknown", "excluded_eventday", "before_landmark"} and row["terminal_timestamp"] is not None and row["terminal_timestamp"] >= row["anchor"] + days * DAY and row["analysis_start"] <= row["anchor"] + days * DAY <= row["analysis_end"] for row in rows)
        weighted_outcomes, equal_outcomes = {}, {}
        for kind in ("pin", "remove", "replace", "unknown_boundary", "ambiguous_boundary", "censored_before_horizon", "unchanged_observed_through_horizon", "clock_unknown"):
            weighted_outcomes[kind] = _ratio(context, f"{prefix}.horizon.{days}.reference_weighted.{kind}", [(identity, cats[kind], valid[identity]) for identity, cats in by_repo.items()], population, unit="reference")
            equal_outcomes[kind] = _ratio(context, f"{prefix}.horizon.{days}.repository_equal.{kind}", [(identity, cats[kind] / valid[identity], 1) for identity, cats in by_repo.items() if valid[identity]], population)
        result["horizons"][str(days)] = dict(fixed_baseline_denominator=len(rows), within_interval_reference_denominator=sum(valid.values()), counts=dict(sorted(categories.items())), at_risk_just_before=risk, observed_pin_cumulative=lower, upper_bound=upper, repository_equal_observed_pin=equal, reference_weighted_outcomes=weighted_outcomes, repository_equal_outcomes=equal_outcomes)
    result["first_pin_days_quantiles"] = {str(q): _quantile(context, prefix + ".first_pin_days." + str(q), [(row["repository_id"], row["first_pin_days"]) for row in rows], population, q, "reference") for q in (0, .25, .5, .75, 1)}
    result["first_pin_days_conditioning"] = "UTC commit date minus anchor date, integer calendar days; quantiles interpolate this date distribution, not exact disclosure-to-response hours. Known observed first pins only."
    result["interpretation"] = "Observed first terminal outcomes on the fixed initial reference denominator; bounds retain unknown/censored units. No AJ estimate or independent-censoring assumption."
    return result


def summarize(records, ids, settings=None, context=None, population="formal"):
    settings = settings or {}
    ids = set(map(str, ids)) - set(map(str, settings.get("excluded_ids", ())))
    records = [record for record in records if record["repository_id"] in ids]
    if {record["repository_id"] for record in records} != ids or len(records) != len(ids):
        raise ValueError("Missing or duplicate repository record")
    described = [describe_repository(record, settings) for record in records]
    rq1_rows = [value["rq1"] for value in described]
    label = settings.get("label", "main")
    prefix1, prefix2 = f"rq1.{population}.{label}", f"rq2.{population}.{label}"
    first = Counter(row["first_response_state"] for row in rq1_rows)
    first_counts = {kind: first[kind] for kind in sorted(set(FIRST_STATES) | set(first))}
    combinations = Counter(row["full_window_combination"] for row in rq1_rows)
    rq1 = dict(repositories=len(ids), first_response_counts=first_counts, first_response_proportions={}, full_window_combination_counts=dict(sorted(combinations.items())),
               full_window_behavior_repository_counts=dict(sorted(Counter(kind for row in rq1_rows for kind in set(row["full_window_behaviors"])).items())), horizons={},
               metric_prefix=prefix1, target_state_horizons={}, settings={k: sorted(v) if isinstance(v, set) else v for k, v in settings.items()})
    for kind in first_counts:
        rq1["first_response_proportions"][kind] = _ratio(context, prefix1 + ".first_response." + kind, [(row["repository_id"], int(row["first_response_state"] == kind), 1) for row in rq1_rows], population)
    rq1["full_window_combination_proportions"] = {kind: _ratio(context, prefix1 + ".combination." + kind, [(row["repository_id"], int(row["full_window_combination"] == kind), 1) for row in rq1_rows], population) for kind in sorted(combinations)}
    rq1["full_window_behavior_repository_proportions"] = {kind: _ratio(context, prefix1 + ".behavior." + kind, [(row["repository_id"], int(kind in row["full_window_behaviors"]), 1) for row in rq1_rows], population) for kind in rq1["full_window_behavior_repository_counts"]}
    for days in HORIZONS:
        known = set(FIRST_STATES) - {"no_observed_change", "unknown_boundary", "ambiguous_boundary", "excluded_eventday", "before_landmark", "clock_unknown"}
        categories = {row["repository_id"]: _horizon(row, days, known) for row in rq1_rows}
        counted = Counter(categories.values())
        observed = {identity for identity, kind in categories.items() if kind in known}
        uncertain = {identity for identity, kind in categories.items() if kind in {"unknown_boundary", "ambiguous_boundary", "clock_unknown", "censored_before_horizon"}}
        complete = {identity for identity, kind in categories.items() if kind in known | {"unchanged_observed_through_horizon"}}
        in_scope = ids - {identity for identity, kind in categories.items() if kind == "outside_analysis_interval"}
        rq1["horizons"][str(days)] = dict(denominator=len(ids), within_interval_denominator=len(in_scope), counts=dict(sorted(counted.items())),
            at_risk_just_before=sum(row["timing_state"] not in {"clock_unknown", "excluded_eventday", "before_landmark"} and row["terminal_timestamp"] is not None and row["terminal_timestamp"] >= row["anchor"] + days * DAY and row["analysis_start"] <= row["anchor"] + days * DAY <= row["analysis_end"] for row in rq1_rows),
            observed_response_lower=_ratio(context, f"{prefix1}.horizon.{days}.lower", [(identity, int(identity in observed), 1) for identity in sorted(in_scope)], population),
            response_upper_bound=_ratio(context, f"{prefix1}.horizon.{days}.upper", [(identity, int(identity in observed | uncertain), 1) for identity in sorted(in_scope)], population),
            complete_case_response=_ratio(context, f"{prefix1}.horizon.{days}.complete", [(identity, int(identity in observed), 1) for identity in sorted(complete)], population, binary=True),
            complete_case_denominator=len(complete), interpretation="Fixed repository denominator; early ancestry censoring and unknown boundaries are retained, not scored unchanged.")
        target_states = {record["repository_id"]: _target_state_at(record, days, settings) for record in records}
        rq1["target_state_horizons"][str(days)] = dict(denominator=len(ids), counts=dict(sorted(Counter(target_states.values()).items())),
            proportions={kind: _ratio(context, f"{prefix1}.target_state.{days}.{kind}", [(identity, int(state == kind), 1) for identity, state in sorted(target_states.items())], population) for kind in sorted(set(target_states.values()))})
    rq1["response_time_days_quantiles"] = {str(q): _quantile(context, f"{prefix1}.response_days.{q}", [(row["repository_id"], row["response_time_days"]) for row in rq1_rows], population, q, "repository") for q in (0, .25, .5, .75, 1)}
    rq1["response_time_conditioning"] = "Known first response before any unknown/ambiguous target boundary with monotone observed calendar; integer UTC commit date minus anchor date. Quantiles interpolate the date distribution, not exact disclosure-to-response hours; conditional, not population median."
    rq1["horizon_rule"] = "Configuration observed at event-date UTC boundary plus d calendar days (events at the boundary included); UTC day boundary is not a verified disclosure instant."
    rq1["tag_update_semantics"] = "A mutable/unresolved uses ref value changed; this category does not prove the ref was a tag rather than a branch."
    rq1["no_observed_change_followup_counts"] = dict(sorted(Counter(
        "ancestry_censored" if row["observed_end"] < row["analysis_end"] else "observed_to_window_end"
        for row in rq1_rows if row["first_response_state"] == "no_observed_change").items()))
    references = [row for value in described for row in value["references"]]
    groups = [row for value in described for row in value["new_actions"]]
    grouped = defaultdict(list)
    for row in groups:
        grouped[row["repository_id"]].append(row)
    new = dict(groups=len(groups), repositories_with_new_groups=len(grouped), no_new_group_repositories=len(ids) - len(grouped),
               configuration_counts=dict(sorted(Counter(row["configuration"] for row in groups).items())), repository_equal={}, reference_weighted={})
    for kind in ("all_full_sha", "mixed", "all_non_sha", "unknown"):
        new["configuration_counts"].setdefault(kind, 0)
        new["repository_equal"][kind] = _ratio(context, prefix2 + ".new.repository_equal." + kind, [(identity, sum(row["configuration"] == kind for row in rows) / len(rows), 1) for identity, rows in grouped.items()], population)
        new["reference_weighted"][kind] = _ratio(context, prefix2 + ".new.observed_reference_weighted." + kind, [(identity, sum(row["references"] for row in rows if row["configuration"] == kind), sum(row["references"] for row in rows)) for identity, rows in grouped.items()], population, unit="observed_first_configuration_reference")
    known_groups = {identity: [row for row in rows if row["configuration"] != "unknown"] for identity, rows in grouped.items()}
    known_groups = {identity: rows for identity, rows in known_groups.items() if rows}
    new["known_group_repositories"] = len(known_groups)
    new["known_groups"] = sum(map(len, known_groups.values()))
    new["repository_equal"]["all_full_sha_known_groups"] = _ratio(context, prefix2 + ".new.repository_equal.all_full_sha_known_groups", [(identity, sum(row["configuration"] == "all_full_sha" for row in rows) / len(rows), 1) for identity, rows in known_groups.items()], population)
    new["reference_weighted"]["full_sha_known_groups"] = _ratio(context, prefix2 + ".new.reference_weighted.full_sha_known_groups", [(identity, sum(row["full_sha"] for row in rows), sum(row["references"] for row in rows)) for identity, rows in known_groups.items()], population, unit="first_configuration_reference")
    new["reference_weighted"]["all_full_sha_group_reference_weighted"] = _ratio(context, prefix2 + ".new.reference_weighted.all_full_sha_group_reference_weighted", [(identity, sum(row["references"] for row in rows if row["configuration"] == "all_full_sha"), sum(row["references"] for row in rows)) for identity, rows in known_groups.items()], population, unit="first_configuration_reference")
    new["reference_denominator_known_groups"] = sum(row["references"] for rows in known_groups.values() for row in rows)
    new["identity_state_counts"] = dict(sorted(Counter(row["identity_state"] for row in groups).items()))
    new["interpretation"] = "Action identity first observations, not copied/reintroduced steps; unknown history/calendar remains unknown. Primary known-group ratios exclude unknown groups; repositories without eligible new groups have undefined ratios. Category reference weights count observed first-configuration instances, possibly partial for unknown groups, and do not assert their complete denominator."
    rq2 = dict(repositories=len(ids), existing=_existing_summary(references, ids, prefix2 + ".existing", context, population), new_actions=new,
               baseline_unknown_repositories=sum(not value["baseline_complete"] for value in described), metric_prefix=prefix2,
               checks=dict(target_action_primary_rows=sum(row["action"] == records[0]["target_action"] for row in references + groups) if records else 0,
                           reference_outcome_conservation=sum(_existing_count for _existing_count in Counter(row["analysis_outcome"] for row in references).values()) == len(references)))
    rq2["existing"]["repositories_without_baseline_references"] -= rq2["baseline_unknown_repositories"]
    rq2["existing"]["repositories_with_unknown_baseline"] = rq2["baseline_unknown_repositories"]
    rq2["existing"]["baseline_reference_denominator_complete_for_population"] = rq2["baseline_unknown_repositories"] == 0
    clock_rows = [row for row in references if row["timing_state"] == "clock_unknown"]
    rq2["calendar_eligibility"] = dict(
        clock_unalignable_repository_ids=sorted(record["repository_id"] for record in records if not record["clock_aligned"]),
        baseline_reference_denominator=len(references), baseline_references_with_clock_unknown=len(clock_rows),
        repository_ids_with_clock_unknown_references=sorted({row["repository_id"] for row in clock_rows}),
        prior_pin_outcomes_recoded=sum(row["outcome"] == "pin" for row in clock_rows),
        unknown_calendar_observed_outcome_counts=dict(sorted(Counter(row["outcome"] for row in clock_rows).items())),
        reasons={reason: dict(references=sum(row["clock_unknown_reason"] == reason for row in clock_rows),
            repository_ids=sorted({row["repository_id"] for row in clock_rows if row["clock_unknown_reason"] == reason}),
            prior_pin_outcomes_recoded=sum(row["clock_unknown_reason"] == reason and row["outcome"] == "pin" for row in clock_rows))
            for reason in sorted({row["clock_unknown_reason"] for row in clock_rows})},
        observed_topological_outcome_counts=dict(sorted(Counter(row["outcome"] for row in references).items())),
        interpretation="Main outcomes retain unknown-calendar references in the fixed denominator as clock_unknown. Raw outcome remains a topology-based identity diagnostic, not evidence of calendar-attributable post-incident extension.",
        method_revision="Post-freeze statistical correction identified during review; not preregistered. Extraction rules and frozen histories unchanged.")
    rq1["checks"] = dict(first_response_partition=sum(first_counts.values()) == len(ids), combinations_partition=sum(combinations.values()) == len(ids), horizon_partitions=all(sum(row["counts"].values()) == len(ids) for row in rq1["horizons"].values()), target_state_partitions=all(sum(row["counts"].values()) == len(ids) for row in rq1["target_state_horizons"].values()))
    return dict(rq1=rq1, rq2=rq2)


def _csv(path, rows):
    rows = list(rows)
    fields = sorted({key for row in rows for key in row}) or ["repository_id"]
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value for key, value in row.items()} for row in rows)


def _record_line(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def run(root, output_dir, context):
    root, output_dir = Path(root).resolve(), Path(output_dir).resolve()
    require_context(root, context, inputs=("study.toml", "reports/risksets.csv"))
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {Path(path).stem: path for path in context["freeze_manifest"]["input_sha256"] if path.startswith("data/derived/measurement-v3/") and Path(path).stem.isdigit()}
    if set(files) != set(context["raw_ids"]):
        raise ValueError("Frozen physical repository input set differs")
    records = []
    cache_path = output_dir / "rq12_repository_records.jsonl"
    cached = context.get("rq12_records")
    if cached is not None:
        receipt = context.get("rq12_record_cache_receipt", {})
        memory_digest = hashlib.sha256()
        for record in cached:
            memory_digest.update(_record_line(record).encode("utf-8"))
        memory_hash = memory_digest.hexdigest()
        if (receipt.get("freeze_sha256") != context["guard"]["freeze_sha256"] or receipt.get("implementation_sha256") != sha256(Path(__file__))
                or not receipt.get("path") or receipt.get("sha256") != sha256(receipt["path"]) or memory_hash != receipt["sha256"]):
            raise ValueError("Compact RQ12 cache receipt/source/freeze/content differs")
        if {record["repository_id"] for record in cached} != set(files) or len(cached) != len(files):
            raise ValueError("Compact RQ12 cache has missing or repeated repositories")
    cached_by_id = {record["repository_id"]: record for record in cached} if cached is not None else {}
    with cache_path.open("w", encoding="utf-8", newline="\n") as stream:
        for identity, name in sorted(files.items()):
            record = extract_path(root / name, context["study"]) if cached is None else cached_by_id[identity]
            if record["repository_id"] != identity:
                raise ValueError("Repository identity differs from frozen file")
            records.append(record)
            stream.write(_record_line(record))
            print(f"RQ1/RQ2 extracted {len(records)}/{len(files)} {record['repository']}", flush=True)
    context["rq12_records"] = records
    context["rq12_record_cache_receipt"] = dict(path=str(cache_path), sha256=sha256(cache_path), freeze_sha256=context["guard"]["freeze_sha256"], implementation_sha256=sha256(Path(__file__)))
    receipt_path = output_dir / "rq12_record_cache_receipt.json"
    write_json(receipt_path, context["rq12_record_cache_receipt"])
    context["rq12_target_measurements"] = [record["target_measurement"] for record in records]
    summaries = {population: summarize(records, context[population + "_ids"], context=context, population=population) for population in ("formal", "raw")}
    main_rows = [dict(population=population, **describe_repository(record)["rq1"]) for population in ("formal", "raw") for record in records if record["repository_id"] in context[population + "_ids"]]
    _csv(output_dir / "rq1_repository_results.csv", main_rows)
    for key, filename in (("references", "rq2_existing_reference_results.csv"), ("new_actions", "rq2_new_action_results.csv")):
        _csv(output_dir / filename, [dict(population=population, **row) for population in ("formal", "raw") for record in records if record["repository_id"] in context[population + "_ids"] for row in describe_repository(record)[key]])
    old_limit = csv.field_size_limit(2**31 - 1)
    try:
        with (root / "reports/risksets.csv").open(encoding="utf-8", newline="") as stream:
            excluded = {(row["repository_id"], row["lineage_id"]) for row in csv.DictReader(stream) if row["row_kind"] == "episode" and row["episode_kind"] == "post_anchor_born_fixed" and row["repository_id"] in context["formal_ids"]}
    finally:
        csv.field_size_limit(old_limit)
    contamination = sum((record["repository_id"], row.get("lineage_id")) in excluded or any((record["repository_id"], lid) in excluded for lid in row.get("lineage_ids", [])) for record in records if record["repository_id"] in context["formal_ids"] for key in ("references", "new_actions") for row in describe_repository(record)[key])
    outputs = {path.name: dict(path=str(path), sha256=sha256(path)) for path in sorted(output_dir.glob("rq*.csv"))}
    outputs[cache_path.name] = dict(path=str(cache_path), sha256=sha256(cache_path))
    outputs[receipt_path.name] = dict(path=str(receipt_path), sha256=sha256(receipt_path))
    result = {}
    for number in (1, 2):
        name, section = f"rq{number}_summary", f"rq{number}"
        report = dict(rule_version=RULE_VERSION, formal=summaries["formal"][section], raw=summaries["raw"][section], outputs=outputs,
                      checks=dict(raw457=len(context["raw_ids"]) == 457, formal455=len(context["formal_ids"]) == 455,
                                  formal_target_born_lineages_identified=len(excluded) == 65, target_born_primary_rows=contamination == 0),
                      freeze_sha256=context["guard"]["freeze_sha256"], input_authority="Full frozen-input guard before and after the coordinated batch",
                      implementation_sha256=sha256(Path(__file__)))
        report["checks"]["formal_and_raw_partitions"] = all(all(summaries[pop]["rq1"]["checks"].values()) for pop in ("formal", "raw"))
        report["checks"]["no_target_action_primary_rows"] = all(summaries[pop]["rq2"]["checks"]["target_action_primary_rows"] == 0 for pop in ("formal", "raw"))
        report["checks"]["existing_outcome_conservation"] = all(summaries[pop]["rq2"]["checks"]["reference_outcome_conservation"] for pop in ("formal", "raw"))
        report["excluded_target_born_lineage_count"] = len(excluded)
        if number == 2:
            report["calendar_eligibility"] = {pop: summaries[pop]["rq2"]["calendar_eligibility"] for pop in ("formal", "raw")}
        write_json(output_dir / (name + ".json"), report)
        result[name] = report
    return result
