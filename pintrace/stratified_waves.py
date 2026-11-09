"""Stratified sensitivity of the two conversion waves (RQ2) within the formal cohort.

The coverage audit shows that the cohort is more active, more often organizational and more often pinned at baseline
than other pre-incident dependents. This module repeats the headline wave ratios inside strata of those pre-incident
covariates, using the same records and the same repository-clustered bootstrap as post_review (owner_clustered).
The strata were fixed before any stratified result was computed; a ratio whose pre-incident rate is zero is reported
as undefined rather than imputed.

Usage: python -m pintrace.stratified_waves
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import formal_extensions as fx
from . import post_review as pr
from .formal_common import WORKING_MANIFEST, sha256, write_json

RULE_VERSION = "stratified-waves-v1"
BATCH = "stratified-waves-v1"
COVARIATES = "data/derived/control-v1/treated_covariates.json"
BASELINES = "data/derived/cohort-v1/baselines"
PERIODS = ("pre", "post_0_30", "post_360_540")
STATISTICS = ("rr_m1", "rr_m1318", "share_ratio_m1", "share_ratio_m1318")


def target_all_sha(row):
    return row.get("target_full_sha", 0) > 0 and row.get("target_mutable", 0) == 0


def strata(rows):
    """Stratum name -> repository IDs, from pre-incident covariates of the formal cohort. Each stratum has its
    complement, so that every split partitions the cohort."""
    activity = sorted(r["git_commits_180d"] for r in rows)
    q1, median = statistics.quantiles(activity, n=4)[0], statistics.median(activity)
    split = lambda test: ({r["repository_id"] for r in rows if test(r)}, {r["repository_id"] for r in rows if not test(r)})
    output = {"all": {r["repository_id"] for r in rows}}
    for name, complement, test in (
            ("activity_q1_or_less", "activity_above_q1", lambda r: r["git_commits_180d"] <= q1),
            ("activity_median_or_less", "activity_above_median", lambda r: r["git_commits_180d"] <= median),
            ("user_owned", "organization_owned", lambda r: r.get("owner_type") == "User"),
            ("target_not_all_sha", "target_all_sha", lambda r: not target_all_sha(r))):
        output[name], output[complement] = split(test)
    return output, dict(activity_q1=q1, activity_median=median)


def describe(records, ids):
    """Counts and exposure in the three periods behind the ratios, plus the repository-clustered ratios."""
    periods = pr.conversion_periods(records, ids)["periods"]
    with np.errstate(divide="ignore", invalid="ignore"):  # replicates with no pre-incident conversion are dropped
        clustered = pr.owner_clustered(records, ids, None)["other.repository"]
    # Missing when every replicate was undefined; non-finite when the stratum's own pre-incident rate is zero.
    ratios = {key: clustered[key] if key in clustered and math.isfinite(clustered[key]["estimate"]) else None
              for key in STATISTICS}
    return dict(repositories=len(ids),
                periods={p: dict(conversions=periods[p]["conversions"], exposure=periods[p]["exposure"]) for p in PERIODS},
                ratios=ratios,
                undefined=[key for key, value in ratios.items() if value is None])


def covariate_rows(root, formal):
    """Formal repositories with activity, owner type and baseline target references."""
    covariates = {str(r["repository_id"]): r for r in json.loads((root / COVARIATES).read_text(encoding="utf-8"))}
    baselines = {}
    for path in sorted((root / BASELINES).glob("*.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
        if summary.get("repository_id") in formal and summary["status"] == "confirmed":
            baselines.setdefault(summary["repository_id"], summary)
    if set(covariates) != formal or set(baselines) != formal:
        raise ValueError("Covariates or baselines do not cover the formal cohort exactly")
    return [dict(covariates[i], repository_id=i, target_full_sha=baselines[i].get("target_full_sha", 0),
                 target_mutable=baselines[i].get("target_mutable", 0)) for i in sorted(formal, key=int)]


def check_against_published(result, published):
    """The unstratified estimates must equal those in reports/post_review.json."""
    for key in ("rr_m1", "rr_m1318", "share_ratio_m1318"):
        mine, theirs = result["ratios"][key]["estimate"], published["clusters"]["other.repository"][key]["estimate"]
        if abs(mine - theirs) > 1e-9:
            raise ValueError(f"Unstratified {key} {mine} differs from the published {theirs}")


def run(root):
    root = Path(root).resolve()
    run_path, run_record = fx.latest_working_run(root)
    results = run_record["output_directory"] + "/"
    bound = fx._verify(root, run_record, WORKING_MANIFEST, results)
    frozen = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    formal = {str(i) for i in frozen["formal_population"]["formal_eligible_repository_ids"]}
    with (root / (results + "rq12_repository_records.jsonl")).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    groups, cuts = strata(covariate_rows(root, formal))
    output = {name: describe(records, ids) for name, ids in groups.items()}
    check_against_published(output["all"], json.loads((root / "reports/post_review.json").read_text(encoding="utf-8")))
    inputs = dict(bound, **{COVARIATES: sha256(root / COVARIATES), "reports/post_review.json": sha256(root / "reports/post_review.json")},
                  **{p.relative_to(root).as_posix(): sha256(p) for p in sorted((root / BASELINES).glob("*.json"))})
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), source_run=run_path,
                  cuts=cuts, strata=output, input_sha256=inputs,
                  implementation_sha256={p: sha256(root / p) for p in ("pintrace/stratified_waves.py", "pintrace/post_review.py",
                                                                       "pintrace/formal_extensions.py")},
                  interpretation="Ratios use the other-Action conversion rate and the repository share with a conversion, "
                                 "against each stratum's own pre-incident period; intervals resample repositories.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / "data/derived" / BATCH / "runs" / stamp
    write_json(directory / "stratified_waves.json", result)
    target = root / "reports/stratified_waves.json"
    if target.relative_to(root).as_posix() in frozen["input_sha256"]:
        raise ValueError("Attempt to overwrite a frozen input")
    target.write_bytes((directory / "stratified_waves.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[1])
