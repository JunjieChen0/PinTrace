import pytest

from pintrace.formal_common import add_quantile, add_ratio, cluster_bootstrap


def context():
    return {"populations": {"formal": ["a", "b", "c"]}, "metrics": []}


def test_cluster_rows_stay_together_and_replicate_exactly():
    ctx = context()
    add_ratio(ctx, "reference_weighted", [("a", 1, 10), ("a", 1, 10), ("b", 1, 1)])
    add_quantile(ctx, "episode_duration", [("a", 1), ("a", 100), ("b", 4)])
    first = cluster_bootstrap(ctx["metrics"], ctx["populations"], replicates=2000)
    second = cluster_bootstrap(ctx["metrics"], ctx["populations"], replicates=2000)
    assert first == second
    assert first["metrics"]["reference_weighted"]["estimate"] == 3 / 21
    assert first["metrics"]["episode_duration"]["estimate"] == 4
    assert first["metrics"]["reference_weighted"]["n_clusters"] == 2


def test_zero_bootstrap_is_marked_and_binomial_interval_is_not_zero_width():
    ctx = context()
    add_ratio(ctx, "zero", [("a", 0, 1), ("b", 0, 1)], binary=True)
    result = cluster_bootstrap(ctx["metrics"], ctx["populations"], replicates=2000)["metrics"]["zero"]
    assert result["status"] == "zero_degenerate"
    assert result["exact_binomial"]["upper"] > 0
    assert result["exact_binomial"]["denominator"] == 2


def test_no_eligible_new_dependencies_is_not_imputed_as_zero():
    ctx = context()
    assert add_ratio(ctx, "empty", [("a", 0, 0)]) is None
    result = cluster_bootstrap(ctx["metrics"], ctx["populations"], replicates=2000)["metrics"]["empty"]
    assert result["n_clusters"] == 0
    assert result["estimate"] is None and result["lower"] is None


def test_invalid_exact_denominator_and_duplicate_estimands_fail():
    ctx = context()
    with pytest.raises(ValueError, match="binary"):
        add_ratio(ctx, "bad", [("a", 1, 2)], binary=True)
    add_ratio(ctx, "one", [("a", 1, 1)])
    with pytest.raises(ValueError, match="Duplicate"):
        add_ratio(ctx, "one", [("b", 0, 1)])
    with pytest.raises(ValueError, match="ineligible"):
        add_ratio(ctx, "other", [("outside", 0, 1)])
