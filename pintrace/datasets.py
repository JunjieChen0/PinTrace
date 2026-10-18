"""Derive pilot candidates from frozen pre-incident workflow versions."""

import argparse
import csv
import gzip
import hashlib
import json
import tarfile
import tomllib
from datetime import datetime
from pathlib import Path

import requests

from .workflow import TARGET, parse_workflow

NEEDED = {"repositories.csv.gz", "workflows.csv.gz", "workflows.tar.gz"}


def prepare(root, config, download=False):
    raw = root / "data" / "raw" / ("zenodo-" + config["dataset_record"])
    raw.mkdir(parents=True, exist_ok=True)
    metadata = raw / "record.json"
    if not metadata.exists():
        response = requests.get("https://zenodo.org/api/records/" + config["dataset_record"], timeout=45)
        response.raise_for_status()
        metadata.write_text(json.dumps(response.json(), indent=2), encoding="utf-8")
    record = json.loads(metadata.read_text(encoding="utf-8"))
    for item in record["files"]:
        if item["key"] not in NEEDED:
            continue
        path = raw / item["key"]
        if not path.exists():
            if not download:
                raise FileNotFoundError(str(path) + ": rerun with --download")
            print("Downloading", item["key"], item["size"], flush=True)
            with requests.get(item["links"]["self"], stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                with path.open("xb") as output:
                    for chunk in response.iter_content(4 * 1024 * 1024):
                        output.write(chunk)
        with path.open("rb") as source:
            actual = "md5:" + hashlib.file_digest(source, "md5").hexdigest()
        if path.stat().st_size != item["size"] or actual != item["checksum"]:
            raise ValueError("Incomplete/corrupt cache; refusing to overwrite: " + str(path))
        print("Verified", item["key"], flush=True)

    cutoff = int(datetime.fromisoformat(config["event_anchor"].replace("Z", "+00:00")).timestamp())
    latest = {}
    rows = 0
    with gzip.open(raw / "workflows.csv.gz", "rt", encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            rows += 1
            timestamp = int(row["committed_date"])
            if timestamp >= cutoff:
                continue
            key = row["uid"]
            previous = latest.get(key)
            rank = (timestamp, row["commit_hash"])
            if previous is None or rank > (previous["timestamp"], previous["commit_hash"]):
                latest[key] = {"repository": row["repository"], "path": row["file_path"],
                               "file_hash": row["file_hash"], "commit_hash": row["commit_hash"],
                               "timestamp": timestamp, "valid_workflow": row["valid_workflow"]}
            if rows % 500000 == 0:
                print("Metadata rows", rows, flush=True)
    live = [r for r in latest.values() if r["file_hash"] != "--" and r["valid_workflow"] == "True"]
    required = {r["file_hash"] for r in live}
    matched = {}
    members = 0
    with tarfile.open(raw / "workflows.tar.gz", "r|gz") as archive:
        for member in archive:
            members += 1
            if members % 100000 == 0:
                print("Archive members", members, "target versions", len(matched), flush=True)
            digest = Path(member.name).name
            if not member.isfile() or digest not in required:
                continue
            content = archive.extractfile(member).read()
            if TARGET.encode() not in content.lower():
                continue
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("Archived workflow content hash mismatch: " + digest)
            parsed = parse_workflow(content.decode("utf-8"), ".github/workflows/archived.yml")
            if not parsed["errors"] and any(r["kind"] == "action" and r["action"] == TARGET for r in parsed["references"]):
                matched[digest] = [r for r in parsed["references"] if r["kind"] == "action" and r["action"] == TARGET]
    candidates = {}
    for row in live:
        if row["file_hash"] in matched:
            candidates.setdefault(row["repository"], row)
    ranked = sorted(candidates, key=lambda name: hashlib.sha256((config["sampling_seed"] + ":" + name.lower()).encode()).hexdigest())
    selected = ranked[:config["pilot_size"]]
    reports = root / "reports"
    reports.mkdir(exist_ok=True)
    fields = ["repository", "dataset_record", "sampling_seed", "rank", "archived_path", "archived_commit", "archived_content_sha256"]
    with (reports / "pilot_candidates.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for rank, name in enumerate(selected, 1):
            row = candidates[name]
            writer.writerow(dict(repository=name, dataset_record=config["dataset_record"],
                                 sampling_seed=config["sampling_seed"], rank=rank,
                                 archived_path=row["path"], archived_commit=row["commit_hash"],
                                 archived_content_sha256=row["file_hash"]))
    summary = {"record": config["dataset_record"], "source_checksums_verified": True,
               "metadata_rows": rows, "pre_anchor_workflow_histories": len(live),
               "archive_members": members, "target_version_hashes": len(matched),
               "candidate_repositories": len(candidates), "selected": selected,
               "selection": "SHA-256(seed:lowercase_repository) ascending; no response-outcome filtering",
               "caveat": "Archive timestamps and uid are candidate hints, not verified default-branch baseline states."}
    (reports / "candidate_sampling.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    prepare(root, config, args.download)


if __name__ == "__main__":
    main()

