"""Offline maintenance-activity proxies for treated and control repositories.

Everything is derived from first-parent commit timestamps of the fixed observed commit in the local bare clones, plus
repository metadata already held in the API cache. No author, committer or message field is read or written. The proxies
do not depend on the upstream release opportunities that define RQ3, so they stay defined for repositories with no
upstream-action release in the window.
"""

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import tomllib

from .history import git
from .pilot import timestamp

RULE_VERSION = "maintenance-activity-v1"
DAY = 86400
BIN_DAYS = 30
OUTPUT = "data/derived/maintenance-activity-v1"
CACHE_FIELDS = ("pushed_at", "archived", "disabled", "stargazers_count", "forks_count", "open_issues_count", "subscribers_count")


def commit_times(directory, head):
    """First-parent committer timestamps of the fixed commit's ancestry, newest first. Timestamps only."""
    output = git(directory, "rev-list", "--first-parent", "--timestamp", head).decode()
    return sorted((int(line.split()[0]) for line in output.splitlines() if line), reverse=True)


def bin_counts(times, anchor, first_day, last_day):
    """Commit counts in consecutive 30-day bins labelled by their start day relative to the anchor."""
    starts = list(range(first_day, last_day, BIN_DAYS))
    counts = {start: 0 for start in starts}
    for stamp in times:
        offset = math.floor((stamp - anchor) / DAY)
        if first_day <= offset < last_day:
            counts[first_day + (offset - first_day) // BIN_DAYS * BIN_DAYS] += 1
    return counts


def longest_gap_days(times, start, end):
    """Longest run of days with no commit inside [start, end), counting the edges."""
    inside = sorted(stamp for stamp in times if start <= stamp < end)
    points = [start, *inside, end]
    return max((b - a) / DAY for a, b in zip(points, points[1:]))


def profile(times, anchor, window_start, window_end, head_time):
    """Pure function of commit times; `times` holds every first-parent committer timestamp."""
    up_to_end = [t for t in times if t <= window_end]
    last_before_anchor = next((t for t in sorted(times, reverse=True) if t < anchor), None)
    last_by_end = max(up_to_end) if up_to_end else None
    post_days = math.floor((window_end - anchor) / DAY)
    result = dict(
        commits_pre_180d=sum(anchor - 180 * DAY <= t < anchor for t in times),
        commits_pre_365d=sum(anchor - 365 * DAY <= t < anchor for t in times),
        commits_post_180d=sum(anchor <= t < anchor + 180 * DAY for t in times),
        commits_post_360d=sum(anchor <= t < anchor + 360 * DAY for t in times),
        commits_post_to_end=sum(anchor <= t <= window_end for t in times),
        active_bins_pre=sum(v > 0 for v in bin_counts(times, anchor, -180, 0).values()),
        active_bins_post=sum(v > 0 for v in bin_counts(times, anchor, 0, post_days - post_days % BIN_DAYS).values()),
        days_since_last_commit_at_anchor=None if last_before_anchor is None else (anchor - last_before_anchor) / DAY,
        days_since_last_commit_at_end=None if last_by_end is None else (window_end - last_by_end) / DAY,
        longest_gap_post_days=longest_gap_days(times, anchor, window_end),
        first_commit_after_anchor_days=next(((t - anchor) / DAY for t in sorted(times) if t >= anchor), None),
        head_before_window_end=head_time < window_end,
        bins_30d=bin_counts(times, anchor, -180, post_days - post_days % BIN_DAYS))
    # An absent commit through the end is observable for every repository whose clone was taken after the window closed.
    for days in (90, 180):
        gap = result["days_since_last_commit_at_end"]
        result[f"no_commit_last_{days}d_at_end"] = None if gap is None else gap >= days
    result["any_commit_in_window"] = result["commits_post_to_end"] > 0
    return result


def cached_metadata(root, repository):
    url = "https://api.github.com/repos/" + repository
    path = Path(root) / "data/cache/api" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    if not path.exists():
        return {}
    saved = json.loads(path.read_text(encoding="utf-8"))
    return dict({key: saved["data"].get(key) for key in CACHE_FIELDS}, metadata_retrieved_at=saved.get("retrieved_at"))


def treated_records(root):
    from .measurement import confirmed_records
    formal = {str(i) for i in json.loads((Path(root) / "reports/entity_identity_summary.json").read_text(encoding="utf-8"))["formal_eligible_repository_ids"]}
    return [dict(row, group="treated") for row in confirmed_records(root) if row["repository_id"] in formal]


def control_records(root):
    chosen = json.loads((Path(root) / "data/derived/control-v1/matched_controls.json").read_text(encoding="utf-8"))["controls"]
    from .control_baseline import eligible_controls
    return [dict(row, group="control") for row in eligible_controls(root, {str(row["repository_id"]) for row in chosen})]


def collect(root, records, config):
    anchor, start, end = (timestamp(config[key]) for key in ("event_anchor", "window_start", "window_end"))
    rows = []
    for record in sorted(records, key=lambda r: (r["group"], int(r["repository_id"]))):
        directory = Path(root) / record["git_directory"]
        times = commit_times(directory, record["observed_head"])
        values = profile(times, anchor, start, end, times[0] if times else 0)
        rows.append(dict(repository_id=record["repository_id"], repository=record["repository"], group=record["group"],
                         observed_commits=len(times), observed_head_date=datetime.fromtimestamp(times[0], timezone.utc).isoformat() if times else None,
                         **values, **cached_metadata(root, record["repository"])))
    return rows


def write(root, rows, config, extra_inputs):
    directory = Path(root) / OUTPUT
    directory.mkdir(parents=True, exist_ok=True)
    flat = [{k: v for k, v in row.items() if k != "bins_30d"} for row in rows]
    with (directory / "activity.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(flat[0]))
        writer.writeheader()
        writer.writerows(flat)
    (directory / "activity_bins.json").write_text(json.dumps({row["repository_id"]: row["bins_30d"] for row in rows}, sort_keys=True), encoding="utf-8")
    summary = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), window=dict(config),
                   counts={group: sum(r["group"] == group for r in rows) for group in ("treated", "control")},
                   input_sha256={name: hashlib.sha256((Path(root) / name).read_bytes()).hexdigest() for name in extra_inputs},
                   output_sha256={name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ("activity.csv", "activity_bins.json")},
                   limits=["Commit timestamps are committer clocks on first-parent ancestry and can be nonmonotonic or rewritten.",
                           "The clone was taken after the window closed, so a repository whose last commit precedes the window end is observed inactive through collection.",
                           "Cached metadata fields were retrieved after the window and describe the retrieval date, not the anchor.",
                           "No author, committer or message field is read."])
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", choices=("treated", "control", "both"), default="both")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    records = []
    inputs = ["study.toml"]
    if args.groups in ("treated", "both"):
        records += treated_records(root)
        inputs.append("reports/entity_identity_summary.json")
    if args.groups in ("control", "both"):
        records += control_records(root)
        inputs.append("data/derived/control-v1/matched_controls.json")
    summary = write(root, collect(root, records, config), config, inputs)
    print(json.dumps(dict(counts=summary["counts"]), indent=2))


if __name__ == "__main__":
    main()
