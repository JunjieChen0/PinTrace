"""Describe observed pinning episodes and predeclared comparison layers."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tomllib

from .pilot import timestamp
from .measurement import BATCH_DIRECTORY, RULES_VERSION as MEASUREMENT_RULES_VERSION
from .measurement_io import IO_RULES_VERSION, file_sha256, load_analysis_measurement

RULES_VERSION = "risksets-v5"
POST_EPISODE_KINDS = ("post_anchor_observed_pin", "post_anchor_born_fixed")
KNOWN_FIXED_ENTRY_EVENTS = {"first_observed_action", "copied_reference", "reintroduced_action", "replace"}
DEFAULT_REGULAR_ACTIONS = ("actions/checkout", "actions/setup-python", "actions/setup-node",
                           "actions/upload-artifact", "actions/download-artifact")
SPARSE_REPOSITORIES = 30
SPARSE_EPISODE_COVERAGE = 0.25
ANALYSIS_REFERENCE_FIELDS = ("kind", "action", "repository", "ref", "ref_type", "path", "job", "step_id",
    "step_index", "line", "lineage_id", "lineage_status", "snapshot_sha", "snapshot_timestamp", "comment_kind",
    "comment_version", "comment_attachment_state", "comment_action", "comment_identity_state")


def compact_reference(value):
    if isinstance(value, list):
        return [compact_reference(item) for item in value]
    if value is None:
        return None
    result = {key: value[key] for key in ANALYSIS_REFERENCE_FIELDS if key in value}
    if "configuration_coverage" in value:
        result["configuration_coverage"] = {"status": value["configuration_coverage"].get("status", "unknown")}
    return result


def compact_measurement(detail):
    """Discard repeated configuration bodies from the in-memory analysis view.

    The source file and its hash remain unchanged. Projection retains exactly
    the fields used by risksets and opportunity_join, including parse boundaries.
    """
    lineages = detail.get("lineages", {})
    return dict(summary=detail["summary"],
                snapshots=[{key: state[key] for key in ("sha", "timestamp", "date", "complete") if key in state}
                           for state in detail.get("snapshots", [])],
                timeline=detail.get("timeline", {}), replay={"passed": detail.get("replay", {}).get("passed", False)},
                lineages=dict(conservation={"passed": lineages.get("conservation", {}).get("passed", False)},
                              references=[compact_reference(ref) for ref in lineages.get("references", [])],
                              events=[dict(event, before=compact_reference(event.get("before")), after=compact_reference(event.get("after")))
                                      for event in lineages.get("events", [])]))


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _id(detail):
    return str(detail["summary"]["repository_id"])


def _refs(snapshot):
    return [ref for ref in snapshot.get("references", []) if ref.get("kind") == "action"]


def _fixed(ref):
    return ref.get("ref_type") == "full_sha"


def _target_state(refs, target):
    selected = [ref for ref in refs if ref["action"] == target]
    pinned = sum(_fixed(ref) for ref in selected)
    return ("target_absent" if not selected else "all_full_sha" if pinned == len(selected)
            else "mixed" if pinned else "none_full_sha"), pinned, len(selected) - pinned


def _deduplicate(details, baseline):
    grouped = defaultdict(list)
    for detail in details:
        if detail.get("summary", {}).get("repository_id") is not None:
            if not baseline or detail["summary"].get("status") == "confirmed":
                grouped[_id(detail)].append(detail)
    selected, conflicts = {}, []
    for identity, group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda item: item["summary"].get("repository", "").lower())
        winner = ordered[0]
        if len(ordered) == 1:
            selected[identity] = winner
            continue
        signatures = set()
        for detail in ordered:
            payload = (detail.get("baseline_snapshot") if baseline else detail)
            signatures.add(json.dumps(payload, sort_keys=True, ensure_ascii=False))
        if len(signatures) > 1:
            conflicts.append(identity)
        else:
            selected[identity] = winner
    return selected, conflicts, set(grouped)


def _measurement_index(detail, baseline):
    summary, snapshots = detail["summary"], detail.get("snapshots", [])
    if summary.get("measurement_rules_version") != MEASUREMENT_RULES_VERSION:
        return None, "measurement_rules_version_mismatch"
    if (summary.get("status") != "measured" or not detail.get("replay", {}).get("passed")
            or not detail.get("lineages", {}).get("conservation", {}).get("passed")):
        return None, "measurement_or_replay_not_passed"
    indices = {state["sha"]: index for index, state in enumerate(snapshots)}
    baseline_sha = baseline["summary"]["baseline_commit"]
    endpoint_sha = detail.get("timeline", {}).get("endpoint_commit", summary.get("endpoint_commit"))
    if baseline_sha not in indices or endpoint_sha not in indices:
        return None, "baseline_or_endpoint_not_in_timeline"
    first, last = indices[baseline_sha], indices[endpoint_sha]
    if first > last or not snapshots[first].get("complete"):
        return None, "baseline_timeline_or_parse_incomplete"
    refs_by_commit = defaultdict(list)
    observations = defaultdict(list)
    for ref in detail["lineages"]["references"]:
        index = indices.get(ref["snapshot_sha"])
        if index is not None and ref.get("lineage_id"):
            refs_by_commit[ref["snapshot_sha"]].append(ref)
            observations[ref["lineage_id"]].append((index, ref))
    for rows in observations.values():
        rows.sort(key=lambda item: item[0])
    events_by_commit, exits = defaultdict(list), defaultdict(list)
    pin_counts = Counter()
    all_pins = []
    for event in detail["lineages"]["events"]:
        index = indices.get(event["commit"])
        if index is None:
            continue
        events_by_commit[event["commit"]].append(event)
        if event["event"] in {"unpin", "remove", "replace", "unknown_boundary", "ambiguous_transition"}:
            before = event.get("before") or []
            for ref in before if isinstance(before, list) else [before]:
                if ref.get("lineage_id"):
                    exits[ref["lineage_id"]].append((index, event))
        if event["event"] == "pin":
            before, after = event.get("before") or {}, event.get("after") or {}
            valid = (before.get("kind") == after.get("kind") == "action"
                     and before.get("action") == after.get("action")
                     and not _fixed(before) and _fixed(after)
                     and before.get("lineage_id") == after.get("lineage_id")
                     and bool(after.get("lineage_id")))
            if valid:
                pin_counts[after["lineage_id"]] += 1
                all_pins.append((index, event, pin_counts[after["lineage_id"]]))
    return dict(detail=detail, snapshots=snapshots, indices=indices, baseline_index=first,
                endpoint_index=last, refs_by_commit=refs_by_commit, observations=observations,
                events_by_commit=events_by_commit, exits=exits, pins=all_pins), None


def _continuous_fixed(index, lineage, start, end):
    observed = [(point, ref) for point, ref in index["observations"].get(lineage, []) if start <= point <= end]
    if not observed or observed[0][0] != start or observed[-1][0] != end:
        return False, "baseline_reference_not_surviving_at_entry"
    if any(not _fixed(ref) for _, ref in observed):
        return False, "baseline_reference_unpinned_before_entry"
    if any(start < point <= end for point, _ in index["exits"].get(lineage, [])):
        return False, "unknown_or_closed_lineage_before_entry"
    return True, "baseline_fixed_reference_alive_and_fixed_at_entry"


def _fixed_age(index, lineage, point, entry_time):
    # Only an observed pin or pinned introduction with a complete prior tree
    # identifies the beginning. The first window observation is left censored.
    rows = [(i, ref) for i, ref in index["observations"].get(lineage, []) if i <= point]
    if not rows:
        return "unknown", None
    start = len(rows) - 1
    while start > 0 and _fixed(rows[start - 1][1]):
        start -= 1
    initial_index, initial = rows[start]
    known = start > 0 or initial.get("lineage_status") in {"known_entry", "replacement_entry"}
    if not known:
        return "left_censored_unknown", None
    elapsed = entry_time - index["snapshots"][initial_index]["timestamp"]
    return ("observed_within_window", elapsed) if elapsed >= 0 else ("clock_order_unknown", None)


def _episode(index, identity, ref, point, entry_time, kind, ordinal, study):
    endpoint = index["endpoint_index"]
    exits = [(i, event) for i, event in index["exits"].get(ref["lineage_id"], []) if point < i <= endpoint]
    end_index, exit_event = exits[0] if exits else (endpoint, None)
    endpoint_time = index["snapshots"][endpoint]["timestamp"]
    horizon = timestamp(study["window_end"])
    covers_end = index["detail"]["summary"].get("observed_ancestry_covers_end") is True
    end_time = exit_event["timestamp"] if exit_event else horizon if covers_end else min(horizon, endpoint_time)
    elapsed = end_time - entry_time
    age_state, age = _fixed_age(index, ref["lineage_id"], point, entry_time)
    episode_id = "ep-" + hashlib.sha256(f"{identity}\0{ref['lineage_id']}\0{kind}\0{index['snapshots'][point]['sha']}".encode()).hexdigest()[:20]
    return dict(row_kind="episode", count_unit="reference_pinning_episode", repository_id=identity,
                repository=index["detail"]["summary"]["canonical_repository"], action=ref["action"],
                episode_id=episode_id, lineage_id=ref["lineage_id"], episode_kind=kind,
                entry_commit=index["snapshots"][point]["sha"], entry_timestamp=entry_time,
                entry_date=_iso(entry_time), entry_time_basis=("anchor_with_preanchor_tree" if kind == "baseline_fixed" else
                    "observed_fixed_entry_commit" if kind == "post_anchor_born_fixed" else "observed_pin_commit"),
                entry_mode="baseline_fixed" if kind == "baseline_fixed" else "pin",
                source_entry_event="baseline_fixed" if kind == "baseline_fixed" else "pin",
                entry_mode_state="baseline_observation" if kind == "baseline_fixed" else "observed_transition",
                pin_ordinal_in_observed_lineage=ordinal, repeated_observed_lineage_pin=ordinal > 1,
                fixed_age_state=age_state, fixed_age_seconds=age,
                entry_comment_kind=ref.get("comment_kind", "unknown"),
                entry_comment_identity_state=ref.get("comment_identity_state", "unknown"),
                entry_configuration_coverage=ref.get("configuration_coverage", {}).get("status", "unknown"),
                exit_commit=index["snapshots"][end_index]["sha"], exit_timestamp=end_time, exit_date=_iso(end_time),
                exit_kind=exit_event["event"] if exit_event else "study_window_censored" if covers_end else "observable_ancestry_censored",
                observed_followup_seconds=elapsed if elapsed >= 0 else None,
                positive_observed_followup=elapsed > 0,
                clock_order_state="nonnegative" if elapsed >= 0 else "clock_or_followup_boundary_unknown",
                opportunity_status="pending_join", opportunity_evidence_grade="not_joined",
                model_eligible=False, model_exclusion_reason="upstream_opportunity_evidence_not_joined")


def _comparisons(index, identity, focal, point, baseline_refs, regular):
    rows = []
    for action in regular:
        candidates = [ref for ref in baseline_refs if ref["action"] == action and _fixed(ref)]
        alive, reasons = [], []
        for ref in candidates:
            okay, reason = _continuous_fixed(index, ref["lineage_id"], index["baseline_index"], point)
            reasons.append(reason)
            if okay:
                alive.append(ref)
        ages = [_fixed_age(index, ref["lineage_id"], point, focal["entry_timestamp"])[0] for ref in alive]
        rows.append(dict(row_kind="comparison", count_unit="repository_action_identity_group",
                         comparison_layer="same_repository_baseline_fixed", focal_episode_id=focal["episode_id"],
                         focal_episode_kind=focal["episode_kind"],
                         focal_repository_id=identity, repository_id=identity, repository=focal["repository"], action=action,
                         entry_commit=focal["entry_commit"], entry_timestamp=focal["entry_timestamp"],
                         eligible=bool(alive), eligibility_reason="eligible" if alive else ";".join(sorted(set(reasons))) if reasons else "no_baseline_fixed_regular_action",
                         baseline_fixed_reference_count=len(candidates), eligible_reference_count=len(alive),
                         eligible_lineage_ids=sorted(ref["lineage_id"] for ref in alive),
                         fixed_age_states=sorted(set(ages)), opportunity_status="pending_join",
                         opportunity_evidence_grade="not_joined", model_eligible=False))
    return rows


def _co_pin_comparisons(index, identity, focal, regular):
    co_pins = defaultdict(set)
    for _, event, _ in index["pins"]:
        if event["commit"] == focal["entry_commit"] and event["after"]["action"] in regular:
            co_pins[event["after"]["action"]].add(event["after"]["lineage_id"])
    return [dict(row_kind="comparison", count_unit="repository_action_identity_group",
                 comparison_layer="same_commit_co_pinned_supplement", focal_episode_id=focal["episode_id"],
                 focal_episode_kind=focal["episode_kind"], focal_repository_id=identity,
                 repository_id=identity, repository=focal["repository"], action=action,
                 entry_commit=focal["entry_commit"], entry_timestamp=focal["entry_timestamp"], eligible=True,
                 eligibility_reason="observed_pin_in_same_commit_no_motive_claim",
                 eligible_reference_count=len(lineages), eligible_lineage_ids=sorted(lineages),
                 fixed_age_states=["observed_zero_at_pin_commit"], opportunity_status="pending_join",
                 opportunity_evidence_grade="not_joined", model_eligible=False)
            for action, lineages in sorted(co_pins.items())]


def _first_observed_fixed(index, repository_row, target, anchor, end):
    if repository_row["baseline_target_state"] != "none_full_sha":
        return
    states = index["snapshots"][index["baseline_index"] + 1:index["endpoint_index"] + 1]
    in_window = [state for state in states if anchor <= state["timestamp"] <= end]
    observed_end = end if index["detail"]["summary"].get("observed_ancestry_covers_end") is True else min(end, index["snapshots"][index["endpoint_index"]]["timestamp"])
    elapsed = observed_end - anchor
    repository_row.update(first_observed_target_full_sha_status="not_observed_in_complete_post_anchor_snapshots",
        post_anchor_complete_snapshot_count=sum(state.get("complete") is True for state in in_window),
        post_anchor_incomplete_snapshot_count=sum(state.get("complete") is not True for state in in_window),
        post_anchor_observed_followup_seconds=elapsed if elapsed >= 0 else None,
        first_observed_target_full_sha_history_unknown=any(not state.get("complete") for state in index["snapshots"][:index["endpoint_index"] + 1]))
    for point in range(index["baseline_index"] + 1, index["endpoint_index"] + 1):
        state = index["snapshots"][point]
        refs = [ref for ref in index["refs_by_commit"][state["sha"]] if ref["action"] == target and _fixed(ref)]
        if not state.get("complete") or not anchor <= state["timestamp"] <= end or not refs:
            continue
        modes, explained = set(), set()
        lineages = {ref["lineage_id"] for ref in refs}
        for event in index["events_by_commit"][state["sha"]]:
            after = event.get("after") or []
            after = after if isinstance(after, list) else [after]
            matched = {ref.get("lineage_id") for ref in after} & lineages
            if matched:
                modes.add(event["event"])
                explained.update(matched)
        if explained != lineages:
            modes.add("unresolved_entry_event")
        repository_row.update(first_observed_target_full_sha_status="observed_post_anchor",
            first_observed_target_full_sha_commit=state["sha"],
            first_observed_target_full_sha_timestamp=state["timestamp"],
            first_observed_target_full_sha_source_modes=sorted(modes),
            first_observed_target_full_sha_reference_count=len(refs),
            first_observed_target_full_sha_history_unknown=(any(not prior.get("complete") for prior in index["snapshots"][:point])
                or bool(modes & {"unknown_boundary", "ambiguous_transition", "unresolved_entry_event"})))
        return


def build_risksets(baseline_details, measurement_details, study, cohort_flow=None, candidate_names=None):
    """Pure function; inputs are loaded detail dictionaries, never future PR data."""
    target, anchor, end = study["target_action"], timestamp(study["event_anchor"]), timestamp(study["window_end"])
    baselines, baseline_conflicts, confirmed = _deduplicate(baseline_details, baseline=True)
    measurements, measurement_conflicts, measured_ids = _deduplicate(measurement_details, baseline=False)
    frequencies, baselines_by_id = {}, {}
    baseline_states, baseline_problems = Counter(), {}
    rows, indexed = [], {}
    for identity in sorted(confirmed):
        detail = baselines.get(identity)
        if detail is None:
            baseline_problems[identity] = "alias_baseline_conflict"
            baseline_states["unknown"] += 1
            rows.append(dict(row_kind="repository", count_unit="stable_repository_id", repository_id=identity,
                             baseline_target_state="unknown", measurement_status="alias_baseline_conflict",
                             opportunity_status="pending_join"))
            continue
        snapshot, summary = detail.get("baseline_snapshot") or {}, detail["summary"]
        state, pinned, mutable = _target_state(_refs(snapshot), target)
        valid = snapshot.get("complete") is True and state != "target_absent" and snapshot.get("sha") == summary.get("baseline_commit")
        baseline_states[state if valid else "unknown"] += 1
        if not valid:
            baseline_problems[identity] = "baseline_missing_incomplete_or_inconsistent"
        else:
            baselines_by_id[identity] = detail
            by_action = defaultdict(list)
            for ref in _refs(snapshot):
                by_action[ref["action"]].append(ref)
            for action, refs in by_action.items():
                record = frequencies.setdefault(action, dict(action=action, upstream_repository=refs[0]["repository"],
                        baseline_repository_count=0, baseline_reference_count=0,
                        baseline_full_sha_repository_count=0, baseline_full_sha_reference_count=0,
                        baseline_mutable_reference_count=0, baseline_sha_targets=set(),
                        observed_full_sha_repository_ids=set(), observed_full_sha_lineages=set(), observed_sha_targets=set()))
                count = sum(_fixed(ref) for ref in refs)
                record["baseline_repository_count"] += 1
                record["baseline_reference_count"] += len(refs)
                record["baseline_full_sha_repository_count"] += bool(count)
                record["baseline_full_sha_reference_count"] += count
                record["baseline_mutable_reference_count"] += len(refs) - count
                record["baseline_sha_targets"].update(ref["ref"] for ref in refs if _fixed(ref))
        measurement = measurements.get(identity)
        index, problem = (None, baseline_problems.get(identity)) if not valid else (
            _measurement_index(measurement, detail) if measurement else (None, "measurement_not_available"))
        if index is not None:
            indexed[identity] = index
        rows.append(dict(row_kind="repository", count_unit="stable_repository_id", repository_id=identity,
                         repository=summary["canonical_repository"], baseline_target_state=state if valid else "unknown",
                         baseline_target_full_sha_references=pinned, baseline_target_mutable_references=mutable,
                         measurement_status="usable" if index else problem,
                         baseline_commit=summary["baseline_commit"], opportunity_status="pending_join"))

    ranked = sorted(frequencies, key=lambda action: (-frequencies[action]["baseline_repository_count"], action))
    regular = [action for action in ranked if action.startswith("actions/") and action != target][:5]
    if len(regular) < 5:
        regular.extend(action for action in ranked if action != target and action not in regular)
    regular = regular[:5]
    for row in rows:
        state = row["baseline_target_state"]
        row.update(first_observed_target_full_sha_status=("baseline_already_fixed" if state in {"all_full_sha", "mixed"}
                    else "measurement_unknown" if state == "none_full_sha" else "baseline_unknown"),
                   first_observed_target_full_sha_commit=None, first_observed_target_full_sha_timestamp=None,
                   first_observed_target_full_sha_source_modes=[], first_observed_target_full_sha_reference_count=None,
                   first_observed_target_full_sha_history_unknown=None, post_anchor_complete_snapshot_count=None,
                   post_anchor_incomplete_snapshot_count=None, post_anchor_observed_followup_seconds=None)
    episodes, focal_episodes = [], []
    out_of_time_pins, out_of_time_fixed_entries, invalid_measurement_baselines = 0, 0, []
    for identity, index in sorted(indexed.items()):
        baseline_index, endpoint = index["baseline_index"], index["endpoint_index"]
        baseline_commit = index["snapshots"][baseline_index]["sha"]
        baseline_refs = index["refs_by_commit"][baseline_commit]
        expected = Counter((r["action"], r["ref"], r["path"], r["job"], r.get("step_id"), r.get("step_index")) for r in _refs(baselines_by_id[identity]["baseline_snapshot"]))
        observed = Counter((r["action"], r["ref"], r["path"], r["job"], r.get("step_id"), r.get("step_index")) for r in baseline_refs)
        if observed != expected:
            invalid_measurement_baselines.append(identity)
            next(row for row in rows if row["row_kind"] == "repository" and row["repository_id"] == identity)["measurement_status"] = "measurement_baseline_reference_mismatch"
            continue
        repository_row = next(row for row in rows if row["row_kind"] == "repository" and row["repository_id"] == identity)
        _first_observed_fixed(index, repository_row, target, anchor, end)
        for ref in baseline_refs:
            if ref["action"] == target and _fixed(ref):
                prior = [ordinal for point, event, ordinal in index["pins"] if point <= baseline_index and event["after"]["lineage_id"] == ref["lineage_id"]]
                episodes.append(_episode(index, identity, ref, baseline_index, anchor, "baseline_fixed", prior[-1] if prior else 0, study))
        # Adoption wording refers to observed repository/action history, never
        # developer intent. Multiple references pinned in one commit stay one
        # repository/action adoption group while retaining separate episodes.
        earlier_fixed_commits = {ref["snapshot_sha"] for ref in index["detail"]["lineages"]["references"]
                                 if ref.get("action") == target and _fixed(ref)}
        first_fixed_index = min((index["indices"][sha] for sha in earlier_fixed_commits), default=None)
        for point, event, ordinal in index["pins"]:
            if event["after"]["action"] != target or not baseline_index < point <= endpoint:
                continue
            if not anchor <= event["timestamp"] <= end:
                out_of_time_pins += 1
                continue
            episode = _episode(index, identity, event["after"], point, event["timestamp"], "post_anchor_observed_pin", ordinal, study)
            episode["earlier_unresolved_history"] = any(not state.get("complete") for state in index["snapshots"][:point])
            episode["repository_action_pinning_history"] = ("earlier_history_unknown" if episode["earlier_unresolved_history"] or event.get("identity_history_status") == "earlier_gap"
                    else "first_observed_fixed_commit" if point == first_fixed_index else "already_observed_fixed_earlier")
            episode["earlier_full_sha_in_lineage"] = any(i < point and _fixed(ref) for i, ref in index["observations"][event["after"]["lineage_id"]])
            episode["repeated_observed_lineage_pin"] |= episode["earlier_full_sha_in_lineage"]
            episodes.append(episode)
            focal_episodes.append((identity, point, episode))
            rows.extend(_comparisons(index, identity, episode, point, baseline_refs, regular))
            rows.extend(_co_pin_comparisons(index, identity, episode, regular))

        for point in range(baseline_index + 1, endpoint + 1):
            state = index["snapshots"][point]
            for event in index["events_by_commit"][state["sha"]]:
                kind = event["event"]
                if kind not in KNOWN_FIXED_ENTRY_EVENTS | {"unknown_boundary", "ambiguous_transition"}:
                    continue
                after = event.get("after") or []
                for ref in after if isinstance(after, list) else [after]:
                    if ref.get("action") != target or not _fixed(ref):
                        continue
                    if not anchor <= event["timestamp"] <= end:
                        out_of_time_fixed_entries += 1
                        continue
                    lineage = ref.get("lineage_id")
                    known = (kind in KNOWN_FIXED_ENTRY_EVENTS and bool(lineage)
                             and ref.get("lineage_status") in {"known_entry", "replacement_entry"}
                             and index["snapshots"][point - 1].get("complete") is True
                             and index["observations"][lineage][0][0] == point)
                    if not known:
                        rows.append(dict(row_kind="fixed_entry_diagnostic", count_unit="reference_fixed_entry_diagnostic",
                            repository_id=identity, repository=repository_row["repository"], action=target, lineage_id=lineage,
                            entry_commit=event["commit"], entry_timestamp=event["timestamp"], entry_mode=kind,
                            source_entry_event=kind, entry_mode_state="unknown",
                            diagnosis_reason="entry_identity_or_prior_tree_unresolved",
                            opportunity_status="pending_join", model_eligible=False))
                        continue
                    episode = _episode(index, identity, ref, point, event["timestamp"], "post_anchor_born_fixed", 0, study)
                    episode.update(entry_mode=kind, source_entry_event=kind, entry_mode_state="known_local_entry",
                        earlier_unresolved_history=(any(not prior.get("complete") for prior in index["snapshots"][:point])
                            or event.get("identity_history_status") == "earlier_gap"), earlier_full_sha_in_lineage=False)
                    episode["repository_action_pinning_history"] = ("earlier_history_unknown" if episode["earlier_unresolved_history"]
                        else "first_observed_fixed_commit" if point == first_fixed_index else "already_observed_fixed_earlier")
                    episodes.append(episode)
                    focal_episodes.append((identity, point, episode))
                    rows.extend(_comparisons(index, identity, episode, point, baseline_refs, regular))
                    rows.extend(_co_pin_comparisons(index, identity, episode, regular))

        for ref in index["detail"]["lineages"]["references"]:
            if _fixed(ref):
                action = ref["action"]
                record = frequencies.setdefault(action, dict(action=action, upstream_repository=ref["repository"],
                         baseline_repository_count=0, baseline_reference_count=0,
                         baseline_full_sha_repository_count=0, baseline_full_sha_reference_count=0,
                         baseline_mutable_reference_count=0, baseline_sha_targets=set(),
                         observed_full_sha_repository_ids=set(), observed_full_sha_lineages=set(), observed_sha_targets=set()))
                record["observed_full_sha_repository_ids"].add(identity)
                if ref.get("lineage_id"):
                    record["observed_full_sha_lineages"].add((identity, ref["lineage_id"]))
                record["observed_sha_targets"].add(ref["ref"])

    usable_ids = set(indexed) - set(invalid_measurement_baselines)
    # A supplementary same-Action comparison uses providers fixed before the
    # anchor. It never requires survival to the end of the study window.
    for identity, point, focal in focal_episodes:
        for provider in sorted(usable_ids - {identity}):
            index = indexed[provider]
            start = index["baseline_index"]
            states = index["snapshots"][:index["endpoint_index"] + 1]
            if any(a["timestamp"] > b["timestamp"] for a, b in zip(states, states[1:])):
                reason, eligible, selected = "cross_repository_clock_alignment_unknown", False, []
            else:
                available = [i for i, state in enumerate(states) if state["timestamp"] <= focal["entry_timestamp"]]
                asof = available[-1] if available else -1
                candidates = [ref for ref in index["refs_by_commit"][states[start]["sha"]] if ref["action"] == target and _fixed(ref)]
                selected = [ref for ref in candidates if asof >= start and _continuous_fixed(index, ref["lineage_id"], start, asof)[0]]
                eligible, reason = bool(selected), "eligible" if selected else "no_continuously_baseline_fixed_target_at_entry"
            if eligible or reason == "cross_repository_clock_alignment_unknown":
                rows.append(dict(row_kind="comparison", count_unit="repository_action_identity_group",
                                 comparison_layer="cross_repository_same_action_supplement", focal_episode_id=focal["episode_id"],
                                 focal_episode_kind=focal["episode_kind"],
                                 focal_repository_id=identity, repository_id=provider,
                                 repository=index["detail"]["summary"]["canonical_repository"], action=target,
                                 entry_timestamp=focal["entry_timestamp"], eligible=eligible, eligibility_reason=reason,
                                 eligible_reference_count=len(selected), eligible_lineage_ids=sorted(ref["lineage_id"] for ref in selected),
                                 fixed_age_states=["baseline_age_requires_delayed_entry_adjustment"], opportunity_status="pending_join",
                                 opportunity_evidence_grade="not_joined", model_eligible=False))

    rows.extend(episodes)
    comparisons = [row for row in rows if row["row_kind"] == "comparison"]
    positive_focal_ids = {row["episode_id"] for row in episodes if row["positive_observed_followup"]}
    layers_by_kind, pairing_by_kind = {}, {}
    for kind in POST_EPISODE_KINDS:
        kind_comparisons = [row for row in comparisons if row["focal_episode_kind"] == kind]
        layers = {}
        for layer in ("same_repository_baseline_fixed", "same_commit_co_pinned_supplement", "cross_repository_same_action_supplement"):
            selected = [row for row in kind_comparisons if row["comparison_layer"] == layer and row["eligible"]]
            layers[layer] = dict(pair_rows=len(selected), focal_repository_ids=len({row["focal_repository_id"] for row in selected}),
                                 focal_episode_ids=len({row["focal_episode_id"] for row in selected}),
                                 positive_followup_focal_episode_ids=len({row["focal_episode_id"] for row in selected if row["focal_episode_id"] in positive_focal_ids}),
                                 positive_followup_focal_repository_ids=len({row["focal_repository_id"] for row in selected if row["focal_episode_id"] in positive_focal_ids}),
                                 comparator_repository_action_groups=len({(row["repository_id"], row["action"]) for row in selected}),
                                 comparator_repository_ids=len({row["repository_id"] for row in selected}))
        focal_count = sum(focal["episode_kind"] == kind for _, _, focal in focal_episodes)
        primary = layers["same_repository_baseline_fixed"]
        episode_coverage = primary["focal_episode_ids"] / focal_count if focal_count else None
        layers_by_kind[kind] = layers
        pairing_by_kind[kind] = dict(episode_coverage=episode_coverage,
            unmatched_reason_counts=dict(Counter(row["eligibility_reason"] for row in kind_comparisons
                if row["comparison_layer"] == "same_repository_baseline_fixed" and not row["eligible"])),
            supplement_display_enabled=(primary["focal_repository_ids"] < SPARSE_REPOSITORIES
                or (episode_coverage is not None and episode_coverage < SPARSE_EPISODE_COVERAGE)),
            predeclared_rule=dict(minimum_primary_focal_repositories=SPARSE_REPOSITORIES, minimum_episode_coverage=SPARSE_EPISODE_COVERAGE),
            semantics="Engineering sparsity/display rule, not power analysis or proof of model adequacy; all layers are always emitted")
    frequency_rows = []
    for action, record in frequencies.items():
        row = {key: value for key, value in record.items() if not isinstance(value, set)}
        row.update(baseline_repository_action_groups=record["baseline_repository_count"],
                   baseline_sha_targets=sorted(record["baseline_sha_targets"]), observed_sha_targets=sorted(record["observed_sha_targets"]),
                   observed_full_sha_repository_count=len(record["observed_full_sha_repository_ids"]),
                   observed_full_sha_lineage_count=len(record["observed_full_sha_lineages"]),
                   opportunity_required=action == target or bool(record["baseline_full_sha_reference_count"] or record["observed_full_sha_lineages"]),
                   incident_target_full_scope=action == target, baseline_regular_comparator=action in regular)
        frequency_rows.append(row)
    frequency_rows.sort(key=lambda row: (-row["baseline_repository_count"], row["action"]))
    flow = list(cohort_flow) if cohort_flow is not None else None
    flow_confirmed = {str(row["repository_id"]) for row in flow or []
                      if row.get("status") == "confirmed" and row.get("repository_id")}
    flow_missing_ids = [row.get("repository", "unknown") for row in flow or []
                        if row.get("status") == "confirmed" and not row.get("repository_id")]
    expected_names = set(candidate_names) if candidate_names is not None else None
    names = [row.get("repository") for row in flow or []]
    names_match = (len(names) == len(set(names)) and set(names) == expected_names) if expected_names is not None else True
    flow_complete = (flow is not None and bool(flow) and not flow_missing_ids and names_match
                     and flow_confirmed == confirmed and all(row.get("status") in {
                         "confirmed", "not_at_baseline", "inaccessible_unknown", "no_pre_event_history", "parse_incomplete"} for row in flow))
    coverage_complete = bool(confirmed) and usable_ids == confirmed and not baseline_conflicts and not measurement_conflicts
    baseline_episodes = [row for row in episodes if row["episode_kind"] == "baseline_fixed"]
    post_episodes = [row for row in episodes if row["episode_kind"] == "post_anchor_observed_pin"]
    born_episodes = [row for row in episodes if row["episode_kind"] == "post_anchor_born_fixed"]
    diagnostics = [row for row in rows if row["row_kind"] == "fixed_entry_diagnostic"]
    none_repositories = [row for row in rows if row["row_kind"] == "repository" and row["baseline_target_state"] == "none_full_sha"]
    first_observed = [row for row in none_repositories if row["first_observed_target_full_sha_status"] == "observed_post_anchor"]
    summary = dict(rules_version=RULES_VERSION, generated_at_utc=None,
        measurement_batch_directory=BATCH_DIRECTORY, measurement_rules_version=MEASUREMENT_RULES_VERSION,
        scope="cohort_complete_descriptive_summary" if coverage_complete and flow_complete else "provisional_available_measurements",
        frozen_for_formal_models=False,
        coverage=dict(candidate_names=len(flow) if flow is not None else None,
                      expected_candidate_names=len(expected_names) if expected_names is not None else None,
                      candidate_names_match_source=names_match, flow_confirmed_ids_match_details=flow_confirmed == confirmed,
                      confirmed_flow_rows_missing_ids=flow_missing_ids,
                      candidate_status_counts=dict(Counter(row.get("status", "unknown") for row in flow)) if flow is not None else None,
                      candidate_flow_complete=flow_complete, confirmed_repository_ids=len(confirmed),
                      usable_baseline_repository_ids=len(baselines_by_id), measured_repository_ids=len(usable_ids),
                      measured_fraction=len(usable_ids) / len(confirmed) if confirmed else None,
                      not_yet_usable_repository_ids=sorted(confirmed - usable_ids),
                      measurement_files_outside_confirmed_ids=sorted(measured_ids - confirmed),
                      baseline_alias_conflict_ids=baseline_conflicts, measurement_conflict_ids=measurement_conflicts,
                      baseline_problem_ids=baseline_problems, measurement_baseline_mismatch_ids=invalid_measurement_baselines,
                      all_confirmed_measurements_usable=coverage_complete),
        baseline_target_repository_states=dict(baseline_states),
        risksets=dict(baseline_fixed_target_reference_episodes=len(baseline_episodes),
                      baseline_fixed_target_repository_ids=len({row["repository_id"] for row in baseline_episodes}),
                      baseline_fixed_positive_followup_repository_ids=len({row["repository_id"] for row in baseline_episodes if row["positive_observed_followup"]}),
                      post_anchor_pin_reference_episodes=len(post_episodes),
                      post_anchor_pin_repository_ids=len({row["repository_id"] for row in post_episodes}),
                      post_anchor_pin_repository_action_groups=len({(row["repository_id"], row["action"]) for row in post_episodes}),
                      post_anchor_pin_commit_groups=len({(row["repository_id"], row["entry_commit"]) for row in post_episodes}),
                      post_anchor_pin_positive_followup_repository_ids=len({row["repository_id"] for row in post_episodes if row["positive_observed_followup"]}),
                       post_anchor_born_fixed_reference_episodes=len(born_episodes),
                       post_anchor_born_fixed_repository_ids=len({row["repository_id"] for row in born_episodes}),
                       post_anchor_born_fixed_commit_groups=len({(row["repository_id"], row["entry_commit"]) for row in born_episodes}),
                       post_anchor_born_fixed_positive_followup_repository_ids=len({row["repository_id"] for row in born_episodes if row["positive_observed_followup"]}),
                       post_anchor_born_fixed_source_event_counts=dict(Counter(row["source_entry_event"] for row in born_episodes)),
                       post_anchor_born_fixed_exit_kind_counts=dict(Counter(row["exit_kind"] for row in born_episodes)),
                       episode_kind_counts=dict(Counter(row["episode_kind"] for row in episodes)),
                       episode_kind_repository_counts={kind: len({row["repository_id"] for row in episodes if row["episode_kind"] == kind})
                           for kind in ("baseline_fixed", *POST_EPISODE_KINDS)},
                      repeated_observed_lineage_pin_episodes=sum(row["repeated_observed_lineage_pin"] for row in post_episodes),
                      baseline_exit_kind_counts=dict(Counter(row["exit_kind"] for row in baseline_episodes)),
                      post_anchor_exit_kind_counts=dict(Counter(row["exit_kind"] for row in post_episodes)),
                      earlier_history_unknown_post_anchor_episodes=sum(row["repository_action_pinning_history"] == "earlier_history_unknown" for row in post_episodes),
                      entry_fixed_age_unknown_episodes=sum(row["fixed_age_state"] != "observed_within_window" for row in episodes),
                      observed_pin_events_excluded_for_clock_boundary=out_of_time_pins,
                       fixed_entry_references_excluded_for_clock_boundary=out_of_time_fixed_entries,
                      opportunity_join_completed=False, delay_model_eligible_episodes=0),
        fixed_entry_diagnostics=dict(reference_rows=len(diagnostics), repository_ids=len({row["repository_id"] for row in diagnostics}),
            source_event_counts=dict(Counter(row["source_entry_event"] for row in diagnostics))),
        repository_first_observed_target_full_sha=dict(baseline_none_confirmed_repository_ids=len(none_repositories),
            baseline_none_usable_repository_ids=sum(row["repository_id"] in usable_ids for row in none_repositories),
            observed_repository_ids=len(first_observed),
            not_observed_repository_ids=sum(row["first_observed_target_full_sha_status"] == "not_observed_in_complete_post_anchor_snapshots" for row in none_repositories),
            not_observed_with_unknown_snapshot_repository_ids=sum(row["first_observed_target_full_sha_status"] == "not_observed_in_complete_post_anchor_snapshots"
                and row["first_observed_target_full_sha_history_unknown"] for row in none_repositories),
            not_observed_without_positive_followup_repository_ids=sum(row["first_observed_target_full_sha_status"] == "not_observed_in_complete_post_anchor_snapshots"
                and (row["post_anchor_observed_followup_seconds"] is None or row["post_anchor_observed_followup_seconds"] <= 0) for row in none_repositories),
            measurement_unknown_repository_ids=sum(row["first_observed_target_full_sha_status"] == "measurement_unknown" for row in none_repositories),
            source_mode_repository_counts=dict(Counter(mode for row in first_observed for mode in row["first_observed_target_full_sha_source_modes"])),
            source_mode_set_repository_counts=dict(Counter("+".join(row["first_observed_target_full_sha_source_modes"]) for row in first_observed)),
            earlier_history_unknown_repository_ids=sum(row["first_observed_target_full_sha_history_unknown"] for row in first_observed)),
        regular_action_selection=dict(actions=regular,
                rule="Rank baseline repository/action frequency descending, Action identity ascending; prefer actions/*, fill other non-target identities only if fewer than five",
                baseline_scope_complete=flow_complete and not baseline_problems,
                source="all_usable_confirmed_baselines_not_postincident_outcomes",
                replacements_for_initial_list=sorted(set(regular) - set(DEFAULT_REGULAR_ACTIONS))),
        comparison_layers=layers_by_kind["post_anchor_observed_pin"],
        primary_pairing=pairing_by_kind["post_anchor_observed_pin"],
        comparison_layers_by_episode_kind=layers_by_kind,
        primary_pairing_by_episode_kind=pairing_by_kind,
        count_units=dict(repository="stable repository ID", action_group="repository ID / Action identity",
                         episode="continuous observed full-SHA period within one reference lineage",
                         comparisons="Rows sharing a repository/action or focal episode are not independent samples"),
        limitations=["Temporal post-anchor pinning is not evidence of incident motivation or a causal effect.",
                     "Baseline fixed references enter at the anchor; their pin age can be left censored.",
                      "Born-fixed entries are new local reference lineages, not non-SHA to SHA transitions or lifetime first adoptions.",
                     "Comparators need only survive fixed until entry, never until the endpoint.",
                     "No upstream opportunity, matching exchangeability, or statistical power is inferred by this table."])
    return dict(risksets=rows, summary=summary, upstream_frequencies=frequency_rows)


def _write_csv(path, rows):
    fields = sorted({field for row in rows for field in row}) or ["row_kind"]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (list, dict)) else value
                         for key, value in row.items()} for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    study = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    baseline_paths = sorted((root / "data/derived/cohort-v1/baselines").glob("*.json"))
    measurement_paths = [path for path in sorted((root / "data/derived" / BATCH_DIRECTORY).glob("*.json")) if path.stem.isdigit()]
    flow_path = root / "reports/cohort_flow.csv"
    with flow_path.open(encoding="utf-8", newline="") as stream:
        flow = list(csv.DictReader(stream))
    candidate_path = root / "reports/candidate_source_membership.csv"
    with candidate_path.open(encoding="utf-8", newline="") as stream:
        candidates = [row["repository"] for row in csv.DictReader(stream)]
    paths = [root / "study.toml", Path(__file__), root / "pintrace/measurement.py", root / "pintrace/measurement_io.py",
             root / "pyproject.toml", flow_path, candidate_path, *baseline_paths, *measurement_paths]
    hashes = {path.relative_to(root).as_posix(): file_sha256(path) for path in paths}
    result = build_risksets([json.loads(path.read_text(encoding="utf-8")) for path in baseline_paths],
                           [load_analysis_measurement(path, compact_reference) for path in measurement_paths], study, flow, candidates)
    inputs_unchanged = all(file_sha256(root / name) == value for name, value in hashes.items())
    if not inputs_unchanged:
        result["summary"]["scope"] = "provisional_available_measurements"
    if args.require_complete and result["summary"]["scope"] != "cohort_complete_descriptive_summary":
        parser.error("Candidate flow or confirmed measurement coverage is incomplete; no reports overwritten")
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    _write_csv(reports / "risksets.csv", result["risksets"])
    _write_csv(reports / "upstream_frequencies.csv", result["upstream_frequencies"])
    result["summary"].update(generated_at_utc=datetime.now(timezone.utc).isoformat(), source_sha256=hashes,
            measurement_io_version=IO_RULES_VERSION,
            inputs_unchanged=inputs_unchanged,
            output_sha256={name: hashlib.sha256((reports / name).read_bytes()).hexdigest() for name in ("risksets.csv", "upstream_frequencies.csv")})
    (reports / "riskset_summary.json").write_text(json.dumps(result["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(scope=result["summary"]["scope"],
            confirmed_ids=result["summary"]["coverage"]["confirmed_repository_ids"],
            measured_ids=result["summary"]["coverage"]["measured_repository_ids"],
            baseline_target_states=result["summary"]["baseline_target_repository_states"],
            risksets=result["summary"]["risksets"], comparison_layers=result["summary"]["comparison_layers"]), indent=2))


if __name__ == "__main__":
    main()
