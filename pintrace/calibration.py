"""Replay frozen development intervals from cached raw workflow blobs."""

import csv
import hashlib
import json
import tomllib
from collections import Counter, defaultdict
from pathlib import Path

from .workflow import action_counts, changes, parse_workflow

EVENTS = ["action_added", "action_removed", "pinning_counts_changed",
          "sha_targets_changed", "mutable_refs_changed"]


def select_examples(details, seed):
    pools = defaultdict(list)
    for detail in details:
        repository = detail["summary"]["repository"]
        snapshots = detail["snapshots"]
        by_commit = {row["sha"]: row for row in snapshots}
        positions = {row["sha"]: index for index, row in enumerate(snapshots)}
        event_pairs = set()
        for event in detail["events"]:
            before, after = event["previous_commit"], event["commit"]
            if before not in by_commit or after not in by_commit:
                continue
            event_pairs.add((before, after))
            if positions[after] != positions[before] + 1:
                continue
            if not by_commit[before]["complete"] or not by_commit[after]["complete"]:
                continue
            if event["event"] in EVENTS:
                pools[event["event"]].append({"repository": repository, "before": by_commit[before],
                    "after": by_commit[after], "action_focus": event["action"], "hint": event["event"]})
        for before, after in zip(snapshots, snapshots[1:]):
            if before["complete"] and after["complete"] and (before["sha"], after["sha"]) not in event_pairs:
                pools["no_reported_event"].append({"repository": repository, "before": before,
                    "after": after, "action_focus": None, "hint": "no_reported_event"})

    def rank(item):
        key = ":".join([seed, item["repository"], item["before"]["sha"],
                        item["after"]["sha"], item["action_focus"] or ""])
        return hashlib.sha256(key.encode()).hexdigest()

    selected = []
    for kind in [*EVENTS, "no_reported_event"]:
        limit = 10 if kind == "no_reported_event" else 8
        pool = sorted(pools[kind], key=rank)
        # Prioritize different repositories without excluding rare-case repeats.
        first, rest, seen = [], [], set()
        for item in pool:
            if item["repository"] not in seen:
                first.append(item)
                seen.add(item["repository"])
            else:
                rest.append(item)
        selected.extend((first + rest)[:limit])
    return selected, {kind: len(pool) for kind, pool in sorted(pools.items())}


def replay_snapshot(snapshot, blob_directory):
    references, problems = [], []
    status = "passed"
    for item in snapshot["files"]:
        path = blob_directory / item["blob"]
        if item["kind"] != "blob" or item["mode"] not in ("100644", "100755"):
            problems.append({"path": item["path"], "reason": "unsupported_file_mode"})
            status = "unknown" if status != "failed" else status
            continue
        if not path.exists():
            problems.append({"path": item["path"], "reason": "missing_cached_blob"})
            status = "unknown" if status != "failed" else status
            continue
        content = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        if actual != item["blob"]:
            problems.append({"path": item["path"], "reason": "blob_identity_mismatch"})
            status = "failed"
            continue
        try:
            parsed = parse_workflow(content.decode("utf-8-sig"), item["path"])
        except UnicodeDecodeError:
            parsed = {"errors": ["not_utf8"]}
        if parsed["errors"]:
            problems.append({"path": item["path"], "reason": "parse_incomplete", "errors": parsed["errors"]})
            status = "unknown" if status != "failed" else status
        else:
            references.extend(parsed["references"])
    if not problems:
        if not snapshot["complete"]:
            status = "unknown"
            problems.append({"reason": "saved_snapshot_incomplete"})
        elif references != snapshot["references"] or action_counts(references) != snapshot["counts"]:
            status = "failed"
            problems.append({"reason": "saved_snapshot_differs_from_replay"})
    return {"status": status, "problems": problems, "references": references}


def replay_interval(item, expected_events, blob_directory):
    checks = {side: replay_snapshot(item[side], blob_directory) for side in ("before", "after")}
    statuses = {check["status"] for check in checks.values()}
    status = "failed" if "failed" in statuses else "unknown" if "unknown" in statuses else "passed"
    event_match = None
    actual = []
    if status == "passed":
        actual = changes(checks["before"]["references"], checks["after"]["references"])
        fields = ("action", "event", "before", "after")
        expected = [{key: event[key] for key in fields} for event in expected_events]
        order = lambda event: json.dumps(event, sort_keys=True)
        event_match = sorted(actual, key=order) == sorted(expected, key=order)
        if not event_match:
            status = "failed"
    return {"status": status, "snapshots": checks,
            "events_match_saved_output": event_match, "replayed_events": actual}


def main():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    seed = config["sampling_seed"] + ":calibration-v1"
    paths = sorted((root / "data/derived/repositories").glob("*.json"))
    details = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    details = [detail for detail in details if detail["summary"].get("status") == "eligible"]
    examples, available = select_examples(details, seed)
    raw = root / "data/derived/replay-v1"
    raw.mkdir(parents=True, exist_ok=True)
    by_repository = {detail["summary"]["repository"]: detail for detail in details}
    queue = []
    with (raw / "check_packets.jsonl").open("w", encoding="utf-8") as output:
        for index, item in enumerate(examples, 1):
            identifier = f"CAL-{index:03d}"
            expected = [event for event in by_repository[item["repository"]]["events"]
                        if event["previous_commit"] == item["before"]["sha"]
                        and event["commit"] == item["after"]["sha"]]
            result = replay_interval(item, expected, root / "data/cache/blobs")
            packet = {"item_id": identifier, "repository": item["repository"],
                      "action_focus": item["action_focus"], "automatic_stratum": item["hint"],
                      "check_method": "raw_blob_identity_and_yaml_event_replay", "result": result}
            for side in ["before", "after"]:
                snapshot = item[side]
                packet[side] = {"commit": snapshot["sha"], "date": snapshot["date"],
                                "references": snapshot["references"],
                                "files": [{"path": f["path"], "blob": f["blob"],
                                           "local_content": "data/cache/blobs/" + f["blob"]}
                                          for f in snapshot["files"]]}
            output.write(json.dumps(packet, ensure_ascii=False) + "\n")
            queue.append({"item_id": identifier, "repository": item["repository"],
                          "before_commit": item["before"]["sha"], "after_commit": item["after"]["sha"],
                          "after_date": item["after"]["date"],
                          "evidence_url": f"https://github.com/{item['repository']}/commit/{item['after']['sha']}",
                          "packet_path": "data/derived/replay-v1/check_packets.jsonl",
                          "status": result["status"]})
    with (root / "reports/calibration_queue.csv").open("w", encoding="utf-8", newline="") as output:
        fields = ["item_id", "repository", "before_commit", "after_commit", "after_date", "evidence_url",
                  "packet_path", "status"]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(queue)
    manifest = {"seed": seed, "purpose": "Automated raw-file replay and regression checks.",
                "selected": len(examples), "selected_repositories": len({item["repository"] for item in examples}),
                "available_by_automatic_stratum": available,
                "selected_by_automatic_stratum": dict(Counter(item["hint"] for item in examples)),
                "status_counts": dict(Counter(item["status"] for item in queue)),
                "source_files_sha256": {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                        for path in paths},
                "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "limitations": ["Replay agreement measures reproducibility, not behavioral classification accuracy.",
                                "Packets do not yet include longitudinal Bot configuration histories.",
                                "Errors shared with the original parser require semantic boundary tests or additional evidence."]}
    (root / "reports/calibration_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in manifest.items() if key != "source_files_sha256"}, indent=2))


if __name__ == "__main__":
    main()
