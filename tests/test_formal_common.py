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


# --- input authorities: the frozen manifest and the working-input manifest -----------------------------------------

import json

from pintrace.formal_common import (FROZEN_MANIFEST, WORKING_MANIFEST, bound_input, bound_measurements, expected_counts,
                                    make_context, manifest_file, require_context, sha256, verify_frozen_inputs, write_json)


def authority(root, path, status, **extra):
    (root / "reports").mkdir(exist_ok=True)
    (root / "study.toml").write_text('[study]\nwindow_end = "2026-09-14T23:59:59Z"\n', encoding="utf-8")
    manifest = dict(status=status, formal_population=dict(raw_confirmed_repository_ids=["1", "2"], formal_eligible_repository_ids=["1"]),
                    input_sha256={"study.toml": sha256(root / "study.toml"), **extra.pop("inputs", {})}, **extra)
    write_json(root / path, manifest)
    return manifest


def test_working_manifest_is_accepted_only_at_its_own_path_with_its_own_status(tmp_path):
    authority(tmp_path, WORKING_MANIFEST, "working_inputs")
    guard = verify_frozen_inputs(tmp_path, manifest_path=WORKING_MANIFEST)
    assert guard["manifest_path"] == WORKING_MANIFEST and manifest_file(tmp_path, guard) == tmp_path.resolve() / WORKING_MANIFEST
    authority(tmp_path, FROZEN_MANIFEST, "working_inputs")
    with pytest.raises(ValueError, match="requires frozen inputs"):
        verify_frozen_inputs(tmp_path)  # a working manifest cannot pass as the frozen one
    authority(tmp_path, WORKING_MANIFEST, "frozen")
    with pytest.raises(ValueError, match="working_inputs"):
        verify_frozen_inputs(tmp_path, manifest_path=WORKING_MANIFEST)  # nor the reverse


def test_context_rehashes_against_the_authority_that_verified_it(tmp_path):
    authority(tmp_path, FROZEN_MANIFEST, "frozen")
    authority(tmp_path, WORKING_MANIFEST, "working_inputs")
    guard = verify_frozen_inputs(tmp_path, manifest_path=WORKING_MANIFEST)
    context = make_context(tmp_path, guard)
    require_context(tmp_path, context, inputs=("study.toml",))
    # editing the frozen manifest must not disturb a working batch; editing the working one must
    (tmp_path / FROZEN_MANIFEST).write_text("{}", encoding="utf-8")
    require_context(tmp_path, context, inputs=("study.toml",))
    manifest = json.loads((tmp_path / WORKING_MANIFEST).read_text(encoding="utf-8"))
    manifest["note"] = "edited"
    write_json(tmp_path / WORKING_MANIFEST, manifest)
    with pytest.raises(ValueError, match="Freeze authority changed"):
        require_context(tmp_path, context, inputs=("study.toml",))


def test_default_guard_still_points_at_the_frozen_manifest(tmp_path):
    authority(tmp_path, FROZEN_MANIFEST, "frozen")
    guard = verify_frozen_inputs(tmp_path)
    assert guard["manifest_path"] == FROZEN_MANIFEST
    assert manifest_file(tmp_path, dict(guard=guard)) == tmp_path.resolve() / FROZEN_MANIFEST
    assert manifest_file(tmp_path, dict(guard={})) == tmp_path.resolve() / FROZEN_MANIFEST  # legacy guards carry no path


def bound_context(*names):
    return {"freeze_manifest": {"input_sha256": {name: "0" * 64 for name in names}}}


def test_bound_input_requires_exactly_one_match():
    context = bound_context("data/derived/tables-v5/repositories.csv", "data/derived/tables-v5/snapshots.csv")
    assert bound_input(context, r"data/derived/tables-v\d+/repositories\.csv") == "data/derived/tables-v5/repositories.csv"
    with pytest.raises(ValueError, match="exactly one"):
        bound_input(context, r"data/derived/tables-v\d+/missing\.csv")
    both = bound_context("data/derived/tables-v4/repositories.csv", "data/derived/tables-v5/repositories.csv")
    with pytest.raises(ValueError, match="exactly one"):
        bound_input(both, r"data/derived/tables-v\d+/repositories\.csv")


def test_bound_measurements_maps_ids_and_rejects_mixed_batches():
    context = bound_context("data/derived/measurement-v4/10.json", "data/derived/measurement-v4/20.json",
                            "data/derived/measurement-v4/summary.json", "data/derived/measurement-control-v1/30.json",
                            "data/derived/measurement-verification-v4/40.json")
    assert bound_measurements(context) == {"10": "data/derived/measurement-v4/10.json", "20": "data/derived/measurement-v4/20.json"}
    mixed = bound_context("data/derived/measurement-v3/10.json", "data/derived/measurement-v4/20.json")
    with pytest.raises(ValueError, match="more than one batch"):
        bound_measurements(mixed)


def test_expected_counts_come_from_the_authority_and_fail_closed_when_absent():
    declared = {"freeze_manifest": dict(raw_repository_count=457, formal_repository_count=455,
                                        formal_episode_kind_counts=dict(post_anchor_born_fixed=dict(episodes=104)))}
    assert expected_counts(declared) == dict(raw=457, formal=455, born_fixed=104)
    assert expected_counts({"freeze_manifest": {}}) == dict(raw=None, formal=None, born_fixed=None)


def test_closed_models_read_from_either_authority_layout():
    from pintrace.formal_common import closed_models
    closed = dict(Cox="closed", paired="closed")
    assert closed_models({"freeze_manifest": dict(closed_models=closed)}) == closed
    assert closed_models({"freeze_manifest": dict(carried_from_original_freeze=dict(closed_models=closed))}) == closed
    with pytest.raises(KeyError):
        closed_models({"freeze_manifest": {}})  # an authority that declares nothing must not read as "nothing is closed"
