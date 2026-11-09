"""Strata of the wave sensitivity (pintrace/stratified_waves.py) and invariants of reports/stratified_waves.json."""

import json
from pathlib import Path

import pytest

from pintrace import stratified_waves as sw

ROOT = Path(__file__).resolve().parents[1]
SPLITS = (("activity_q1_or_less", "activity_above_q1"), ("activity_median_or_less", "activity_above_median"),
          ("user_owned", "organization_owned"), ("target_not_all_sha", "target_all_sha"))


def rows():
    return [dict(repository_id=str(i), git_commits_180d=i, owner_type="User" if i % 3 == 0 else "Organization",
                 target_full_sha=1 if i % 4 == 0 else 0, target_mutable=0 if i % 4 == 0 else 2) for i in range(1, 13)]


def test_every_split_partitions_the_cohort():
    groups, cuts = sw.strata(rows())
    assert groups["all"] == {str(i) for i in range(1, 13)}
    for stratum, complement in SPLITS:
        assert groups[stratum] | groups[complement] == groups["all"] and not groups[stratum] & groups[complement]
    assert cuts["activity_median"] == 6.5
    assert groups["activity_median_or_less"] == {str(i) for i in range(1, 7)}
    assert groups["user_owned"] == {"3", "6", "9", "12"} and groups["target_all_sha"] == {"4", "8", "12"}


def test_unstratified_estimate_must_match_the_published_one():
    published = {"clusters": {"other.repository": {k: {"estimate": 2.0} for k in ("rr_m1", "rr_m1318", "share_ratio_m1318")}}}
    sw.check_against_published({"ratios": {k: {"estimate": 2.0} for k in ("rr_m1", "rr_m1318", "share_ratio_m1318")}}, published)
    with pytest.raises(ValueError, match="rr_m1318"):
        sw.check_against_published({"ratios": {"rr_m1": {"estimate": 2.0}, "rr_m1318": {"estimate": 2.5},
                                               "share_ratio_m1318": {"estimate": 2.0}}}, published)


def stratum(**changes):
    metric = dict(estimate=2.0, lower=1.1, upper=4.0, valid_replicates=2000, requested_replicates=2000)
    base = dict(repositories=10, periods={p: dict(conversions=3, exposure=50.0) for p in sw.PERIODS},
                ratios={k: dict(metric) for k in sw.STATISTICS}, undefined_reasons={})
    return {**base, **changes}


def test_acceptance_passes_a_complete_stratum_and_names_each_gap():
    assert sw.acceptance({"ok": stratum()})["passed"]
    empty = stratum(periods={p: dict(conversions=0, exposure=0.0) for p in sw.PERIODS})
    thin = stratum(ratios={k: dict(estimate=2.0, lower=1.0, upper=3.0, valid_replicates=1000, requested_replicates=2000)
                           for k in sw.STATISTICS})
    silent = stratum(ratios={**stratum()["ratios"], "rr_m1318": None})
    explained = stratum(ratios={**stratum()["ratios"], "rr_m1318": None},
                        undefined_reasons={"rr_m1318": "no pre-incident conversion in this stratum"})
    result = sw.acceptance({"empty": empty, "thin": thin, "silent": silent, "explained": explained})
    assert not result["passed"]
    assert any(p.startswith("empty: pre") for p in result["problems"])
    assert any(p.startswith("thin: rr_m1 rests on 1000") for p in result["problems"])
    assert "silent: rr_m1318 is undefined without a reason" in result["problems"]
    assert not any(p.startswith("explained") for p in result["problems"])


def test_report_passes_acceptance():
    path = ROOT / "reports/stratified_waves.json"
    if not path.exists():
        pytest.skip("stratified_waves.json not built; run python -m pintrace.stratified_waves --check on real data")
    assert sw.acceptance(json.loads(path.read_text(encoding="utf-8"))["strata"])["passed"]


def test_report_strata_add_up_to_the_cohort():
    path = ROOT / "reports/stratified_waves.json"
    if not path.exists():
        pytest.skip("stratified_waves.json not built")
    strata = json.loads(path.read_text(encoding="utf-8"))["strata"]
    for stratum, complement in SPLITS:
        assert strata[stratum]["repositories"] + strata[complement]["repositories"] == strata["all"]["repositories"]
        for period in sw.PERIODS:
            assert (strata[stratum]["periods"][period]["conversions"] + strata[complement]["periods"][period]["conversions"]
                    == strata["all"]["periods"][period]["conversions"])
    published = json.loads((ROOT / "reports/post_review.json").read_text(encoding="utf-8"))
    assert strata["all"]["ratios"]["rr_m1"]["estimate"] == pytest.approx(
        published["clusters"]["other.repository"]["rr_m1"]["estimate"])
