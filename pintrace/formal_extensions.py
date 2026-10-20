"""Pre-incident conversion baselines, response-target versions and updater routes.

The module reads a completed formal-statistics batch and the bound upstream
evidence, and writes a new extension batch (formal-stats-v2 after the frozen
formal-stats-v1 run; formal-stats-v4 after a working-input formal-stats-v3 run). Three analyses are added:

* an interrupted time series of reference conversions in 30-day bins, using the
  cohort's own pre-incident history as the comparison period;
* the advisory version line reached by first target-reference edits and pins;
* the release identity of SHAs adopted after a normal upstream opportunity.

Pure functions take the compact repository records of ``formal_rq12`` so the
calculations can be tested without the frozen measurements.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from .formal_common import FROZEN_MANIFEST, REPLICATES, SEED, WORKING_MANIFEST, sha256, write_json
from .formal_rq12 import describe_repository

RULE_VERSION = "formal-extensions-v1"
DAY = 86400
BIN_DAYS = 30
PRE_BINS, POST_BINS = 6, 12
BINS = tuple(range(-PRE_BINS, POST_BINS))
PER = 100  # rates are events per 100 mutable-reference 30-day periods
PATCHED = (46, 0, 1)  # first patched version in GHSA-mrrh-fwg8-r2c3
COMPROMISED = "0e58ed8671d6b60d0890c21b07f8835ace038e67"
KINDS = ("pin", "remove", "mutable_update")
PERIODS = {"pre": range(-6, 0), "post_0_30": range(0, 1), "post_30_90": range(1, 3), "post_90_180": range(3, 6),
           "post_180_360": range(6, 12), "post_30_360": range(1, 12), "post_0_360": range(0, 12)}
TRAJECTORY_DAYS = (0, 7, 30, 90, 180, 365)
LEAVE_OUT = (1, 5, 10)
RUN = "data/derived/formal-stats-v1/runs/20261018T155426854985Z/manifest.json"
RESULTS = "data/derived/formal-stats-v1/results/"
WORKING_RUNS = "data/derived/formal-stats-v3/runs"
WORKING_EXTENSIONS = "formal-stats-v4"
ENTRIES = "data/derived/maintenance-scope-v1/opportunity_entries.jsonl"
UPSTREAM = "data/derived/upstream-opportunities/"
VERSION = re.compile(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def version_key(ref):
    match = VERSION.fullmatch(str(ref or ""))
    return tuple(int(part) for part in match.groups() if part is not None) if match else None


def advisory_line(version):
    """Classify a tj-actions version against the advisory's first patched release."""
    if version is None:
        return "non_version"
    if len(version) == 1:
        return "patched_line" if version[0] >= PATCHED[0] else "earlier_line"
    return "patched_line" if version + (0,) * (3 - len(version)) >= PATCHED else "earlier_line"


def tag_index(document):
    """Map peeled commit SHAs to every tag observed or released at that commit."""
    index = defaultdict(set)
    for row in [*document.get("observations", []), *document.get("release_records", [])]:
        if row.get("target") and row.get("tag"):
            index[row["target"].lower()].add(row["tag"])
    return index


def sha_line(sha, index):
    sha = str(sha or "").lower()
    if sha == COMPROMISED:
        return "compromised_commit"
    lines = {advisory_line(version_key(tag)) for tag in index.get(sha, ())}
    if not lines:
        return "untagged_commit"
    return "patched_line_commit" if "patched_line" in lines else "earlier_line_commit"


def ref_line(ref, index):
    if ref.get("ref_type") == "full_sha":
        return sha_line(ref.get("ref"), index)
    return advisory_line(version_key(ref.get("ref")))


PATCHED_LINES = {"patched_line", "patched_line_commit"}


def _observed_end(record):
    snapshots = record["snapshots"]
    return record["window_end"] if record["observed_ancestry_covers_end"] else snapshots[record["endpoint_index"]]["timestamp"]


def conversion_series(record):
    """Event counts and complete-state exposure per 30-day bin around the anchor.

    Exposure integrates mutable direct references over time, split into the
    target Action and all other Actions. Events count lineage transitions whose
    previous reference was mutable and whose adjacent states are both complete.
    """
    anchor, target, snapshots = record["anchor"], record["target_action"], record["snapshots"]
    end_index, observed_end, width = record["endpoint_index"], _observed_end(record), BIN_DAYS * DAY
    output = {scope: {b: dict(exposure=0.0, **{kind: 0 for kind in KINDS}) for b in BINS} for scope in ("other", "target")}
    for i in range(end_index + 1):
        state = snapshots[i]
        if state["complete"] is not True:
            continue
        low = state["timestamp"]
        high = min(snapshots[i + 1]["timestamp"] if i < end_index else observed_end, observed_end)
        mutable_target = state["target_references"] - state["target_full_sha"]
        mutable_other = state["action_references"] - state["full_sha_references"] - mutable_target
        if high <= low:
            continue
        for b in BINS:
            overlap = min(high, anchor + (b + 1) * width) - max(low, anchor + b * width)
            if overlap > 0:
                output["other"][b]["exposure"] += mutable_other * overlap / width
                output["target"][b]["exposure"] += mutable_target * overlap / width
    for event in record["events"]:
        before, i = event.get("before"), event["index"]
        if event["event"] not in KINDS or not isinstance(before, dict) or before.get("ref_type") == "full_sha":
            continue
        if not 1 <= i <= end_index or snapshots[i - 1]["complete"] is not True or snapshots[i]["complete"] is not True:
            continue
        b = math.floor((event["timestamp"] - anchor) / width)
        if b in output["other"] and event["timestamp"] <= observed_end:
            output["target" if before.get("action") == target else "other"][b][event["event"]] += 1
    return output


class Clustered:
    """Whole-repository resampling of named per-repository totals.

    Uses the seed, replicate count and multinomial draw rule of the main batch;
    every statistic evaluated on one instance shares the same draw matrix.
    """

    def __init__(self, ids):
        import numpy as np
        self.ids = sorted(str(identity) for identity in ids)
        rng = np.random.default_rng(SEED)
        self.matrix = rng.multinomial(len(self.ids), np.full(len(self.ids), 1 / len(self.ids)), size=REPLICATES)
        self.columns, self.cache = {}, None

    def add(self, name, values):
        if name in self.columns:
            raise ValueError("Duplicate clustered column: " + name)
        self.columns[name] = [float(values.get(identity, 0.0)) for identity in self.ids]
        self.cache = None

    def evaluate(self, statistic, clusters=None):
        import numpy as np
        if self.cache is None:
            names = sorted(self.columns)
            values = np.array([self.columns[name] for name in names]).T
            self.cache = names, values.sum(axis=0), self.matrix @ values
        names, totals, draws = self.cache
        with np.errstate(divide="ignore", invalid="ignore"):
            estimate = statistic({name: totals[k] for k, name in enumerate(names)})
            samples = np.asarray(statistic({name: draws[:, k] for k, name in enumerate(names)}), dtype=float)
        samples = samples[np.isfinite(samples)]
        estimate = float(estimate) if np.isfinite(estimate) else None
        bounds = [float(v) for v in np.quantile(samples, [0.025, 0.975], method="linear")] if len(samples) else [None, None]
        return dict(estimate=estimate, lower=bounds[0], upper=bounds[1], valid_replicates=int(len(samples)),
                    requested_replicates=REPLICATES, n_clusters=clusters, seed=SEED, interval="percentile")


def new_identity_series(record):
    """Known first-observed Action identities per 30-day bin and how many were fully pinned at introduction.

    Uses the same group definition as the main RQ2 analysis (confirmed first observation, target Action and
    copied or reintroduced steps excluded) but extends it to the pre-incident bins.
    """
    anchor, width = record["anchor"], BIN_DAYS * DAY
    output = {b: dict(known=0, fixed=0) for b in BINS}
    for group in record["first_action_groups"]:
        if group["action"] == record["target_action"] or group["excluded_copy_or_reintroduction"] or group["configuration"] == "unknown":
            continue
        b = math.floor((group["timestamp"] - anchor) / width)
        if b in output and group["index"] <= record["endpoint_index"] and group["timestamp"] <= _observed_end(record):
            output[b]["known"] += 1
            output[b]["fixed"] += group["configuration"] == "all_full_sha"
    return output


def new_identity_summary(included, clustered_ids):
    series = {record["repository_id"]: new_identity_series(record) for record in included}
    clustered = Clustered(clustered_ids)
    for b in BINS:
        clustered.add(f"new.{b}.known", {i: s[b]["known"] for i, s in series.items()})
        clustered.add(f"new.{b}.fixed", {i: s[b]["fixed"] for i, s in series.items()})
        clustered.add(f"new.{b}.has", {i: float(s[b]["known"] > 0) for i, s in series.items()})
    total = lambda s, field, bins: sum(s[f"new.{b}.{field}"] for b in bins)
    share = lambda bins: lambda s: total(s, "fixed", bins) / total(s, "known", bins)
    n = len(series)
    output = dict(bins=[dict(bin=b, start_day=b * BIN_DAYS, known=sum(s[b]["known"] for s in series.values()),
                             fixed=sum(s[b]["fixed"] for s in series.values()),
                             repositories=sum(s[b]["known"] > 0 for s in series.values()),
                             share=clustered.evaluate(share((b,)), n)) for b in BINS], periods={}, differences={})
    for name, bins in PERIODS.items():
        output["periods"][name] = dict(known=sum(s[b]["known"] for s in series.values() for b in bins),
                                       fixed=sum(s[b]["fixed"] for s in series.values() for b in bins),
                                       share=clustered.evaluate(share(bins), n))
        if name != "pre":
            output["differences"][name] = clustered.evaluate(lambda s, bins=bins: share(bins)(s) - share(PERIODS["pre"])(s), n)
    return output


def interrupted_series(records, ids):
    """Pooled rates, rate ratios, excess conversions and a placebo anchor."""
    included = [record for record in records if record["repository_id"] in ids and record["clock_aligned"]]
    series = {record["repository_id"]: conversion_series(record) for record in included}
    clustered = Clustered(series)
    for scope in ("other", "target"):
        for b in BINS:
            clustered.add(f"{scope}.{b}.exposure", {i: s[scope][b]["exposure"] for i, s in series.items()})
            clustered.add(f"{scope}.{b}.at_risk", {i: float(s[scope][b]["exposure"] > 0) for i, s in series.items()})
            clustered.add(f"{scope}.{b}.converted", {i: float(s[scope][b]["exposure"] > 0 and s[scope][b]["pin"] > 0) for i, s in series.items()})
            for kind in KINDS:
                clustered.add(f"{scope}.{b}.{kind}", {i: s[scope][b][kind] for i, s in series.items()})

    def total(sums, scope, field, bins):
        return sum(sums[f"{scope}.{b}.{field}"] for b in bins)

    def rate(scope, kind, bins):
        return lambda s: PER * total(s, scope, kind, bins) / total(s, scope, "exposure", bins)

    def ratio(scope, kind, post, pre):
        return lambda s: (total(s, scope, kind, post) / total(s, scope, "exposure", post)) / (total(s, scope, kind, pre) / total(s, scope, "exposure", pre))

    def excess(scope, kind, post, pre):
        return lambda s: total(s, scope, kind, post) - total(s, scope, kind, pre) / total(s, scope, "exposure", pre) * total(s, scope, "exposure", post)

    def share(scope, bins):
        return lambda s: total(s, scope, "converted", bins) / total(s, scope, "at_risk", bins)

    def share_ratio(scope, post, pre):
        return lambda s: share(scope, post)(s) / share(scope, pre)(s)

    n = len(series)
    result = dict(repositories=n, excluded_clock_unaligned=sorted(set(ids) - {r["repository_id"] for r in included}),
                  bin_days=BIN_DAYS, rate_unit=f"events per {PER} mutable-reference {BIN_DAYS}-day periods",
                  bins={}, periods={}, ratios={}, excess={}, repository_share={}, placebo={})
    for scope in ("other", "target"):
        result["bins"][scope] = [dict(bin=b, start_day=b * BIN_DAYS, end_day=(b + 1) * BIN_DAYS,
                                      exposure=round(sum(s[scope][b]["exposure"] for s in series.values()), 6),
                                      at_risk_repositories=sum(s[scope][b]["exposure"] > 0 for s in series.values()),
                                      converting_repositories=sum(s[scope][b]["exposure"] > 0 and s[scope][b]["pin"] > 0 for s in series.values()),
                                      **{kind: sum(s[scope][b][kind] for s in series.values()) for kind in KINDS},
                                      **{kind + "_rate": clustered.evaluate(rate(scope, kind, (b,)), n) for kind in KINDS},
                                      repository_share=clustered.evaluate(share(scope, (b,)), n))
                                 for b in BINS]
        result["periods"][scope] = {name: dict(exposure=round(sum(s[scope][b]["exposure"] for s in series.values() for b in bins), 6),
                                               **{kind: sum(s[scope][b][kind] for s in series.values() for b in bins) for kind in KINDS},
                                               **{kind + "_rate": clustered.evaluate(rate(scope, kind, bins), n) for kind in KINDS})
                                    for name, bins in PERIODS.items()}
        result["ratios"][scope] = {f"{kind}.{name}": clustered.evaluate(ratio(scope, kind, bins, PERIODS["pre"]), n)
                                   for kind in KINDS for name, bins in PERIODS.items() if name != "pre"}
        result["excess"][scope] = {f"pin.{name}": clustered.evaluate(excess(scope, "pin", bins, PERIODS["pre"]), n)
                                   for name, bins in PERIODS.items() if name != "pre"}
        result["repository_share"][scope] = {name: clustered.evaluate(share(scope, bins), n) for name, bins in PERIODS.items()}
        result["repository_share"][scope]["ratio.post_0_30"] = clustered.evaluate(share_ratio(scope, PERIODS["post_0_30"], PERIODS["pre"]), n)
        # Placebo months: each pre-incident month against the remaining five.
        result["placebo"][scope] = [dict(bin=b, start_day=b * BIN_DAYS,
                                         pin_ratio=clustered.evaluate(ratio(scope, "pin", (b,), [p for p in PERIODS["pre"] if p != b]), n),
                                         share_ratio=clustered.evaluate(share_ratio(scope, (b,), [p for p in PERIODS["pre"] if p != b]), n))
                                    for b in PERIODS["pre"]]
    result["concentration"] = concentration(series)
    result["update_waves"] = update_concentration(records, ids)
    result["new_identity"] = new_identity_summary(included, series)
    result["interpretation"] = ("The cohort's own pre-incident 180 days supply the comparison rate. Ratios compare pooled rates; "
                                "placebo months compare each pre-incident month with the other five.")
    return result


def update_concentration(records, ids):
    """Per 30-day bin: mutable-reference updates, and the single most common (Action, new reference) target.

    A large share on one Action and ref marks an ecosystem release wave, not a repository-level decision.
    """
    counts = {b: Counter() for b in BINS}
    for record in records:
        if record["repository_id"] not in ids or not record["clock_aligned"]:
            continue
        anchor, width, end_index, snapshots = record["anchor"], BIN_DAYS * DAY, record["endpoint_index"], record["snapshots"]
        for event in record["events"]:
            before, after, i = event.get("before"), event.get("after"), event["index"]
            if event["event"] != "mutable_update" or not isinstance(before, dict) or not isinstance(after, dict):
                continue
            if not 1 <= i <= end_index or snapshots[i - 1]["complete"] is not True or snapshots[i]["complete"] is not True:
                continue
            b = math.floor((event["timestamp"] - anchor) / width)
            if b in counts and event["timestamp"] <= _observed_end(record):
                counts[b][(after["action"], after["ref"])] += 1
    output = []
    for b in BINS:
        total = sum(counts[b].values())
        (action, ref), top = counts[b].most_common(1)[0] if total else ((None, None), 0)
        output.append(dict(bin=b, start_day=b * BIN_DAYS, updates=total, top_action=action, top_ref=ref, top_updates=top,
                           top_share=top / total if total else None))
    return output


def concentration(series, scope="other"):
    """First-month conversions by repository and rate ratios without the largest contributors."""
    first = Counter({identity: s[scope][0]["pin"] for identity, s in series.items()})
    total = sum(first.values())
    pre = Counter({identity: sum(s[scope][b]["pin"] for b in PERIODS["pre"]) for identity, s in series.items()})
    pre_total = sum(pre.values())
    output = dict(first_month_conversions=total, repositories_with_conversions=sum(v > 0 for v in first.values()),
                  top10_share=sum(v for _, v in first.most_common(10)) / total if total else None,
                  pre_conversions=pre_total, pre_top5_share=sum(v for _, v in pre.most_common(5)) / pre_total if pre_total else None,
                  leave_out={}, leave_out_pre={})
    for k in LEAVE_OUT:
        keep = {identity: s for identity, s in series.items() if identity not in {i for i, _ in first.most_common(k)}}
        pins = lambda bins: sum(s[scope][b]["pin"] for s in keep.values() for b in bins)
        exposure = lambda bins: sum(s[scope][b]["exposure"] for s in keep.values() for b in bins)
        post, base = pins(PERIODS["post_0_30"]), pins(PERIODS["pre"])
        defined = exposure(PERIODS["post_0_30"]) > 0 and exposure(PERIODS["pre"]) > 0 and base > 0
        output["leave_out"][str(k)] = ((post / exposure(PERIODS["post_0_30"])) / (base / exposure(PERIODS["pre"]))) if defined else None
        # Drop the largest pre-incident contributors instead: the baseline is the other concentrated side.
        dropped = {i for i, _ in pre.most_common(k)}
        rest = {identity: s for identity, s in series.items() if identity not in dropped}
        post_p = sum(s[scope][b]["pin"] for s in rest.values() for b in PERIODS["post_0_30"])
        base_p = sum(s[scope][b]["pin"] for s in rest.values() for b in PERIODS["pre"])
        post_e = sum(s[scope][b]["exposure"] for s in rest.values() for b in PERIODS["post_0_30"])
        base_e = sum(s[scope][b]["exposure"] for s in rest.values() for b in PERIODS["pre"])
        output["leave_out_pre"][str(k)] = ((post_p / post_e) / (base_p / base_e)) if base_p > 0 else None
    return output


EARLIER_LINES = {"earlier_line", "earlier_line_commit"}


def _lines_summary(lines):
    """Patched, earlier or mixed only when every value is classifiable; untagged, non-version and the
    compromised commit stay 'unclassified' because they cannot be placed on either side of the advisory."""
    if not lines:
        return "no_target_value"
    if any(line not in PATCHED_LINES | EARLIER_LINES for line in lines):
        return "unclassified"
    patched = sum(line in PATCHED_LINES for line in lines)
    return "all_patched" if patched == len(lines) else "all_earlier" if not patched else "mixed"


def response_targets(record, index):
    """Version line of the earliest target edit or pin, plus day-365 target references."""
    row = describe_repository(record)["rq1"]
    target, state = record["target_action"], row["first_response_state"]
    event_kind = {"tag_update": "mutable_update", "pin": "pin", "sha_target_change": "sha_target_change"}.get(state)
    output = dict(repository_id=record["repository_id"], first_response_state=state, first_commit=row["first_commit"])
    if event_kind:
        events = [e for e in record["events"] if e["commit"] == row["first_commit"] and e["event"] == event_kind
                  and isinstance(e.get("after"), dict) and e["after"].get("action") == target]
        after = [ref_line(e["after"], index) for e in events]
        before = [ref_line(e["before"], index) for e in events if isinstance(e.get("before"), dict)]
        output.update(after_lines=dict(Counter(after)), before_lines=dict(Counter(before)), first_target=_lines_summary(after),
                      comment_versions=sorted({str(e["after"].get("comment_version")) for e in events if e["after"].get("comment_version")}))
    output["compromised_pins"] = compromised_pins(record)
    output["day365"] = target_lines_at(record, 365, index)
    output["trajectory"] = {str(days): trajectory_state(target_lines_at(record, days, index)) for days in TRAJECTORY_DAYS}
    return output


def compromised_pins(record):
    """Each pin of the target to the compromised commit and the hours until that reference changed again."""
    target, found = record["target_action"], []
    refs = lambda value: value if isinstance(value, list) else [value] if value else []
    for e in record["events"]:
        if any(x.get("action") == target and str(x.get("ref")).lower() == COMPROMISED for x in refs(e.get("after"))):
            lineages = {x.get("lineage_id") for x in refs(e.get("after")) if str(x.get("ref")).lower() == COMPROMISED}
            later = [f["timestamp"] for f in record["events"] if f["index"] > e["index"] and any(
                x.get("lineage_id") in lineages and str(x.get("ref")).lower() == COMPROMISED for x in refs(f.get("before")))]
            found.append(dict(timestamp=e["timestamp"], replaced_after_hours=(min(later) - e["timestamp"]) / 3600 if later else None))
    unique = {(row["timestamp"], row["replaced_after_hours"]): row for row in found}
    return sorted(unique.values(), key=lambda row: row["timestamp"])


def trajectory_state(state):
    """Collapse target references at one horizon into reference form and advisory line."""
    if state["state"] in {"clock_unknown", "unknown", "ancestry_censored"}:
        return "not_observed"
    if state["state"] == "absent":
        return "absent"
    lines, refs, full = state["lines"], state["references"], state["full_sha"]
    patched = sum(count for line, count in lines.items() if line in PATCHED_LINES)
    earlier = sum(count for line, count in lines.items() if line in EARLIER_LINES)
    label = "unclassified" if refs - patched - earlier else "patched" if not earlier else "earlier" if not patched else "mixed_line"
    if full == refs:
        return "pinned_" + label
    if full == 0:
        return "mutable_" + label
    return "mixed_form"


def target_lines_at(record, days, index):
    # Day 0 is the last state strictly before the anchor, matching the baseline.
    cutoff = record["anchor"] + days * DAY - (1 if days == 0 else 0)
    if not record["clock_aligned"]:
        return dict(state="clock_unknown")
    if not record["baselines"][str(int(record["anchor"]))]["complete"]:
        return dict(state="unknown")
    if _observed_end(record) < cutoff:
        return dict(state="ancestry_censored")
    snapshots = record["snapshots"][:record["endpoint_index"] + 1]
    state = next((s for s in reversed(snapshots) if s["timestamp"] <= cutoff), None)
    if state is None or state["complete"] is not True:
        return dict(state="unknown")
    refs = [ref for ref in record["target_measurement"]["lineages"]["references"] if ref.get("snapshot_sha") == state["sha"]]
    if len(refs) != state["target_references"]:
        raise ValueError("Target reference rows differ from snapshot count for " + record["repository_id"])
    if not refs:
        return dict(state="absent")
    lines = [ref_line(ref, index) for ref in refs]
    return dict(state=_lines_summary(lines), lines=dict(Counter(lines)), references=len(refs),
                full_sha=sum(ref.get("ref_type") == "full_sha" for ref in refs))


def adopted_route(entry, outcome, index):
    """Release identity of the SHA a fixed lineage moved to after an opportunity."""
    if outcome == "adopted_target":
        return "opportunity_target"
    if outcome != "other_sha_change":
        return None
    tags = index.get(str(entry.get("adopted_sha") or "").lower(), set())
    if not tags:
        return "untagged_commit"
    opportunity = version_key(entry.get("opportunity_tag"))
    versions = [v for v in (version_key(tag) for tag in tags) if v is not None and len(v) >= 2]
    if not versions:
        return "moving_tag_only"  # only a major tag such as v4, which can be repointed
    pad = lambda v: v + (0,) * (3 - len(v))
    if opportunity and any(pad(v) > pad(opportunity) for v in versions):
        return "later_release"
    return "other_release"


def pick_release_tag(tags):
    """Most specific release tag (patch level, highest) among the tags bound to one commit."""
    versions = [(version_key(tag), tag) for tag in tags if version_key(tag) is not None and len(version_key(tag)) >= 2]
    return max(versions, key=lambda item: item[0] + (0,) * (3 - len(item[0])))[1] if versions else None


def route_evidence(rows, entries, indices):
    by_key = {(str(e["repository_id"]), e["lineage_id"], e["opportunity_id"]): e for e in entries}
    detail = []
    for row in rows:
        entry = by_key[(row["repository_id"], row["lineage_id"], row["opportunity_id"])]
        if entry["outcome"] != row["outcome"]:
            raise ValueError("Maintenance record outcome differs from the frozen entry")
        detail.append(dict(repository_id=row["repository_id"], lineage_id=row["lineage_id"], role=row["role"],
                           upstream=entry["upstream_repository"], outcome_timestamp=row["outcome_timestamp"],
                           grade=row["evidence_grade"], outcome=row["outcome"], comment=entry["comment_identity_state"],
                           route=adopted_route(entry, row["outcome"], indices[entry["upstream_repository"]]),
                           adopted_sha=entry.get("adopted_sha"), opportunity_tag=entry.get("opportunity_tag"),
                           adopted_tags=sorted(indices[entry["upstream_repository"]].get(str(entry.get("adopted_sha") or "").lower(), ()))))
    return detail


def summarize_routes(detail, ids):
    changed = [d for d in detail if d["route"] is not None]
    clustered = Clustered(ids)
    groups = {"all": lambda d: True, "target": lambda d: d["role"] == "target", "comparison": lambda d: d["role"] == "comparison",
              "comment_verified": lambda d: d["comment"] == "verified_consistent", "comment_other": lambda d: d["comment"] != "verified_consistent"}
    routes = ("opportunity_target", "later_release", "other_release", "moving_tag_only", "untagged_commit")
    summary = dict(entries=len(detail), changed_entries=len(changed), counts={}, shares={}, repositories={})
    for group, keep in groups.items():
        rows = [d for d in changed if keep(d)]
        summary["counts"][group] = {route: sum(d["route"] == route for d in rows) for route in routes}
        summary["repositories"][group] = {route: len({d["repository_id"] for d in rows if d["route"] == route}) for route in routes}
        summary["repositories"][group]["any"] = len({d["repository_id"] for d in rows})
        clustered.add(group + ".changed", Counter(d["repository_id"] for d in rows))
        for route in routes:
            clustered.add(f"{group}.{route}", Counter(d["repository_id"] for d in rows if d["route"] == route))
        clusters = len({d["repository_id"] for d in rows})
        summary["shares"][group] = {route: clustered.evaluate(lambda s, g=group, r=route: s[f"{g}.{r}"] / s[g + ".changed"], clusters) for route in routes}
        summary["shares"][group]["release_channel"] = clustered.evaluate(
            lambda s, g=group: (s[f"{g}.opportunity_target"] + s[f"{g}.later_release"]) / s[g + ".changed"], clusters)
    # Several opportunity entries can share one SHA change; count each change once.
    distinct = {}
    for d in changed:
        distinct.setdefault((d["repository_id"], d["lineage_id"], d["outcome_timestamp"], d["adopted_sha"]), d)
    summary["distinct_changes"] = dict(changes=len(distinct), repositories=len({k[0] for k in distinct}),
                                       untagged=sum(d["route"] == "untagged_commit" for d in distinct.values()),
                                       by_comment=dict(Counter(d["comment"] for d in distinct.values())),
                                       by_role=dict(Counter(d["role"] for d in distinct.values())))
    # Which release an earlier-release adoption landed on, against the tag that defined the opportunity.
    pairs = Counter((d["upstream"], d["opportunity_tag"], pick_release_tag(d["adopted_tags"])) for d in changed if d["route"] == "other_release")
    summary["other_release_pairs"] = [dict(upstream=u, opportunity_tag=o, adopted_tag=a, entries=n) for (u, o, a), n in pairs.most_common()]
    summary["comment_by_route"] = {route: dict(Counter(d["comment"] for d in changed if d["route"] == route)) for route in routes}
    summary["upstream_by_route"] = {route: dict(Counter(d["upstream"] for d in changed if d["route"] == route)) for route in routes}
    summary["interpretation"] = ("Tag identity comes from every archived visit and release record of the upstream. An untagged "
                                 "adopted SHA had no observed tag binding; a later release skips past the opportunity tag.")
    return summary


def summarize_targets(rows):
    counts = lambda key, state: dict(Counter(r.get(key) for r in rows if r["first_response_state"] == state))
    day365 = Counter(r["day365"]["state"] for r in rows)
    pinned_full = [r for r in rows if r["day365"]["state"] in {"all_patched", "all_earlier", "mixed", "unclassified"}
                   and r["day365"]["full_sha"] == r["day365"]["references"]]
    trajectory = {str(days): dict(Counter(r["trajectory"][str(days)] for r in rows)) for days in TRAJECTORY_DAYS}
    return dict(repositories=len(rows), first_response_counts=dict(Counter(r["first_response_state"] for r in rows)),
                trajectory=trajectory,
                tag_update=counts("first_target", "tag_update"), pin=counts("first_target", "pin"),
                sha_target_change=counts("first_target", "sha_target_change"),
                pin_lines=dict(sum((Counter(r["after_lines"]) for r in rows if r["first_response_state"] == "pin"), Counter())),
                tag_update_lines=dict(sum((Counter(r["after_lines"]) for r in rows if r["first_response_state"] == "tag_update"), Counter())),
                compromised_pins=[r["repository_id"] for r in rows if r["compromised_pins"]],
                compromised_replaced_after_hours=[row["replaced_after_hours"] for r in rows for row in r["compromised_pins"]],
                day365=dict(day365), day365_all_full_sha=dict(Counter(r["day365"]["state"] for r in pinned_full)),
                interpretation="Advisory-patched line means v46.0.1 or later, a v46+ major tag, or a commit carrying such a tag in archived or released tag bindings.")


def latest_working_run(root):
    """Newest completed full run of the working-input batch, as (manifest path, record)."""
    found = []
    for path in sorted((root / WORKING_RUNS).glob("*/manifest.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("completed") and not record.get("extract_only") and record.get("manifest_path") == WORKING_MANIFEST:
            found.append((path.relative_to(root).as_posix(), record))
    if not found:
        raise ValueError("No completed full working-input formal-statistics run exists")
    return found[-1]


def _verify(root, run, manifest_path=FROZEN_MANIFEST, results=RESULTS):
    if not run.get("completed") or run.get("extract_only") or not run.get("frozen_inputs_unchanged"):
        raise ValueError("A completed full formal-statistics run is required")
    if sha256(root / manifest_path) != run["freeze_sha256"]:
        raise ValueError("The input freeze differs from the completed run")
    frozen = json.loads((root / manifest_path).read_text(encoding="utf-8"))["input_sha256"]
    bound = {}
    for name in ("rq12_repository_records.jsonl", "rq3_maintenance_records.csv", "rq1_summary.json"):
        path = results + name
        if sha256(root / path) != run["outputs"][path]:
            raise ValueError("Completed run output changed: " + path)
        bound[path] = run["outputs"][path]
    for path in [ENTRIES, *(p for p in frozen if p.startswith(UPSTREAM) and p.count("/") == 3 and p.endswith(".json"))]:
        if sha256(root / path) != frozen[path]:
            raise ValueError("Frozen input changed: " + path)
        bound[path] = frozen[path]
    return bound


def run(root, *, working=False):
    root = Path(root).resolve()
    if working:
        run_path, run_record = latest_working_run(root)
        manifest_path, results, batch = WORKING_MANIFEST, run_record["output_directory"] + "/", WORKING_EXTENSIONS
    else:
        run_path, run_record = RUN, json.loads((root / RUN).read_text(encoding="utf-8"))
        manifest_path, results, batch = FROZEN_MANIFEST, RESULTS, "formal-stats-v2"
    bound = _verify(root, run_record, manifest_path, results)
    frozen = json.loads((root / manifest_path).read_text(encoding="utf-8"))
    formal = {str(i) for i in frozen["formal_population"]["formal_eligible_repository_ids"]}
    with (root / (results + "rq12_repository_records.jsonl")).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    indices = {}
    for path in (p for p in bound if p.startswith(UPSTREAM)):
        document = json.loads((root / path).read_text(encoding="utf-8"))
        indices[document["summary"]["repository"]] = tag_index(document)
    target_index = indices["tj-actions/changed-files"]
    targets = [response_targets(record, target_index) for record in records if record["repository_id"] in formal]
    published = json.loads((root / (results + "rq1_summary.json")).read_text(encoding="utf-8"))["formal"]["first_response_counts"]
    reproduced = Counter(row["first_response_state"] for row in targets)
    if any(reproduced.get(k, 0) != v for k, v in published.items()):
        raise ValueError("Recomputed first responses differ from the frozen RQ1 summary")
    csv.field_size_limit(2**31 - 1)
    with (root / (results + "rq3_maintenance_records.csv")).open(encoding="utf-8", newline="") as stream:
        rows = [row for row in csv.DictReader(stream) if row["population"] == "formal"]
    with (root / ENTRIES).open(encoding="utf-8") as stream:
        entries = [json.loads(line) for line in stream if line.strip()]
    detail = route_evidence(rows, entries, indices)
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  implementation_sha256=sha256(Path(__file__)), source_run=run_path, input_sha256=bound,
                  interrupted_series=interrupted_series(records, formal),
                  response_targets=summarize_targets(targets),
                  updater_routes=summarize_routes(detail, {d["repository_id"] for d in detail}),
                  checks=dict(first_response_reproduced=True, formal_repositories=len(targets) == len(formal),
                              maintenance_rows=len(rows), all_rows_joined=len(detail) == len(rows)))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / "data/derived" / batch / "runs" / stamp
    write_json(directory / "formal_extensions.json", result)
    write_json(directory / "response_targets.json", targets)
    write_json(directory / "updater_routes.json", detail)
    (root / "reports").mkdir(exist_ok=True)
    target = root / "reports/formal_extensions.json"
    if target.relative_to(root).as_posix() in frozen["input_sha256"]:
        raise ValueError("Attempt to overwrite a frozen input")
    target.write_bytes((directory / "formal_extensions.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--working", action="store_true", help="Extend the newest completed formal-stats-v3 run (working-input authority)")
    run(Path(__file__).resolve().parents[1], working=parser.parse_args().working)
