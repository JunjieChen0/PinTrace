"""Invariants for the post-review analyses reported in reports/post_review.json."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = json.loads((ROOT / "reports/post_review.json").read_text(encoding="utf-8"))


def test_conversion_split_sums_to_the_total():
    split = REPORT["rq2_split"]
    assert split["within"]["365"] + split["after_day_365"] == split["conversions"]
    assert split["within"]["30"] <= split["within"]["90"] <= split["within"]["365"]


def test_stock_series_is_ordered_and_bounded():
    series = REPORT["stock"]["series"]
    offsets = [row["offset_days"] for row in series]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)
    for row in series:
        for key in ("repository_equal", "pooled"):
            assert 0.0 <= row[key]["lower"] <= row[key]["estimate"] <= row[key]["upper"] <= 1.0


def test_owner_clustering_keeps_the_point_estimates():
    clusters = REPORT["clusters"]
    for scope in ("other", "target"):
        repository, owner = clusters[f"{scope}.repository"], clusters[f"{scope}.owner"]
        for key in repository:
            assert abs(repository[key]["estimate"] - owner[key]["estimate"]) < 1e-9
            assert owner[key]["n_clusters"] <= repository[key]["n_clusters"]
