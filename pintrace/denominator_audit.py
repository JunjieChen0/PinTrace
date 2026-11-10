"""Denominator audit for the revision: where each reported population comes from and why units drop out.

Read-only over existing results. It answers four requests of the review:
  - why only part of the 455 repositories enter the 18-month stock estimate, with a carry-forward sensitivity;
  - which Actions contribute to the fixed periods, and how the target maintenance subset relates to them;
  - that every Table 5 dimension sums to the grade totals once all categories are listed;
  - one table of analysis populations.

Usage: python -m pintrace.denominator_audit
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import formal_extensions as fx
from . import post_review as pr
from .formal_common import WORKING_MANIFEST, sha256, write_json

RULE_VERSION = "denominator-audit-v1"
BATCH = "denominator-audit-v1"
DAY = 86400
CROSS_TABS = ("entry_configuration_coverage", "entry_comment_kind", "entry_comment_identity_state")


# ---------------------------------------------------------------- stock attrition

def stock_status(record, offset):
    """Why a repository does or does not enter the stock at ``offset`` days after the anchor."""
    when = record["anchor"] + offset * DAY
    if not record["clock_aligned"]:
        return "excluded_clock_unaligned"
    if when > pr._observed_end(record):
        return "no_default_branch_commit_after_last_observed"
    state = pr._snapshot_at(record, when)
    if state is None:
        return "no_complete_snapshot"
    total, _ = pr._other_counts(state)
    return "counted" if total else "no_other_action_references"


def stock_attrition(records, ids, offsets=pr.KEY_OFFSETS):
    included = [r for r in records if r["repository_id"] in ids]
    return {str(offset): dict(Counter(stock_status(r, offset) for r in included)) for offset in offsets}


def carried_forward(record):
    """The record with its last observed state held to the window end (a new dict; the input is unchanged)."""
    return dict(record, observed_ancestry_covers_end=True)


def carry_forward_stock(records, ids, observed_at, window_end):
    """Stock series when quiet repositories keep their last state; valid only if every clone postdates the window."""
    early = sorted(rid for rid in ids if observed_at[rid] < window_end)
    if early:
        raise ValueError(f"{len(early)} repositories were observed before the window end; carry-forward is not determinate")
    series = pr.stock_series([carried_forward(r) for r in records], ids)
    return {str(row["offset_days"]): row for row in series["series"] if row["offset_days"] in pr.KEY_OFFSETS}


def observation_times(root, ids):
    """Earliest record-building time per repository from the cohort baselines (Unix seconds, UTC)."""
    times = {}
    for path in sorted((root / "data/derived/cohort-v1/baselines").glob("*.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
        rid = str(summary.get("repository_id"))
        if rid in ids:
            stamp = datetime.fromisoformat(summary["checked_at_utc"]).timestamp()
            times[rid] = min(times.get(rid, stamp), stamp)
    missing = ids - set(times)
    if missing:
        raise ValueError(f"{len(missing)} formal repositories have no cohort record")
    return times


# ---------------------------------------------------------------- periods and Table 5

def periods(persistence, maintenance, routes):
    identity = persistence["populations"]["formal"]["identity"]
    kinds = {kind: value["identity"] for kind, value in persistence["populations"]["formal"]["by_kind"].items()}
    target = maintenance["populations"]["formal"]["target"]
    if sum(k["episodes"] for k in kinds.values()) != identity["episodes"]:
        raise ValueError("Period kinds do not sum to the identity total")
    return dict(
        scope="Fixed periods are built only for lineages of the compromised Action (pintrace/risksets.py, ref['action'] == target).",
        identity=dict(episodes=identity["episodes"], repositories=identity["repositories"]),
        by_kind={kind: dict(episodes=v["episodes"], repositories=v["repositories"]) for kind, v in kinds.items()},
        maintenance_target=dict(episodes=target["counts"]["episodes"], repositories=target["counts"]["repositories"],
                                entries=target["counts"]["entries"], opportunities=target["counts"]["opportunity_ids"],
                                by_grade={g: v["counts"]["entries"] for g, v in target["by_grade"].items()},
                                periods_without_graded_opportunity=identity["episodes"] - target["counts"]["episodes"]),
        another_sha=dict(changed_entries=routes["changed_entries"], entries=routes["entries"],
                         distinct_changes=routes["distinct_changes"]["changes"],
                         changes_by_role=routes["distinct_changes"]["by_role"],
                         note="Covers target and comparison lineages in the same repositories, so it exceeds the target-only subset."))


def table5(cross_tabs, maintenance):
    """Every category of every dimension, with per-grade totals checked against the subset's grade counts."""
    grades = {g: v["counts"]["entries"] for g, v in maintenance["populations"]["formal"]["target"]["by_grade"].items()}
    output = {}
    for field in CROSS_TABS:
        rows = cross_tabs[field]
        totals = Counter()
        for row in rows:
            totals[row["grade"]] += row["entries"]
        if dict(totals) != grades:
            raise ValueError(f"{field} sums to {dict(totals)}, not {grades}")
        output[field] = dict(rows=[dict(category=r["category"], grade=r["grade"], entries=r["entries"],
                                        repositories=r["repositories"]) for r in rows], totals=dict(totals))
    return output


# ---------------------------------------------------------------- populations

def populations(reports):
    manifest, rq2, ext, persistence, maintenance = (reports[k] for k in ("manifest", "rq2", "ext", "persistence", "maintenance"))
    population = manifest["formal_population"]
    existing = rq2["formal"]["existing"]
    identity = persistence["populations"]["formal"]["identity"]
    target = maintenance["populations"]["formal"]["target"]["counts"]
    return [
        dict(analysis="Confirmed measurements", unit="repository", n=manifest["raw_repository_count"], note="before identity exclusions"),
        dict(analysis="Formal population (RQ1)", unit="repository", n=manifest["formal_repository_count"],
             excluded=len(population["excluded_confirmed_repository_ids"]), note="identity exclusions"),
        dict(analysis="RQ2 existing references", unit="repository", n=existing["repositories_with_baseline_references"],
             excluded=existing["repositories_without_baseline_references"], note="no other-Action reference at baseline"),
        dict(analysis="RQ2 baseline references", unit="reference", n=existing["baseline_references"]),
        dict(analysis="Interrupted series", unit="repository", n=ext["interrupted_series"]["repositories"],
             excluded=len(ext["interrupted_series"]["excluded_clock_unaligned"]), note="clock not alignable to the anchor"),
        dict(analysis="Fixed periods (RQ3 persistence)", unit="period", n=identity["episodes"],
             repositories=identity["repositories"], note="compromised Action only"),
        dict(analysis="Target maintenance subset", unit="opportunity entry", n=target["entries"], periods=target["episodes"],
             repositories=target["repositories"], note="periods with a grade A or B upstream release opportunity"),
    ]


# ---------------------------------------------------------------- run

def run(root):
    root = Path(root).resolve()
    run_path, run_record = fx.latest_working_run(root)
    results = run_record["output_directory"] + "/"
    bound = fx._verify(root, run_record, WORKING_MANIFEST, results)
    frozen = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    ids = {str(i) for i in frozen["formal_population"]["formal_eligible_repository_ids"]}
    with (root / (results + "rq12_repository_records.jsonl")).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    load = lambda name: json.loads((root / "reports" / name).read_text(encoding="utf-8"))
    reports = dict(manifest=frozen, rq2=load("rq2_summary.json"), ext=load("formal_extensions.json"),
                   persistence=load("rq3_persistence_summary.json"), maintenance=load("rq3_maintenance_results.json"),
                   h1h2=load("h1_h2_summary.json"), post=load("post_review.json"))
    window_ends = {r["window_end"] for r in records if r["repository_id"] in ids}
    if len(window_ends) != 1:
        raise ValueError("Formal records do not share one window end")
    window_end = window_ends.pop()
    observed = observation_times(root, ids)
    reported = {k: v["repositories"] for k, v in reports["post"]["stock_key_offsets"].items()}
    attrition = stock_attrition(records, ids)
    if any(attrition[k].get("counted", 0) != n for k, n in reported.items()):
        raise ValueError("Stock attrition does not reproduce the reported repository counts")
    utc = lambda seconds: datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    result = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
        implementation_sha256=sha256(Path(__file__)), source_run=run_path, input_sha256=bound,
        stock=dict(attrition=attrition, reported_repositories=reported,
                   observation=dict(window_end_utc=utc(window_end), earliest_record_utc=utc(min(observed.values()))),
                   carry_forward=carry_forward_stock(records, ids, observed, window_end),
                   carry_forward_attrition=stock_attrition([carried_forward(r) for r in records], ids),
                   interpretation="The main analysis ends a repository's observation at its last default-branch commit. "
                                  "Every repository was observed after the window end, so a repository with no later commit "
                                  "kept its last state to the window end; the carry-forward series uses that state."),
        periods=periods(reports["persistence"], reports["maintenance"], reports["ext"]["updater_routes"]),
        table5=table5(reports["h1h2"]["maintenance_target_cross_tabs"], reports["maintenance"]),
        populations=populations(reports))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / "data/derived" / BATCH / "runs" / stamp
    write_json(directory / "denominator_audit.json", result)
    target = root / "reports/denominator_audit.json"
    if target.relative_to(root).as_posix() in frozen["input_sha256"]:
        raise ValueError("Attempt to overwrite a frozen input")
    target.write_bytes((directory / "denominator_audit.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[1])
