"""Collect upstream opportunity evidence for an enlarged upstream set in budgeted batches.

Software Heritage allows 120 anonymous requests per hour. Every batch calls `pintrace.opportunities` with a request budget
below that limit; archive objects are cached on disk, so each pass only pays for evidence that is still missing. The loop
ends when every selected upstream has no recorded evidence gap, when a pass issues no new archive request, or after a bounded
number of passes. A repository with a residual gap keeps its gap in the payload: absence of evidence is never filled in.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from .opportunities import archive_token

RULE_VERSION = "upstream-selection-v2"
OUTPUT = "data/derived/upstream-selection-v2"
HOUR = 3600
# Software Heritage permits 120 requests per hour anonymously and 1200 with a token; keep one request below each limit.
ANONYMOUS_BUDGET_LIMIT = 119
AUTHENTICATED_BUDGET_LIMIT = 1199


def select_upstreams(rows, top, required=()):
    """Upstream repositories ranked by the number of baseline repositories that use any of their actions.

    `rows` are the dictionaries of reports/upstream_frequencies.csv. Ties break on baseline reference count, then name.
    Repositories in `required` are always kept, so the earlier collection and the target stay inside the set.
    """
    by_repository = {}
    for row in rows:
        entry = by_repository.setdefault(row["upstream_repository"], dict(repositories=0, references=0, actions=[]))
        entry["repositories"] = max(entry["repositories"], int(row["baseline_repository_count"]))
        entry["references"] += int(row["baseline_reference_count"])
        entry["actions"].append(row["action"])
    ranked = sorted(by_repository, key=lambda name: (-by_repository[name]["repositories"], -by_repository[name]["references"], name))
    chosen = list(ranked[:top])
    chosen += [name for name in required if name not in chosen]
    return [dict(upstream_repository=name, baseline_repositories=by_repository[name]["repositories"],
                 baseline_references=by_repository[name]["references"], actions=sorted(by_repository[name]["actions"]))
            for name in chosen if name in by_repository]


def write_selection(root, top, required):
    root = Path(root)
    source = root / "reports/upstream_frequencies.csv"
    with source.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    selected = select_upstreams(rows, top, required)
    directory = root / OUTPUT
    directory.mkdir(parents=True, exist_ok=True)
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), top=top, required=list(required),
                  input_sha256={"reports/upstream_frequencies.csv": hashlib.sha256(source.read_bytes()).hexdigest()},
                  upstream_repositories=[row["upstream_repository"] for row in selected], detail=selected,
                  rule="Rank upstream repositories by baseline repository count, then baseline reference count, then name; keep the required ones.")
    path = directory / "selection.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if previous["upstream_repositories"] != result["upstream_repositories"]:
            raise ValueError("A different upstream selection is already recorded; it will not be overwritten")
        return previous
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def gaps(root, repositories):
    """Repositories whose payload is missing or still records an evidence gap."""
    report_path = Path(root) / "reports/opportunity_coverage.json"
    if not report_path.exists():
        return list(repositories)
    items = {row["repository"]: row for row in json.loads(report_path.read_text(encoding="utf-8"))["upstreams"]}
    return [name for name in repositories if name not in items or items[name]["errors"]]


def requests_issued(root):
    return json.loads((Path(root) / "reports/opportunity_coverage.json").read_text(encoding="utf-8"))["archive_requests_issued"]


def throttle_wait(root, now=None):
    """Seconds until a recorded archive rate limit ends; zero when none is active."""
    path = Path(root) / "data/cache/softwareheritage/rate-limit.json"
    if not path.exists():
        return 0
    until = json.loads(path.read_text(encoding="utf-8")).get("unavailable_until", 0)
    return max(0, int(until - (time.time() if now is None else now)) + 5)


def run_pass(root, repositories, budget, workers):
    command = [sys.executable, "-B", "-X", "utf8", "-m", "pintrace.opportunities", "--root", str(root), "--repositories", *repositories,
               "--archive-max-requests", str(budget), "--workers", str(workers)]
    return subprocess.run(command, cwd=root, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=40)
    parser.add_argument("--budget", type=int, default=100)
    parser.add_argument("--max-passes", type=int, default=40)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--select-only", action="store_true")
    args = parser.parse_args()
    limit = AUTHENTICATED_BUDGET_LIMIT if archive_token() else ANONYMOUS_BUDGET_LIMIT
    if not 1 <= args.budget <= limit or args.top < 1 or args.max_passes < 1:
        parser.error(f"budget must be 1..{limit} for {'authenticated' if archive_token() else 'anonymous'} access, top and max-passes positive")
    root = Path(__file__).resolve().parents[1]
    required = ("actions/checkout", "tj-actions/changed-files", "actions/setup-node", "actions/setup-python", "actions/cache",
                "actions/upload-artifact", "actions/download-artifact")
    selection = write_selection(root, args.top, required)
    repositories = selection["upstream_repositories"]
    print(json.dumps(dict(upstreams=len(repositories), selection_sha256=hashlib.sha256((root / OUTPUT / "selection.json").read_bytes()).hexdigest())), flush=True)
    if args.select_only:
        return
    for number in range(1, args.max_passes + 1):
        started = time.monotonic()
        code = run_pass(root, repositories, args.budget, args.workers)
        remaining = gaps(root, repositories)
        issued = requests_issued(root)
        print(json.dumps(dict(pass_number=number, returncode=code, new_archive_requests=issued, upstreams_with_gaps=len(remaining),
                              at_utc=datetime.now(timezone.utc).isoformat())), flush=True)
        if code or not remaining:
            break
        if issued == 0 and not throttle_wait(root):
            print("No archive request was issued, no rate limit is active and gaps remain; the residual gaps are not budget-limited.", flush=True)
            break
        time.sleep(max(0, HOUR + 30 - (time.monotonic() - started), throttle_wait(root)))


if __name__ == "__main__":
    main()
