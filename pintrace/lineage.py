"""Conservative, deterministic direct-Action reference lineages.

Input snapshots must already follow oldest-to-newest first-parent topology.
Timestamps never match references. Step-node positions can match only when an
identical Git blob proves unchanged workflow bytes at a known file identity. A lineage
ends at a replacement or an unknown boundary; unknown entries start fresh IDs.
The conservation ledger counts reference instances, not Action identity groups.
"""

from collections import Counter, defaultdict
from hashlib import sha256
import json


RULES_VERSION = "lineage-v2"


def _stable(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _actions(snapshot):
    return [dict(ref) for ref in snapshot.get("references", []) if ref.get("kind") == "action"]


def _conflicting_anchors(old, new):
    """A changed explicit identity must not be overridden by a weaker key."""
    return any(old.get(key) and new.get(key) and old[key] != new[key]
               for key in ("step_id", "step_context"))


def _file_blobs(files):
    counts = Counter(item.get("path") for item in files)
    return {item["path"]: item["blob"] for item in files
            if counts[item.get("path")] == 1 and item.get("kind") == "blob"
            and item.get("mode") in ("100644", "100755") and item.get("blob")}


def _match(old, new, renames, old_files=(), new_files=()):
    """Return only unique pairs, plus connected unresolved transition groups."""
    previous, current = set(range(len(old))), set(range(len(new)))
    pairs = []
    old_blobs, new_blobs = _file_blobs(old_files), _file_blobs(new_files)

    def scope(ref, before=False):
        path = ref.get("path")
        return (renames.get(path, path) if before else path, ref.get("job"))

    def stage(key, rule, compatible):
        left, right = defaultdict(list), defaultdict(list)
        for index in sorted(previous):
            value = key(old[index], True)
            if value is not None:
                left[value].append(index)
        for index in sorted(current):
            value = key(new[index], False)
            if value is not None:
                right[value].append(index)
        for value in sorted(left.keys() & right.keys(), key=_stable):
            if len(left[value]) == len(right[value]) == 1:
                a, b = left[value][0], right[value][0]
                if compatible(old[a], new[b]):
                    pairs.append((a, b, rule))
                    previous.remove(a)
                    current.remove(b)

    def unchanged_node(ref, before):
        path = ref.get("path")
        mapped = renames.get(path, path) if before else path
        blob = (old_blobs if before else new_blobs).get(path)
        if (not blob or not ref.get("job") or type(ref.get("step_index")) is not int
                or ref["step_index"] < 0 or type(ref.get("line")) is not int or ref["line"] < 1):
            return None
        return (mapped, blob, ref["job"], ref["step_index"], ref["line"])

    # Identical object identity proves that this AST position did not move or
    # change, including repeated/aliased uses nodes. Position alone never does.
    stage(unchanged_node, "identical_workflow_blob_node",
          lambda a, b: all(a.get(key) == b.get(key) for key in ("kind", "action", "ref", "ref_type")))

    stage(lambda ref, before: (*scope(ref, before), ref["step_id"])
          if ref.get("step_id") else None,
          "unique_step_id", lambda a, b: True)
    stage(lambda ref, before: (*scope(ref, before), _stable(ref["step_context"]))
          if ref.get("step_context") else None,
          "unique_step_context", lambda a, b: not (a.get("step_id") and b.get("step_id")
                                                   and a["step_id"] != b["step_id"]))
    stage(lambda ref, before: (*scope(ref, before), ref["action"], ref["step_name"])
          if ref.get("step_name") else None,
          "unique_action_step_name", lambda a, b: not _conflicting_anchors(a, b))
    stage(lambda ref, before: (*scope(ref, before), ref["action"]),
          "unique_remaining_action_in_job", lambda a, b: not _conflicting_anchors(a, b))

    # Unmatched references can represent rewrites, moves without Git rename
    # evidence, or conflicting identities.  Keep every connected component
    # with both sides unknown rather than guessing replacements or removals.
    edges = defaultdict(set)
    for a in sorted(previous):
        for b in sorted(current):
            if (old[a]["action"] == new[b]["action"]
                    or scope(old[a], True) == scope(new[b])
                    or old[a].get("step_id") and old[a].get("step_id") == new[b].get("step_id")):
                edges[("old", a)].add(("new", b))
                edges[("new", b)].add(("old", a))
    groups = []
    visited = set()
    for node in sorted(edges):
        if node in visited:
            continue
        pending, component = [node], set()
        while pending:
            item = pending.pop()
            if item in component:
                continue
            component.add(item)
            pending.extend(edges[item] - component)
        visited.update(component)
        left = sorted(index for side, index in component if side == "old")
        right = sorted(index for side, index in component if side == "new")
        previous.difference_update(left)
        current.difference_update(right)
        groups.append((left, right))
    return pairs, sorted(previous), sorted(current), groups


def _version_event(old, new):
    if old.get("ref_type") != "full_sha" and new.get("ref_type") == "full_sha":
        return "pin"
    if old.get("ref_type") == "full_sha" and new.get("ref_type") != "full_sha":
        return "unpin"
    if old.get("ref") != new.get("ref"):
        return "sha_target_change" if new.get("ref_type") == "full_sha" else "mutable_update"
    return None


def reconstruct(repository, snapshots):
    """Return JSON-serializable observations, events, quality and instance ledger.

    ``complete`` must be explicitly True for a snapshot to be measurable.
    Renames on each *after* snapshot map old workflow paths to new paths.
    Optional step_context must contain meaningful non-uses context (None for an
    otherwise empty step); callers must exclude comments and sequence position.
    Only first-observed identity groups are claimed: history before the input
    window is unobserved, and any earlier incomplete interval remains explicit.
    """
    references, events, intervals = [], [], []
    totals = Counter()
    seen_actions = set()
    active = []
    previous_snapshot = None
    initial_instances = 0
    history_has_unknown = False
    serial = 0

    def observation(ref, snapshot, status, lineage_id=None):
        nonlocal serial
        if lineage_id is None:
            serial += 1
            identity = f"{repository}\0{snapshot['sha']}\0{serial}"
            lineage_id = "rl-" + sha256(identity.encode()).hexdigest()[:20]
        return dict(ref, lineage_id=lineage_id, repository_name=repository,
                    snapshot_sha=snapshot["sha"], snapshot_date=snapshot.get("date"),
                    snapshot_timestamp=snapshot.get("timestamp"), lineage_status=status)

    def event(kind, snapshot, before=None, after=None, **fields):
        row = dict(event=kind, repository=repository, commit=snapshot["sha"],
                   previous_commit=previous_snapshot["sha"] if previous_snapshot else None,
                   timestamp=snapshot.get("timestamp"), date=snapshot.get("date"),
                   before=before, after=after, **fields)
        events.append(row)
        totals["event_" + kind] += 1

    def entry_kind(action, old_actions, entered_actions):
        if action in old_actions or action in entered_actions:
            return "copied_reference"
        entered_actions.add(action)
        return "reintroduced_action" if action in seen_actions else "first_observed_action"

    for index, snapshot in enumerate(snapshots):
        complete = snapshot.get("complete") is True
        raw = _actions(snapshot)
        if index == 0 and complete:
            active = [observation(ref, snapshot, "baseline") for ref in raw]
            references.extend(active)
            seen_actions.update(ref["action"] for ref in active)
            initial_instances = len(active)
            previous_snapshot = snapshot
            continue

        ledger = Counter(before_instances=len(active))
        fresh = []
        if not complete:
            ledger["unknown_exits"] = len(active)
            if active:
                event("unknown_boundary", snapshot, before=active,
                      reason="incomplete_snapshot", reference_exits=len(active), reference_entries=0)
            # Partial parse output is evidence only; it has no inferred lineage.
            references.extend(dict(ref, lineage_id=None, repository_name=repository,
                                   snapshot_sha=snapshot["sha"], snapshot_date=snapshot.get("date"),
                                   snapshot_timestamp=snapshot.get("timestamp"),
                                   lineage_status="snapshot_incomplete") for ref in raw)
            history_has_unknown = True
            totals["incomplete_snapshots"] += 1
        elif previous_snapshot is None or previous_snapshot.get("complete") is not True:
            fresh = [observation(ref, snapshot, "unknown_entry") for ref in raw]
            ledger["unknown_entries"] = len(fresh)
            event("unknown_boundary", snapshot, after=fresh,
                  reason="after_incomplete_snapshot", reference_exits=0, reference_entries=len(fresh))
        else:
            totals["complete_adjacent_intervals"] += 1
            pairs, removed, added, ambiguous = _match(active, raw, snapshot.get("renames", {}),
                                                     previous_snapshot.get("files", []), snapshot.get("files", []))
            mapped = {}
            old_actions = {ref["action"] for ref in active}
            entered_actions = set()
            for a, b, rule in pairs:
                totals["matching_rule_" + rule] += 1
                old, new = active[a], raw[b]
                if old["action"] == new["action"]:
                    current = observation(new, snapshot, "matched", old["lineage_id"])
                    kind = _version_event(old, new)
                    if kind:
                        event(kind, snapshot, before=old, after=current, matching_rule=rule)
                    ledger["continued_instances"] += 1
                else:
                    current = observation(new, snapshot, "replacement_entry")
                    kind = entry_kind(new["action"], old_actions, entered_actions)
                    event("replace", snapshot, before=old, after=current, matching_rule=rule,
                          entry_kind=kind, reference_entries=1, reference_exits=1,
                          identity_history_status="earlier_gap" if history_has_unknown else "observed_window")
                    ledger["replacement_entries"] += 1
                    ledger["replacement_exits"] += 1
                    totals["entry_kind_" + kind] += 1
                current["lineage_matching_rule"] = rule
                mapped[b] = current
            for a in removed:
                event("remove", snapshot, before=active[a], reference_exits=1)
                ledger["removal_exits"] += 1
            for b in added:
                new = raw[b]
                kind = entry_kind(new["action"], old_actions, entered_actions)
                current = observation(new, snapshot, "known_entry")
                event(kind, snapshot, after=current, reference_entries=1,
                      identity_history_status="earlier_gap" if history_has_unknown else "observed_window")
                ledger[kind + "_entries"] += 1
                totals["entry_kind_" + kind] += 1
                mapped[b] = current
            if ambiguous:
                totals["ambiguous_intervals"] += 1
            for left, right in ambiguous:
                before = [active[a] for a in left]
                after = [observation(raw[b], snapshot, "lineage_ambiguous") for b in right]
                event("ambiguous_transition", snapshot, before=before, after=after,
                      reason="no_unique_supported_match", lineage_status="lineage_ambiguous",
                      reference_exits=len(before), reference_entries=len(after))
                ledger["unknown_exits"] += len(before)
                ledger["unknown_entries"] += len(after)
                totals["ambiguous_before_instances"] += len(before)
                totals["ambiguous_after_instances"] += len(after)
                for b, current in zip(right, after):
                    mapped[b] = current
            fresh = [mapped[b] for b in range(len(raw))]

        ledger["after_instances"] = len(fresh)
        ledger["known_entries"] = sum(ledger[key] for key in (
            "first_observed_action_entries", "copied_reference_entries", "reintroduced_action_entries",
            "replacement_entries"))
        ledger["known_exits"] = ledger["removal_exits"] + ledger["replacement_exits"]
        ledger["balance"] = (ledger["before_instances"] + ledger["known_entries"]
                             + ledger["unknown_entries"] - ledger["after_instances"]
                             - ledger["known_exits"] - ledger["unknown_exits"])
        intervals.append(dict(previous_commit=previous_snapshot["sha"] if previous_snapshot else None,
                              commit=snapshot["sha"], **ledger, passed=ledger["balance"] == 0))
        for key in ("known_entries", "known_exits", "unknown_entries", "unknown_exits",
                    "replacement_entries", "replacement_exits", "removal_exits",
                    "first_observed_action_entries", "copied_reference_entries", "reintroduced_action_entries"):
            totals[key] += ledger[key]
        active = fresh
        references.extend(fresh)
        if complete:
            seen_actions.update(ref["action"] for ref in fresh)
        previous_snapshot = snapshot

    final_instances = len(active)
    balance = (initial_instances + totals["known_entries"] + totals["unknown_entries"]
               - final_instances - totals["known_exits"] - totals["unknown_exits"])
    return dict(repository=repository, rules_version=RULES_VERSION, references=references, events=events,
                conservation=dict(initial_instances=initial_instances, final_instances=final_instances,
                                  known_entries=totals["known_entries"], known_exits=totals["known_exits"],
                                  unknown_entries=totals["unknown_entries"], unknown_exits=totals["unknown_exits"],
                                  balance=balance, passed=balance == 0 and all(row["passed"] for row in intervals),
                                  endpoint_known=bool(snapshots) and snapshots[-1].get("complete") is True,
                                  formula="initial + known_entries + unknown_entries = final + known_exits + unknown_exits",
                                  replacement_accounting="one known exit and one known entry per replacement",
                                  intervals=intervals),
                quality=dict(totals, snapshots=len(snapshots), observed_action_groups=len(seen_actions),
                             ambiguous_transition_rate=(totals["ambiguous_intervals"] / totals["complete_adjacent_intervals"]
                                                        if totals["complete_adjacent_intervals"] else None),
                             history_has_unknown=history_has_unknown,
                             matching_uses_step_position=True,
                             matching_uses_unverified_step_position=False,
                             matching_uses_blob_proven_node_position=True,
                             matching_uses_timestamps=False))
