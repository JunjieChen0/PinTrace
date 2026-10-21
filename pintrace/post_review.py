"""Post-review analyses: stock of pinned references, the period split of conversions, and owner-clustered intervals.

The manuscript reports conversion rates (a flow). This module adds the level of pinned references over time (a
stock), splits the RQ2 conversions by period, and repeats the headline ratios with owners instead of repositories
as the resampling cluster. It reads the same working-input authority as formal_extensions.

Usage: python -m pintrace.post_review [--working]
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import formal_extensions as fx
from .formal_common import REPLICATES, SEED, WORKING_MANIFEST, sha256, write_json

RULE_VERSION = "post-review-v1"
BATCH = "formal-stock-v1"
DAY = 86400
STOCK_OFFSETS = tuple(range(-180, 541, 30))
KEY_OFFSETS = (-30, 0, 30, 90, 180, 360, 540)


def _observed_end(record):
    snapshots = record["snapshots"]
    return record["window_end"] if record["observed_ancestry_covers_end"] else snapshots[record["endpoint_index"]]["timestamp"]


def _snapshot_at(record, when):
    """Last complete snapshot at or before ``when``, within the observed part of the record."""
    if when > _observed_end(record):
        return None
    chosen = None
    for index in range(record["endpoint_index"] + 1):
        state = record["snapshots"][index]
        if state["timestamp"] > when:
            break
        if state["complete"] is True:
            chosen = state
    return chosen


def _other_counts(state):
    """References to Actions other than the compromised one, and how many of them are full SHAs."""
    total = state["action_references"] - state["target_references"]
    full = state["full_sha_references"] - state["target_full_sha"]
    return (total, full) if total > 0 else (0, 0)


class Cluster:
    """Whole-cluster resampling with the seed and replicate count of the main batch."""

    def __init__(self, members):
        self.members = dict(members)
        self.ids = sorted(set(self.members.values()))
        rng = np.random.default_rng(SEED)
        self.matrix = rng.multinomial(len(self.ids), np.full(len(self.ids), 1 / len(self.ids)), size=REPLICATES)
        self.position = {name: i for i, name in enumerate(self.ids)}

    def rows(self, per_repository):
        """Aggregate per-repository vectors into one row per cluster."""
        width = len(next(iter(per_repository.values())))
        out = np.zeros((len(self.ids), width))
        for repository, vector in per_repository.items():
            out[self.position[self.members[repository]]] += np.asarray(vector, dtype=float)
        return out

    def interval(self, statistic, per_repository):
        rows = self.rows(per_repository)
        values = []
        for draw in self.matrix:
            try:
                value = statistic(draw @ rows)
            except (ZeroDivisionError, FloatingPointError):
                continue
            if np.isfinite(value):
                values.append(value)
        values = np.asarray(values)
        return dict(estimate=float(statistic(np.ones(len(self.ids)) @ rows)),
                    lower=float(np.percentile(values, 2.5)), upper=float(np.percentile(values, 97.5)),
                    valid_replicates=int(values.size), n_clusters=len(self.ids),
                    requested_replicates=REPLICATES, seed=SEED, interval="percentile")


def stock_series(records, ids):
    """Repository-equal and pooled share of other-Action references held as full SHAs, by 30-day offset."""
    included = [r for r in records if r["repository_id"] in ids]
    per_repository = defaultdict(dict)
    for record in included:
        anchor = record["anchor"]
        for offset in STOCK_OFFSETS:
            state = _snapshot_at(record, anchor + offset * DAY)
            if state is None:
                continue
            total, full = _other_counts(state)
            if total:
                per_repository[record["repository_id"]][offset] = (full, total)
    members = {r["repository_id"]: r["repository_id"] for r in included}
    cluster = Cluster(members)
    rows = []
    for offset in STOCK_OFFSETS:
        available = {repo: [float(v[offset][0]) / v[offset][1], 1.0] for repo, v in per_repository.items() if offset in v}
        counts = {repo: [float(v[offset][0]), float(v[offset][1])] for repo, v in per_repository.items() if offset in v}
        share = cluster.interval(lambda r: float(r[0] / r[1]), available) if available else None
        pooled = cluster.interval(lambda r: float(r[0] / r[1]), counts) if counts else None
        rows.append(dict(offset_days=offset, repositories=len(available),
                         repository_equal=share, pooled=pooled))
    return dict(bin_days=30, offsets=list(STOCK_OFFSETS), series=rows)


def conversion_periods(records, ids):
    """Conversions of other Actions to full SHAs, counted by period."""
    included = [r for r in records if r["repository_id"] in ids and r["clock_aligned"]]
    series = {r["repository_id"]: fx.conversion_series(r) for r in included}
    output = {}
    for name, bins in fx.PERIODS.items():
        output[name] = dict(bins=[bins.start, bins.stop - 1],
                            conversions=sum(s["other"][b]["pin"] for s in series.values() for b in bins),
                            removals=sum(s["other"][b]["remove"] for s in series.values() for b in bins),
                            mutable_updates=sum(s["other"][b]["mutable_update"] for s in series.values() for b in bins),
                            exposure=round(sum(s["other"][b]["exposure"] for s in series.values() for b in bins), 3))
    total = sum(row["conversions"] for row in output.values())
    first = output["post_0_30"]["conversions"]
    late = output["post_360_540"]["conversions"]
    return dict(periods=output, total_all_periods=total, first_month=first, late_wave=late,
                late_share_of_post=late / max(1, sum(v["conversions"] for k, v in output.items() if k.startswith("post_"))),
                first_share_of_post=first / max(1, sum(v["conversions"] for k, v in output.items() if k.startswith("post_"))))


def rq2_split(root, results):
    """Split the 4,229 first-outcome conversions of baseline references by when they happened."""
    summary = json.loads((root / (results + "rq2_summary.json")).read_text(encoding="utf-8"))["formal"]["existing"]
    denominator = summary["baseline_references"]
    total = summary["outcome_counts"]["pin"]
    cumulative = {day: round(row["observed_pin_cumulative"] * denominator) for day, row in summary["horizons"].items()}
    within_365 = cumulative["365"]
    return dict(baseline_references=denominator, conversions=total,
                within={day: cumulative[day] for day in ("7", "30", "90", "180", "365")},
                within_share={day: cumulative[day] / total for day in ("7", "30", "90", "180", "365")},
                after_day_365=total - within_365, after_day_365_share=(total - within_365) / total)


def owner_clustered(records, ids, stock):
    """Headline ratios resampled by repository and by owner."""
    included = [r for r in records if r["repository_id"] in ids and r["clock_aligned"]]
    series = {r["repository_id"]: fx.conversion_series(r) for r in included}
    owner = {r["repository_id"]: r["repository"].split("/")[0] for r in included}
    pre, m1, late = fx.PERIODS["pre"], fx.PERIODS["post_0_30"], fx.PERIODS["post_360_540"]

    def vectors(scope):
        out = {}
        for repo, s in series.items():
            row = [sum(s[scope][b][kind] for b in bins) for bins in (pre, m1, late) for kind in ("pin",)]
            row += [sum(s[scope][b]["exposure"] for b in bins) for bins in (pre, m1, late)]
            row += [float(sum(s[scope][b]["exposure"] > 0 and s[scope][b]["pin"] > 0 for b in bins)) for bins in (pre, m1, late)]
            row += [float(sum(s[scope][b]["exposure"] > 0 for b in bins)) for bins in (pre, m1, late)]
            out[repo] = row
        return out

    def statistic(r):
        pin_pre, pin_m1, pin_late, exp_pre, exp_m1, exp_late, conv_pre, conv_m1, conv_late, risk_pre, risk_m1, risk_late = r
        return dict(
            rr_m1=(pin_m1 / exp_m1) / (pin_pre / exp_pre),
            rr_m1318=(pin_late / exp_late) / (pin_pre / exp_pre),
            share_ratio_m1=(conv_m1 / risk_m1) / (conv_pre / risk_pre),
            share_ratio_m1318=(conv_late / risk_late) / (conv_pre / risk_pre),
        )

    output = {}
    for scope in ("other", "target"):
        data = vectors(scope)
        for name, members in (("repository", {r: r for r in series}), ("owner", owner)):
            cluster = Cluster(members)
            rows = cluster.rows(data)
            estimate = statistic(np.ones(len(cluster.ids)) @ rows)
            draws = defaultdict(list)
            for draw in cluster.matrix:
                try:
                    value = statistic(draw @ rows)
                except (ZeroDivisionError, FloatingPointError):
                    continue
                if all(np.isfinite(v) for v in value.values()):
                    for key, v in value.items():
                        draws[key].append(v)
            output[f"{scope}.{name}"] = {
                key: dict(estimate=float(estimate[key]), lower=float(np.percentile(v, 2.5)), upper=float(np.percentile(v, 97.5)),
                          valid_replicates=len(v), n_clusters=len(cluster.ids), requested_replicates=REPLICATES,
                          seed=SEED, interval="percentile")
                for key, v in draws.items()}
    return output


def run(root, *, working=False):
    root = Path(root).resolve()
    if working:
        run_path, run_record = fx.latest_working_run(root)
        manifest_path, results = WORKING_MANIFEST, run_record["output_directory"] + "/"
    else:
        raise SystemExit("Only the working-input authority is supported")
    bound = fx._verify(root, run_record, manifest_path, results)
    frozen = json.loads((root / manifest_path).read_text(encoding="utf-8"))
    ids = {str(i) for i in frozen["formal_population"]["formal_eligible_repository_ids"]}
    with (root / (results + "rq12_repository_records.jsonl")).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]

    stock = stock_series(records, ids)
    periods = conversion_periods(records, ids)
    split = rq2_split(root, results)
    owners = owner_clustered(records, ids, stock)
    key = {str(offset): next(row for row in stock["series"] if row["offset_days"] == offset) for offset in KEY_OFFSETS}
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  implementation_sha256=sha256(Path(__file__)), source_run=run_path, input_sha256=bound,
                  stock=stock, stock_key_offsets=key, conversions=periods, rq2_split=split, clusters=owners,
                  checks=dict(repositories=len(ids), records=len(records),
                              stock_offsets=len(STOCK_OFFSETS)))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / "data/derived" / BATCH / "runs" / stamp
    write_json(directory / "post_review.json", result)
    target = root / "reports/post_review.json"
    if target.relative_to(root).as_posix() in frozen["input_sha256"]:
        raise ValueError("Attempt to overwrite a frozen input")
    target.write_bytes((directory / "post_review.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working", action="store_true", help="Use the working-input authority")
    run(Path(__file__).resolve().parents[1], working=parser.parse_args().working)
