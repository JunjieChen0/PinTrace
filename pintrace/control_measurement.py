"""Measure the matched control cohort with the unchanged treated-cohort measurement code.

The module only swaps the record source and the output batch directory at run time. `measurement.py` is not edited, so
the implementation hashes written into control files equal those of the treated batch run with the same sources.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import tomllib

from . import measurement
from .control_baseline import eligible_controls
from .github import github_token

BATCH = "measurement-control-v1"
SELECTION = "data/derived/control-v1/matched_controls.json"


def control_records(root):
    """Matched controls only: the selection file lists repository IDs; baselines come from the control ledger."""
    root = Path(root)
    chosen = {str(row["repository_id"]) for row in json.loads((root / SELECTION).read_text(encoding="utf-8"))["controls"]}
    return eligible_controls(root, chosen)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--ids", nargs="+", help="restrict the run to these repository IDs, so a retry does not reload every finished file")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8 or args.limit is not None and args.limit < 1:
        parser.error("workers must be 1..8 and limit positive")
    root = Path(__file__).resolve().parents[1]
    measurement.BATCH_DIRECTORY = BATCH
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    records = control_records(root)
    if args.ids:
        records = [row for row in records if row["repository_id"] in {str(identity) for identity in args.ids}]
    chosen = records[:args.limit] if args.limit else records
    paths = [root / "study.toml", *[root / "pintrace" / name for name in measurement.SOURCE_NAMES]]
    implementation = {path.relative_to(root).as_posix(): measurement.digest(path) for path in paths}
    manifest = dict(rules_version=measurement.RULES_VERSION, batch_directory=BATCH, started_at_utc=datetime.now(timezone.utc).isoformat(),
                    arguments=vars(args), implementation_sha256=implementation, selection_sha256=measurement.digest(root / SELECTION),
                    selected_repository_ids=[row["repository_id"] for row in chosen], planned_ids=len(chosen))
    manifest_path = root / "data/derived" / BATCH / ("run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
    measurement.save(manifest_path, manifest)
    token = github_token() if chosen else ""
    completed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(measurement.collect, row, root, config, token, args.retry, implementation) for row in chosen]
        for job in as_completed(jobs):
            completed.append(job.result())
            print(f"{len(completed)}/{len(chosen)} {completed[-1]['repository']} {completed[-1]['status']}", flush=True)
    manifest.update(finished_at_utc=datetime.now(timezone.utc).isoformat(),
                    implementation_unchanged=all(measurement.digest(root / name) == value for name, value in implementation.items()),
                    status_counts={status: sum(row["status"] == status for row in completed) for status in sorted({row["status"] for row in completed})})
    measurement.save(manifest_path, manifest)
    print(json.dumps(manifest["status_counts"]))


if __name__ == "__main__":
    main()
