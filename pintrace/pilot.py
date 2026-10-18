"""Run the bounded engineering pilot, without causal effect estimation."""

import argparse
import csv
import hashlib
import json
import platform
import subprocess
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from .github import GitHub, github_token
from .history import clone, first_parent_history, git, workflow_commits, workflow_files
from .workflow import TARGET, action_counts, changes, parse_workflow


def timestamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def snapshot(client, directory, repository, point, parsed_cache):
    references, errors, files = [], [], []
    for item in workflow_files(directory, point["sha"]):
        files.append(item)
        if item["kind"] != "blob" or item["mode"] not in ("100644", "100755"):
            errors.append({"path": item["path"], "error": "unsupported_file_mode"})
            continue
        key = (item["path"], item["blob"])
        if key not in parsed_cache:
            content = client.blob(repository, point["sha"], item["path"], item["blob"])
            try:
                parsed_cache[key] = parse_workflow(content.decode("utf-8-sig"), item["path"])
            except UnicodeDecodeError:
                parsed_cache[key] = {"references": [], "errors": ["not_utf8"]}
        parsed = parsed_cache[key]
        item["inactive_empty_document"] = parsed.get("inactive_empty_document", False)
        if parsed["errors"]:
            errors.append({"path": item["path"], "error": parsed["errors"]})
        else:
            references.extend(parsed["references"])
    return dict(point, date=iso(point["timestamp"]), references=references, errors=errors,
                files=files, complete=not errors, counts=action_counts(references))


def collect(repository, root, config, token):
    client = GitHub(root / "data/cache", token)
    record = {"repository": repository, "status": "failed"}
    try:
        metadata = client.api("repos/" + repository)
        if metadata.get("private"):
            raise ValueError("Repository is no longer public")
        canonical = metadata["full_name"]
        record.update(canonical_repository=canonical, fork=metadata["fork"], archived=metadata["archived"],
                      observed_default_branch=metadata["default_branch"],
                      current_size_kb=metadata["size"])
        directory = clone(canonical, root / "data/cache/repos" / (repository.replace("/", "__") + ".git"))
        history = first_parent_history(directory)
        record.update(observed_head=history[0]["sha"], observed_head_date=iso(history[0]["timestamp"]),
                      ancestry_commits=len(history),
                      clock_inversions=sum(a["timestamp"] < b["timestamp"] for a, b in zip(history, history[1:])))
        start, anchor, end = map(timestamp, (config["window_start"], config["event_anchor"], config["window_end"]))

        def prior(bound, inclusive=False):
            return next((p for p in history if p["timestamp"] < bound or (inclusive and p["timestamp"] == bound)), None)

        baseline = prior(anchor)
        endpoint = prior(end, inclusive=True)
        pre_start = prior(start)
        if baseline is None or endpoint is None:
            raise ValueError("No observable pre-anchor or endpoint commit")
        parsed_cache = {}
        baseline_snapshot = snapshot(client, directory, canonical, baseline, parsed_cache)
        record.update(baseline_commit=baseline["sha"], baseline_date=iso(baseline["timestamp"]),
                      baseline_parse_complete=baseline_snapshot["complete"],
                      target_baseline_full_sha=baseline_snapshot["counts"].get(TARGET, {}).get("full_sha", 0),
                      target_baseline_mutable=baseline_snapshot["counts"].get(TARGET, {}).get("mutable_or_unresolved", 0))
        if not baseline_snapshot["complete"]:
            record.update(status="baseline_parse_incomplete", error="Baseline contains unsupported or invalid workflows")
            snapshots = [baseline_snapshot]
            events = []
        elif TARGET not in baseline_snapshot["counts"]:
            record.update(status="baseline_ineligible", error="Frozen archive hint not confirmed on observable first-parent baseline")
            snapshots = [baseline_snapshot]
            events = []
        else:
            relevant = workflow_commits(directory)
            wanted = {baseline["sha"], endpoint["sha"]}
            if pre_start:
                wanted.add(pre_start["sha"])
            wanted.update(p["sha"] for p in history if p["sha"] in relevant and start <= p["timestamp"] <= end)
            points = [p for p in reversed(history) if p["sha"] in wanted]
            snapshots = []
            for index, point in enumerate(points):
                state = baseline_snapshot if point["sha"] == baseline["sha"] else snapshot(client, directory, canonical, point, parsed_cache)
                snapshots.append(state)
                if (index + 1) % 25 == 0:
                    print(repository, "snapshots", index + 1, "/", len(points), flush=True)
            events = []
            for before, after in zip(snapshots, snapshots[1:]):
                if not before["complete"] or not after["complete"]:
                    continue
                delta = changes(before["references"], after["references"])
                for event in delta:
                    event.update(repository=repository, commit=after["sha"], previous_commit=before["sha"],
                                 timestamp=after["timestamp"], date=after["date"],
                                 evidence_url=f"https://github.com/{canonical}/commit/{after['sha']}")
                events.extend(delta)
            final = next(s for s in snapshots if s["sha"] == endpoint["sha"])
            response = next((e for e in events if e["action"] == TARGET and e["timestamp"] >= anchor), None)
            if response:
                message = git(directory, "show", "-s", "--format=%s", response["commit"]).decode("utf-8", "replace").strip()
                record.update(first_target_change=response["event"], first_target_change_commit=response["commit"],
                              first_target_change_date=response["date"], first_target_change_message=message)
            record.update(status="eligible", pre_window_baseline_available=pre_start is not None,
                          endpoint_commit=endpoint["sha"], endpoint_date=iso(endpoint["timestamp"]),
                          endpoint_parse_complete=final["complete"],
                          target_endpoint_full_sha=final["counts"].get(TARGET, {}).get("full_sha", 0),
                          target_endpoint_mutable=final["counts"].get(TARGET, {}).get("mutable_or_unresolved", 0),
                          snapshots=len(snapshots), incomplete_snapshots=sum(not s["complete"] for s in snapshots),
                          post_anchor_events=sum(e["timestamp"] >= anchor for e in events),
                          post_anchor_sha_target_changes=sum(e["timestamp"] >= anchor and e["event"] == "sha_targets_changed" for e in events),
                          observed_ancestry_covers_end=history[0]["timestamp"] >= end)
        detail = {"summary": record, "snapshots": snapshots, "events": events,
                  "limitations": ["Current default-branch ancestry cannot prove historical default-branch identity or absence of force pushes.",
                                  "Aggregate removal/addition does not establish semantic replacement.",
                                  "No execution, compromise, or incident causation is inferred."]}
        output = root / "data/derived/repositories" / (repository.replace("/", "__") + ".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as error:
        record["error"] = f"{type(error).__name__}: {error}"
    return record


def audit_upstreams(root, records, token, config):
    frequencies = Counter()
    for record in records:
        if record["status"] != "eligible":
            continue
        path = root / "data/derived/repositories" / (record["repository"].replace("/", "__") + ".json")
        detail = json.loads(path.read_text(encoding="utf-8"))
        for ref in next(s for s in detail["snapshots"] if s["sha"] == record["baseline_commit"])["references"]:
            if ref["kind"] == "action":
                frequencies[ref["repository"]] += 1
    client = GitHub(root / "data/cache", token)
    audits = []
    for repository, count in frequencies.most_common(5):
        item = {"repository": repository, "baseline_occurrences": count,
                "historical_tag_mapping_verified": False, "update_latency_identified": False}
        releases = []
        try:
            for page in range(1, 21):
                batch = client.api(f"repos/{repository}/releases", per_page=100, page=page)
                releases.extend(r for r in batch if not r.get("draft"))
                if len(batch) < 100:
                    break
            item["release_listing_truncated"] = len(batch) == 100
            selected = [r for r in releases if r.get("published_at") and config["event_anchor"] <= r["published_at"] <= config["window_end"]]
            item["release_records_in_post_window"] = len(selected)
            item["release_evidence"] = [{k: r.get(k) for k in ("id", "tag_name", "published_at", "prerelease", "html_url")} for r in selected]
        except Exception as error:
            item["error"] = str(error)
        audits.append(item)
    (root / "reports/upstream_feasibility.json").write_text(json.dumps(audits, indent=2), encoding="utf-8")


def save_results(root, records):
    (root / "reports").mkdir(exist_ok=True)
    fields = sorted(set().union(*(record.keys() for record in records)))
    with (root / "reports/pilot_results.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(records, key=lambda r: r["repository"].lower()))
    summary = {"retrieved_at_utc": datetime.now(timezone.utc).isoformat(), "attempted": len(records),
               "statuses": dict(Counter(r["status"] for r in records)),
               "eligible_with_parse_gaps": sum(r.get("incomplete_snapshots", 0) > 0 for r in records),
               "repositories_with_clock_inversions": sum(r.get("clock_inversions", 0) > 0 for r in records),
               "purpose": "Engineering feasibility only; no population adoption or causal effect estimate."}
    (root / "reports/pilot_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default="reports/pilot_candidates.csv")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    with (root / args.candidates).open(encoding="utf-8", newline="") as source:
        candidates = list(csv.DictReader(source))[:args.limit]
    token = github_token()
    (root / "reports").mkdir(exist_ok=True)
    source_files = [root / "study.toml", root / args.candidates, *sorted((root / "pintrace").glob("*.py"))]
    manifest = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
                "python": platform.python_version(), "requests": requests.__version__, "pyyaml": yaml.__version__,
                "git": subprocess.check_output(["git", "--version"], text=True).strip(),
                "config": config, "arguments": vars(args),
                "source_sha256": {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}}
    (root / "reports/run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    records = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect, row["repository"], root, config, token): row["repository"] for row in candidates}
        for future in as_completed(futures):
            record = future.result()
            records.append(record)
            save_results(root, records)
            print(len(records), "/", len(candidates), record["repository"], record["status"], record.get("error", ""), flush=True)
    audit_upstreams(root, records, token, config)


if __name__ == "__main__":
    main()

