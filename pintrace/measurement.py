"""Collect fixed-HEAD workflow/configuration timelines and replayable lineages."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tomllib

from .configuration import CONFIG_PATHS, RULE_VERSION as CONFIG_RULES_VERSION
from .configuration import coverage_for, enrich_workflow, parse_configuration
from .github import GitHub, github_token
from .history import git, workflow_files
from .lineage import RULES_VERSION as LINEAGE_RULES_VERSION, reconstruct
from .pilot import iso, timestamp
from .workflow import action_counts


RULES_VERSION = "measurement-v4"
BATCH_DIRECTORY = "measurement-v4"
SOURCE_NAMES = ("measurement.py", "lineage.py", "configuration.py", "workflow.py", "history.py", "github.py", "pilot.py")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def fixed_history(directory, head):
    """Read exactly the cohort's observed commit, never the moving HEAD ref."""
    return [dict(sha=sha, timestamp=int(clock)) for clock, sha in
            (line.split() for line in git(directory, "rev-list", "--first-parent", "--timestamp", head).decode().splitlines())]


def modification_commits(directory, head, paths):
    output = git(directory, "log", "--first-parent", "--full-history", "--diff-merges=first-parent",
                 "--no-patch", "--format=%H", head, "--", *paths).decode()
    return set(output.splitlines())


def select_points(history, workflow_changes, configuration_changes, config, baseline_sha):
    """Use topology for sequence and retain clock-inverted intermediate changes."""
    start, end = timestamp(config["window_start"]), timestamp(config["window_end"])
    endpoint = next((point for point in history if point["timestamp"] <= end), None)
    before_start = next((point for point in history if point["timestamp"] < start), None)
    by_sha = {point["sha"]: index for index, point in enumerate(history)}
    if baseline_sha not in by_sha or endpoint is None:
        raise ValueError("Cohort baseline or endpoint absent from fixed first-parent ancestry")
    indices = [by_sha[baseline_sha], by_sha[endpoint["sha"]],
               by_sha[before_start["sha"]] if before_start else len(history) - 1]
    low, high = min(indices), max(indices)
    in_range = {point["sha"] for point in history[low:high + 1]}
    required_config = configuration_changes & in_range
    wanted = ((workflow_changes | configuration_changes) & in_range) | {history[i]["sha"] for i in indices}
    points = [point for point in reversed(history) if point["sha"] in wanted]
    audit = dict(endpoint_commit=endpoint["sha"],
                 pre_start_commit=before_start["sha"] if before_start else None,
                 baseline_commit=baseline_sha, topology_bounds=[history[high]["sha"], history[low]["sha"]],
                 required_configuration_commits=sorted(required_config),
                 configuration_only_commits=sorted(required_config - workflow_changes),
                 included_clock_outside_window=[point["sha"] for point in points
                                                if point["sha"] not in {history[i]["sha"] for i in indices}
                                                and not start <= point["timestamp"] <= end],
                 source_clock_inversions=sum(a["timestamp"] < b["timestamp"] for a, b in zip(history, history[1:])),
                 historical_default_branch_unknown=True)
    return points, audit


def configuration_files(directory, sha):
    output = git(directory, "ls-tree", "-r", "-z", sha, "--", *CONFIG_PATHS)
    entries = []
    for item in output.split(b"\0"):
        if item:
            metadata, path = item.split(b"\t", 1)
            mode, kind, blob = metadata.decode().split()
            path = path.decode("utf-8")
            if path in CONFIG_PATHS:
                entries.append(dict(path=path, mode=mode, kind=kind, blob=blob))
    return entries


def _content(client, repository, point, item):
    if item["kind"] != "blob" or item["mode"] not in ("100644", "100755"):
        raise ValueError("unsupported_file_mode")
    content = client.blob(repository, point["sha"], item["path"], item["blob"])
    # Keep the hash check even for injected/offline clients used by replays.
    actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    if actual != item["blob"]:
        raise ValueError("Git blob identity mismatch")
    return content.decode("utf-8-sig")


def read_state(client, directory, repository, point, branch, parsed_cache):
    references, errors, files = [], [], workflow_files(directory, point["sha"])
    for source in files:
        key = (source["path"], source["blob"])
        try:
            if key not in parsed_cache:
                parsed_cache[key] = enrich_workflow(_content(client, repository, point, source), source["path"])
            parsed = parsed_cache[key]
            source["inactive_empty_document"] = parsed.get("inactive_empty_document", False)
            source["cache_blob_path"] = "data/cache/blobs/" + source["blob"]
            if parsed["errors"]:
                errors.append(dict(path=source["path"], errors=parsed["errors"]))
            references.extend(dict(ref) for ref in parsed["references"])
        except Exception as error:
            errors.append(dict(path=source["path"], errors=[type(error).__name__ + ": " + str(error)]))

    texts, configuration_errors = {}, []
    config_files = configuration_files(directory, point["sha"])
    for source in config_files:
        try:
            texts[source["path"]] = _content(client, repository, point, source)
            source["cache_blob_path"] = "data/cache/blobs/" + source["blob"]
        except Exception as error:
            configuration_errors.append(dict(path=source["path"], errors=[type(error).__name__ + ": " + str(error)]))
    configuration = parse_configuration(texts, branch)
    configuration_errors.extend(configuration["errors"])
    configuration_complete = not configuration_errors
    for reference in references:
        if configuration_complete:
            coverage = coverage_for(reference, configuration, branch)
        else:
            coverage = dict(status="unknown", reasons=["configuration_tree_incomplete"],
                            inheritance_unknown=True, global_configuration_unknown=True,
                            scope="local_configuration_intent", by_tool={})
        reference.update(configuration_coverage=coverage,
                         configuration_complete=configuration_complete,
                         configuration_snapshot_sha=point["sha"],
                         configuration_source_branch=branch,
                         historical_default_branch_unknown=True)
    return dict(point, date=iso(point["timestamp"]), references=references, files=files,
                errors=errors, complete=not errors, configuration=configuration,
                configuration_files=config_files, configuration_errors=configuration_errors,
                configuration_complete=configuration_complete, counts=action_counts(references))


def workflow_renames(directory, before, after):
    """Unique identical blobs first; Git's explicit rename diff second."""
    old = {item["path"]: item for item in before["files"]}
    new = {item["path"]: item for item in after["files"]}
    removed, added = set(old) - set(new), set(new) - set(old)
    old_hashes, new_hashes = defaultdict(list), defaultdict(list)
    for path in sorted(removed):
        old_hashes[old[path]["blob"]].append(path)
    for path in sorted(added):
        new_hashes[new[path]["blob"]].append(path)
    renames, evidence = {}, []
    for blob in sorted(old_hashes.keys() & new_hashes.keys()):
        if len(old_hashes[blob]) == len(new_hashes[blob]) == 1:
            a, b = old_hashes[blob][0], new_hashes[blob][0]
            renames[a] = b
            evidence.append(dict(old_path=a, new_path=b, method="unique_identical_blob", blob=blob))
    if removed - renames.keys() and added - set(renames.values()):
        output = git(directory, "diff", "--name-status", "-z", "--find-renames=50%",
                     before["sha"], after["sha"], "--", ".github/workflows").split(b"\0")
        index = 0
        while index < len(output) and output[index]:
            status = output[index].decode()
            index += 1
            if status.startswith(("R", "C")):
                a, b = output[index].decode("utf-8"), output[index + 1].decode("utf-8")
                index += 2
                identical_blob_tie = (a in old and b in new and old[a]["blob"] == new[b]["blob"]
                                      and (len(old_hashes[old[a]["blob"]]) > 1 or len(new_hashes[new[b]["blob"]]) > 1))
                if (status.startswith("R") and a in removed and b in added and not identical_blob_tie
                        and a not in renames and b not in renames.values()):
                    renames[a] = b
                    evidence.append(dict(old_path=a, new_path=b, method="git_rename_diff",
                                         similarity=int(status[1:]), old_blob=old[a]["blob"], new_blob=new[b]["blob"]))
            else:
                index += 1
    return renames, evidence


class _CacheOnly:
    def __init__(self, root):
        self.root = root

    def blob(self, repository, commit, path, blob_sha):
        return (self.root / "data/cache/blobs" / blob_sha).read_bytes()


def replay_detail(detail, root):
    """Reparse all recorded source objects offline and reconstruct the same events."""
    record = detail["summary"]
    client, parsed = _CacheOnly(root), {}
    directory = root / record["git_directory"]
    reconstructed, mismatches = [], []
    objects = {(item["blob"], item["mode"], item["kind"]) for state in detail["snapshots"]
               for item in state["files"] + state["configuration_files"]}
    required = {blob for blob, mode, kind in objects if kind == "blob" and mode in ("100644", "100755")}
    verified = set()
    for blob in required:
        try:
            content = client.blob(None, None, None, blob)
            if hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest() == blob:
                verified.add(blob)
        except OSError:
            pass
    for expected in detail["snapshots"]:
        try:
            current = read_state(client, directory, record["canonical_repository"], expected,
                                 record["observed_default_branch"], parsed)
            if reconstructed:
                current["renames"], current["rename_evidence"] = workflow_renames(directory, reconstructed[-1], current)
            else:
                current.update(renames={}, rename_evidence=[])
        except Exception as error:
            mismatches.append(dict(commit=expected["sha"], field="source_tree_or_rename_read",
                                   error=type(error).__name__ + ": " + str(error)))
            continue
        for key in ("references", "complete", "configuration_complete", "configuration", "renames", "rename_evidence"):
            if current[key] != expected[key]:
                mismatches.append(dict(commit=expected["sha"], field=key))
        reconstructed.append(current)
    lineages = reconstruct(record["canonical_repository"], reconstructed)
    lineage_equal = lineages == detail["lineages"]
    return dict(required_blob_objects=len(required), verified_blob_objects=len(verified),
                missing_or_mismatched_blobs=sorted(required - verified),
                source_states_checked=len(reconstructed), state_mismatches=mismatches,
                lineage_replay_equal=lineage_equal,
                passed=required == verified and not mismatches and lineage_equal,
                semantics="Offline implementation replay consistency; not behavioral classification accuracy")


def collect(record, root, config, token, retry=False, implementation_hashes=None):
    output = root / "data/derived" / BATCH_DIRECTORY / (record["repository_id"] + ".json")
    if output.exists():
        saved = json.loads(output.read_text(encoding="utf-8"))
        same_implementation = saved.get("implementation_sha256") == implementation_hashes
        if same_implementation and (saved["summary"]["status"] == "measured" or not retry):
            return saved["summary"]
    summary = dict(record, status="measurement_failed", measurement_rules_version=RULES_VERSION,
                   lineage_rules_version=LINEAGE_RULES_VERSION, configuration_rules_version=CONFIG_RULES_VERSION,
                   measurement_checked_at_utc=datetime.now(timezone.utc).isoformat())
    detail = dict(summary=summary, snapshots=[], implementation_sha256=implementation_hashes)
    try:
        if record.get("alias_baseline_conflict"):
            raise ValueError("Stable repository aliases have conflicting frozen baselines; resolve before measurement")
        directory = root / record["git_directory"]
        if git(directory, "rev-parse", "--is-shallow-repository").strip() != b"false":
            raise ValueError("Shallow history cannot support longitudinal measurement")
        history = fixed_history(directory, record["observed_head"])
        workflows = modification_commits(directory, record["observed_head"], (".github/workflows",))
        configurations = modification_commits(directory, record["observed_head"], CONFIG_PATHS)
        points, timeline = select_points(history, workflows, configurations, config, record["baseline_commit"])
        parsed, client = {}, GitHub(root / "data/cache", token)
        for index, point in enumerate(points):
            state = read_state(client, directory, record["canonical_repository"], point,
                               record["observed_default_branch"], parsed)
            if detail["snapshots"]:
                state["renames"], state["rename_evidence"] = workflow_renames(directory, detail["snapshots"][-1], state)
            else:
                state.update(renames={}, rename_evidence=[])
            detail["snapshots"].append(state)
            if (index + 1) % 100 == 0:
                print(f"{record['repository']} measurement snapshots {index + 1}/{len(points)}", flush=True)
        selected = {state["sha"] for state in detail["snapshots"]}
        timeline["missing_configuration_commits"] = sorted(set(timeline["required_configuration_commits"]) - selected)
        timeline["configuration_commit_coverage_passed"] = not timeline["missing_configuration_commits"]
        # The independent second Git path-log query detects discovery omissions.
        rediscovered = modification_commits(directory, record["observed_head"], CONFIG_PATHS)
        timeline["configuration_path_log_repeat_equal"] = configurations == rediscovered
        detail["timeline"] = timeline
        detail["lineages"] = reconstruct(record["canonical_repository"], detail["snapshots"])
        detail["replay"] = replay_detail(detail, root)
        summary.update(status="measured" if detail["replay"]["passed"] else "measurement_incomplete",
                       snapshots=len(points), workflow_incomplete_snapshots=sum(not state["complete"] for state in detail["snapshots"]),
                       configuration_incomplete_snapshots=sum(not state["configuration_complete"] for state in detail["snapshots"]),
                       configuration_only_commits=len(timeline["configuration_only_commits"]),
                       configuration_commit_coverage_passed=timeline["configuration_commit_coverage_passed"],
                       configuration_path_log_repeat_equal=timeline["configuration_path_log_repeat_equal"],
                       conservation_passed=detail["lineages"]["conservation"]["passed"],
                       source_replay_passed=detail["replay"]["passed"],
                       lineage_events=len(detail["lineages"]["events"]),
                       ambiguous_transition_rate=detail["lineages"]["quality"]["ambiguous_transition_rate"],
                       required_blob_objects=detail["replay"]["required_blob_objects"],
                       verified_blob_objects=detail["replay"]["verified_blob_objects"],
                       endpoint_commit=timeline["endpoint_commit"],
                       observed_ancestry_covers_end=bool(history) and history[0]["timestamp"] >= timestamp(config["window_end"]))
    except Exception as error:
        summary["error"] = type(error).__name__ + ": " + str(error)
    save(output, detail)
    return summary


def confirmed_records(root):
    grouped = defaultdict(list)
    for path in sorted((root / "data/derived/cohort-v1/baselines").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))["summary"]
        if row["status"] == "confirmed":
            row["cohort_record_path"] = path.relative_to(root).as_posix()
            row["cohort_record_sha256"] = digest(path)
            grouped[row["repository_id"]].append(row)
    selected = []
    for identity in sorted(grouped, key=lambda value: int(value)):
        records = sorted(grouped[identity], key=lambda row: row["repository"].lower())
        winner = dict(records[0], candidate_aliases=[row["repository"] for row in records])
        winner["alias_baseline_conflict"] = len({row["baseline_commit"] for row in records}) > 1
        selected.append(winner)
    return selected


def write_progress(root, records, planned, confirmed):
    fields = sorted({key for record in records for key in record}) or ["repository_id", "repository", "status"]
    path = root / "reports/measurement_progress.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(records, key=lambda row: row["repository"].lower()))
    summary = dict(rules_version=RULES_VERSION, batch_directory=BATCH_DIRECTORY, confirmed_ids_at_run_start=confirmed,
                   planned_ids_this_run=planned, completed_ids_this_run=len(records),
                   status_counts=dict(Counter(row["status"] for row in records)),
                   snapshots=sum(row.get("snapshots", 0) for row in records),
                   required_blob_objects_across_repositories=sum(row.get("required_blob_objects", 0) for row in records),
                   verified_blob_objects_across_repositories=sum(row.get("verified_blob_objects", 0) for row in records),
                   configuration_only_commits=sum(row.get("configuration_only_commits", 0) for row in records),
                   successful_replay_ids=sum(row.get("source_replay_passed", False) for row in records),
                   scope="This run only; cohort collection can still be incomplete")
    save(root / "reports/measurement_summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or args.limit is not None and args.limit < 1:
        parser.error("workers must be 1..8 and limit positive")
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    records = confirmed_records(root)
    chosen = records[:args.limit] if args.limit else records
    paths = [root / "study.toml", *[root / "pintrace" / name for name in SOURCE_NAMES]]
    implementation = {path.relative_to(root).as_posix(): digest(path) for path in paths}
    manifest = dict(rules_version=RULES_VERSION, started_at_utc=datetime.now(timezone.utc).isoformat(),
                    arguments=vars(args), implementation_sha256=implementation,
                    selected_repository_ids=[row["repository_id"] for row in chosen],
                    cohort_record_sha256={row["cohort_record_path"]: row["cohort_record_sha256"] for row in chosen},
                    confirmed_ids_at_start=len(records), planned_ids=len(chosen))
    manifest_path = root / "data/derived" / BATCH_DIRECTORY / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    save(manifest_path, manifest)
    completed = []
    write_progress(root, completed, len(chosen), len(records))
    token = github_token() if chosen else ""
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(collect, row, root, config, token, args.retry, implementation) for row in chosen]
        for job in as_completed(jobs):
            row = job.result()
            completed.append(row)
            write_progress(root, completed, len(chosen), len(records))
            print(f"{len(completed)}/{len(chosen)} {row['repository']} {row['status']}", flush=True)
    manifest.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                    implementation_unchanged=all(digest(root / name) == value for name, value in implementation.items()),
                    summary=write_progress(root, completed, len(chosen), len(records)))
    save(manifest_path, manifest)
    print(json.dumps(manifest["summary"], indent=2))


if __name__ == "__main__":
    main()
