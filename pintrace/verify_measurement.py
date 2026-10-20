"""Audit the complete candidate/cohort scope and replay fixed measurement inputs."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tomllib

from .configuration import CONFIG_PATHS
from decimal import Decimal

import ijson
from ijson.common import ObjectBuilder

from .history import git
from .lineage import reconstruct
from .measurement import BATCH_DIRECTORY, SOURCE_NAMES, confirmed_records, fixed_history
from .measurement import modification_commits, read_state, replay_detail, save, select_points, workflow_renames
from .measurement import _CacheOnly
from .measurement_io import sections


RULES_VERSION = "full-measurement-verification-v4"
VERIFICATION_DIRECTORY = "measurement-verification-v4"
REQUIRED_REFERENCE_FIELDS = ("comment_kind", "comment_identity_state", "comment_attachment_state",
                             "configuration_coverage", "configuration_complete", "configuration_snapshot_sha")
UNFINISHED_COHORT = {"pending", "identity_pending", "collection_failed", "blob_unavailable"}
COUNT_KEYS = ("known_entries", "known_exits", "unknown_entries", "unknown_exits")
# json.loads of a file this large builds a text copy of the whole file first and exhausts memory. Above the limit the same
# dictionary is built by the streaming parser; every check that follows is the unchanged one.
LARGE_FILE_BYTES = 300 * 2**20
# A file this large is verified alone after the worker pool has finished, so its memory is not added to the other workers'.
HEAVY_FILE_BYTES = 2**30
# A dictionary built from a file this large does not fit next to the re-read states of the replay (about 3.4 times the file
# size for the parsed copy alone). Such a file is verified one snapshot at a time; the row it produces has the same fields.
STREAM_VERIFY_BYTES = 3 * 2**30
SNAPSHOT_KEYS_NEEDED_BY_LINEAGE = ("sha", "timestamp", "date", "complete", "references", "files", "renames", "rename_evidence")
DETAIL_KEYS = {"summary", "snapshots", "implementation_sha256", "timeline", "lineages", "replay"}


def digest(path):
    """SHA-256 of a file read in chunks; the value equals that of measurement.digest without holding the file in memory."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_detail(path):
    """The measurement file as a dictionary, equal to json.loads of its text for every size."""
    path = Path(path)
    if path.stat().st_size <= LARGE_FILE_BYTES:
        return json.loads(path.read_text(encoding="utf-8"))
    detail = dict(sections(path, DETAIL_KEYS))
    if "summary" not in detail:
        raise KeyError("summary")
    return detail


def read_summary(path):
    """Only the leading summary object; the rest of the file is not read."""
    stream = sections(path, {"summary"})
    try:
        for _, value in stream:
            return value
    finally:
        stream.close()
    raise KeyError("summary")


def split_by_weight(paths):
    """Ordinary files for the worker pool and heavy files for sequential verification after it."""
    ordinary = [path for path in paths if path.stat().st_size <= HEAVY_FILE_BYTES]
    heavy = sorted((path for path in paths if path.stat().st_size > HEAVY_FILE_BYTES), key=lambda path: path.stat().st_size)
    return ordinary, heavy


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def implementation_hashes(root):
    paths = [root / "study.toml", *[root / "pintrace" / name for name in SOURCE_NAMES]]
    return {path.relative_to(root).as_posix(): digest(path) for path in paths}


def audit_scope(candidate_names, flow_rows, cohort_records, measurements):
    """Set equality uses all available outputs, never only the current run count."""
    names = set(candidate_names)
    flow_names = [row["repository"] for row in flow_rows]
    records_by_name = {row["repository"]: row for row in cohort_records}
    expected = {str(row["repository_id"]) for row in cohort_records
                if row.get("status") == "confirmed" and row.get("repository_id")}
    successful = {str(row["repository_id"]) for row in measurements if row.get("status") == "measured"}
    pending_names = sorted(name for name in names if name not in records_by_name
                           or records_by_name[name].get("status") in UNFINISHED_COHORT)
    identity_failures = sorted(row["repository"] for row in cohort_records
                               if not row.get("repository_id") and not (
                                   row.get("status") == "inaccessible_unknown" and row.get("http_status") in (404, 451)))
    mismatched_flow = sorted(name for name in names & records_by_name.keys()
                             if next((row.get("status") for row in flow_rows if row["repository"] == name), None)
                             != records_by_name[name].get("status"))
    duplicate_measurement_ids = sorted(identity for identity, count in Counter(
        str(row["repository_id"]) for row in measurements).items() if count > 1)
    result = dict(candidate_names=len(candidate_names), unique_candidate_names=len(names),
                  ledger_names=len(flow_names), ledger_name_set_equal=set(flow_names) == names,
                  ledger_has_duplicate_names=len(flow_names) != len(set(flow_names)),
                  missing_or_pending_candidate_names=pending_names,
                  extra_cohort_names=sorted(set(records_by_name) - names),
                  identity_or_access_code_failures=identity_failures, flow_status_mismatches=mismatched_flow,
                  confirmed_repository_ids=sorted(expected), measured_repository_ids=sorted(successful),
                  pending_measurement_ids=sorted(expected - successful), unexpected_measurement_ids=sorted(successful - expected),
                  duplicate_measurement_ids=duplicate_measurement_ids,
                  baseline_status_counts=dict(Counter(row.get("status") for row in cohort_records)))
    result["passed"] = (len(candidate_names) == len(names) and result["ledger_name_set_equal"]
                        and not result["ledger_has_duplicate_names"] and not pending_names
                        and not result["extra_cohort_names"] and not identity_failures and not mismatched_flow
                        and expected == successful and not duplicate_measurement_ids)
    return result


def audit_conservation(lineages, snapshots):
    """Independently recompute every stored balance and adjacent instance count."""
    conservation = lineages.get("conservation", {})
    intervals = conservation.get("intervals", [])
    errors = []
    measured_count = lambda state: sum(ref.get("kind") == "action" for ref in state["references"]) if state.get("complete") is True else 0
    start = 1 if snapshots and snapshots[0].get("complete") is True else 0
    expected = snapshots[start:]
    if len(intervals) != len(expected):
        errors.append("interval_count_mismatch")
    totals = Counter()
    for index, (row, state) in enumerate(zip(intervals, expected), start=start):
        before = snapshots[index - 1] if index else None
        if row.get("commit") != state["sha"] or row.get("previous_commit") != (before["sha"] if before else None):
            errors.append("interval_topology_mismatch:" + state["sha"])
        old_count, new_count = measured_count(before) if before else 0, measured_count(state)
        values = {key: row.get(key, 0) for key in COUNT_KEYS}
        if any(type(value) is not int or value < 0 for value in values.values()):
            errors.append("invalid_instance_count:" + state["sha"])
            continue
        balance = old_count + values["known_entries"] + values["unknown_entries"] - new_count - values["known_exits"] - values["unknown_exits"]
        if (row.get("before_instances", 0) != old_count or row.get("after_instances", 0) != new_count
                or balance != 0 or row.get("balance") != balance or row.get("passed") is not True):
            errors.append("interval_conservation_mismatch:" + state["sha"])
        continued = old_count - values["known_exits"] - values["unknown_exits"]
        if continued < 0 or row.get("continued_instances", 0) != continued:
            errors.append("continued_instances_mismatch:" + state["sha"])
        if row.get("replacement_entries", 0) != row.get("replacement_exits", 0):
            errors.append("replacement_not_two_sided:" + state["sha"])
        totals.update(values)
    initial = measured_count(snapshots[0]) if snapshots else 0
    final = measured_count(snapshots[-1]) if snapshots else 0
    balance = initial + totals["known_entries"] + totals["unknown_entries"] - final - totals["known_exits"] - totals["unknown_exits"]
    if (conservation.get("initial_instances") != initial or conservation.get("final_instances") != final
            or any(conservation.get(key) != totals[key] for key in COUNT_KEYS)
            or balance != 0 or conservation.get("balance") != balance or conservation.get("passed") is not True):
        errors.append("global_conservation_mismatch")
    return dict(passed=not errors, errors=errors, initial_instances=initial, final_instances=final,
                balance=balance, **{key: totals[key] for key in COUNT_KEYS})


def audit_reference_fields(snapshots):
    missing, full_sha = [], 0
    for state in snapshots:
        for index, reference in enumerate(state["references"]):
            if reference.get("kind") != "action" or reference.get("ref_type") != "full_sha":
                continue
            full_sha += 1
            absent = [key for key in REQUIRED_REFERENCE_FIELDS if key not in reference]
            coverage = reference.get("configuration_coverage")
            if not isinstance(coverage, dict) or "status" not in coverage:
                absent.append("configuration_coverage.status")
            if reference.get("configuration_snapshot_sha") != state["sha"]:
                absent.append("configuration_snapshot_sha_matches_state")
            if state.get("configuration_complete") is not True and isinstance(coverage, dict) and coverage.get("status") != "unknown":
                absent.append("incomplete_configuration_must_be_unknown")
            if absent:
                missing.append(dict(commit=state["sha"], reference_index=index, missing=absent))
    return dict(passed=not missing, full_sha_observations=full_sha, missing_fields=missing)


def _tree_objects(directory, commit):
    output = git(directory, "ls-tree", "-r", "-z", commit, "--", ".github/workflows", *CONFIG_PATHS)
    workflow, configuration = [], []
    for item in output.split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        path = path.decode("utf-8")
        row = dict(path=path, mode=mode, kind=kind, blob=blob)
        if path.startswith(".github/workflows/") and path.count("/") == 2 and path.lower().endswith((".yml", ".yaml")):
            workflow.append(row)
        elif path in CONFIG_PATHS:
            configuration.append(row)
    return workflow, configuration


def _objects(entries):
    return sorted([{key: item[key] for key in ("path", "mode", "kind", "blob")} for item in entries], key=lambda row: row["path"])


def _source_state(root, detail):
    """Cache invalidation covers ordinary raw-cache and Git object-store changes."""
    blobs = {item["blob"] for state in detail["snapshots"]
             for item in state["files"] + state["configuration_files"]}
    return _source_state_of(root, detail["summary"]["git_directory"], blobs)


def _source_state_of(root, git_directory, blobs):
    paths = [(root / "data/cache/blobs" / blob) for blob in sorted(blobs)]
    objects = root / git_directory / "objects"
    if objects.exists():
        for directory in sorted(objects.iterdir()):
            if directory.is_dir() and (directory.name == "pack" or len(directory.name) == 2):
                paths.extend(sorted(path for path in directory.iterdir() if path.is_file()))
    records = []
    for path in paths:
        try:
            stat = path.stat()
            records.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
        except OSError:
            records.append((path.relative_to(root).as_posix(), "unavailable"))
    return _hash(records)


STREAM_PREFIXES = {"summary", "snapshots.item", "implementation_sha256", "lineages.repository", "lineages.rules_version",
                   "lineages.references.item", "lineages.events.item", "lineages.conservation", "lineages.quality"}
_MISSING = object()
_ACTION = {"kind": "action"}


def _stored_items(path, prefixes, key_prefixes):
    """measurement_io.sections plus ``(prefix + '.#key', name)`` for every key of the objects at ``key_prefixes``.

    The key events let the streaming check see keys it was not told to expect, which a prefix filter would drop silently.
    """
    prefixes, builder, selected, depth = set(prefixes), None, None, 0
    with Path(path).open("rb") as stream:
        for prefix, event, value in ijson.parse(stream):
            if builder is None:
                if event == "map_key" and prefix in key_prefixes:
                    yield prefix + ".#key", value
                    continue
                if prefix not in prefixes or event in {"map_key", "end_map", "end_array"}:
                    continue
                builder, selected = ObjectBuilder(), prefix
            builder.event(event, float(value) if isinstance(value, Decimal) else value)
            if event in {"start_map", "start_array"}:
                depth += 1
            elif event in {"end_map", "end_array"}:
                depth -= 1
            if depth == 0:
                yield selected, builder.value
                builder = None


class _Stored:
    """One pass over a stored measurement file shared by the snapshot replay and the later lineage comparison."""

    def __init__(self, path):
        self._items = _stored_items(path, STREAM_PREFIXES, {"lineages"})
        self.pending = None

    def next(self):
        if self.pending is not None:
            item, self.pending = self.pending, None
            return item
        return next(self._items, None)

    def close(self):
        self._items.close()


class _Lazy:
    """The part of a list that lineage.reconstruct uses (iteration, len and the last element), fed from a generator."""

    def __init__(self, source):
        self.source, self.last, self.count = source, None, 0

    def __iter__(self):
        for state in self.source:
            self.last = state
            self.count += 1
            yield state

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index == -1:
            return self.last
        raise IndexError(index)


class _Facts:
    """What the whole-file checks read from every stored snapshot, kept as small values."""

    def __init__(self):
        self.shas, self.stamps, self.complete, self.configuration_complete = [], [], [], []
        self.inheritance_unknown, self.actions, self.objects = [], [], set()
        self.tree_mismatches, self.mismatches, self.reference_missing, self.full_sha_observations = [], [], [], 0

    def observe(self, state, directory):
        self.shas.append(state["sha"])
        self.stamps.append(state["timestamp"])
        self.complete.append(state.get("complete"))
        self.configuration_complete.append(state.get("configuration_complete"))
        self.inheritance_unknown.append(state["configuration"].get("inheritance_unknown") is True)
        self.actions.append(sum(ref.get("kind") == "action" for ref in state["references"]) if state.get("complete") is True else 0)
        workflow, configuration = _tree_objects(directory, state["sha"])
        if _objects(workflow) != _objects(state["files"]):
            self.tree_mismatches.append(dict(commit=state["sha"], field="workflow_tree"))
        if _objects(configuration) != _objects(state["configuration_files"]):
            self.tree_mismatches.append(dict(commit=state["sha"], field="configuration_tree"))
        fields = audit_reference_fields([state])
        self.reference_missing.extend(fields["missing_fields"])
        self.full_sha_observations += fields["full_sha_observations"]
        self.objects.update((item["blob"], item["mode"], item["kind"]) for item in state["files"] + state["configuration_files"])


def _replayed_states(stored, summary, directory, client, parsed, facts):
    """Re-read every stored snapshot from Git and the blob cache, compare it, and hand the re-read state to the lineage rebuild."""
    previous = None
    while True:
        item = stored.next()
        if item is None:
            return
        prefix, expected = item
        if prefix != "snapshots.item":
            stored.pending = item
            return
        facts.observe(expected, directory)
        try:
            current = read_state(client, directory, summary["canonical_repository"], expected, summary["observed_default_branch"], parsed)
            if previous is not None:
                current["renames"], current["rename_evidence"] = workflow_renames(directory, previous, current)
            else:
                current.update(renames={}, rename_evidence=[])
        except Exception as error:
            facts.mismatches.append(dict(commit=expected["sha"], field="source_tree_or_rename_read",
                                         error=type(error).__name__ + ": " + str(error)))
            continue
        for key in ("references", "complete", "configuration_complete", "configuration", "renames", "rename_evidence"):
            if current[key] != expected[key]:
                facts.mismatches.append(dict(commit=expected["sha"], field=key))
        previous = current
        yield current


def _verify_streaming(root, path, expected_record, implementation, config, row, output):
    """The checks of verify_one for a file too large to hold as a dictionary: one stored snapshot in memory at a time.

    Only the lineage rebuilt from the re-read states is held completely. The stored lineage is compared with it item by item,
    so a stored copy never exists. The cache is not consulted: a file of this size is checked once per run.
    """
    stored = _Stored(path)
    try:
        first = stored.next()
        if first is None or first[0] != "summary":
            raise KeyError("summary")
        summary = first[1]
        signature = dict(measurement_sha256=digest(path), implementation_sha256=implementation,
                         verifier_sha256=digest(Path(__file__)), expected_cohort_record_sha256=expected_record["cohort_record_sha256"])
    except Exception as error:
        stored.close()
        row.update(passed=False, errors=[type(error).__name__ + ": " + str(error)])
        save(output, row)
        return row
    try:
        facts, client, parsed = _Facts(), _CacheOnly(root), {}
        directory = root / expected_record["git_directory"]
        lazy = _Lazy(_replayed_states(stored, summary, directory, client, parsed, facts))
        rebuilt = reconstruct(summary["canonical_repository"], lazy)
        stored_implementation, stored_keys, counts = None, set(), dict(references=0, events=0)
        same = dict(references=True, events=True)
        seen = dict(repository=_MISSING, rules_version=_MISSING, conservation=_MISSING, quality=_MISSING)
        ambiguous_commits, unknown_boundary_events = set(), 0
        while True:
            item = stored.next()
            if item is None:
                break
            prefix, value = item
            if prefix == "snapshots.item":
                raise ValueError("Unexpected measurement section order")
            if prefix == "implementation_sha256":
                stored_implementation = value
            elif prefix == "lineages.#key":
                stored_keys.add(value)
            elif prefix in ("lineages.references.item", "lineages.events.item"):
                name = prefix.split(".")[1]
                rows = rebuilt[name]
                same[name] = same[name] and counts[name] < len(rows) and rows[counts[name]] == value
                counts[name] += 1
                if name == "events":
                    ambiguous_commits.update([value["commit"]] if value["event"] == "ambiguous_transition" else [])
                    unknown_boundary_events += value["event"] == "unknown_boundary"
            else:
                seen[prefix.split(".")[1]] = value
        if not stored_keys:
            raise KeyError("lineages")
        lineage_equal = (stored_keys == set(rebuilt) and same["references"] and same["events"]
                         and counts["references"] == len(rebuilt["references"]) and counts["events"] == len(rebuilt["events"])
                         and all(seen[name] is not _MISSING and seen[name] == rebuilt[name] for name in seen))
        stored_conservation = {} if seen["conservation"] is _MISSING else seen["conservation"]
        row["checks"]["implementation_hashes_equal"] = stored_implementation == implementation
        identity_fields = ("repository_id", "baseline_commit", "observed_head", "git_directory", "cohort_record_sha256")
        row["checks"]["frozen_cohort_input_equal"] = all(summary.get(key) == expected_record.get(key) for key in identity_fields)
        row["checks"]["alias_baselines_consistent"] = not expected_record.get("alias_baseline_conflict", False)
        history = fixed_history(directory, expected_record["observed_head"])
        workflows = modification_commits(directory, expected_record["observed_head"], (".github/workflows",))
        configurations = modification_commits(directory, expected_record["observed_head"], CONFIG_PATHS)
        expected_points, timeline = select_points(history, workflows, configurations, config, expected_record["baseline_commit"])
        row["checks"]["snapshot_topology_and_scope_equal"] = [point["sha"] for point in expected_points] == facts.shas
        expected_clocks = {point["sha"]: point["timestamp"] for point in expected_points}
        row["checks"]["snapshot_clocks_equal"] = all(expected_clocks.get(sha) == stamp for sha, stamp in zip(facts.shas, facts.stamps))
        row["configuration_only_commits"] = timeline["configuration_only_commits"]
        row["missing_configuration_commits"] = sorted(set(timeline["required_configuration_commits"]) - set(facts.shas))
        row["checks"]["all_configuration_changes_present"] = not row["missing_configuration_commits"]
        row["tree_mismatches"] = facts.tree_mismatches
        row["checks"]["tree_file_objects_equal"] = not facts.tree_mismatches
        if expected_record.get("baseline_tree"):
            actual_tree = git(directory, "rev-parse", expected_record["baseline_commit"] + "^{tree}").decode().strip()
            row["checks"]["cohort_baseline_tree_equal"] = actual_tree == expected_record["baseline_tree"]
        light = [dict(sha=sha, complete=complete, references=[_ACTION] * count)
                 for sha, complete, count in zip(facts.shas, facts.complete, facts.actions)]
        row["conservation"] = audit_conservation(dict(conservation=stored_conservation), light)
        row["reference_fields"] = dict(passed=not facts.reference_missing, full_sha_observations=facts.full_sha_observations,
                                       missing_fields=facts.reference_missing)
        required = {blob for blob, mode, kind in facts.objects if kind == "blob" and mode in ("100644", "100755")}
        verified = set()
        for blob in required:
            try:
                content = client.blob(None, None, None, blob)
                if hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest() == blob:
                    verified.add(blob)
            except OSError:
                pass
        row["replay"] = dict(required_blob_objects=len(required), verified_blob_objects=len(verified),
                             missing_or_mismatched_blobs=sorted(required - verified), source_states_checked=lazy.count,
                             state_mismatches=facts.mismatches, lineage_replay_equal=lineage_equal,
                             passed=required == verified and not facts.mismatches and lineage_equal,
                             semantics="Offline implementation replay consistency; not behavioral classification accuracy")
        row["checks"].update(conservation=row["conservation"]["passed"], reference_fields=row["reference_fields"]["passed"],
                               raw_source_and_lineage_replay=row["replay"]["passed"])
        complete_pairs = sum(a is True and b is True for a, b in zip(facts.complete, facts.complete[1:]))
        row["quality"] = dict(snapshots=len(facts.shas), workflow_unknown_snapshots=sum(value is not True for value in facts.complete),
                              configuration_unknown_snapshots=sum(value is not True for value in facts.configuration_complete),
                              inheritance_unknown_snapshots=sum(facts.inheritance_unknown),
                              complete_adjacent_intervals=complete_pairs, ambiguous_intervals=len(ambiguous_commits),
                              unknown_boundary_events=unknown_boundary_events,
                              **{key: row["conservation"][key] for key in ("unknown_entries", "unknown_exits")})
        signature["source_object_state_sha256"] = _source_state_of(root, summary["git_directory"], {blob for blob, _, _ in facts.objects})
    except Exception as error:
        row["errors"].append(type(error).__name__ + ": " + str(error))
    finally:
        stored.close()
    row["passed"] = bool(row["checks"]) and all(row["checks"].values()) and not row["errors"]
    row["cache_signature"] = signature
    save(output, row)
    return row


def verify_one(root, path, expected_record, implementation, config):
    identity = expected_record["repository_id"]
    output = root / "data/derived" / VERIFICATION_DIRECTORY / (identity + ".json")
    row = dict(repository_id=identity, repository=expected_record["canonical_repository"],
               rules_version=RULES_VERSION, checked_at_utc=datetime.now(timezone.utc).isoformat(),
               measurement_path=path.relative_to(root).as_posix(), cache_used=False, checks={}, errors=[])
    if path.stat().st_size > STREAM_VERIFY_BYTES:
        return _verify_streaming(root, path, expected_record, implementation, config, row, output)
    try:
        detail = load_detail(path)
        signature = dict(measurement_sha256=digest(path), implementation_sha256=implementation,
                         verifier_sha256=digest(Path(__file__)), expected_cohort_record_sha256=expected_record["cohort_record_sha256"],
                         source_object_state_sha256=_source_state(root, detail))
    except Exception as error:
        row.update(passed=False, errors=[type(error).__name__ + ": " + str(error)])
        save(output, row)
        return row
    if output.exists():
        cached = json.loads(output.read_text(encoding="utf-8"))
        if cached.get("cache_signature") == signature and cached.get("passed") is True:
            return dict(cached, cache_used=True)
    try:
        summary, states, lineages = detail["summary"], detail["snapshots"], detail["lineages"]
        row["checks"]["implementation_hashes_equal"] = detail.get("implementation_sha256") == implementation
        identity_fields = ("repository_id", "baseline_commit", "observed_head", "git_directory", "cohort_record_sha256")
        row["checks"]["frozen_cohort_input_equal"] = all(summary.get(key) == expected_record.get(key) for key in identity_fields)
        row["checks"]["alias_baselines_consistent"] = not expected_record.get("alias_baseline_conflict", False)
        directory = root / expected_record["git_directory"]
        history = fixed_history(directory, expected_record["observed_head"])
        workflows = modification_commits(directory, expected_record["observed_head"], (".github/workflows",))
        configurations = modification_commits(directory, expected_record["observed_head"], CONFIG_PATHS)
        expected_points, timeline = select_points(history, workflows, configurations, config, expected_record["baseline_commit"])
        row["checks"]["snapshot_topology_and_scope_equal"] = [point["sha"] for point in expected_points] == [state["sha"] for state in states]
        expected_clocks = {point["sha"]: point["timestamp"] for point in expected_points}
        row["checks"]["snapshot_clocks_equal"] = all(expected_clocks.get(state["sha"]) == state["timestamp"] for state in states)
        state_ids = {state["sha"] for state in states}
        row["configuration_only_commits"] = timeline["configuration_only_commits"]
        row["missing_configuration_commits"] = sorted(set(timeline["required_configuration_commits"]) - state_ids)
        row["checks"]["all_configuration_changes_present"] = not row["missing_configuration_commits"]
        tree_mismatches = []
        for state in states:
            workflow, configuration = _tree_objects(directory, state["sha"])
            if _objects(workflow) != _objects(state["files"]):
                tree_mismatches.append(dict(commit=state["sha"], field="workflow_tree"))
            if _objects(configuration) != _objects(state["configuration_files"]):
                tree_mismatches.append(dict(commit=state["sha"], field="configuration_tree"))
        row["tree_mismatches"] = tree_mismatches
        row["checks"]["tree_file_objects_equal"] = not tree_mismatches
        if expected_record.get("baseline_tree"):
            actual_tree = git(directory, "rev-parse", expected_record["baseline_commit"] + "^{tree}").decode().strip()
            row["checks"]["cohort_baseline_tree_equal"] = actual_tree == expected_record["baseline_tree"]
        row["conservation"] = audit_conservation(lineages, states)
        row["reference_fields"] = audit_reference_fields(states)
        row["replay"] = replay_detail(detail, root)
        row["checks"].update(conservation=row["conservation"]["passed"], reference_fields=row["reference_fields"]["passed"],
                               raw_source_and_lineage_replay=row["replay"]["passed"])
        events = lineages["events"]
        complete_pairs = sum(a.get("complete") is True and b.get("complete") is True for a, b in zip(states, states[1:]))
        ambiguous_commits = {event["commit"] for event in events if event["event"] == "ambiguous_transition"}
        row["quality"] = dict(snapshots=len(states), workflow_unknown_snapshots=sum(state.get("complete") is not True for state in states),
                              configuration_unknown_snapshots=sum(state.get("configuration_complete") is not True for state in states),
                              inheritance_unknown_snapshots=sum(state["configuration"].get("inheritance_unknown") is True for state in states),
                              complete_adjacent_intervals=complete_pairs, ambiguous_intervals=len(ambiguous_commits),
                              unknown_boundary_events=sum(event["event"] == "unknown_boundary" for event in events),
                              **{key: row["conservation"][key] for key in ("unknown_entries", "unknown_exits")})
    except Exception as error:
        row["errors"].append(type(error).__name__ + ": " + str(error))
    try:
        signature["source_object_state_sha256"] = _source_state(root, detail)
    except Exception as error:
        row["errors"].append(type(error).__name__ + ": " + str(error))
    row["passed"] = bool(row["checks"]) and all(row["checks"].values()) and not row["errors"]
    row["cache_signature"] = signature
    save(output, row)
    return row


def load_baseline_verification(root):
    path = root / "reports/cohort_verification.json"
    try:
        detail = json.loads(path.read_text(encoding="utf-8"))
        manifest_path = root / "data/derived/cohort-v1/verified_baseline_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        baseline_files = {item.relative_to(root).as_posix() for item in
                          (root / "data/derived/cohort-v1/baselines").glob("*.json")}
        manifest_mismatches = [name for name, expected in manifest.items()
                               if not (root / name).exists() or digest(root / name) != expected]
        manifest_equal = (set(manifest) == baseline_files and len(manifest) == detail["completed_names"]
                          and not manifest_mismatches)
        sources = detail["source_sha256"]
        source_mismatches = [name for name, expected in sources.items()
                             if not (root / ("pintrace/verify_cohort.py" if name == "implementation" else name)).exists()
                             or digest(root / ("pintrace/verify_cohort.py" if name == "implementation" else name)) != expected]
        tie_path = root / "reports/tie_resolution_summary.json"
        ties = json.loads(tie_path.read_text(encoding="utf-8"))
        tie_sources_equal = (ties["total"] == 6 and ties["resolved"] + ties["tie_unresolved"] == 6
                             and digest(root / "data/derived/cohort-v1/tie_source_records.json") == ties["source_records_sha256"]
                             and digest(root / "pintrace/ties.py") == ties["implementation_sha256"])
        return dict(report_path=path.relative_to(root).as_posix(), report_sha256=digest(path),
                    baseline_manifest_sha256=digest(manifest_path), tie_summary_sha256=digest(tie_path),
                    manifest_input_hashes_equal=manifest_equal, manifest_mismatches=manifest_mismatches,
                    source_mismatches=source_mismatches, tie_source_hashes_equal=tie_sources_equal,
                    baseline_freeze_ready=(detail.get("baseline_freeze_ready") is True and manifest_equal
                                           and bool(sources) and not source_mismatches and tie_sources_equal))
    except Exception as error:
        return dict(report_path=path.relative_to(root).as_posix(), baseline_freeze_ready=False,
                    error=type(error).__name__ + ": " + str(error))


def make_report(scope, checks, expected_names=580, input_errors=(), baseline_verification=None):
    expected = set(scope["confirmed_repository_ids"])
    passed = {row["repository_id"] for row in checks if row["passed"]}
    totals = Counter()
    for row in checks:
        totals.update(row.get("quality", {}))
    snapshots, adjacent = totals["snapshots"], totals["complete_adjacent_intervals"]
    failures = [dict(repository_id=row["repository_id"], checks=row.get("checks"), errors=row.get("errors"))
                for row in checks if not row["passed"]]
    baseline_verification = baseline_verification or dict(baseline_freeze_ready=False, error="Baseline verification report unavailable")
    return dict(rules_version=RULES_VERSION, batch_directory=BATCH_DIRECTORY, checked_at_utc=datetime.now(timezone.utc).isoformat(),
                scope=scope, verified_ids=sorted(passed), pending_verification_ids=sorted(expected - passed),
                baseline_verification=baseline_verification,
                failed_checks=failures, input_errors=list(input_errors), quality=dict(totals),
                workflow_unknown_snapshot_rate=totals["workflow_unknown_snapshots"] / snapshots if snapshots else None,
                configuration_unknown_snapshot_rate=totals["configuration_unknown_snapshots"] / snapshots if snapshots else None,
                ambiguous_transition_rate=totals["ambiguous_intervals"] / adjacent if adjacent else None,
                gate_passed=(scope["passed"] and scope["candidate_names"] == expected_names
                             and passed == expected and len(checks) == len(expected) and not input_errors
                             and baseline_verification.get("baseline_freeze_ready") is True),
                interpretation="Reproducible source/implementation consistency; unknown states remain unknown, not classification accuracy")


def write_report(root, report, checks):
    report["repository_checks"] = checks
    save(root / "reports/full_replay_report.json", report)
    status = "通过" if report["gate_passed"] else "尚未通过"
    scope = report["scope"]
    lines = ["# PinTrace 全量测量与重放验收", "", f"冻结门槛：**{status}**。", "",
             f"全候选基线验收：{'通过' if report['baseline_verification']['baseline_freeze_ready'] else '尚未通过'}。", "",
             f"候选名称：{scope['candidate_names']}；确认仓库 ID：{len(scope['confirmed_repository_ids'])}；"
             f"成功测量 ID：{len(scope['measured_repository_ids'])}；核验通过 ID：{len(report['verified_ids'])}。", "",
             f"待完成候选：{len(scope['missing_or_pending_candidate_names'])}；待测量 ID：{len(scope['pending_measurement_ids'])}；"
             f"待核验 ID：{len(report['pending_verification_ids'])}；多余测量 ID：{len(scope['unexpected_measurement_ids'])}。", "",
             "| 核查项目 | 数值 |", "| --- | --- |"]
    for key, label in (("snapshots", "快照"), ("workflow_unknown_snapshots", "工作流未知快照"),
                       ("configuration_unknown_snapshots", "配置未知快照"), ("ambiguous_intervals", "歧义转移区间"),
                       ("unknown_entries", "未知边界转入实例"), ("unknown_exits", "未知边界转出实例")):
        lines.append(f"| {label} | {report['quality'].get(key, 0)} |")
    lines += ["", f"详细待处理 ID、逐仓库检查、输入哈希及错误见同名 JSON 和 `data/derived/{VERIFICATION_DIRECTORY}/`。", "",
              "该验收检查对象身份、范围覆盖、实现重放和计数守恒。无效 YAML 与无法唯一对应的谱系仍保留未知状态；通过不表示行为分类准确率为 100%。", ""]
    (root / "reports/full_replay_report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("workers must be 1..8")
    root = Path(__file__).resolve().parents[1]
    with (root / "reports/candidate_source_membership.csv").open(encoding="utf-8", newline="") as stream:
        candidates = [row["repository"] for row in csv.DictReader(stream)]
    with (root / "reports/cohort_flow.csv").open(encoding="utf-8", newline="") as stream:
        flow = list(csv.DictReader(stream))
    cohort = [json.loads(path.read_text(encoding="utf-8"))["summary"]
              for path in sorted((root / "data/derived/cohort-v1/baselines").glob("*.json"))]
    paths = sorted(path for path in (root / "data/derived" / BATCH_DIRECTORY).glob("*.json") if path.stem.isdigit())
    measurements, errors = [], []
    for path in paths:
        try:
            record = read_summary(path)
            if record["repository_id"] != path.stem:
                errors.append("measurement_filename_identity_mismatch:" + path.name)
            measurements.append(record)
        except Exception as error:
            errors.append(path.name + ": " + type(error).__name__ + ": " + str(error))
    scope = audit_scope(candidates, flow, cohort, measurements)
    expected = {row["repository_id"]: row for row in confirmed_records(root)}
    implementation = implementation_hashes(root)
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    baseline = load_baseline_verification(root)
    input_paths = [root / "reports/candidate_source_membership.csv", root / "reports/cohort_flow.csv", *paths,
                   *sorted((root / "data/derived/cohort-v1/baselines").glob("*.json"))]
    for name in ("reports/cohort_verification.json", "data/derived/cohort-v1/verified_baseline_manifest.json",
                 "reports/tie_resolution_summary.json", "data/derived/cohort-v1/tie_source_records.json",
                 "pintrace/verify_cohort.py", "pintrace/ties.py"):
        if (root / name).exists():
            input_paths.append(root / name)
    input_hashes = {path.relative_to(root).as_posix(): digest(path) for path in input_paths}
    jobs = [path for path in paths if path.stem in expected]
    ordinary, heavy = split_by_weight(jobs)
    checks = []
    write_report(root, make_report(scope, checks, input_errors=errors, baseline_verification=baseline), checks)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(verify_one, root, path, expected[path.stem], implementation, config) for path in ordinary]
        for future in as_completed(futures):
            row = future.result()
            checks.append(row)
            print(f"{len(checks)}/{len(jobs)} {row['repository']} {'passed' if row['passed'] else 'failed'}", flush=True)
    for path in heavy:
        row = verify_one(root, path, expected[path.stem], implementation, config)
        checks.append(row)
        print(f"{len(checks)}/{len(jobs)} {row['repository']} {'passed' if row['passed'] else 'failed'} (checked alone)", flush=True)
    if implementation_hashes(root) != implementation:
        errors.append("frozen_implementation_changed_during_verification")
    for name, expected_hash in input_hashes.items():
        if not (root / name).exists() or digest(root / name) != expected_hash:
            errors.append("input_changed_during_verification:" + name)
    report = make_report(scope, checks, input_errors=errors, baseline_verification=baseline)
    report["input_sha256"] = input_hashes
    report["implementation_sha256"] = implementation
    write_report(root, report, checks)
    print(json.dumps(dict(gate_passed=report["gate_passed"],
                          pending_verification_ids=len(report["pending_verification_ids"]),
                          quality=report["quality"]), indent=2))


if __name__ == "__main__":
    main()
