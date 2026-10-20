"""Side-by-side comparison of treated and matched-control repositories around the event anchor.

Controls hold no target-action workflow at their baseline, so the comparable outcome is the conversion of mutable direct
references to other Actions into full-SHA pins, and the pinned share of newly introduced Action identities. Each group is
compared with its own pre-incident 180 days, and the two group-level ratios are compared with each other. Intervals come
from whole-repository resampling, drawn separately inside each group with fixed seeds, 2000 replicates, percentile 95%.
The contrast is descriptive. It adjusts for the matched covariates only and does not establish an incident effect.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tomllib

from .formal_common import REPLICATES, SEED
from .formal_extensions import BINS, KINDS, PER, PERIODS, conversion_series, new_identity_series

RULE_VERSION = "control-compare-v1"
OUTPUT = "reports/control_comparison.json"
FIRST_BIN = BINS[0]


def _columns(bins):
    return [b - FIRST_BIN for b in bins]


class Group:
    """Per-repository bin arrays for one group: exposure, events, at-risk and converted flags, new identities."""

    def __init__(self, name, series, identities):
        import numpy as np
        self.name = name
        self.ids = sorted(series)
        width = len(BINS)
        fields = {}
        for scope in ("other", "target"):
            for field in ("exposure", "at_risk", "converted", *KINDS):
                fields[f"{scope}.{field}"] = np.zeros((len(self.ids), width))
        for row, identity in enumerate(self.ids):
            for scope in ("other", "target"):
                for b in BINS:
                    cell = series[identity][scope][b]
                    column = b - FIRST_BIN
                    fields[f"{scope}.exposure"][row, column] = cell["exposure"]
                    fields[f"{scope}.at_risk"][row, column] = float(cell["exposure"] > 0)
                    fields[f"{scope}.converted"][row, column] = float(cell["exposure"] > 0 and cell["pin"] > 0)
                    for kind in KINDS:
                        fields[f"{scope}.{kind}"][row, column] = cell[kind]
        fields["new.known"] = np.array([[identities[i][b]["known"] for b in BINS] for i in self.ids], dtype=float)
        fields["new.fixed"] = np.array([[identities[i][b]["fixed"] for b in BINS] for i in self.ids], dtype=float)
        self.fields = fields

    def totals(self, weights=None):
        """Column sums per field; with a (replicates x repositories) weight matrix, one row of sums per replicate."""
        return {name: (array.sum(axis=0) if weights is None else weights @ array) for name, array in self.fields.items()}


def _sum(sums, name, bins):
    return sums[name][..., _columns(bins)].sum(axis=-1)


def rate(sums, scope, kind, bins):
    return PER * _sum(sums, f"{scope}.{kind}", bins) / _sum(sums, f"{scope}.exposure", bins)


def rate_ratio(sums, scope, kind, bins, pre=None):
    return rate(sums, scope, kind, bins) / rate(sums, scope, kind, PERIODS["pre"] if pre is None else pre)


def converted_share(sums, scope, bins):
    return _sum(sums, f"{scope}.converted", bins) / _sum(sums, f"{scope}.at_risk", bins)


def converted_share_ratio(sums, scope, bins, pre=None):
    return converted_share(sums, scope, bins) / converted_share(sums, scope, PERIODS["pre"] if pre is None else pre)


def fixed_share(sums, bins):
    return _sum(sums, "new.fixed", bins) / _sum(sums, "new.known", bins)


def fixed_share_change(sums, bins, pre=None):
    return fixed_share(sums, bins) - fixed_share(sums, PERIODS["pre"] if pre is None else pre)


# Placebo anchor 90 days before the real one. Only the 90 days after it (up to the real anchor) are used, and the
# pre-period holds the three months that the observation window start leaves before it.
PLACEBO_PERIODS = {"pre": range(-3, 0), "post_0_30": range(0, 1), "post_30_90": range(1, 3), "post_0_90": range(0, 3)}
PLACEBO_SHIFT_DAYS = 90


def statistics(sums_t, sums_c, periods=PERIODS):
    """Every reported quantity as a function of the two groups' sums. Values may be scalars or per-replicate arrays."""
    import numpy as np
    pre = periods["pre"]
    output = {}
    for name, bins in periods.items():
        if name == "pre":
            continue
        for kind in KINDS:
            output[f"treated.other.{kind}_ratio.{name}"] = rate_ratio(sums_t, "other", kind, bins, pre)
            output[f"control.other.{kind}_ratio.{name}"] = rate_ratio(sums_c, "other", kind, bins, pre)
            output[f"ratio_of_ratios.other.{kind}.{name}"] = rate_ratio(sums_t, "other", kind, bins, pre) / rate_ratio(sums_c, "other", kind, bins, pre)
        output[f"treated.other.converted_share_ratio.{name}"] = converted_share_ratio(sums_t, "other", bins, pre)
        output[f"control.other.converted_share_ratio.{name}"] = converted_share_ratio(sums_c, "other", bins, pre)
        output[f"ratio_of_ratios.other.converted_share.{name}"] = converted_share_ratio(sums_t, "other", bins, pre) / converted_share_ratio(sums_c, "other", bins, pre)
        output[f"treated.new_fixed_share_change.{name}"] = fixed_share_change(sums_t, bins, pre)
        output[f"control.new_fixed_share_change.{name}"] = fixed_share_change(sums_c, bins, pre)
        output[f"difference_in_changes.new_fixed_share.{name}"] = fixed_share_change(sums_t, bins, pre) - fixed_share_change(sums_c, bins, pre)
    for name, bins in periods.items():
        for kind in KINDS:
            output[f"treated.other.{kind}_rate.{name}"] = rate(sums_t, "other", kind, bins)
            output[f"control.other.{kind}_rate.{name}"] = rate(sums_c, "other", kind, bins)
        output[f"treated.other.converted_share.{name}"] = converted_share(sums_t, "other", bins)
        output[f"control.other.converted_share.{name}"] = converted_share(sums_c, "other", bins)
        output[f"treated.new_fixed_share.{name}"] = fixed_share(sums_t, bins)
        output[f"control.new_fixed_share.{name}"] = fixed_share(sums_c, bins)
    return {key: value for key, value in output.items() if np.ndim(value) >= 0}


def shift_record(record, fake_anchor, true_anchor):
    """The same repository history read as if the anchor were `fake_anchor`, with observation ending at the real anchor.

    Only the anchor and the end of observation change. Snapshots, events and first-observation groups are the measured ones,
    so nothing after the real anchor can enter a placebo bin.
    """
    return dict(record, anchor=fake_anchor, window_end=true_anchor, observed_ancestry_covers_end=True)


def compare(treated, control, replicates=REPLICATES, seed=SEED, periods=PERIODS):
    import numpy as np
    draws = {}
    for group, offset in ((treated, 0), (control, 1)):
        rng = np.random.default_rng(seed + offset)
        n = len(group.ids)
        draws[group.name] = rng.multinomial(n, np.full(n, 1 / n), size=replicates).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        estimate = statistics(treated.totals(), control.totals(), periods)
        samples = statistics(treated.totals(draws[treated.name]), control.totals(draws[control.name]), periods)
    result = {}
    for key, value in estimate.items():
        values = np.asarray(samples[key], dtype=float)
        values = values[np.isfinite(values)]
        point = float(value) if np.isfinite(value) else None
        bounds = [float(v) for v in np.quantile(values, [0.025, 0.975], method="linear")] if len(values) else [None, None]
        result[key] = dict(estimate=point, lower=bounds[0], upper=bounds[1], valid_replicates=int(len(values)), requested_replicates=replicates)
    return result


def placebo_estimates(treated_records, control_records, treated_ids, control_ids, true_anchor, replicates=REPLICATES):
    """The same contrast with a fake anchor PLACEBO_SHIFT_DAYS before the real one and observation cut at the real anchor."""
    fake = true_anchor - PLACEBO_SHIFT_DAYS * 86400
    shifted = lambda records: [shift_record(record, fake, true_anchor) for record in records]
    treated, _ = build_group("treated", shifted(treated_records), treated_ids)
    control, _ = build_group("control", shifted(control_records), control_ids)
    return dict(fake_anchor=fake, true_anchor=true_anchor, shift_days=PLACEBO_SHIFT_DAYS, periods={k: [v.start, v.stop] for k, v in PLACEBO_PERIODS.items()},
                repositories=dict(treated=len(treated.ids), control=len(control.ids)),
                estimates=compare(treated, control, replicates=replicates, periods=PLACEBO_PERIODS))


def build_group(name, records, ids):
    included = [record for record in records if record["repository_id"] in ids and record["clock_aligned"]]
    series = {record["repository_id"]: conversion_series(record) for record in included}
    identities = {record["repository_id"]: new_identity_series(record) for record in included}
    return Group(name, series, identities), sorted(set(ids) - set(series))


def load_records(root, directory, ids, study):
    from .formal_rq12 import extract_path
    records = []
    for identity in sorted(ids, key=int):
        path = Path(root) / directory / f"{identity}.json"
        if path.is_file():
            records.append(extract_path(path, study))
    return records


def run(root):
    root = Path(root)
    study = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    formal = {str(i) for i in json.loads((root / "reports/entity_identity_summary.json").read_text(encoding="utf-8"))["formal_eligible_repository_ids"]}
    selection = json.loads((root / "data/derived/control-v1/matched_controls.json").read_text(encoding="utf-8"))
    controls = {str(row["repository_id"]) for row in selection["controls"]}
    treated_records = load_records(root, "data/derived/measurement-v4", formal, study)
    control_records = load_records(root, "data/derived/measurement-control-v1", controls, study)
    treated, treated_excluded = build_group("treated", treated_records, formal)
    control, control_excluded = build_group("control", control_records, controls)
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), seed=SEED, replicates=REPLICATES,
                  window=dict(start=study["window_start"], anchor=study["event_anchor"], end=study["window_end"]),
                  repositories=dict(treated=len(treated.ids), control=len(control.ids)),
                  treated_without_usable_record=treated_excluded, control_without_usable_record=control_excluded,
                  balance=dict(smd_after_selection=selection["smd_after_selection"], smd_pool_before_selection=selection["smd_pool_before_selection"],
                               threshold=selection["smd_threshold"], balance_passed=selection["balance_passed"]),
                  estimates=compare(treated, control),
                  placebo=placebo_estimates(treated_records, control_records, formal, controls, treated_records[0]["anchor"]),
                  input_sha256={"data/derived/control-v1/matched_controls.json": hashlib.sha256((root / "data/derived/control-v1/matched_controls.json").read_bytes()).hexdigest(),
                                "study.toml": hashlib.sha256((root / "study.toml").read_bytes()).hexdigest()},
                  interpretation=("Each group is compared with its own pre-incident 180 days; the ratio of the two ratios is descriptive. "
                                  "Matching adjusts for pre-anchor activity, mutable-reference share and owner type only."))
    (root / OUTPUT).write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    outcome = run(Path(__file__).resolve().parents[1])
    print(json.dumps(dict(repositories=outcome["repositories"], balance_passed=outcome["balance"]["balance_passed"]), indent=2))
