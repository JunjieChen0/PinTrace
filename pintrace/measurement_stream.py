"""Measure selected repositories with the unchanged measurement code and a streaming JSON writer.

`measurement.save` serializes with one `json.dumps` call. For a history with thousands of snapshots that builds a string of
several gigabytes next to the object graph and exhausts memory. This module replaces only the writer, at run time, with an
encoder that yields chunks. The bytes written equal those of `json.dumps(value, ensure_ascii=False, indent=2)`, and
`measurement.py` is not edited, so the implementation hashes stored in the output equal those of an ordinary run.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tomllib

from . import measurement
from .github import github_token


def stream_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(".pending")
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2)
    with pending.open("w", encoding="utf-8") as stream:
        for chunk in encoder.iterencode(value):
            stream.write(chunk)
    pending.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="+", help="repository IDs to measure, one at a time")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    measurement.save = stream_save
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    wanted = {str(identity) for identity in args.ids}
    records = [row for row in measurement.confirmed_records(root) if row["repository_id"] in wanted]
    if {row["repository_id"] for row in records} != wanted:
        parser.error("Some IDs are not confirmed cohort members")
    paths = [root / "study.toml", *[root / "pintrace" / name for name in measurement.SOURCE_NAMES]]
    implementation = {path.relative_to(root).as_posix(): measurement.digest(path) for path in paths}
    token = github_token()
    for row in records:
        started = datetime.now(timezone.utc).isoformat()
        summary = measurement.collect(row, root, config, token, True, implementation)
        print(json.dumps(dict(repository_id=row["repository_id"], repository=row["repository"], status=summary["status"],
                              snapshots=summary.get("snapshots"), started_at_utc=started, finished_at_utc=datetime.now(timezone.utc).isoformat())), flush=True)


if __name__ == "__main__":
    main()
