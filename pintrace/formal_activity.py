"""Repository maintenance activity as context for fixed-period persistence (RQ3), independent of upstream releases.

The upstream-opportunity layer reaches few repositories (the same-repository primary comparison for observed pins still
covers one). Offline commit-timestamp proxies from ``maintenance_activity`` exist for every formal repository, so this
module asks two questions that need no release opportunity:

* Do fixed periods in repositories that were busy before the incident end more often than those in quiet ones?
  Pre-incident commit volume is the stratifier; it is measured before every period starts, never after.
* How much of the persistence without an observed exit belongs to repositories with no commits in the last 90 or
  180 days, where a pin can survive simply because nobody touched the file? Both right-censoring kinds count:
  ``study_window_censored`` requires the observed ancestry to reach the window end, so a repository that went quiet
  earlier can only contribute ``observable_ancestry_censored`` periods, and counting the first kind alone would make
  dormancy zero by construction.

Rates are known exits per 100 episode-years of observed exposure. Unknown boundaries are bounded rather than
resolved: the lower rate counts known exits only; the upper rate also counts unknown interruptions as exits. Intervals
resample whole repositories with the seed and draw rule of the main batch. Nothing here is causal and no model is opened.
"""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .formal_common import WORKING_MANIFEST, annotate, sha256, write_json
from .formal_extensions import Clustered, latest_working_run
from .formal_rq3 import CENSORS, EXITS, KINDS, UNKNOWN, _duration

RULE_VERSION = "formal-activity-v1"
TIERS = ("low", "mid", "high")
YEAR_DAYS = 365.25
PER = 100
ACTIVITY = "data/derived/maintenance-activity-v1/"
OUTPUT = "data/derived/formal-stats-v4/runs"
EPISODES = "rq3_episode_records.csv"


def truthy(value):
    return {"True": True, "False": False}.get(value) if isinstance(value, str) else value


def tier_cuts(values):
    """Upper bounds of the low and mid tiers: the last value of the lowest and of the middle third.

    A value equal to a cut belongs to the lower tier, so ties cannot straddle tiers; with ties the tiers differ in size.
    """
    ordered = sorted(values)
    count = len(ordered)
    if count < 3:
        raise ValueError("At least three repositories are needed to form activity tiers")
    return ordered[count // 3 - 1], ordered[2 * count // 3 - 1]


def tier_of(commits, cuts):
    return "low" if commits <= cuts[0] else "mid" if commits <= cuts[1] else "high"


def join_activity(episodes, activity):
    """Duration-eligible formal episodes with the repository's activity row; a missing row is an error, not a skip."""
    rows = []
    for row in episodes:
        if truthy(row["formal"]) is not True or truthy(row["duration_eligible"]) is not True:
            continue
        repository = str(row["repository_id"])
        if repository not in activity:
            raise ValueError("No maintenance-activity row for repository " + repository)
        rows.append(dict(row, repository_id=repository, activity=activity[repository], duration=_duration(row)))
    if any(row["duration"] is None or row["duration"] <= 0 for row in rows):
        raise ValueError("A duration-eligible episode lacks a positive observed duration")
    return rows


def columns(rows, tiers):
    """Per-repository sufficient statistics for every (tier, kind) cell and for each pooled tier."""
    totals = {}

    def add(prefix, repository, row):
        cell = totals.setdefault(prefix, {})
        values = cell.setdefault(repository, dict(episodes=0.0, known=0.0, unknown=0.0, days=0.0))
        values["episodes"] += 1
        values["known"] += row["exit_kind"] in EXITS
        values["unknown"] += row["exit_kind"] in UNKNOWN
        values["days"] += row["duration"]

    for row in rows:
        tier = tiers[row["repository_id"]]
        add(f"{tier}.all", row["repository_id"], row)
        add(f"{tier}.{row['episode_kind']}", row["repository_id"], row)
    return totals


def rate(known, unknown, days):
    return (known + unknown) / (days / YEAR_DAYS) * PER


def summarize(rows, activity, cuts=None):
    """Pure function of the joined rows; returns the report body and leaves file handling to ``run``."""
    repositories = sorted({row["repository_id"] for row in rows})
    pre = {r: int(float(activity[r]["commits_pre_180d"])) for r in repositories}
    cuts = cuts or tier_cuts(pre.values())
    tiers = {r: tier_of(pre[r], cuts) for r in repositories}
    cluster = Clustered(repositories)
    cells = columns(rows, tiers)
    for prefix, per_repository in cells.items():
        for field in ("episodes", "known", "unknown", "days"):
            cluster.add(f"{prefix}.{field}", {r: v[field] for r, v in per_repository.items()})
    out = dict(cuts=dict(low_max=cuts[0], mid_max=cuts[1]), repositories_by_tier={t: sum(v == t for v in tiers.values()) for t in TIERS},
               rates={}, rate_ratios={})
    for tier in TIERS:
        for scope in ("all", *KINDS):
            prefix = f"{tier}.{scope}"
            if prefix not in cells:
                continue
            total = {k: sum(v[k] for v in cells[prefix].values()) for k in ("episodes", "known", "unknown", "days")}
            known = annotate(cluster.evaluate(lambda c, p=prefix: (c[p + ".known"] / (c[p + ".days"] / YEAR_DAYS)) * PER, len(cells[prefix])))
            upper = annotate(cluster.evaluate(lambda c, p=prefix: ((c[p + ".known"] + c[p + ".unknown"]) / (c[p + ".days"] / YEAR_DAYS)) * PER, len(cells[prefix])))
            out["rates"][prefix] = dict(episodes=int(total["episodes"]), repositories=len(cells[prefix]), known_exits=int(total["known"]),
                                        unknown_interruptions=int(total["unknown"]), exposure_years=total["days"] / YEAR_DAYS,
                                        known_exit_rate_per_100_years=known, known_or_unknown_exit_rate_per_100_years=upper)
    for scope in ("all", *KINDS):
        low, high = f"low.{scope}", f"high.{scope}"
        if low in cells and high in cells:
            for label, extra in (("known", False), ("known_or_unknown", True)):
                def ratio(c, a=low, b=high, extra=extra):
                    top = lambda p: (c[p + ".known"] + (c[p + ".unknown"] if extra else 0)) / c[p + ".days"]
                    return top(a) / top(b)
                out["rate_ratios"][f"low_vs_high.{scope}.{label}"] = annotate(cluster.evaluate(ratio, len(set(cells[low]) | set(cells[high]))))
    return out, tiers


def dormancy(rows, activity):
    """Share of right-censored periods (no observed exit) in repositories silent for the last 90 / 180 days."""
    persisting = [row for row in rows if row["exit_kind"] in CENSORS]
    result = dict(persisting_episodes=len(persisting), persisting_repositories=len({r["repository_id"] for r in persisting}),
                  censoring_kinds=list(CENSORS), by_exit_kind={})
    for kind in CENSORS:
        group = [row for row in persisting if row["exit_kind"] == kind]
        result["by_exit_kind"][kind] = dict(episodes=len(group), **{
            f"dormant_{days}d": sum(truthy(activity[row["repository_id"]][f"no_commit_last_{days}d_at_end"]) is True for row in group)
            for days in (90, 180)})
    repositories = sorted({row["repository_id"] for row in persisting})
    cluster = Clustered(repositories)
    unknown = {}
    for days in (90, 180):
        field = f"no_commit_last_{days}d_at_end"
        known, dormant = {}, {}
        for row in persisting:
            flag = truthy(activity[row["repository_id"]][field])
            if flag is None:
                unknown[days] = unknown.get(days, 0) + 1
                continue
            known[row["repository_id"]] = known.get(row["repository_id"], 0.0) + 1
            dormant[row["repository_id"]] = dormant.get(row["repository_id"], 0.0) + bool(flag)
        cluster.add(f"known{days}", known)
        cluster.add(f"dormant{days}", dormant)
        result[f"dormant_{days}d"] = dict(
            episodes_with_known_state=int(sum(known.values())), dormant_episodes=int(sum(dormant.values())),
            episodes_with_unknown_state=unknown.get(days, 0),
            share=annotate(cluster.evaluate(lambda c, d=days: c[f"dormant{d}"] / c[f"known{d}"], len(known))))
    return result


def read_activity(root, treated_ids, control_ids):
    """Activity rows bound to the files and populations they were produced for."""
    root = Path(root)
    summary = json.loads((root / ACTIVITY / "summary.json").read_text(encoding="utf-8"))
    for name, expected in summary["output_sha256"].items():
        if sha256(root / ACTIVITY / name) != expected:
            raise ValueError("Maintenance-activity output changed since it was produced: " + name)
    with (root / ACTIVITY / "activity.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    treated = {row["repository_id"] for row in rows if row["group"] == "treated"}
    control = {row["repository_id"] for row in rows if row["group"] == "control"}
    if treated != {str(i) for i in treated_ids}:
        raise ValueError("Treated activity rows differ from the formal population")
    if control != {str(i) for i in control_ids}:
        raise ValueError("Control activity rows differ from the matched controls")
    return {row["repository_id"]: row for row in rows if row["group"] == "treated"}, summary


def run(root):
    root = Path(root).resolve()
    run_path, run_record = latest_working_run(root)
    manifest = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    if sha256(root / WORKING_MANIFEST) != run_record["freeze_sha256"]:
        raise ValueError("The working-input manifest differs from the completed run")
    episodes_path = run_record["output_directory"] + "/" + EPISODES
    if sha256(root / episodes_path) != run_record["outputs"][episodes_path]:
        raise ValueError("Completed run output changed: " + episodes_path)
    controls = json.loads((root / "data/derived/control-v1/matched_controls.json").read_text(encoding="utf-8"))["controls"]
    activity, summary = read_activity(root, manifest["formal_population"]["formal_eligible_repository_ids"],
                                      [row["repository_id"] for row in controls])
    with (root / episodes_path).open(encoding="utf-8", newline="") as stream:
        rows = join_activity(list(csv.DictReader(stream)), activity)
    body, tiers = summarize(rows, activity)
    result = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), source_run=run_path,
        input_sha256={episodes_path: run_record["outputs"][episodes_path],
                      **{ACTIVITY + name: digest for name, digest in summary["output_sha256"].items()},
                      ACTIVITY + "summary.json": sha256(root / ACTIVITY / "summary.json"),
                      WORKING_MANIFEST: sha256(root / WORKING_MANIFEST)},
        implementation_sha256=sha256(Path(__file__)),
        population=dict(episodes=len(rows), repositories=len({r["repository_id"] for r in rows}),
                        repositories_by_kind={k: len({r["repository_id"] for r in rows if r["episode_kind"] == k}) for k in KINDS}),
        activity_by_pre_incident_tier=body, window_end_dormancy=dormancy(rows, activity),
        checks=dict(treated_rows_equal_formal_population=True, control_rows_equal_matched_controls=True,
                    activity_outputs_match_their_summary=True, every_episode_has_activity=True,
                    tiers_cover_all_repositories=len(tiers) == len({r["repository_id"] for r in rows})),
        provenance_note="The activity summary records the pre-extension identity report hash; the formal and control repository sets it was built for "
                        "were compared to the current sets and are identical, which is the check applied here.",
        limits=["Pre-incident commit volume describes the repository, not the maintenance of the pinned file or the dependency update route.",
                "Unknown boundaries are bounded, not resolved; the upper rate treats every unknown interruption as an exit.",
                "Commit timestamps are committer clocks on first-parent ancestry and can be nonmonotonic.",
                "Dormancy is measured to the window end from a clone taken later; it says the repository was silent, not that its pins were unattended by other means.",
                "Intervals with status zero_degenerate or partial_replicates do not establish absence of a difference.",
                "Descriptive association only; no causal or model-based claim and no closed model is opened."])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / OUTPUT / stamp
    write_json(directory / "formal_activity.json", result)
    target = root / "reports/formal_activity.json"
    target.write_bytes((directory / "formal_activity.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run(Path(__file__).resolve().parents[1])
