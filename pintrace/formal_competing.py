"""Competing-risk cumulative incidence of fixed-period exits (RQ3), non-parametric and descriptive.

A fixed period ends in one of several ways, and ending one way removes the period from risk of the others. The plain
observed fractions in ``rq3_persistence_summary`` keep the initial denominator and are not censor-adjusted. Here the
Aalen-Johansen estimator gives the cumulative incidence of each cause with right censoring handled:

* ``unpin``    fixed -> mutable reference
* ``remove``   fixed -> reference removed
* ``replace``  fixed -> replaced by another action
* ``unknown``  unknown boundary or ambiguous transition (an interruption whose cause cannot be classified)

A SHA-to-SHA update does not end a period (the lineage stays fixed), so there is no fixed -> updated exit to count.
Censoring is ``study_window_censored`` or ``observable_ancestry_censored``. Unknown interruptions are reported as a
fourth competing cause, so the three known causes are lower bounds; the sensitivity series treat unknown as censored
(an upper-leaning reading) and as an exit (the any-exit upper bound).

Intervals resample whole repositories with the seed and draw matrix of the main batch (all formal repositories), so
they are comparable with the existing trajectories. No regression is fitted: the Fine-Gray and Cox models stay in the
closed list of the input authority regardless of sample size, and strata below the planning threshold are described
with their precision rather than modelled.
"""

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path

from .formal_activity import truthy
from .formal_common import REPLICATES, SEED, WORKING_MANIFEST, annotate, sha256, write_json
from .formal_extensions import Clustered, latest_working_run
from .formal_rq3 import CENSORS, EXITS, KINDS, UNKNOWN, _duration

RULE_VERSION = "formal-competing-v1"
HORIZONS = (30, 90, 180, 365)
THRESHOLD = 150  # planned minimum number of episodes before any model would be considered
CAUSES = (*EXITS, "unknown")
CODES = {**{kind: 0 for kind in CENSORS}, **{kind: number for number, kind in enumerate(EXITS, 1)}, **{kind: 4 for kind in UNKNOWN}}
OUTPUT = "data/derived/formal-stats-v4/runs"
EPISODES = "rq3_episode_records.csv"
SPARSE_AT_RISK = 10


def cumulative_incidence(times, causes, weights, grid, n_causes):
    """Weighted Aalen-Johansen cumulative incidence at ``grid``.

    ``causes`` is 0 for censored and 1..n_causes for events; a period censored at the time of an event stays at risk
    for that event. ``weights`` is (replicates, episodes); a single row of ones gives the point estimate. Returns the
    incidence array (replicates, causes, grid) and a (replicates, grid) mask of replicates with anyone still observed
    at the horizon (otherwise the incidence there is not estimable).
    """
    import numpy as np
    times, causes = np.asarray(times, dtype=float), np.asarray(causes, dtype=int)
    weights = np.atleast_2d(np.asarray(weights, dtype=float))
    grid = np.asarray(grid, dtype=float)
    replicates = weights.shape[0]
    output = np.zeros((replicates, n_causes, len(grid)))
    valid = (weights @ (times[:, None] >= grid[None, :])) > 0
    event_times = np.unique(times[causes > 0])
    if not event_times.size:
        return output, valid
    risk = weights @ (times[:, None] >= event_times[None, :])
    same = times[:, None] == event_times[None, :]
    events = np.stack([weights @ (same & (causes[:, None] == cause)) for cause in range(1, n_causes + 1)], axis=1)
    hazard = np.divide(events.sum(axis=1), risk, out=np.zeros_like(risk), where=risk > 0)
    survival_before = np.concatenate([np.ones((replicates, 1)), np.cumprod(1 - hazard, axis=1)[:, :-1]], axis=1)
    steps = np.divide(events, risk[:, None, :], out=np.zeros_like(events), where=risk[:, None, :] > 0)
    cumulative = np.cumsum(survival_before[:, None, :] * steps, axis=2)
    position = np.searchsorted(event_times, grid, side="right") - 1
    for column, last in enumerate(position):
        if last >= 0:
            output[:, :, column] = cumulative[:, :, last]
    return output, valid


def eligible_rows(episodes):
    """Formal, duration-eligible episodes with a positive duration and a known exit kind."""
    rows = []
    for row in episodes:
        if truthy(row["formal"]) is not True or truthy(row["duration_eligible"]) is not True:
            continue
        if row["exit_kind"] not in CODES:
            raise ValueError("Unexpected exit kind: " + str(row["exit_kind"]))
        duration = _duration(row)
        if duration is None or duration <= 0:
            raise ValueError("A duration-eligible episode lacks a positive observed duration")
        rows.append(dict(row, repository_id=str(row["repository_id"]), duration=duration))
    return rows


def interval(point, samples, valid, clusters):
    import numpy as np
    kept = np.asarray(samples)[np.asarray(valid)]
    bounds = [float(v) for v in np.quantile(kept, [0.025, 0.975], method="linear")] if len(kept) else [None, None]
    return annotate(dict(estimate=None if point is None else float(point), lower=bounds[0], upper=bounds[1],
                         valid_replicates=int(len(kept)), requested_replicates=REPLICATES, n_clusters=clusters, seed=SEED,
                         interval="percentile"))


def summarize_stratum(rows, cluster, horizons=HORIZONS):
    """Cumulative incidence per cause and horizon for one set of episodes."""
    import numpy as np
    if not rows:
        return dict(episodes=0, repositories=0)
    times = np.array([row["duration"] for row in rows])
    primary = np.array([CODES[row["exit_kind"]] for row in rows])
    unknown_censored = np.where(primary == 4, 0, primary)
    position = {identity: index for index, identity in enumerate(cluster.ids)}
    weights = cluster.matrix[:, [position[row["repository_id"]] for row in rows]].astype(float)
    ones = np.ones((1, len(rows)))
    point4, point_ok = cumulative_incidence(times, primary, ones, horizons, 4)
    draws4, valid = cumulative_incidence(times, primary, weights, horizons, 4)
    point3, _ = cumulative_incidence(times, unknown_censored, ones, horizons, 3)
    draws3, _ = cumulative_incidence(times, unknown_censored, weights, horizons, 3)
    repositories = len({row["repository_id"] for row in rows})
    series = {name: (point4[0, number], draws4[:, number, :]) for number, name in enumerate(CAUSES)}
    series["known_exit_unknown_competing"] = (point4[0, :3].sum(axis=0), draws4[:, :3, :].sum(axis=1))
    series["known_exit_unknown_censored"] = (point3[0].sum(axis=0), draws3.sum(axis=1))
    series["any_exit_including_unknown"] = (point4[0].sum(axis=0), draws4.sum(axis=1))
    result = dict(episodes=len(rows), repositories=repositories, meets_planning_threshold=len(rows) >= THRESHOLD,
                  planning_threshold=THRESHOLD,
                  cause_counts={cause: int(sum(CODES[row["exit_kind"]] == number for row in rows)) for number, cause in enumerate(CAUSES, 1)},
                  censored=int(sum(CODES[row["exit_kind"]] == 0 for row in rows)), horizons={})
    for column, horizon in enumerate(horizons):
        at_risk = int((times >= horizon).sum())
        estimable = bool(point_ok[0, column])
        block = dict(at_risk=at_risk, sparse_tail=at_risk < SPARSE_AT_RISK,
                     events_through_horizon={cause: int(((times <= horizon) & (primary == number)).sum()) for number, cause in enumerate(CAUSES, 1)},
                     naive_known_exit_fraction=float(((times <= horizon) & np.isin(primary, (1, 2, 3))).mean()), cumulative_incidence={})
        for name, (point, draws) in series.items():
            block["cumulative_incidence"][name] = interval(point[column] if estimable else None, draws[:, column], valid[:, column], repositories)
        result["horizons"][str(horizon)] = block
    return result


def _exit_counts(rows):
    counts = {}
    for row in rows:
        counts[row["exit_kind"]] = counts.get(row["exit_kind"], 0) + 1
    return dict(sorted(counts.items()))


def run(root):
    root = Path(root).resolve()
    run_path, run_record = latest_working_run(root)
    manifest = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    if sha256(root / WORKING_MANIFEST) != run_record["freeze_sha256"]:
        raise ValueError("The working-input manifest differs from the completed run")
    episodes_path = run_record["output_directory"] + "/" + EPISODES
    if sha256(root / episodes_path) != run_record["outputs"][episodes_path]:
        raise ValueError("Completed run output changed: " + episodes_path)
    followup_path = "reports/maintenance_followup_audit.json"
    if sha256(root / followup_path) != manifest["input_sha256"][followup_path]:
        raise ValueError("The follow-up audit is not the one the working-input manifest binds")
    followup = json.loads((root / followup_path).read_text(encoding="utf-8"))
    persistence_path = run_record["output_directory"] + "/rq3_persistence_summary.json"
    if sha256(root / persistence_path) != run_record["outputs"][persistence_path]:
        raise ValueError("Completed run output changed: " + persistence_path)
    persistence = json.loads((root / persistence_path).read_text(encoding="utf-8"))["populations"]["formal"]
    with (root / episodes_path).open(encoding="utf-8", newline="") as stream:
        rows = eligible_rows(csv.DictReader(stream))
    formal_ids = [str(i) for i in manifest["formal_population"]["formal_eligible_repository_ids"]]
    cluster = Clustered(formal_ids)
    if {row["repository_id"] for row in rows} - set(formal_ids):
        raise ValueError("An eligible episode belongs to a repository outside the formal population")
    target_ids = set(followup["target_episode_ids"])
    target = [row for row in rows if row["episode_id"] in target_ids]
    if len(target) != len(target_ids):
        raise ValueError("Some target-layer episodes are not duration-eligible formal episodes")
    strata = {"all_duration_eligible": rows, **{kind: [r for r in rows if r["episode_kind"] == kind] for kind in KINDS},
              "rq3_target_layer": target}
    result = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), source_run=run_path,
        input_sha256={episodes_path: run_record["outputs"][episodes_path], persistence_path: run_record["outputs"][persistence_path],
                      followup_path: manifest["input_sha256"][followup_path],
                      WORKING_MANIFEST: sha256(root / WORKING_MANIFEST)},
        implementation_sha256=sha256(Path(__file__)), horizons_days=list(HORIZONS), cause_definitions=dict(
            unpin="fixed -> mutable reference", remove="fixed -> reference removed", replace="fixed -> replaced by another action",
            unknown="unknown boundary or ambiguous transition", censored=list(CENSORS)),
        resampling=dict(clusters=len(formal_ids), note="whole formal repositories, same seed and draw matrix as the main batch"),
        strata={name: summarize_stratum(group, cluster) for name, group in strata.items()},
        checks=dict(episodes_equal_persistence_duration_layer=len(rows) == persistence["duration"]["episodes"],
                    repositories_equal_persistence_duration_layer=len({r["repository_id"] for r in rows}) == persistence["duration"]["repositories"],
                    exit_counts_equal_persistence_duration_layer=_exit_counts(rows) == persistence["duration"]["exit_counts"],
                    target_layer_fully_eligible=len(target) == len(target_ids)),
        limits=["Non-parametric description. No Fine-Gray or Cox model is fitted; both stay closed in the input authority.",
                "Known causes are lower bounds because unknown interruptions compete with them; the unknown-censored series reads them the other way.",
                "Periods in one repository are not independent; intervals resample whole repositories.",
                "Intervals with status sparse_tail, zero_degenerate or partial_replicates do not establish absence of risk.",
                "Baseline-fixed periods enter at the anchor with an unknown pin age, so their time axis starts at the anchor, not at pinning.",
                "The target layer (periods with an eligible upstream opportunity) is small and selected; it is described, not compared."])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / OUTPUT / stamp
    write_json(directory / "formal_competing_risks.json", result)
    (root / "reports/formal_competing_risks.json").write_bytes((directory / "formal_competing_risks.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run(Path(__file__).resolve().parents[1])
