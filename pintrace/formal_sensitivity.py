"""Sensitivity matrix for the headline estimates, crossed with the placebo evidence.

Four kinds of rows answer four different questions and are never mixed in one verdict:

* ``robustness`` scenarios keep the estimand and change the population, the anchor by a day or the incident day
  (raw instead of formal population, minus the pilot, anchor +-1 day, no incident day). Each is compared with the main
  estimate: ``consistent`` (each point inside the other interval), ``overlapping`` or ``separated``. Only these rows
  decide whether a claim is stable.
* ``landmarks`` (first 7 / 30 days dropped) condition on having no response yet, so a shift is expected for first
  responses; they are described, not judged.
* ``calendar_strata`` (before the SHA policy, between policy and immutable releases, after immutable releases) change
  the estimand by design and are described, not judged.
* ``window_end`` scenarios end the observation window 6 calendar months after the anchor, at the original study window
  end (read from the preserved v1 snapshot) and at the full window. The full-window rerun must reproduce the published
  main estimate exactly, which verifies the rerun path itself. The other two show how the estimates accumulate as the
  window grows and how extending the window changed the originally published result; they are described, not judged.

Placebo evidence is attached to a claim with an explicit relation. ``same_construct`` means the placebo estimates the
quantity the claim reports; ``related_construct`` means a neighbouring quantity (for example a conversion rate for a
cumulative pinned share). Only a same-construct placebo that separates from the claim, plus a stable robustness row,
makes a claim ``cross_supported``. Responses to the compromised Action itself have no applicable placebo, because a fake
anchor has no meaning for them. Evidence grades A and B are separate metrics (grade C does not occur in the data).
How much each maintenance result depends on the conservative lineage rule is read from the ``lineage_no_longer_present``
outcome; no alternative lineage rule is rerun.
"""

import argparse
import calendar
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import tomllib

from .formal_activity import truthy
from .formal_common import SEED, REPLICATES, WORKING_MANIFEST, annotate, cluster_bootstrap, sha256, write_json
from .formal_extensions import latest_working_run
from .formal_rq3 import number

RULE_VERSION = "formal-sensitivity-v1"
OUTPUT = "data/derived/formal-stats-v4/runs"
DAY = 86400
ROBUSTNESS = ("raw_main", "exclude_pilot", "anchor_minus_one_day", "anchor_plus_one_day", "exclude_incident_day")
LANDMARKS = ("after_first_seven_days", "after_first_thirty_days")
STRATA = ("before_sha_policy", "sha_policy_before_immutable", "after_immutable_releases")
POPULATION = {"raw_main": "raw", "exclude_pilot": "formal_exclude_pilot"}
WINDOW_LABELS = ("end_6_months_after_anchor", "end_original_window", "end_full_window")
FULL_WINDOW = "end_full_window"
ORIGINAL_STUDY = "data/derived/v1-baseline/study.toml"

# metric name -> key template; {p} population, {l} scenario label
METRICS = {
    "rq1_first_response_pin": "rq1.{p}.{l}.first_response.pin",
    "rq1_first_response_remove": "rq1.{p}.{l}.first_response.remove",
    "rq1_first_response_tag_update": "rq1.{p}.{l}.first_response.tag_update",
    "rq1_no_observed_change": "rq1.{p}.{l}.first_response.no_observed_change",
    "rq1_response_lower_bound_90d": "rq1.{p}.{l}.horizon.90.lower",
    "rq1_response_upper_bound_365d": "rq1.{p}.{l}.horizon.365.upper",
    "rq2_existing_pin_180d_repository_equal": "rq2.{p}.{l}.existing.horizon.180.pin.repository_equal",
    "rq2_existing_pin_365d_repository_equal": "rq2.{p}.{l}.existing.horizon.365.pin.repository_equal",
    "rq2_new_actions_all_full_sha": "rq2.{p}.{l}.new.repository_equal.all_full_sha_known_groups",
    "rq3_retention_180d_baseline_fixed": "robust.{l}.persistence.{p}.baseline_fixed.retention.180.determinate_retained",
    "rq3_retention_180d_observed_pin": "robust.{l}.persistence.{p}.post_anchor_observed_pin.retention.180.determinate_retained",
    "rq3_retention_180d_born_fixed": "robust.{l}.persistence.{p}.post_anchor_born_fixed.retention.180.determinate_retained",
    "maintenance_target_A_adopted": "robust.{l}.maintenance.{p}.target.A.outcome.adopted_target",
    "maintenance_target_B_adopted": "robust.{l}.maintenance.{p}.target.B.outcome.adopted_target",
    "maintenance_target_A_lineage_lost": "robust.{l}.maintenance.{p}.target.A.outcome.lineage_no_longer_present",
    "maintenance_target_B_lineage_lost": "robust.{l}.maintenance.{p}.target.B.outcome.lineage_no_longer_present",
}
# the subset a window-end rerun can recompute without the opportunity machinery
RERUN = tuple(name for name in METRICS if not name.startswith("maintenance") and "365d" not in name)

# claim -> metric, plus either the placebo evidence with its relation to the claim or the reason none applies
TARGET_REASON = "a response to the compromised Action is incident-defined; a fake anchor has no meaning for it"
CLAIMS = {
    "first_response_pin_share": dict(metric="rq1_first_response_pin", reason=TARGET_REASON),
    "first_response_removal_share": dict(metric="rq1_first_response_remove", reason=TARGET_REASON),
    "existing_references_pinned_by_180d": dict(
        metric="rq2_existing_pin_180d_repository_equal", relation="related_construct",
        placebo=("month_scan", "first post-incident month against every pre-incident month"),
        note="placebo estimates a 30-day conversion rate of other Actions; the claim is a cumulative pinned share at 180 days"),
    "new_actions_fully_pinned_at_introduction": dict(
        metric="rq2_new_actions_all_full_sha", relation="related_construct",
        placebo=("claim", "new_fixed_share_change_vs_control_first_30d"),
        note="placebo is a pooled treated-minus-control change in the fixed share of new references per 30-day bin; the claim is a repository-equal share over all known groups"),
    "persistence_180d_observed_pin": dict(metric="rq3_retention_180d_observed_pin", reason="descriptive retention; no counterfactual design"),
    "persistence_180d_baseline_fixed": dict(metric="rq3_retention_180d_baseline_fixed", reason="descriptive retention; no counterfactual design"),
    "maintenance_A_target_adoption": dict(metric="maintenance_target_A_adopted", reason="opportunity-conditioned outcome; no placebo design"),
}


def key_for(template, label, population):
    return template.format(l=label, p=population)


def entry(metrics, key):
    value = metrics.get(key)
    if value is None or value.get("estimate") is None:
        return None
    return value


def compare(main, other):
    """Position of ``other`` against ``main`` by estimate and 95% percentile interval."""
    if main is None or other is None or None in (main.get("lower"), main.get("upper"), other.get("lower"), other.get("upper")):
        return "undefined"
    if other["upper"] < main["lower"] or other["lower"] > main["upper"]:
        return "separated"
    inside = other["lower"] <= main["estimate"] <= other["upper"] and main["lower"] <= other["estimate"] <= main["upper"]
    return "consistent" if inside else "overlapping"


def cell(metrics, main_key, key):
    value, main = entry(metrics, key), entry(metrics, main_key)
    if value is None:
        return dict(key=key, available=False, relation="undefined")
    out = {field: value[field] for field in ("estimate", "lower", "upper", "valid_replicates")}
    out.update(key=key, available=True, relation=compare(main, value), interval_status=annotate(dict(value, requested_replicates=value.get("requested_replicates", REPLICATES)))["status"],
               shift_from_main=None if main is None else value["estimate"] - main["estimate"])
    return out


def build_matrix(metrics):
    result = {}
    for name, template in METRICS.items():
        main_key = key_for(template, "formal_main", "formal")
        row = dict(main=cell(metrics, main_key, main_key), robustness={}, landmarks={}, calendar_strata={})
        for label in ROBUSTNESS:
            row["robustness"][label] = cell(metrics, main_key, key_for(template, label, POPULATION.get(label, "formal")))
        for label in LANDMARKS:
            row["landmarks"][label] = cell(metrics, main_key, key_for(template, label, "formal"))
        for label in STRATA:
            row["calendar_strata"][label] = cell(metrics, main_key, key_for(template, label, "formal"))
        relations = [c["relation"] for c in row["robustness"].values()]
        row["summary"] = dict(scenarios=len(relations), consistent=relations.count("consistent"), overlapping=relations.count("overlapping"),
                              separated=relations.count("separated"), undefined=relations.count("undefined"),
                              separated_scenarios=[l for l, c in row["robustness"].items() if c["relation"] == "separated"],
                              undefined_scenarios=[l for l, c in row["robustness"].items() if c["relation"] == "undefined"],
                              # landmarks are not judged, but a separated landmark stays visible beside the judged count
                              separated_landmarks=[l for l, c in row["landmarks"].items() if c["relation"] == "separated"])
        result[name] = row
    return result


def placebo_evidence(spec, placebo):
    if "placebo" not in spec:
        return dict(available=False, reason=spec["reason"])
    kind, detail = spec["placebo"]
    if kind == "month_scan":
        scan = placebo["within_pre_months"]["other"]["first_post_month"]["lower_above_every_pre_month_upper"]
        return dict(available=True, relation=spec["relation"], design=detail, supports=bool(scan), note=spec["note"])
    verdict = placebo["claims"][detail]
    return dict(available=True, relation=spec["relation"], design="fake anchor 90 days earlier", placebo_claim=detail,
                verdict=verdict["verdict"], marginal=verdict["marginal"], supports=verdict["verdict"] == "separated_from_placebo", note=spec["note"])


def cross_support(matrix, placebo):
    claims = {}
    for claim, spec in CLAIMS.items():
        summary = matrix[spec["metric"]]["summary"]
        evidence = placebo_evidence(spec, placebo)
        stable = summary["separated"] == 0 and summary["undefined"] == 0
        reasons = []
        if not evidence["available"]:
            reasons.append("no placebo design: " + evidence["reason"])
        elif not evidence["supports"]:
            reasons.append("placebo does not separate")
        elif evidence["relation"] != "same_construct":
            reasons.append("placebo estimates a related construct: " + evidence["note"])
        if not stable:
            reasons.append(f"{summary['separated']} robustness scenario(s) separated ({', '.join(summary['separated_scenarios']) or 'none'}), "
                           f"{summary['undefined']} undefined ({', '.join(summary['undefined_scenarios']) or 'none'})")
        if summary["separated_landmarks"]:
            reasons.append("landmark rows separated (not judged, estimand changes): " + ", ".join(summary["separated_landmarks"]))
        if not evidence["available"]:
            status = "no_placebo_design"
        elif not evidence["supports"]:
            status = "placebo_does_not_separate"
        elif not stable:
            status = "sensitivity_unstable"
        elif evidence["relation"] != "same_construct":
            status = "related_placebo_only"
        else:
            status = "cross_supported"
        claims[claim] = dict(metric=spec["metric"], sensitivity=dict(summary, main_status=matrix[spec["metric"]]["main"].get("interval_status")),
                             placebo=evidence, robustness_stable=stable, status=status, cross_supported=status == "cross_supported", why_not=reasons)
    return claims


def six_months_after(anchor):
    """Last second of the calendar day six months after the anchor, matching the time-of-day of the study window end."""
    moment = datetime.fromtimestamp(anchor, timezone.utc)
    index = moment.month - 1 + 6
    year, month = moment.year + index // 12, index % 12 + 1
    return int(datetime(year, month, min(moment.day, calendar.monthrange(year, month)[1]), 23, 59, 59, tzinfo=timezone.utc).timestamp())


def window_ends(anchor, original_end, full_end):
    ends = {WINDOW_LABELS[0]: six_months_after(anchor), WINDOW_LABELS[1]: int(original_end), WINDOW_LABELS[2]: int(full_end)}
    if not anchor < ends[WINDOW_LABELS[0]] < ends[WINDOW_LABELS[1]] < ends[WINDOW_LABELS[2]]:
        raise ValueError("Window ends must increase from the anchor")
    return ends


def day(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")


def rerun_windows(records, episodes, formal_ids, anchor, ends):
    """Recompute the rerunnable metrics with the observation window ended at each of ``ends`` (label -> timestamp)."""
    from .formal_rq12 import summarize
    from .formal_rq3 import summarize_persistence
    from .formal_robustness import truncate_episodes

    ids = sorted(str(identity) for identity in formal_ids)
    members = set(ids)
    context = dict(metrics=[], populations=dict(formal=ids))
    formal_episodes = [row for row in episodes if str(row["repository_id"]) in members]
    registered = {}
    for label, end in ends.items():
        summarize(records, members, dict(anchor=anchor, end=end, label=label), context=context, population="formal")
        selected, excluded = truncate_episodes(formal_episodes, anchor, end)
        eligible = {row["episode_id"] for row in selected if row["duration_eligible"] and row["observed_followup_seconds"] > 0}
        summarize_persistence(selected, eligible, context, population="formal", prefix="robust." + label)
        registered[label] = dict(end=day(end), end_timestamp=end, episodes=len(selected), duration_eligible=len(eligible),
                                 episodes_outside_window=excluded["outside_segment"])
    wanted = {key_for(METRICS[name], label, "formal"): (label, name) for label in ends for name in RERUN}
    bootstrap = cluster_bootstrap([m for m in context["metrics"] if m["key"] in wanted], context["populations"])["metrics"]
    rerun = {label: {} for label in ends}
    for key, (label, name) in wanted.items():
        value = bootstrap.get(key)
        if value is not None and value["estimate"] is not None:
            rerun[label][name] = {field: value[field] for field in ("estimate", "lower", "upper", "valid_replicates")}
    return rerun, registered


def full_window_check(rerun, metrics, label=FULL_WINDOW, tolerance=1e-12):
    """The rerun path with the unchanged window must give the published main estimate and interval."""
    mismatched, compared = [], 0
    for name in RERUN:
        published = entry(metrics, key_for(METRICS[name], "formal_main", "formal"))
        value = rerun[label].get(name)
        if published is None and value is None:
            continue
        compared += 1
        if (published is None) != (value is None) or any(
                (published[f] is None) != (value[f] is None) or (published[f] is not None and abs(published[f] - value[f]) > tolerance)
                for f in ("estimate", "lower", "upper")):
            mismatched.append(name)
    return dict(compared=compared, mismatched=mismatched, equal=not mismatched and compared > 0)


def window_end_rows(rerun, metrics, v1):
    """Recomputed window-end estimates beside the published main estimate and, where it exists, the original result."""
    rows = {}
    for name in RERUN:
        main_key = key_for(METRICS[name], "formal_main", "formal")
        row = dict(published_main=entry(metrics, main_key), original_window_published=entry(v1, main_key), windows={})
        for label, values in rerun.items():
            value = values.get(name)
            window = dict(available=value is not None)
            if value is not None:
                window.update(value, relation_to_published_main=compare(row["published_main"], value),
                              relation_to_original_window_published=compare(row["original_window_published"], value))
            row["windows"][label] = window
        rows[name] = row
    return rows


def concentration(extensions):
    """Leave-out ratios from the interrupted series: point estimates only, kept next to the matrix."""
    block = extensions["interrupted_series"]["concentration"]
    above_one = lambda values: all(v is not None and v > 1 for v in values.values())
    return dict(first_month_conversions=block["first_month_conversions"], repositories_with_conversions=block["repositories_with_conversions"],
                top10_share=block["top10_share"], pre_conversions=block["pre_conversions"], pre_top5_share=block["pre_top5_share"],
                leave_out_largest_post_contributors=block["leave_out"], leave_out_largest_pre_contributors=block["leave_out_pre"],
                all_post_leave_out_ratios_above_one=above_one(block["leave_out"]), all_pre_leave_out_ratios_above_one=above_one(block["leave_out_pre"]),
                note="Point estimates without intervals. Removing the largest pre-incident contributors thins the baseline, so those ratios grow.")


def load_episodes(path):
    with path.open(encoding="utf-8", newline="") as stream:
        episodes = [dict(row) for row in csv.DictReader(stream)]
    for row in episodes:
        for field in ("entry_timestamp", "exit_timestamp"):
            row[field] = number(row[field])
        row["duration_eligible"] = truthy(row["duration_eligible"]) is True
        row["formal"] = truthy(row["formal"]) is True
    return episodes


def verified(root, run_record, name):
    path = run_record["output_directory"] + "/" + name
    if sha256(root / path) != run_record["outputs"][path]:
        raise ValueError("Completed run output changed: " + path)
    return root / path, path


def run(root):
    root = Path(root).resolve()
    run_path, run_record = latest_working_run(root)
    manifest = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    if sha256(root / WORKING_MANIFEST) != run_record["freeze_sha256"]:
        raise ValueError("The working-input manifest differs from the completed run")
    uncertainty_file, uncertainty_path = verified(root, run_record, "uncertainty_intervals.json")
    cache_file, cache_path = verified(root, run_record, "rq12_repository_records.jsonl")
    episodes_file, episodes_path = verified(root, run_record, "rq3_episode_records.csv")
    placebo_path, extensions_path = "reports/formal_placebo.json", "reports/formal_extensions.json"
    placebo = json.loads((root / placebo_path).read_text(encoding="utf-8"))
    extensions = json.loads((root / extensions_path).read_text(encoding="utf-8"))
    if placebo["source_run"] != run_path or extensions["source_run"] != run_path:
        raise ValueError("formal_placebo and formal_extensions must come from the newest completed working-input run")
    v1_path = "data/derived/v1-baseline/reports/uncertainty_intervals.json"
    metrics = json.loads(uncertainty_file.read_text(encoding="utf-8"))["metrics"]
    v1 = json.loads((root / v1_path).read_text(encoding="utf-8"))["metrics"]
    study = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    original = tomllib.loads((root / ORIGINAL_STUDY).read_text(encoding="utf-8"))["study"]
    stamp_of = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    anchor = stamp_of(study["event_anchor"])
    if stamp_of(original["event_anchor"]) != anchor:
        raise ValueError("The preserved original study has a different anchor")
    ends = window_ends(anchor, stamp_of(original["window_end"]), stamp_of(study["window_end"]))
    with cache_file.open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    rerun, registered = rerun_windows(records, load_episodes(episodes_file), manifest["formal_population"]["formal_eligible_repository_ids"], anchor, ends)
    del records
    matrix = build_matrix(metrics)
    window_rows = window_end_rows(rerun, metrics, v1)
    self_check = full_window_check(rerun, metrics)
    claims = cross_support(matrix, placebo)
    missing = {name: row["summary"]["undefined_scenarios"] for name, row in matrix.items() if row["summary"]["undefined"]}
    required = dict(main_available_for_every_matrix_metric=all(row["main"]["available"] for row in matrix.values()),
                    full_window_rerun_equals_published_main=self_check["equal"],
                    every_rerun_metric_recomputed_in_some_window=all(any(w["available"] for w in row["windows"].values()) for row in window_rows.values()),
                    window_ends_inside_study_window=ends[WINDOW_LABELS[0]] < ends[WINDOW_LABELS[1]] < ends[WINDOW_LABELS[2]],
                    placebo_and_extensions_from_newest_run=True)
    if not all(required.values()):
        raise ValueError("Sensitivity checks failed: " + ", ".join(name for name, ok in required.items() if not ok)
                         + ("; mismatched: " + ", ".join(self_check["mismatched"][:8]) if self_check["mismatched"] else ""))
    result = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), source_run=run_path, seed=SEED, replicates=REPLICATES,
        input_sha256={uncertainty_path: run_record["outputs"][uncertainty_path], cache_path: run_record["outputs"][cache_path],
                      episodes_path: run_record["outputs"][episodes_path], placebo_path: sha256(root / placebo_path),
                      extensions_path: sha256(root / extensions_path), v1_path: sha256(root / v1_path),
                      ORIGINAL_STUDY: sha256(root / ORIGINAL_STUDY), WORKING_MANIFEST: sha256(root / WORKING_MANIFEST)},
        implementation_sha256=sha256(Path(__file__)),
        scenario_groups=dict(robustness=list(ROBUSTNESS), landmarks=list(LANDMARKS), calendar_strata=list(STRATA), window_end=registered),
        matrix=matrix, window_end=window_rows, full_window_self_check=self_check, concentration=concentration(extensions), claims=claims,
        claim_status_counts={status: sum(c["status"] == status for c in claims.values()) for status in sorted({c["status"] for c in claims.values()})},
        checks=dict(required, robustness_cells_all_available=not missing), cells_not_available=missing,
        limits=["Verdicts place 95% percentile intervals relative to the main estimate; they are not tests.",
                "Landmark and calendar-stratum rows change the estimand and are described only; window-end rows show accumulation and are described only.",
                "Window-end reruns apply the main rules to a shortened window; episodes still unresolved at the new end are right-censored, not exits.",
                "A claim is cross-supported only with a same-construct placebo and a stable robustness row; other claims are listed with the reason.",
                "Maintenance metrics are not rerun at shortened windows because opportunity availability bounds are tied to the full window.",
                "Lineage conservatism is read from the lineage_no_longer_present outcome; no alternative lineage rule is rerun.",
                "Evidence grade C does not occur in the data; grades A and B are reported separately.",
                "No model is fitted and no closed analysis is opened."])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / OUTPUT / stamp
    write_json(directory / "formal_sensitivity.json", result)
    (root / "reports/formal_sensitivity.json").write_bytes((directory / "formal_sensitivity.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run(Path(__file__).resolve().parents[1])
