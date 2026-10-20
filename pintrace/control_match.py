"""Frequency-match the control cohort to the treated cohort on pre-anchor covariates and report balance.

Covariates (all measured before the 2025-03-14 anchor, from Git and the repository metadata cache):
  activity   first-parent commits in the 180 days before the anchor (quartiles of the treated cohort)
  mutable    share of non-target action references that are not full-SHA pins (none / mixed / all)
  owner      Organization or User
Controls are drawn without replacement from eligible candidates inside each treated cell, in proportion to the treated cell
sizes, with a fixed seed. Balance is reported as standardized mean differences on the matching covariates and on
covariates that are not matched on.
"""

import argparse
import hashlib
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
import tomllib

from .control_baseline import activity, reference_profile
from .measurement import confirmed_records, fixed_history
from .pilot import timestamp

RULE_VERSION = "control-match-v1"
SEED = "pintrace-control-match-20250314-v1"
OUTPUT = "data/derived/control-v1"
TARGET_SIZE = 300
THRESHOLD = 0.1


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mutable_class(share):
    return "none" if share is None else "all_pinned" if share == 0 else "all_mutable" if share == 1 else "mixed"


def owner_type(root, repository):
    """Owner type from the API cache written when the baseline was collected; no new request is made."""
    url = "https://api.github.com/repos/" + repository
    path = Path(root) / "data/cache/api" / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    if not path.exists():
        return None
    return ((json.loads(path.read_text(encoding="utf-8"))["data"].get("owner")) or {}).get("type")


def treated_covariates(root):
    root = Path(root)
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    anchor = timestamp(config["event_anchor"])
    formal = {str(i) for i in json.loads((root / "reports/entity_identity_summary.json").read_text(encoding="utf-8"))["formal_eligible_repository_ids"]}
    rows = []
    for record in confirmed_records(root):
        if record["repository_id"] not in formal:
            continue
        saved = json.loads((root / record["cohort_record_path"]).read_text(encoding="utf-8"))
        rows.append((record, saved["baseline_snapshot"]))
    return rows, anchor


def build_treated(root):
    rows, anchor = treated_covariates(root)
    cache = Path(root) / OUTPUT / "treated_covariates.json"
    result = []
    for record, state in rows:
        history = fixed_history(Path(root) / record["git_directory"], record["observed_head"])
        profile = reference_profile(state["references"])
        result.append(dict(repository_id=record["repository_id"], repository=record["repository"],
                           git_commits_180d=activity(history, anchor, 180), git_commits_365d=activity(history, anchor, 365),
                           owner_type=owner_type(root, record["repository"]), baseline_workflow_files=len(state["files"]), **profile))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def quartile_edges(values):
    ordered = sorted(values)
    return [ordered[len(ordered) * k // 4] for k in (1, 2, 3)]


def cell(row, edges):
    return (sum(row["git_commits_180d"] >= edge for edge in edges), mutable_class(row["mutable_share"]), row["owner_type"] or "unknown")


def smd(treated, control):
    """Standardized mean difference with the pooled standard deviation."""
    def moments(values):
        mean = sum(values) / len(values)
        return mean, sum((v - mean) ** 2 for v in values) / (len(values) - 1) if len(values) > 1 else 0.0
    (m1, v1), (m0, v0) = moments(treated), moments(control)
    pooled = math.sqrt((v1 + v0) / 2)
    return 0.0 if pooled == 0 else (m1 - m0) / pooled


def balance(treated, controls):
    covariates = {
        "log1p_commits_180d": lambda r: math.log1p(r["git_commits_180d"]),
        "log1p_commits_365d": lambda r: math.log1p(r["git_commits_365d"]),
        "mutable_share": lambda r: r["mutable_share"] if r["mutable_share"] is not None else 0.0,
        "organization": lambda r: float(r["owner_type"] == "Organization"),
        "log1p_action_references": lambda r: math.log1p(r["action_references"]),
        "log1p_workflow_files": lambda r: math.log1p(r["baseline_workflow_files"]),
    }
    return {name: smd([f(r) for r in treated], [f(r) for r in controls]) for name, f in covariates.items()}


DISTANCE_FIELDS = ("git_commits_180d", "baseline_workflow_files", "action_references")


def distance_scales(treated):
    """Standard deviation of each log1p distance covariate in the treated cohort; a constant covariate gets scale 1."""
    scales = []
    for field in DISTANCE_FIELDS:
        values = [math.log1p(row[field]) for row in treated]
        mean = sum(values) / len(values)
        deviation = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1)) if len(values) > 1 else 0.0
        scales.append(deviation or 1.0)
    return scales


def distance(left, right, scales):
    return math.sqrt(sum(((math.log1p(left[f]) - math.log1p(right[f])) / s) ** 2 for f, s in zip(DISTANCE_FIELDS, scales)))


def select(treated, pool, size, seed=SEED):
    edges = quartile_edges([r["git_commits_180d"] for r in treated])
    wanted, available = {}, {}
    for row in treated:
        wanted[cell(row, edges)] = wanted.get(cell(row, edges), 0) + 1
    for row in pool:
        available.setdefault(cell(row, edges), []).append(row)
    rng = random.Random(seed)
    scale = size / len(treated)
    by_cell = {}
    for row in treated:
        by_cell.setdefault(cell(row, edges), []).append(row)
    spread = distance_scales(treated)
    chosen, shortfall = [], {}
    for key in sorted(wanted, key=str):
        quota = max(1, round(wanted[key] * scale)) if wanted[key] else 0
        candidates = sorted(available.get(key, []), key=lambda r: r["repository_id"])
        # Quartile cells are unbounded at the ends, so a random draw inside a cell can sit far from the treated repository.
        # Each sampled treated repository takes the nearest unused candidate on standardized log activity, workflow files
        # and action references, so size covariates are balanced as well as the cell variables.
        anchors = rng.sample(sorted(by_cell[key], key=lambda r: r["repository_id"]), min(quota, len(by_cell[key])))
        unused = list(candidates)
        taken = 0
        for anchor in sorted(anchors, key=lambda r: (math.log1p(r["git_commits_180d"]), r["repository_id"])):
            if not unused:
                break
            nearest = min(unused, key=lambda r: (distance(anchor, r, spread), r["repository_id"]))
            unused.remove(nearest)
            chosen.append(nearest)
            taken += 1
        if taken < quota:
            shortfall["|".join(map(str, key))] = quota - taken
    return sorted(chosen, key=lambda r: int(r["repository_id"])), edges, wanted, shortfall


def load_pool(root):
    pool = []
    for path in sorted((Path(root) / OUTPUT / "baselines").glob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))["summary"]
        if row.get("status") == "control_eligible" and row.get("git_commits_180d") is not None:
            row["owner_type"] = row.get("owner_type")
            pool.append(row)
    unique = {}
    for row in pool:
        unique.setdefault(row["repository_id"], row)
    return list(unique.values())


def run(root, size=TARGET_SIZE):
    root = Path(root)
    treated = build_treated(root)
    treated_ids = {r["repository_id"] for r in treated}
    pool = [r for r in load_pool(root) if r["repository_id"] not in treated_ids]
    chosen, edges, wanted, shortfall = select(treated, pool, size)
    before, after = balance(treated, pool), balance(treated, chosen)
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), seed=SEED,
                  treated_repositories=len(treated), eligible_pool=len(pool), selected_controls=len(chosen), requested_controls=size,
                  activity_quartile_edges_180d=edges, treated_cells={"|".join(map(str, k)): v for k, v in sorted(wanted.items(), key=str)},
                  shortfall_by_cell=shortfall, smd_threshold=THRESHOLD,
                  smd_pool_before_selection=before, smd_after_selection=after,
                  balance_passed=all(abs(v) < THRESHOLD for v in after.values()),
                  controls=[dict(repository_id=r["repository_id"], repository=r["repository"], cell="|".join(map(str, cell(r, edges)))) for r in chosen],
                  input_sha256={"data/derived/control-v1/treated_covariates.json": sha256(root / OUTPUT / "treated_covariates.json"),
                                "data/derived/control-v1/candidate_pool.csv": sha256(root / OUTPUT / "candidate_pool.csv"),
                                "study.toml": sha256(root / "study.toml")},
                  limits=["Frequency matching balances the listed pre-anchor covariates only; unmeasured differences remain.",
                          "Controls depend on no target-action workflow at the baseline, but may adopt it later; that is recorded, not excluded.",
                          "Owner type is read from the metadata cache at collection time, which is after the anchor."])
    (root / OUTPUT / "matched_controls.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=TARGET_SIZE)
    parser.add_argument("--treated-only", action="store_true")
    args = parser.parse_args()
    base = Path(__file__).resolve().parents[1]
    if args.treated_only:
        rows = build_treated(base)
        print(json.dumps(dict(treated=len(rows)), indent=2))
    else:
        out = run(base, args.size)
        print(json.dumps({k: out[k] for k in ("treated_repositories", "eligible_pool", "selected_controls", "shortfall_by_cell", "smd_pool_before_selection", "smd_after_selection", "balance_passed")}, indent=2))
