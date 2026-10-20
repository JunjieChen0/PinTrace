"""Offline control-candidate frame: roster repositories with a pre-anchor workflow history and no target evidence.

The frame is built only from the already verified Zenodo roster and workflow-history table. It uses no author or
message field. Matching on Git-derived covariates happens later, after the candidate pool is cloned.
"""

import argparse
import csv
import gzip
import hashlib
import json
import random
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from .frames import usable_archive_state

RULE_VERSION = "control-frame-v1"
DAY = 86400
RAW = "data/raw/zenodo-17301952"
OUTPUT = "data/derived/control-v1"
SEED = "pintrace-control-20250314-v1"
POOL_SIZE = 1000
ROSTER_INT = ("commits", "branches", "releases", "contributors", "stars", "issues", "pullrequests", "codelines", "size")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_roster(root):
    with gzip.open(Path(root) / RAW / "repositories.csv.gz", "rt", encoding="utf-8", newline="") as source:
        return {row["name"].lower(): row for row in csv.DictReader(source)}


def excluded_names(root):
    """Names that carried target evidence or entered the treated ledger, in any alias spelling."""
    names = set()
    with (Path(root) / "reports/source_frame_17301952.csv").open(encoding="utf-8", newline="") as stream:
        names.update(row["repository_name_key"] for row in csv.DictReader(stream))
    with (Path(root) / "reports/candidate_source_membership.csv").open(encoding="utf-8", newline="") as stream:
        names.update(row["repository"].lower() for row in csv.DictReader(stream))
    return names


def treated_names(root):
    """Lower-cased names of the confirmed treated cohort, used only to place the control pool on the same size scale."""
    names = set()
    for path in sorted((Path(root) / "data/derived/cohort-v1/baselines").glob("*.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
        if summary["status"] == "confirmed":
            names.add(summary["repository"].lower())
    return names


def decile_edges(values):
    ordered = sorted(values)
    return [ordered[min(len(ordered) - 1, len(ordered) * k // 10)] for k in range(1, 10)]


def stratum(value, edges):
    return sum(value >= edge for edge in edges)


def stratified_pool(eligible, roster, edges, pool_size, rng):
    """Equal quotas per treated roster-commit decile; a stratum with too few candidates is reported, not padded."""
    bins = {k: [] for k in range(10)}
    for name in eligible:
        bins[stratum(int(roster[name]["commits"]), edges)].append(name)
    quota, pool, shortfall = pool_size // 10, [], {}
    for k, names in bins.items():
        take = min(quota, len(names))
        if take < quota:
            shortfall[k] = quota - take
        pool.extend(rng.sample(names, take))
    return sorted(pool), {k: len(v) for k, v in bins.items()}, shortfall


def baseline_workflow_profile(rows, anchor, lookback_days):
    """Per repository: live workflow files at the anchor and distinct workflow-touching commits in the lookback."""
    latest, commits, lookback = {}, {}, anchor - lookback_days * DAY
    for row in rows:
        name, stamp = row["repository"].lower(), int(row["committed_date"])
        if stamp >= anchor:
            continue
        if stamp >= lookback:
            commits.setdefault(name, set()).add(row["commit_hash"])
        key = (name, row["uid"])
        order = (stamp, row["commit_hash"])
        previous = latest.get(key)
        if previous is None or order > previous[0]:
            latest[key] = (order, usable_archive_state(row))
    live = {}
    for (name, _), (_, usable) in latest.items():
        if usable:
            live[name] = live.get(name, 0) + 1
    return live, {name: len(values) for name, values in commits.items()}


SUPPLEMENT_SEED = "pintrace-control-20250314-v2"
SUPPLEMENT_SIZE = 1000


def quartile_cell(value, edges):
    return sum(value >= edge for edge in edges)


def quota_sample(eligible, cell_of, cells, pool_size, rng):
    """Equal quota per cell; a cell with too few candidates is reported as a shortfall, never padded from another cell."""
    bins = {cell: [] for cell in cells}
    for name in eligible:
        bins[cell_of(name)].append(name)
    quota, pool, shortfall = pool_size // len(cells), [], {}
    for cell in cells:
        take = min(quota, len(bins[cell]))
        if take < quota:
            shortfall["|".join(map(str, cell))] = quota - take
        pool.extend(rng.sample(sorted(bins[cell]), take))
    return sorted(pool), {"|".join(map(str, cell)): len(bins[cell]) for cell in cells}, shortfall


def build_supplement(root, lookback_days=180, pool_size=SUPPLEMENT_SIZE):
    """Second candidate pool, stratified on roster commits and live workflow files, excluding every name already drawn.

    The first pool was stratified on commits only and held too few repositories with large workflow sets compared with the
    treated cohort (median 7 baseline workflow files against 2 in the frame). The first pool and its baselines are kept.
    """
    root = Path(root)
    study = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    anchor = int(datetime.fromisoformat(study["event_anchor"].replace("Z", "+00:00")).timestamp())
    roster, excluded = read_roster(root), excluded_names(root)
    with (root / OUTPUT / "candidate_pool.csv").open(encoding="utf-8", newline="") as stream:
        first_pool = {row["repository"].lower() for row in csv.DictReader(stream)}
    with gzip.open(root / RAW / "workflows.csv.gz", "rt", encoding="utf-8", newline="") as source:
        live, recent = baseline_workflow_profile(csv.DictReader(source), anchor, lookback_days)
    treated = json.loads((root / OUTPUT / "treated_covariates.json").read_text(encoding="utf-8"))
    file_edges = [sorted(r["baseline_workflow_files"] for r in treated)[len(treated) * k // 4] for k in (1, 2, 3)]
    commit_edges = decile_edges([int(roster[name]["commits"]) for name in treated_names(root) if name in roster])
    quartile_edges = [commit_edges[2], commit_edges[4], commit_edges[6]]
    eligible = sorted(name for name in roster if live.get(name, 0) > 0 and name not in excluded and name not in first_pool)
    cells = [(c, f) for c in range(4) for f in range(4)]
    rng = random.Random(SUPPLEMENT_SEED)
    pool, available, shortfall = quota_sample(
        eligible, lambda name: (quartile_cell(int(roster[name]["commits"]), quartile_edges), quartile_cell(live[name], file_edges)), cells, pool_size, rng)
    rows = [dict(repository=roster[name]["name"], baseline_live_workflow_files=live[name], workflow_commits_lookback=recent.get(name, 0),
                 roster_commit_stratum=stratum(int(roster[name]["commits"]), commit_edges),
                 **{key: roster[name][key] for key in ("language", *ROSTER_INT)}) for name in pool]
    frame = root / OUTPUT / "candidate_pool_v2.csv"
    with frame.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = dict(rule_version=RULE_VERSION + "-supplement", created_at_utc=datetime.now(timezone.utc).isoformat(), seed=SUPPLEMENT_SEED,
                   anchor=anchor, pool_size=len(rows), eligible_after_exclusion=len(eligible), first_pool_names_excluded=len(first_pool),
                   treated_file_quartile_edges=file_edges, roster_commit_quartile_edges=quartile_edges,
                   eligible_by_cell=available, shortfall_by_cell=shortfall, pool_sha256=sha256(frame),
                   input_sha256={name: sha256(root / name) for name in (f"{RAW}/repositories.csv.gz", f"{RAW}/workflows.csv.gz", "study.toml",
                                                                       f"{OUTPUT}/candidate_pool.csv", f"{OUTPUT}/treated_covariates.json")})
    (root / OUTPUT / "candidate_pool_v2_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def build(root, lookback_days=180, pool_size=POOL_SIZE):
    root = Path(root)
    study = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    anchor = int(datetime.fromisoformat(study["event_anchor"].replace("Z", "+00:00")).timestamp())
    roster = read_roster(root)
    excluded = excluded_names(root)
    with gzip.open(root / RAW / "workflows.csv.gz", "rt", encoding="utf-8", newline="") as source:
        live, recent = baseline_workflow_profile(csv.DictReader(source), anchor, lookback_days)
    eligible = sorted(name for name in roster if live.get(name, 0) > 0 and name not in excluded)
    rng = random.Random(SEED)
    edges = decile_edges([int(roster[name]["commits"]) for name in treated_names(root) if name in roster])
    pool, available, shortfall = stratified_pool(eligible, roster, edges, pool_size, rng)
    rows = [dict(repository=roster[name]["name"], baseline_live_workflow_files=live[name],
                 workflow_commits_lookback=recent.get(name, 0), roster_commit_stratum=stratum(int(roster[name]["commits"]), edges),
                 **{key: roster[name][key] for key in ("language", *ROSTER_INT)})
            for name in pool]
    directory = root / OUTPUT
    directory.mkdir(parents=True, exist_ok=True)
    frame = directory / "candidate_pool.csv"
    with frame.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), seed=SEED, anchor=anchor,
                   lookback_days=lookback_days, roster_size=len(roster), roster_with_live_pre_anchor_workflows=sum(v > 0 for v in live.values()),
                   excluded_names=len(excluded), eligible_after_exclusion=len(eligible), pool_size=len(rows),
                   treated_roster_commit_decile_edges=edges, eligible_by_stratum=available, shortfall_by_stratum=shortfall,
                   input_sha256={name: sha256(root / name) for name in (f"{RAW}/repositories.csv.gz", f"{RAW}/workflows.csv.gz",
                                                                       "reports/source_frame_17301952.csv", "reports/candidate_source_membership.csv", "study.toml")},
                   pool_sha256=sha256(frame),
                   limits=["Eligibility uses archive evidence only; the Git baseline later confirms the target is absent.",
                           "Name keys are not stable identities; the Git/API step records the repository ID.",
                           "No author or message field is used or written."])
    (directory / "candidate_pool_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-size", type=int)
    parser.add_argument("--supplement", action="store_true", help="draw the second pool stratified on commits and workflow files")
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    if args.supplement:
        print(json.dumps(build_supplement(base, pool_size=args.pool_size or SUPPLEMENT_SIZE), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(build(base, pool_size=args.pool_size or POOL_SIZE), indent=2, ensure_ascii=False))
