"""Checks added in the second revision: the tail window, the units of the repository share, commit-level counts of
the second wave, and the dependents-list status of the formal cohort against its measured endpoint.

All quantities are recomputed from the archived compact repository records (formal-stats-v3 results) and the
coverage crawl; nothing is collected. Intervals resample whole repositories with the seed, replicate count and
multinomial draw of the main batch (formal_extensions.Clustered).

- Tail window: the 30-day series ends at day 540 (bins 0..17); the window ends ten UTC days later. The tail
  [day 540, window end] is reported separately so that the bins are not read as covering it.
- Repository share: the share of at-risk repositories with a conversion is pooled over repository-bins. Its
  numerator and denominator are reported, together with the distinct repositories behind them.
- Commit-level wave: each commit that converts at least one other Action in a repository counts once, on the
  same exposure, so that bulk commits do not drive the ratio.
- Endpoint and listing: the formal repositories listed on the dependents page against whether their last
  measured snapshot still references the Action.
- Common panel: the pinned share at days 0, 180, 360 and 540 for the repositories that have a stock at all four,
  so that a rise cannot come from a changing set of repositories.
- Carry-forward exposure: the conversion series counts exposure only while a repository is observed to have
  later commits. Holding each repository's last observed state to the window end (the clones postdate the window,
  see denominator_audit) keeps the events and extends the exposure of quiet repositories.
- Seed variation: the headline rate ratios with independent bootstrap seeds, to separate Monte Carlo variation
  from the determinism shown by repeating one seed.

Usage: python -m pintrace.revision_checks [--check]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from . import coverage_audit as ca
from . import formal_extensions as fx
from . import post_review as pr
from .formal_common import WORKING_MANIFEST, sha256, write_json

RULE_VERSION = "revision-checks-v2"
OUTPUT = "reports/revision_checks.json"
RECORDS = "data/derived/formal-stats-v3/results/rq12_repository_records.jsonl"
WIDTH = fx.BIN_DAYS * fx.DAY
WINDOWS = {"pre": (-6, 0), "m1": (0, 1), "m7_12": (6, 12), "m13_18": (12, 18)}  # in 30-day bins from the anchor
PANEL_OFFSETS = (0, 180, 360, 540)
EXTRA_SEEDS = (1, 2, 3, 4, 5)


def window_counts(record, low, high):
    """Other-Action exposure (mutable reference-months), conversions and converting commits in [low, high).

    Same rules as formal_extensions.conversion_series: complete adjacent states, a mutable previous reference,
    and observation clipped at the end of observable ancestry.
    """
    snapshots, end_index, observed_end = record["snapshots"], record["endpoint_index"], fx._observed_end(record)
    target, exposure = record["target_action"], 0.0
    for i in range(end_index + 1):
        state = snapshots[i]
        if state["complete"] is not True:
            continue
        start = state["timestamp"]
        stop = min(snapshots[i + 1]["timestamp"] if i < end_index else observed_end, observed_end)
        mutable_target = state["target_references"] - state["target_full_sha"]
        mutable_other = state["action_references"] - state["full_sha_references"] - mutable_target
        overlap = min(stop, high) - max(start, low)
        if stop > start and overlap > 0:
            exposure += mutable_other * overlap / WIDTH
    conversions, commits, last = 0, set(), None
    for event in record["events"]:
        before, i, when = event.get("before"), event["index"], event["timestamp"]
        if event["event"] != "pin" or not isinstance(before, dict) or before.get("ref_type") == "full_sha":
            continue
        if before.get("action") == target or not 1 <= i <= end_index:
            continue
        if snapshots[i - 1]["complete"] is not True or snapshots[i]["complete"] is not True:
            continue
        if low <= when < high and when <= observed_end:
            conversions += 1
            commits.add(event["commit"])
            last = when if last is None else max(last, when)
    return dict(exposure=exposure, conversions=conversions, commits=len(commits), last=last)


def bounds(record, name):
    anchor = record["anchor"]
    if name == "tail":
        return anchor + fx.POST_BINS * WIDTH, record["window_end"] + 1
    first, stop = WINDOWS[name]
    return anchor + first * WIDTH, anchor + stop * WIDTH


def load_records(root):
    manifest = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    formal = {str(i) for i in manifest["formal_population"]["formal_eligible_repository_ids"]}
    with (root / RECORDS).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    return [r for r in records if str(r["repository_id"]) in formal]


def rate(name, field):
    return lambda s: s[f"{name}.{field}"] / s[f"{name}.exposure"]


def ratio(a, b, field):
    return lambda s: rate(a, field)(s) / rate(b, field)(s)


def _clustered_windows(records):
    """Per-window totals of the clock-aligned records and a resampler holding them as columns."""
    aligned = [r for r in records if r["clock_aligned"]]
    clustered = fx.Clustered([r["repository_id"] for r in aligned])
    counts = {}
    for name in (*WINDOWS, "tail"):
        rows = {str(r["repository_id"]): window_counts(r, *bounds(r, name)) for r in aligned}
        last = [v["last"] for v in rows.values() if v["last"] is not None]
        counts[name] = dict(conversions=sum(v["conversions"] for v in rows.values()),
                            converting_commits=sum(v["commits"] for v in rows.values()),
                            exposure=round(sum(v["exposure"] for v in rows.values()), 3),
                            last_conversion_utc=datetime.fromtimestamp(max(last), timezone.utc).isoformat() if last else None)
        for field in ("exposure", "conversions", "commits"):
            clustered.add(f"{name}.{field}", {i: v[field] for i, v in rows.items()})
    return aligned, clustered, counts


def wave_checks(records):
    aligned, clustered, counts = _clustered_windows(records)
    n = len(aligned)
    tail_start, tail_stop = bounds(aligned[0], "tail")
    return dict(
        repositories=n,
        windows=counts,
        tail=dict(start_utc=datetime.fromtimestamp(tail_start, timezone.utc).isoformat(),
                  end_utc=datetime.fromtimestamp(tail_stop - 1, timezone.utc).isoformat(),
                  days=(tail_stop - tail_start) // fx.DAY,
                  rate_per_100=clustered.evaluate(lambda s: fx.PER * rate("tail", "conversions")(s), n),
                  rr_vs_pre=clustered.evaluate(ratio("tail", "pre", "conversions"), n)),
        rr_m1_vs_pre=clustered.evaluate(ratio("m1", "pre", "conversions"), n),
        rr_m13_18_vs_pre=clustered.evaluate(ratio("m13_18", "pre", "conversions"), n),
        rr_m13_18_vs_m7_12=clustered.evaluate(ratio("m13_18", "m7_12", "conversions"), n),
        commit_rr_m13_18_vs_pre=clustered.evaluate(ratio("m13_18", "pre", "commits"), n),
        commit_rr_m13_18_vs_m7_12=clustered.evaluate(ratio("m13_18", "m7_12", "commits"), n))


def carried_forward(record):
    """The record with its last observed state held to the window end (a new dict; the input is unchanged)."""
    return dict(record, observed_ancestry_covers_end=True)


def carry_forward_waves(records):
    """The wave checks with each repository's last observed state held to the window end."""
    result = wave_checks([carried_forward(r) for r in records])
    result.pop("tail")
    return result


def panel_stock(records):
    """Repository-equal pinned share at fixed offsets for the aligned repositories with a stock at every offset."""
    shares = {}
    for record in records:
        if not record["clock_aligned"]:
            continue
        row = {}
        for offset in PANEL_OFFSETS:
            state = pr._snapshot_at(record, record["anchor"] + offset * fx.DAY)
            total, full = pr._other_counts(state) if state is not None else (0, 0)
            if total:
                row[offset] = full / total
        if len(row) == len(PANEL_OFFSETS):
            shares[str(record["repository_id"])] = row
    clustered = fx.Clustered(shares)
    clustered.add("n", {identity: 1.0 for identity in shares})
    for offset in PANEL_OFFSETS:
        clustered.add(f"share.{offset}", {identity: row[offset] for identity, row in shares.items()})
    n = len(shares)
    return dict(repositories=n, offsets=list(PANEL_OFFSETS),
                repository_equal={str(offset): clustered.evaluate(lambda s, o=offset: s[f"share.{o}"] / s["n"], n)
                                  for offset in PANEL_OFFSETS})


def seed_variation(records):
    """The first-month and months 13-18 rate ratios under independent bootstrap seeds."""
    import numpy as np
    aligned, clustered, _ = _clustered_windows(records)
    n, k = len(aligned), len(clustered.ids)
    output = {}
    for seed in EXTRA_SEEDS:
        clustered.matrix = np.random.default_rng(seed).multinomial(k, np.full(k, 1 / k), size=fx.REPLICATES)
        clustered.cache = None
        row = {}
        for name, statistic in (("rr_m1_vs_pre", ratio("m1", "pre", "conversions")),
                                ("rr_m13_18_vs_pre", ratio("m13_18", "pre", "conversions"))):
            row[name] = dict(clustered.evaluate(statistic, n), seed=seed)
        output[str(seed)] = row
    return output


def repository_months(records):
    """Repository-bins at risk and with a conversion, and the distinct repositories behind them."""
    output = {}
    series = {str(r["repository_id"]): fx.conversion_series(r) for r in records if r["clock_aligned"]}
    for name, (first, stop) in WINDOWS.items():
        at_risk = converted = 0
        risk_ids, converted_ids = set(), set()
        for identity, s in series.items():
            for b in range(first, stop):
                cell = s["other"][b]
                if cell["exposure"] > 0:
                    at_risk += 1
                    risk_ids.add(identity)
                    if cell["pin"] > 0:
                        converted += 1
                        converted_ids.add(identity)
        output[name] = dict(repository_bins_at_risk=at_risk, repository_bins_converted=converted,
                            share=converted / at_risk, repositories_at_risk=len(risk_ids),
                            repositories_converted=len(converted_ids))
    return output


def endpoint_listing(root, records):
    """Formal repositories by current listing and by whether their last measured snapshot references the Action."""
    dependents = {name.lower() for name in ca.read_dependents(ca.latest_crawl(root))}
    aliases = {}
    for path in sorted((root / "data/derived/cohort-v1/baselines").glob("*.json")):
        summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
        names = {summary["repository"].lower(), (summary.get("canonical_repository") or "").lower()} - {""}
        aliases.setdefault(str(summary.get("repository_id")), set()).update(names)
    table = {}
    for record in records:
        listed = bool(aliases.get(str(record["repository_id"]), set()) & dependents)
        last = record["snapshots"][record["endpoint_index"]]
        state = ("incomplete" if last["complete"] is not True
                 else "references_action" if last["target_references"] > 0 else "no_reference")
        key = ("listed" if listed else "not_listed") + "." + state
        table[key] = table.get(key, 0) + 1
    return dict(sorted(table.items()))


def compute(root):
    records = load_records(root)
    return dict(waves=wave_checks(records), repository_months=repository_months(records),
                endpoint_listing=endpoint_listing(root, records), panel_stock=panel_stock(records),
                carry_forward=carry_forward_waves(records), seed_variation=seed_variation(records))


def inputs(root):
    crawl = ca.latest_crawl(root)
    paths = [RECORDS, WORKING_MANIFEST, *(p.relative_to(root).as_posix() for p in sorted(crawl.glob("*")) if p.is_file())]
    return {path: sha256(root / path) for path in paths}


def run(root, check=False):
    root = Path(root).resolve()
    result = compute(root)
    if check:
        published = json.loads((root / OUTPUT).read_text(encoding="utf-8"))
        if published["input_sha256"] != inputs(root):
            raise ValueError("Inputs differ from the published revision checks")
        for key, value in result.items():
            if json.dumps(value, sort_keys=True) != json.dumps(published[key], sort_keys=True):
                raise ValueError("Recomputed revision checks differ from the published report: " + key)
        print(json.dumps(dict(check=True, recomputed=sorted(result))))
        return published
    report = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  implementation_sha256=sha256(Path(__file__)), input_sha256=inputs(root), **result)
    write_json(root / OUTPUT, report)
    print(json.dumps(dict(completed=True, output=OUTPUT)))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="recompute and compare with the published report")
    run(Path(__file__).resolve().parents[1], check=parser.parse_args().check)
