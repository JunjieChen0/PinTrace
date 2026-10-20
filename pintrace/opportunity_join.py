"""Join dated upstream evidence to specific downstream states and opportunities."""

import bisect
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tomllib

from .opportunities import grade_evidence
from .measurement import BATCH_DIRECTORY, RULES_VERSION as MEASUREMENT_RULES_VERSION
from .risksets import RULES_VERSION as RISKSET_RULES_VERSION, compact_reference
from .measurement_io import IO_RULES_VERSION, file_sha256, load_analysis_measurement

RULES_VERSION = "opportunity-join-v5"
FULL_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)\Z")
NUMERIC_VERSION = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\Z")
SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
TAG_AVAILABILITY_TYPES = {"observed_tag_introduction", "observed_tag_reintroduction"}
EPISODE_ENTRY_MODES = {
    "baseline_fixed": {"baseline_fixed"},
    "post_anchor_observed_pin": {"pin"},
    "post_anchor_born_fixed": {"first_observed_action", "copied_reference", "reintroduced_action", "replace"},
}


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if isinstance(value, str) else float(value)


def _version(tag):
    match = FULL_VERSION.fullmatch(tag or "")
    return tuple(map(int, match.groups())) if match else None


def _true(value):
    return value is True or value == "True"


def _list(value):
    return json.loads(value) if isinstance(value, str) and value else value or []


def _sources(upstreams):
    indexed = {}
    for payload in upstreams:
        repository = payload["summary"]["repository"].lower()
        immutable, archived = defaultdict(list), defaultdict(list)
        for record in payload.get("release_records", []):
            if grade_evidence(record) == "A" and record.get("evidence_type") == "immutable_release":
                immutable[record["tag"]].append(record)
        for record in payload.get("observations", []):
            if grade_evidence(record) == "B":
                archived[record["tag"]].append(record)
        for records in archived.values():
            records.sort(key=lambda record: _time(record["visit_date"]))
        indexed[repository] = dict(payload=payload, immutable=immutable, archived=archived,
                archive_times={tag: [_time(record["visit_date"]) for record in records] for tag, records in archived.items()},
                snapshots={visit["snapshot"]: visit for visit in payload.get("snapshot_visits", [])})
    return indexed


def comment_identity(reference, observed_at, source):
    """Only a literal-tag immutable A binding can establish persistent identity."""
    tag = reference.get("comment_version")
    result = dict(comment_version=tag, comment_identity_state="none" if not tag else "unverified",
                  identity_reason="no_version_hint" if not tag else "no_dated_immutable_literal_tag_binding",
                  identity_evidence_grade=None, identity_source_urls=[], archive_evidence_scope="none")
    if not tag or source is None:
        return result
    if reference.get("comment_attachment_state") != "attached":
        result["identity_reason"] = "comment_attachment_unresolved"
        return result
    if reference.get("comment_action") and reference["comment_action"] != reference.get("action"):
        result["identity_reason"] = "ratchet_action_does_not_match_reference"
        return result
    dated = [record for record in source["immutable"].get(tag, []) if _time(record["published_at"]) <= observed_at]
    targets = {record["target"].lower() for record in dated}
    if len(targets) == 1:
        result.update(comment_identity_state="verified_consistent" if reference["ref"].lower() in targets else "verified_conflict",
                      identity_reason="literal_tag_immutable_binding_published_by_snapshot", identity_evidence_grade="A",
                      identity_source_urls=sorted({record["source_url"] for record in dated}))
    elif len(targets) > 1:
        result["identity_reason"] = "conflicting_immutable_source_records"
    records = source["archived"].get(tag, [])
    if records:
        position = bisect.bisect_right(source["archive_times"][tag], observed_at)
        result["archive_evidence_scope"] = "discrete_visits_only_not_persistent_identity"
        for label, record in (("before", records[position - 1] if position else None),
                              ("after", records[position] if position < len(records) else None)):
            if record:
                result["archive_" + label] = {key: record[key] for key in ("visit_date", "target", "source_url")}
    return result


def _opportunity(record):
    result = dict(record)
    grade = record.get("evidence_grade")
    if grade == "A" and grade_evidence(record) == "A" and record.get("available_at"):
        binding_time = record.get("published_at") if record.get("evidence_type") == "immutable_release" else record.get("event_time")
        if not binding_time or _time(record["available_at"]) != _time(binding_time):
            return None
        result.update(lower=_time(record["available_at"]), upper=_time(record["available_at"]))
    elif (grade == "B" and record.get("available_after") and record.get("available_by")
          and SHA.fullmatch(record.get("target", ""))
          and all(record.get(key) for key in ("lower_snapshot", "upper_snapshot", "lower_url", "upper_url"))):
        kind = record.get("evidence_type")
        if kind == "observed_binding_change":
            if not SHA.fullmatch(record.get("previous_target", "")) or record["previous_target"] == record["target"]:
                return None
        elif kind in TAG_AVAILABILITY_TYPES:
            if not all(record.get(key) is True for key in ("lower_complete", "upper_complete", "lower_snapshot_tags_complete", "upper_snapshot_tags_complete")):
                return None
        else:
            return None
        result.update(lower=_time(record["available_after"]), upper=_time(record["available_by"]))
        if result["lower"] >= result["upper"]:
            return None
    else:
        return None
    key = json.dumps({key: result.get(key) for key in ("repository", "tag", "target", "lower", "upper", "evidence_grade")}, sort_keys=True)
    result["opportunity_id"] = "op-" + hashlib.sha256(key.encode()).hexdigest()[:20]
    return result


def _measurement(detail):
    if (detail.get("summary", {}).get("measurement_rules_version") != MEASUREMENT_RULES_VERSION
            or detail.get("summary", {}).get("status") != "measured" or not detail.get("replay", {}).get("passed")
            or not detail.get("lineages", {}).get("conservation", {}).get("passed")):
        return None
    snapshots = detail["snapshots"]
    positions = {state["sha"]: index for index, state in enumerate(snapshots)}
    endpoint = detail.get("timeline", {}).get("endpoint_commit", detail["summary"].get("endpoint_commit"))
    if endpoint not in positions:
        return None
    snapshots = snapshots[:positions[endpoint] + 1]
    references = defaultdict(dict)
    observations = []
    selected_commits = {state["sha"] for state in snapshots}
    for ref in detail["lineages"]["references"]:
        if ref["snapshot_sha"] in selected_commits:
            observations.append(ref)
            if ref.get("lineage_id"):
                references[ref["snapshot_sha"]][ref["lineage_id"]] = ref
    clocks = [state["timestamp"] for state in snapshots]
    return dict(detail=detail, snapshots=snapshots, clocks=clocks, references=references, observations=observations,
                monotonic=all(a <= b for a, b in zip(clocks, clocks[1:])))


def _current(index, when):
    point = bisect.bisect_right(index["clocks"], when) - 1
    return point if point >= 0 and index["snapshots"][point].get("complete") else None


def _first_change(index, point, reference, target, horizon):
    for state in index["snapshots"][point + 1:]:
        if state["timestamp"] > horizon:
            break
        if not state.get("complete"):
            return "unknown_boundary", state["timestamp"], None
        current = index["references"][state["sha"]].get(reference["lineage_id"])
        if current is None:
            return "lineage_no_longer_present", state["timestamp"], None
        if current.get("ref_type") != "full_sha":
            return "unpinned", state["timestamp"], current
        if current["ref"] != reference["ref"]:
            return ("adopted_target" if current["ref"].lower() == target.lower() else "other_sha_change"), state["timestamp"], current
    end = horizon if index["detail"]["summary"].get("observed_ancestry_covers_end") is True else min(horizon, index["clocks"][-1])
    return "right_censored_no_observed_sha_change", end, None


def _target_segment(index, point, reference):
    first = point
    while first > 0:
        state = index["snapshots"][first - 1]
        earlier = index["references"][state["sha"]].get(reference["lineage_id"])
        if not state.get("complete") or earlier is None or earlier.get("ref_type") != "full_sha" or earlier["ref"] != reference["ref"]:
            break
        first -= 1
    return index["snapshots"][first]["sha"]


def _continuous(index, lineage, start, end):
    first = _current(index, start)
    last = _current(index, end)
    if first is None or last is None:
        return False
    return all(state.get("complete") and index["references"][state["sha"]].get(lineage, {}).get("ref_type") == "full_sha"
               for state in index["snapshots"][first:last + 1])


def _episode_link(row):
    return {key: row.get(key) for key in ("episode_id", "episode_kind", "entry_mode", "source_entry_event")}


def _known_episode(row):
    return (row.get("row_kind") == "episode" and bool(row.get("episode_id"))
            and row.get("entry_mode_state") != "unknown"
            and row.get("entry_mode") in EPISODE_ENTRY_MODES.get(row.get("episode_kind"), set()))


def _scope_links(rows, identity, lineage, when, index, episode_index):
    episodes, comparisons = {}, {}
    for row in rows:
        if str(row.get("repository_id")) != identity:
            continue
        entry = row.get("entry_timestamp")
        if entry in (None, "") or float(entry) > when:
            continue
        if row["row_kind"] == "episode" and row.get("lineage_id") == lineage:
            ending = row.get("exit_timestamp")
            if ending not in (None, "") and when < float(ending):
                episodes[row["episode_id"]] = _episode_link(row)
        elif (row["row_kind"] == "comparison" and _true(row.get("eligible"))
              and lineage in _list(row.get("eligible_lineage_ids")) and _continuous(index, lineage, float(entry), when)):
            focal = episode_index[row["focal_episode_id"]]
            key = (row["focal_episode_id"], row["comparison_layer"])
            comparisons[key] = dict(focal_episode_id=focal["episode_id"], focal_episode_kind=focal["episode_kind"],
                                    focal_entry_mode=focal["entry_mode"], comparison_layer=row["comparison_layer"])
    return [episodes[key] for key in sorted(episodes)], [comparisons[key] for key in sorted(comparisons)]


def _episode_coverage(episodes, candidates, field, value):
    exported = {row["episode_id"] for row in episodes if row[field] == value}
    selected = [row for row in candidates if any(link[field] == value for link in row["rq3_target_episode_links"])]
    linked = {link["episode_id"] for row in selected for link in row["rq3_target_episode_links"] if link[field] == value}
    return dict(exported_episodes=len(exported), candidate_episodes=len(linked),
                candidate_episode_fraction=len(linked) / len(exported) if exported else None,
                candidate_repository_ids=len({row["repository_id"] for row in selected}),
                selected_opportunity_reference_pairs=len(selected),
                actual_target_sha_adoptions=len({(row["repository_id"], row["lineage_id"], row["outcome_timestamp"], row["adopted_sha"])
                                                for row in selected if row["outcome"] == "adopted_target"}))


def _archived_code_version(reference, visit):
    """Infer code identity at one complete visit, never persistent comment truth."""
    bindings = visit["bindings"]
    matched = [tag for tag, target in bindings.items() if target.lower() == reference["ref"].lower()]
    hint = reference.get("comment_version") if reference.get("comment_attachment_state") == "attached" else None
    if reference.get("comment_action") and reference["comment_action"] != reference["action"]:
        return None, "ratchet_action_does_not_match_reference", matched
    explicit = _version(hint)
    if explicit:
        alias_targets = {target.lower() for tag, target in bindings.items() if _version(tag) == explicit}
        if len(alias_targets) > 1:
            return None, "lower_version_alias_targets_conflict", matched
        if bindings.get(hint, "").lower() == reference["ref"].lower():
            return explicit, "literal_full_tag_code_binding_at_complete_lower_visit", [hint]
        return None, "literal_full_tag_not_bound_to_reference_at_lower_visit", matched
    if any(NUMERIC_VERSION.fullmatch(tag) and tag not in bindings for tag in visit.get("tag_names", [])):
        return None, "numeric_tag_targets_unresolved_at_lower_visit", matched
    full_versions = {_version(tag) for tag in matched if _version(tag) is not None}
    majors = {int(match[1]) for tag in matched if (match := NUMERIC_VERSION.fullmatch(tag))}
    if len(majors) > 1:
        return None, "multiple_archived_major_channels_for_old_sha", matched
    if len(full_versions) != 1:
        return None, "multiple_or_no_archived_full_versions_for_old_sha", matched
    version = next(iter(full_versions))
    if len({target.lower() for tag, target in bindings.items() if _version(tag) == version}) > 1:
        return None, "lower_version_alias_targets_conflict", matched
    if hint:
        partial = NUMERIC_VERSION.fullmatch(hint)
        if partial is None:
            return None, "comment_hint_does_not_identify_numeric_release_channel", matched
        prefix = tuple(int(part) for part in partial.groups() if part is not None)
        if version[:len(prefix)] != prefix:
            return None, "comment_channel_conflicts_with_archived_code_version", matched
    return version, "unique_full_version_and_major_for_code_at_complete_lower_visit", matched


def _tag_availability(reference, opportunity, source):
    lower = source["snapshots"].get(opportunity["lower_snapshot"])
    upper = source["snapshots"].get(opportunity["upper_snapshot"])
    if any(not visit or visit.get("complete") is not True or visit.get("tag_listing_complete") is not True
           for visit in (lower, upper)):
        return False, "complete_snapshot_bindings_not_available", None, []
    aliases = opportunity.get("tag_aliases") or [opportunity["tag"]]
    if any(tag in lower.get("tag_names", []) for tag in aliases):
        return False, "candidate_tag_not_absent_at_lower_complete_visit", None, []
    if not any(upper["bindings"].get(tag, "").lower() == opportunity["target"].lower() for tag in aliases):
        return False, "new_tag_target_not_confirmed_in_upper_visit", None, []
    newer = _version(opportunity["tag"])
    if newer is None:
        return False, "new_tag_does_not_identify_full_semver", None, []
    if (opportunity.get("upper_version_target_conflict") is True
            or len({target.lower() for tag, target in upper["bindings"].items() if _version(tag) == newer}) > 1):
        return False, "upper_version_alias_targets_conflict", None, []
    if any(_version(tag) == newer and target.lower() == opportunity["target"].lower() for tag, target in lower["bindings"].items()):
        return False, "numeric_version_target_already_available_at_lower_visit", None, []
    older, reason, tags = _archived_code_version(reference, lower)
    if older is None:
        return False, reason, None, tags
    if older[0] != newer[0] or older >= newer:
        return False, "new_tag_is_not_a_later_same_major_code_version", older, tags
    if reference["ref"].lower() == opportunity["target"].lower():
        return False, "already_at_opportunity_target", older, tags
    return True, "later_same_major_tag_observed_available_after_complete_lower_visit", older, tags


def build_join(measurements, upstreams, risksets, frequencies, study, baseline_details=None):
    sources = _sources(upstreams)
    window_start, horizon = _time(study["window_start"]), _time(study["window_end"])
    evidence_rows, matches, measured_ids = [], [], set()
    observation_keys = set()
    clock_unknown_ids, upstream_totals = set(), {}
    episode_index = {row["episode_id"]: row for row in risksets if _known_episode(row)}
    scope_rows = defaultdict(list)
    for row in risksets:
        identity = str(row.get("repository_id"))
        if _known_episode(row):
            scope_rows[(identity, row["lineage_id"])].append(row)
        elif (row["row_kind"] == "comparison" and _true(row.get("eligible"))
              and row.get("focal_episode_id") in episode_index):
            for lineage in _list(row.get("eligible_lineage_ids")):
                scope_rows[(identity, lineage)].append(row)
    allowed_ids = {str(row["repository_id"]) for row in risksets if row["row_kind"] == "repository" and row.get("baseline_target_state") != "unknown"}
    measured_allowed_ids = {str(row["repository_id"]) for row in risksets if row["row_kind"] == "repository" and row.get("measurement_status") == "usable"}
    for frequency in frequencies:
        repository = frequency["upstream_repository"].lower()
        upstream_totals.setdefault(repository, dict(repository=repository, action_identities=set(), baseline_ids=set(),
            baseline_full_sha_ids=set(), baseline_reference_count=0, baseline_full_sha_reference_count=0))
        upstream_totals[repository]["action_identities"].add(frequency["action"])
    if baseline_details is not None:
        seen = set()
        for baseline in baseline_details:
            identity = str(baseline["summary"].get("repository_id"))
            state = baseline.get("baseline_snapshot") or {}
            if identity not in allowed_ids or identity in seen or state.get("complete") is not True:
                continue
            seen.add(identity)
            for ref in state["references"]:
                if ref.get("kind") != "action":
                    continue
                repository = ref["repository"].lower()
                row = upstream_totals[repository]
                row["baseline_ids"].add(identity)
                row["baseline_reference_count"] += 1
                if ref.get("ref_type") == "full_sha":
                    row["baseline_full_sha_ids"].add(identity)
                    row["baseline_full_sha_reference_count"] += 1
    opportunity_index = {}
    for repository, source in sources.items():
        opportunities = [_opportunity(record) for record in source["payload"].get("opportunities", [])]
        opportunity_index[repository] = {record["opportunity_id"]: record for record in opportunities if record is not None}
    seen_measurements = set()
    for detail in measurements:
        identity = str(detail["summary"]["repository_id"])
        index = _measurement(detail)
        if index is None or identity in seen_measurements or (baseline_details is not None and identity not in measured_allowed_ids):
            continue
        seen_measurements.add(identity)
        measured_ids.add(identity)
        if not index["monotonic"]:
            clock_unknown_ids.add(identity)
        for ref in index["observations"]:
            if ref.get("kind") != "action" or ref.get("ref_type") != "full_sha":
                continue
            source = sources.get(ref["repository"].lower())
            locator = dict(repository_id=identity, snapshot_sha=ref["snapshot_sha"],
                           path=ref["path"], job=ref["job"], step_index=ref.get("step_index"))
            observation_key = hashlib.sha256(json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if observation_key in observation_keys:
                raise ValueError("Nonunique per-snapshot Action reference locator: " + json.dumps(locator, sort_keys=True))
            observation_keys.add(observation_key)
            evidence_rows.append(dict(repository_id=identity, repository=detail["summary"]["canonical_repository"],
                 action=ref["action"], upstream_repository=ref["repository"].lower(), lineage_id=ref.get("lineage_id"),
                 reference_observation_key=observation_key, snapshot_sha=ref["snapshot_sha"],
                 path=ref["path"], job=ref["job"], step_id=ref.get("step_id"), step_index=ref.get("step_index"), line=ref.get("line"),
                 snapshot_commit=ref["snapshot_sha"], snapshot_timestamp=ref["snapshot_timestamp"],
                 ref=ref["ref"], sha=ref["ref"], comment_kind=ref.get("comment_kind", "unknown"),
                 **comment_identity(ref, ref["snapshot_timestamp"], source)))
        for repository, opportunities in opportunity_index.items():
            for opportunity in opportunities.values():
                lower, upper = opportunity["lower"], opportunity["upper"]
                if not window_start <= lower <= upper <= horizon:
                    continue
                if not index["monotonic"]:
                    continue
                point = _current(index, lower)
                if point is None:
                    continue
                state = index["snapshots"][point]
                for ref in index["references"][state["sha"]].values():
                    if ref.get("repository", "").lower() != repository or ref.get("ref_type") != "full_sha":
                        continue
                    grade = opportunity["evidence_grade"]
                    identity_evidence = comment_identity(ref, ref["snapshot_timestamp"], sources[repository])
                    outcome, changed_at, adopted = _first_change(index, point, ref, opportunity["target"], horizon)
                    applicable, reason = False, "channel_or_identity_unresolved"
                    archived_version, archived_tags = None, []
                    if grade == "A":
                        current_version, new_version = _version(ref.get("comment_version")), _version(opportunity.get("tag"))
                        if identity_evidence["comment_identity_state"] == "verified_conflict":
                            reason = "literal_tag_sha_conflict"
                        elif identity_evidence["comment_identity_state"] == "verified_consistent" and current_version and new_version:
                            if current_version[0] == new_version[0] and current_version < new_version:
                                applicable, reason = True, "verified_full_version_identity_later_same_major_release"
                            else:
                                reason = "not_a_later_release_in_verified_same_major_channel"
                    elif opportunity["evidence_type"] in TAG_AVAILABILITY_TYPES:
                        applicable, reason, archived_version, archived_tags = _tag_availability(ref, opportunity, sources[repository])
                        if applicable and not _continuous(index, ref["lineage_id"], lower, upper):
                            applicable, reason = False, "reference_not_continuously_fixed_through_availability_interval"
                    else:
                        if ref.get("comment_attachment_state") != "attached":
                            reason = "comment_attachment_unresolved"
                        elif ref.get("comment_version") not in (opportunity.get("tag_aliases") or [opportunity["tag"]]):
                            reason = "literal_tag_hint_does_not_identify_archived_channel"
                        elif ref.get("comment_action") and ref["comment_action"] != ref["action"]:
                            reason = "ratchet_action_does_not_match_reference"
                        elif ref["ref"].lower() != opportunity["previous_target"].lower():
                            reason = "reference_sha_differs_from_archived_lower_binding"
                        elif changed_at < upper:
                            reason = "downstream_changed_or_censored_before_upper_binding_bound"
                        elif _continuous(index, ref["lineage_id"], lower, upper):
                            applicable, reason = True, "same_literal_tag_lower_binding_and_survival_to_upper_bound"
                    if ref["ref"].lower() == opportunity["target"].lower():
                        applicable, reason = False, "already_at_opportunity_target"
                    if changed_at < upper or changed_at < lower:
                        applicable = False
                        reason = "downstream_changed_or_censored_before_upper_binding_bound"
                    episode_links, comparison_links = _scope_links(scope_rows[(identity, ref["lineage_id"])], identity, ref["lineage_id"], upper, index, episode_index)
                    episode_ids = [link["episode_id"] for link in episode_links]
                    comparison_ids = sorted({link["focal_episode_id"] for link in comparison_links})
                    update_bounds = (dict(lower_days=(changed_at - upper) / 86400, upper_days=(changed_at - lower) / 86400)
                                     if applicable and outcome == "adopted_target" else None)
                    matches.append(dict(repository_id=identity, repository=detail["summary"]["canonical_repository"],
                         action=ref["action"], upstream_repository=repository, lineage_id=ref["lineage_id"],
                         reference_snapshot_commit=ref["snapshot_sha"], reference_sha=ref["ref"],
                         reference_target_segment_start_commit=_target_segment(index, point, ref),
                         opportunity_id=opportunity["opportunity_id"], opportunity_tag=opportunity["tag"],
                         opportunity_tag_aliases=opportunity.get("tag_aliases") or [opportunity["tag"]],
                         opportunity_target=opportunity["target"], evidence_grade=grade,
                         available_after_or_at=lower, available_by=upper,
                         evidence_type=opportunity["evidence_type"],
                         evidence_scope="immutable_release_publication" if grade == "A" else "observed_tag_availability_not_first_ever_publication" if opportunity["evidence_type"] in TAG_AVAILABILITY_TYPES else "discrete_same_tag_binding_change_interval",
                         channel_policy="same_major_after_verified_full_version_identity" if grade == "A" else "same_major_after_code_identity_at_complete_lower_snapshot" if opportunity["evidence_type"] in TAG_AVAILABILITY_TYPES else "same_literal_tag_only",
                         archived_lower_code_version=archived_version, archived_lower_identity_tags=archived_tags,
                         applicable=applicable, applicability_reason=reason,
                         comment_identity_state=identity_evidence["comment_identity_state"],
                         outcome=outcome, outcome_timestamp=changed_at, adopted_sha=adopted["ref"] if adopted else None,
                         update_delay_days=update_bounds, followup_bounds_days=dict(lower=max(0, (changed_at - upper) / 86400), upper=max(0, (changed_at - lower) / 86400)) if applicable else None,
                         rq3_target_episode_ids=episode_ids, comparison_focal_episode_ids=comparison_ids,
                         rq3_target_episode_links=episode_links, comparison_focal_episode_links=comparison_links,
                         target_delay_model_candidate=applicable and bool(episode_ids),
                         target_pin_delay_model_candidate=applicable and any(link["episode_kind"] == "post_anchor_observed_pin" for link in episode_links),
                         target_born_fixed_delay_model_candidate=applicable and any(link["episode_kind"] == "post_anchor_born_fixed" for link in episode_links),
                         target_baseline_delay_model_candidate=applicable and any(link["episode_kind"] == "baseline_fixed" for link in episode_links),
                         archive_reversion_sensitivity_required=grade == "B",
                         formal_model_frozen=False))
    # A release and an archive interval can describe the same target-code
    # opportunity. Select by evidence strength and earliest availability only,
    # never by the observed update outcome or its apparent delay.
    duplicate_groups = defaultdict(list)
    for row in matches:
        row.update(selected_for_delay_analysis=False, evidence_selection_reason="not_applicable")
        if row["applicable"]:
            key = (row["repository_id"], row["lineage_id"], row["reference_target_segment_start_commit"], row["opportunity_target"])
            duplicate_groups[key].append(row)
    for candidates in duplicate_groups.values():
        ordered = sorted(candidates, key=lambda row: (0 if row["evidence_grade"] == "A" else 1,
                row["available_by"], row["available_after_or_at"], row["opportunity_id"]))
        ordered[0].update(selected_for_delay_analysis=True, evidence_selection_reason="strongest_then_earliest_evidence_for_target_segment")
        for row in ordered[1:]:
            row.update(target_delay_model_candidate=False, target_pin_delay_model_candidate=False,
                       target_born_fixed_delay_model_candidate=False, target_baseline_delay_model_candidate=False,
                       evidence_selection_reason="duplicate_target_segment_opportunity_evidence")
    coverage_rows = []
    for repository, totals in sorted(upstream_totals.items()):
        source = sources.get(repository)
        binding_grades = set()
        if source:
            binding_grades.update(grade_evidence(record) for record in source["payload"].get("release_records", []))
            binding_grades.update(grade_evidence(record) for record in source["payload"].get("observations", []))
        best = "A" if "A" in binding_grades else "B" if "B" in binding_grades else "C" if "C" in binding_grades else "not_collected"
        opportunities = list(opportunity_index.get(repository, {}).values())
        selected = [row for row in matches if row["upstream_repository"] == repository and row["applicable"]]
        coverage_rows.append(dict(upstream_repository=repository, action_identities=sorted(totals["action_identities"]),
            baseline_repository_count=len(totals["baseline_ids"]) if baseline_details is not None else None,
            baseline_full_sha_repository_count=len(totals["baseline_full_sha_ids"]) if baseline_details is not None else None,
            baseline_reference_count=totals["baseline_reference_count"] if baseline_details is not None else None,
            baseline_full_sha_reference_count=totals["baseline_full_sha_reference_count"] if baseline_details is not None else None,
            historical_binding_best_grade=best, opportunity_grade_counts=dict(Counter(row["evidence_grade"] for row in opportunities)),
            upstream_presence_proxy_full_sha_references=totals["baseline_full_sha_reference_count"] if best in {"A", "B"} and baseline_details is not None else 0,
            applicable_opportunity_reference_pairs=len(selected),
            applicable_reference_lineages=len({(row["repository_id"], row["lineage_id"]) for row in selected}),
            applicable_downstream_repository_ids=len({row["repository_id"] for row in selected}),
            target_episode_ids_with_opportunity=len({episode for row in selected for episode in row["rq3_target_episode_ids"]}),
            collection_errors=source["payload"]["summary"].get("errors", []) if source else ["upstream_not_collected"] ))
    applicable = [row for row in matches if row["applicable"]]
    selected = [row for row in applicable if row["selected_for_delay_analysis"]]
    target = [row for row in applicable if row["target_delay_model_candidate"]]
    all_lineages = {(row["repository_id"], row["lineage_id"]) for row in evidence_rows if row["lineage_id"]}
    applicable_lineages = {(row["repository_id"], row["lineage_id"]) for row in applicable}
    risk_episode_ids = set(episode_index)
    candidate_episodes = {episode for row in target for episode in row["rq3_target_episode_ids"]}
    baseline_denominator = sum(row["baseline_full_sha_reference_count"] or 0 for row in coverage_rows)
    proxy_counts = Counter()
    for row in coverage_rows:
        proxy_counts[row["historical_binding_best_grade"]] += row["baseline_full_sha_reference_count"] or 0
    summary = dict(rule_version=RULES_VERSION, scope="provisional_evidence_join_not_dictionary_freeze",
        measurement_batch_directory=BATCH_DIRECTORY, measurement_rules_version=MEASUREMENT_RULES_VERSION,
        measured_repository_ids=len(measured_ids), clock_unalignable_repository_ids=sorted(clock_unknown_ids),
        full_sha_reference_observations=len(evidence_rows), comment_identity_counts=dict(Counter(row["comment_identity_state"] for row in evidence_rows)),
        reference_locator_unique=len(observation_keys) == len(evidence_rows),
        reference_locator_fields=["repository_id", "snapshot_sha", "path", "job", "step_index"],
        reference_line_semantics="Original YAML scalar node line; aliases may point to their anchor definition. Use per-snapshot step_index with path/job for occurrence identity, not line alone.",
        opportunity_reference_pair_rows=len(matches), applicable_opportunity_reference_pairs=len(applicable),
        selected_opportunity_reference_pairs=len(selected),
        duplicate_target_opportunity_evidence_rows=len(applicable) - len(selected),
        opportunity_evidence_selection_rule="Same repository/lineage/constant-SHA segment/new target: A before B, then earliest availability bounds; never selected by outcome",
        total_measured_full_sha_reference_lineages=len(all_lineages),
        applicable_reference_lineages=len(applicable_lineages),
        applicable_measured_lineage_fraction=len(applicable_lineages) / len(all_lineages) if all_lineages else None,
        applicable_downstream_repository_ids=len({row["repository_id"] for row in applicable}),
        applicable_grade_counts=dict(Counter(row["evidence_grade"] for row in applicable)),
        applicable_evidence_type_counts=dict(Counter(row["evidence_type"] for row in applicable)),
        target_rq3_episode_candidates=len(candidate_episodes), exported_target_riskset_episodes=len(risk_episode_ids),
        target_rq3_by_episode_kind={kind: _episode_coverage(episode_index.values(), target, "episode_kind", kind)
                                    for kind in EPISODE_ENTRY_MODES},
        target_rq3_by_entry_mode={mode: _episode_coverage(episode_index.values(), target, "entry_mode", mode)
                                 for mode in sorted(set().union(*EPISODE_ENTRY_MODES.values()))},
        unknown_fixed_entry_diagnostic_rows=sum(row["row_kind"] == "fixed_entry_diagnostic" for row in risksets),
        rq3_scope_semantics="Known baseline, observed pin and born-fixed episodes are distinct strata; unknown fixed entries are diagnostic only.",
        target_rq3_candidate_episode_fraction=len(candidate_episodes) / len(risk_episode_ids) if risk_episode_ids else None,
        target_rq3_repository_candidates=len({row["repository_id"] for row in target}),
        target_rq3_opportunity_pairs=len(target),
        actual_target_sha_adoptions=len({(row["repository_id"], row["lineage_id"], row["outcome_timestamp"], row["adopted_sha"]) for row in target if row["outcome"] == "adopted_target"}),
        inapplicability_reasons=dict(Counter(row["applicability_reason"] for row in matches if not row["applicable"])),
        target_rq3_candidate_opportunity_time_range_utc={
            "first_lower_bound": datetime.fromtimestamp(min(row["available_after_or_at"] for row in target), timezone.utc).isoformat(),
            "last_upper_bound": datetime.fromtimestamp(max(row["available_by"] for row in target), timezone.utc).isoformat()} if target else None,
        baseline_full_sha_reference_denominator=baseline_denominator,
        upstream_presence_proxy_full_sha_references_by_best_grade=dict(proxy_counts),
        upstream_presence_proxy_fraction_by_best_grade={key: value / baseline_denominator if baseline_denominator else None for key, value in proxy_counts.items()},
        upstream_presence_proxy_full_sha_references=sum(row["upstream_presence_proxy_full_sha_references"] for row in coverage_rows),
        proxy_semantics="Presence of any A/B upstream binding evidence only; not an applicable-opportunity or latency denominator",
        freeze_ready=False,
        limits=["Literal major tags are not replaced with immutable full-semver tags when verifying comments.",
                "B archives establish discrete bindings and bounded changes, not continuous tag identity or absence of intermediate reversions.",
                "Tag absence then presence in complete visits identifies observed availability, not first-ever tag publication.",
                "Point release availability and commit first availability are distinct.",
                "Monotonic observable downstream timestamps are required for opportunity calendar joins.",
                "Applicable pairs can share a lineage, episode or repository; their count is not an independent sample size.",
                "A configuration lacks an observed target adoption can be censored or superseded; it is not labelled maintenance failure."])
    return dict(summary=summary, upstream_coverage=coverage_rows, reference_evidence=evidence_rows, opportunity_matches=matches)


def render_report(result, riskset_summary):
    summary = result["summary"]
    lines = ["# 上游更新机会覆盖与下游适用性", "", "本报告为当前可用测量的自动关联结果，**尚未冻结数据字典或正式延迟模型**。", "",
             f"当前风险集导出覆盖 {riskset_summary['coverage']['measured_repository_ids']}/{riskset_summary['coverage']['confirmed_repository_ids']} 个确认仓库；本次关联读取 {summary['measured_repository_ids']} 个通过重放的测量文件。两个批次的覆盖范围分别记录，不能混为完整队列。", "",
             "| 上游仓库 | 基线仓库 | 基线完整 SHA 引用 | 历史绑定最高等级 | A/B 机会数 | 实际适用机会×谱系 | 下游仓库 |", "| --- | ---: | ---: | --- | --- | ---: | ---: |"]
    selected = [row for row in result["upstream_coverage"] if row["historical_binding_best_grade"] != "not_collected"]
    for row in sorted(selected, key=lambda row: -(row["baseline_repository_count"] or 0)):
        lines.append(f"| {row['upstream_repository']} | {row['baseline_repository_count']} | {row['baseline_full_sha_reference_count']} | {row['historical_binding_best_grade']} | {json.dumps(row['opportunity_grade_counts'], ensure_ascii=False)} | {row['applicable_opportunity_reference_pairs']} | {row['applicable_downstream_repository_ids']} |")
    lines += ["", f"全基线完整 SHA 引用分母为 {summary['baseline_full_sha_reference_denominator']}；其中 {summary['upstream_presence_proxy_full_sha_references']} 条引用的上游至少有一条 A/B 绑定证据。这只是**上游证据存在的 proxy**，不能作为延迟分析分母。", "",
              f"实际识别 {summary['applicable_opportunity_reference_pairs']} 个适用机会×引用谱系，涉及 {summary['applicable_reference_lineages']} 条谱系、{summary['applicable_downstream_repository_ids']} 个仓库；关联到涉事 Action 风险集的候选为 {summary['target_rq3_episode_candidates']} 个 episode、{summary['target_rq3_repository_candidates']} 个仓库，观察到 {summary['actual_target_sha_adoptions']} 次匹配目标 SHA 的采用。共享仓库/谱系的行不独立。", "",
              f"同一仓库/谱系/连续 SHA 目标段的新目标若有多份证据，先选 A 级，再按同级最早可用界限确定入口，不查看更新结果或延迟大小。保留全部证据诊断，归并后有 {summary['selected_opportunity_reference_pairs']} 个机会×谱系入口，另有 {summary['duplicate_target_opportunity_evidence_rows']} 条重复证据。", "",
              f"基线已固定、既有引用非 SHA→SHA 转换、引入即 SHA 分层统计：`{json.dumps(summary['target_rq3_by_episode_kind'], ensure_ascii=False)}`；具体入口模式为 `{json.dumps(summary['target_rq3_by_entry_mode'], ensure_ascii=False)}`。转换队列的分母不包含引入即固定，未知入口只保留诊断。", "",
              f"A/B/C 等最高上游绑定等级对应的基线 SHA 引用 proxy 数量为 `{json.dumps(summary['upstream_presence_proxy_full_sha_references_by_best_grade'], ensure_ascii=False)}`，比例为 `{json.dumps(summary['upstream_presence_proxy_fraction_by_best_grade'], ensure_ascii=False)}`。真正适用机会的谱系分母为当前已测 {summary['total_measured_full_sha_reference_lineages']} 条完整 SHA 谱系，适用比例为 `{summary['applicable_measured_lineage_fraction']}`；这两个比例衡量的对象不同。", "",
              f"完整 SHA 引用快照共 {summary['full_sha_reference_observations']} 条，注释身份状态：`{json.dumps(summary['comment_identity_counts'], ensure_ascii=False)}`。只有字面相同 tag 的 A 级不可变发布、且发布时间不晚于快照，才可确认一致或冲突。B 级访问记录保留为离散证据，主要版本标签不拿完整版本标签替换。", "",
              "适用性规则：A 级使用已核实完整版本身份的同主版本后续发布。B 级既包括同字面 tag 换绑，也包括完整快照中标签缺席后出现/再出现：后者从下界完整绑定识别旧 SHA 的完整版本及主版本通道，再比较同主版本的更高完整版本。裸 SHA 的版本关联只陈述归档时点的代码身份与预先指定的同主版本策略，不推断维护者意图；版本或通道冲突保持未知。", "",
              "归档缺席后出现不证明该 tag 首次在历史上发布；窗口内可能有未观察到的删除和回转。只有标签名列表完整才能支持缺席；旧 SHA 通道推断还需足够的目标绑定。B 级延迟子集以引用持续固定且未发生首次 SHA 目标变化、退出或解析缺口直到可用时间上界为条件；它描述存活至上界的引用，不能代表所有固定引用。早于上界发生的变化或退出属于不可识别适用性诊断，不标为维护失败，也不强行推导非负延迟。提交时间倒序的仓库保留身份材料，但不做日历时间机会关联。", "",
              "## 暂定 RQ3 范围与缺口", "",
              "固定保留、回退、删除和未知退出仍可在全量可测队列描述。延迟结果只能使用上述逐引用关联到 A/B 机会的子集；B 级另做区间与未观察到的标签回转敏感性分析。当前不对未识别通道、只有 C 级当前状态或没有上游证据的引用推导延迟。", "",
              f"当前候选延迟子集的上游机会时间范围为 `{json.dumps(summary['target_rq3_candidate_opportunity_time_range_utc'], ensure_ascii=False)}`；若为空，尚无可开放的涉事 Action 延迟子集。", "",
              f"不可适用原因：`{json.dumps(summary['inapplicability_reasons'], ensure_ascii=False)}`。", "",
              f"提交时钟不能对齐的已测仓库数：{len(summary['clock_unalignable_repository_ids'])}。", ""]
    for row in selected:
        if row["collection_errors"]:
            errors = row["collection_errors"]
            lines.append(f"- `{row['upstream_repository']}` 仍有 {len(errors)} 条采集异常，详见机会汇总 JSON 与原始上游文件；不能把限流视为无历史记录。")
    lines += ["", f"完整引用身份和机会映射保存在 `data/derived/{RULES_VERSION}/`；输入哈希、分母、适用性和缺口见 `reports/opportunity_join_summary.json`。本次不会改写冻结的历史测量文件。", ""]
    return "\n".join(lines)


def measurement_paths_from_manifest(root, riskset_summary):
    """Use the exact measurement inputs exported by risksets; reject old batches."""
    if (riskset_summary.get("measurement_batch_directory") != BATCH_DIRECTORY
            or riskset_summary.get("measurement_rules_version") != MEASUREMENT_RULES_VERSION):
        raise ValueError("Risksets belong to another measurement batch; regenerate risksets first")
    if riskset_summary.get("rules_version") != RISKSET_RULES_VERSION:
        raise ValueError("Risksets use another analysis rules version; regenerate risksets first")
    prefix = "data/derived/" + BATCH_DIRECTORY + "/"
    paths = []
    for name, expected in sorted(riskset_summary["source_sha256"].items()):
        relative = Path(name)
        if name.startswith(prefix) and relative.parent.as_posix() == prefix.rstrip("/") and relative.suffix == ".json" and relative.stem.isdigit():
            path = root / relative
            if not path.is_file() or file_sha256(path) != expected:
                raise ValueError("Riskset measurement input changed or missing: " + name)
            paths.append(path)
    return paths


def main():
    root = Path(__file__).resolve().parents[1]
    riskset_path, frequencies_path = root / "reports/risksets.csv", root / "reports/upstream_frequencies.csv"
    with riskset_path.open(encoding="utf-8", newline="") as stream:
        risksets = list(csv.DictReader(stream))
    with frequencies_path.open(encoding="utf-8", newline="") as stream:
        frequencies = list(csv.DictReader(stream))
    summary_path = root / "reports/riskset_summary.json"
    riskset_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    baseline_paths = sorted((root / "data/derived/cohort-v1/baselines").glob("*.json"))
    measurement_paths = measurement_paths_from_manifest(root, riskset_summary)
    upstream_paths = sorted((root / "data/derived/upstream-opportunities").glob("*.json"))
    study = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    paths = [Path(__file__), root / "pintrace/opportunities.py", root / "pintrace/measurement.py", root / "pintrace/measurement_io.py",
             root / "pyproject.toml", root / "study.toml", riskset_path, frequencies_path, summary_path,
             *baseline_paths, *measurement_paths, *upstream_paths]
    hashes = {path.relative_to(root).as_posix(): file_sha256(path) for path in paths}
    result = build_join([load_analysis_measurement(path, compact_reference) for path in measurement_paths],
                        [json.loads(path.read_text(encoding="utf-8")) for path in upstream_paths], risksets, frequencies, study,
                        [json.loads(path.read_text(encoding="utf-8")) for path in baseline_paths])
    output = root / "data/derived" / RULES_VERSION
    output.mkdir(parents=True, exist_ok=True)
    for key in ("reference_evidence", "opportunity_matches"):
        path = output / (key + ".jsonl")
        with path.open("w", encoding="utf-8") as stream:
            for row in result[key]:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    result["summary"].update(checked_at_utc=datetime.now(timezone.utc).isoformat(), input_sha256=hashes,
                 measurement_io_version=IO_RULES_VERSION,
                 inputs_unchanged=all(file_sha256(root / name) == value for name, value in hashes.items()),
                 upstream_coverage=result["upstream_coverage"], riskset_export_coverage=riskset_summary["coverage"],
                 details={key: {"path": (output / (key + ".jsonl")).relative_to(root).as_posix(),
                               "sha256": file_sha256(output / (key + ".jsonl"))} for key in ("reference_evidence", "opportunity_matches")})
    (root / "reports/opportunity_join_summary.json").write_text(json.dumps(result["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "reports/opportunity_coverage.md").write_text(render_report(result, riskset_summary), encoding="utf-8")
    print(json.dumps({key: result["summary"][key] for key in ("scope", "measured_repository_ids", "full_sha_reference_observations",
                     "comment_identity_counts", "applicable_opportunity_reference_pairs", "target_rq3_episode_candidates", "target_rq3_repository_candidates")}, indent=2))


if __name__ == "__main__":
    main()
