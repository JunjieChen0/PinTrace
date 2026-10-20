"""Placebo evidence for the incident association, assembled from two independent placebo designs.

* Fake anchor (``control_compare``): the whole treated-versus-control contrast is rerun with the anchor moved 90 days
  earlier and observation cut at the real anchor, so no incident period enters. If the incident were not special, the
  contrasts at the fake anchor would look like those at the real one.
* Pre-incident month scan (``formal_extensions``): each of the six pre-incident months is compared with the other five,
  and the first post-incident month is compared with every pre-incident month.

Each real contrast is paired with its fake-anchor counterpart and given a verdict by comparing 95% percentile
intervals with the null value (1 for ratios, 0 for differences in shares). The verdicts describe interval position;
they are not significance tests. A placebo that departs from the null in the opposite direction is reported as such
instead of being counted as a pass, because it shows the baseline period is itself unstable.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re

from .formal_common import REPLICATES, SEED, WORKING_MANIFEST, annotate, sha256, write_json
from .formal_extensions import BIN_DAYS, PERIODS, latest_working_run

RULE_VERSION = "formal-placebo-v1"
OUTPUT = "data/derived/formal-stats-v4/runs"
MARGINAL = 0.05
DAY = 86400
HEADLINE = {
    "treated_pin_ratio_first_30d": "treated.other.pin_ratio.post_0_30",
    "treated_pin_ratio_30_90d": "treated.other.pin_ratio.post_30_90",
    "treated_vs_control_pin_first_30d": "ratio_of_ratios.other.pin.post_0_30",
    "treated_vs_control_conversion_share_first_30d": "ratio_of_ratios.other.converted_share.post_0_30",
    "treated_vs_control_removal_first_30d": "ratio_of_ratios.other.remove.post_0_30",
    "treated_vs_control_mutable_update_first_30d": "ratio_of_ratios.other.mutable_update.post_0_30",
    "new_fixed_share_change_vs_control_first_30d": "difference_in_changes.new_fixed_share.post_0_30",
}


def null_value(key):
    """Null of a contrast key, or None for a level (rate or share) that has no null."""
    if key.startswith("ratio_of_ratios.") or re.search(r"_ratio\.post_", key):
        return 1.0
    if key.startswith("difference_in_changes.") or re.search(r"_change\.post_", key):
        return 0.0
    return None


def state(entry, null):
    if entry is None or entry.get("estimate") is None or entry.get("lower") is None or entry.get("upper") is None:
        return "undefined"
    return "excludes_null" if entry["lower"] > null or entry["upper"] < null else "includes_null"


def direction(entry, null):
    return "up" if entry["estimate"] > null else "down" if entry["estimate"] < null else "flat"


def compact(entry):
    return {key: entry[key] for key in ("estimate", "lower", "upper", "valid_replicates", "requested_replicates")}


def classify(real, placebo, null):
    """Position of the real interval and of its fake-anchor counterpart relative to the null."""
    real_state, placebo_state = state(real, null), state(placebo, null)
    if "undefined" in (real_state, placebo_state):
        verdict = "undefined"
    elif placebo_state == "includes_null":
        verdict = "separated_from_placebo" if real_state == "excludes_null" else "not_distinguishable_from_null"
    elif real_state == "excludes_null":
        same = direction(placebo, null) == direction(real, null)
        verdict = "placebo_also_shows_effect" if same else "real_departure_placebo_opposite"
    else:
        verdict = "placebo_departs_real_null"
    marginal = None
    if null == 1.0 and real_state == "excludes_null":
        margin = real["lower"] - null if direction(real, null) == "up" else null - real["upper"]
        marginal = margin <= MARGINAL
    return dict(null=null, verdict=verdict, real_state=real_state, placebo_state=placebo_state,
                real_direction=None if real_state == "undefined" else direction(real, null),
                placebo_direction=None if placebo_state == "undefined" else direction(placebo, null),
                marginal=marginal, real=compact(real), placebo=compact(placebo),
                interval_status=dict(real=annotate(real)["status"], placebo=annotate(placebo)["status"]))


def pair_estimates(real, placebo):
    """Contrasts present at both anchors; placebo-only contrasts (windows with no real counterpart) are listed, not dropped."""
    pairs, unpaired = {}, []
    for key in sorted(placebo):
        null = null_value(key)
        if null is None:
            continue
        if key not in real:
            unpaired.append(key)
            continue
        pairs[key] = classify(real[key], placebo[key], null)
    return pairs, unpaired


def month_scan(extensions):
    """Within-pre-incident placebo months and the first post-incident month against every pre-incident month."""
    series, result = extensions["interrupted_series"], {}
    for scope, rows in series["placebo"].items():
        block = {}
        for kind in ("pin_ratio", "share_ratio"):
            states = [state(row[kind], 1.0) for row in rows]
            block[kind] = dict(months=len(rows), excluding_one=sum(s == "excludes_null" for s in states),
                               months_excluding_one=[row["bin"] for row, s in zip(rows, states) if s == "excludes_null"],
                               undefined=sum(s == "undefined" for s in states))
        bins = {row["bin"]: row for row in series["bins"][scope]}
        pre = [bins[b] for b in sorted(bins) if b < 0]
        first = bins.get(0)
        if first is not None and pre:
            highest = max(pre, key=lambda row: row["pin_rate"]["upper"])
            strongest = max(pre, key=lambda row: row["pin_rate"]["estimate"])
            block["first_post_month"] = dict(
                pin_rate=compact(first["pin_rate"]), highest_pre_month_bin=highest["bin"],
                highest_pre_month_upper=highest["pin_rate"]["upper"],
                lower_above_every_pre_month_upper=first["pin_rate"]["lower"] > highest["pin_rate"]["upper"],
                pre_month_estimates={str(row["bin"]): row["pin_rate"]["estimate"] for row in pre},
                estimate_over_highest_pre_month_estimate=(first["pin_rate"]["estimate"] / strongest["pin_rate"]["estimate"]
                                                          if strongest["pin_rate"]["estimate"] else None))
        result[scope] = block
    return result


def windows(control):
    placebo, study = control["placebo"], control["window"]
    iso = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")
    fake = placebo["fake_anchor"]
    periods = {name: dict(start=iso(fake + low * BIN_DAYS * DAY), end=iso(fake + high * BIN_DAYS * DAY))
               for name, (low, high) in placebo["periods"].items()}
    return dict(fake_anchor=iso(fake), true_anchor=iso(placebo["true_anchor"]), shift_days=placebo["shift_days"], periods=periods,
                placebo_pre_days=(placebo["periods"]["pre"][1] - placebo["periods"]["pre"][0]) * BIN_DAYS,
                real_pre_days=len(PERIODS["pre"]) * BIN_DAYS, study_window_start=study["start"][:10],
                pre_window_inside_study_window=periods["pre"]["start"] >= study["start"][:10],
                design="Same repositories and rules; anchor moved earlier by shift_days; observation cut at the real anchor.")


def consistency(control, extensions):
    bins = {row["bin"]: row for row in extensions["interrupted_series"]["bins"]["other"]}
    first = bins[0]["pin_rate"]
    real_first = control["estimates"]["treated.other.pin_rate.post_0_30"]["estimate"]
    return dict(same_seed_and_replicates=(control["seed"], control["replicates"], first["seed"], first["requested_replicates"]) == (SEED, REPLICATES, SEED, REPLICATES),
                same_treated_clusters=control["repositories"]["treated"] == first["n_clusters"],
                placebo_uses_the_same_repositories=control["placebo"]["repositories"] == control["repositories"],
                first_month_rate_agrees_across_reports=math.isclose(real_first, first["estimate"], rel_tol=1e-9))


def build(control, extensions):
    pairs, unpaired = pair_estimates(control["estimates"], control["placebo"]["estimates"])
    claims = {}
    for name, key in HEADLINE.items():
        if key not in pairs:
            raise ValueError("Headline contrast has no paired placebo estimate: " + key)
        claims[name] = dict(key=key, **{field: pairs[key][field] for field in ("verdict", "marginal", "real", "placebo")})
    return dict(windows=windows(control),
                contrasts=dict(paired=len(pairs), unpaired_placebo_keys=unpaired,
                               verdict_counts=dict(sorted(Counter(pair["verdict"] for pair in pairs.values()).items())), pairs=pairs),
                claims=claims, within_pre_months=month_scan(extensions), consistency=consistency(control, extensions))


def run(root):
    root = Path(root).resolve()
    run_path, _ = latest_working_run(root)
    control_path, extensions_path = "reports/control_comparison.json", "reports/formal_extensions.json"
    control = json.loads((root / control_path).read_text(encoding="utf-8"))
    extensions = json.loads((root / extensions_path).read_text(encoding="utf-8"))
    if extensions["source_run"] != run_path:
        raise ValueError("formal_extensions was not built from the newest completed working-input run")
    stale = [name for name, digest in control["input_sha256"].items() if sha256(root / name) != digest]
    if stale:
        raise ValueError("control_comparison inputs changed since it was produced: " + ", ".join(stale))
    manifest = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    created = datetime.fromisoformat(control["created_at_utc"]).timestamp()
    measurements = [name for name in manifest["input_sha256"] if re.fullmatch(r"data/derived/measurement-v\d+/\d+\.json", name)]
    newer = [name for name in measurements if os.path.getmtime(root / name) > created]
    body = build(control, extensions)
    result = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), source_run=run_path,
        input_sha256={control_path: sha256(root / control_path), extensions_path: sha256(root / extensions_path),
                      WORKING_MANIFEST: sha256(root / WORKING_MANIFEST)},
        implementation_sha256=sha256(Path(__file__)),
        populations=dict(treated=control["repositories"]["treated"], control=control["repositories"]["control"],
                         treated_without_usable_record=len(control["treated_without_usable_record"]),
                         control_without_usable_record=len(control["control_without_usable_record"])),
        **body,
        checks=dict(**body["consistency"], control_comparison_inputs_unchanged=True,
                    extensions_from_newest_working_run=True, measurement_files_not_modified_after_comparison=not newer),
        measurement_check=dict(basis="file modification time; control_comparison binds only matched_controls.json and study.toml",
                               files_checked=len(measurements), modified_after_comparison=len(newer)),
        limits=["Two placebo designs, neither a randomisation test: one 90-day fake-anchor shift and a within-pre-incident month scan.",
                "The fake-anchor baseline is 90 days against 180 days at the real anchor; shorter baselines are noisier, so the two are not equivalent.",
                "Verdicts place 95% percentile intervals relative to the null; they are not significance tests, and bounds within 0.05 of a ratio null are flagged marginal.",
                "Monthly pin counts before the incident vary widely, and the month-level ratios inherit that variation.",
                "Intervals with status partial_replicates or zero_degenerate do not establish absence of a difference.",
                "control_comparison does not bind the measurement files it read; they are checked by modification time only.",
                "No model is fitted and no closed analysis is opened."])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / OUTPUT / stamp
    write_json(directory / "formal_placebo.json", result)
    (root / "reports/formal_placebo.json").write_bytes((directory / "formal_placebo.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run(Path(__file__).resolve().parents[1])
