import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from pintrace.formal_common import make_context, sha256, verify_frozen_inputs
from pintrace.formal_rq3 import (DAY, INPUTS, _condition_tables, _project_entry, delay_bounds,
                                retention_status, run, summarize_maintenance, summarize_persistence)


def episode(identity, name, kind="baseline_fixed", days=60, exit_kind="study_window_censored", coverage="covered"):
    return dict(row_kind="episode", repository_id=str(identity), episode_id=name, episode_kind=kind,
                entry_timestamp=0, exit_timestamp=days * DAY, observed_followup_seconds=days * DAY,
                positive_observed_followup=True, clock_order_state="nonnegative", exit_kind=exit_kind,
                fixed_age_seconds=None, entry_configuration_coverage=coverage,
                entry_comment_kind="semantic_version", entry_comment_identity_state="unverified")


def context(episodes, formal=("1", "2"), raw=("1", "2", "3")):
    return dict(metrics=[], populations=dict(formal=list(formal), raw=list(raw)),
                formal_ids=set(formal), raw_ids=set(raw), study=dict(window_end="1970-03-02T00:00:00Z"),
                rq3_data=dict(episodes=episodes))


def entry(identity="1", name="a", grade="A", target="base", focal=("base",), outcome="adopted_target"):
    return dict(repository_id=identity, lineage_id="lineage-" + name,
                reference_target_segment_start_commit="commit-" + name, opportunity_target="sha-" + name,
                opportunity_id="op-" + name, evidence_grade=grade,
                evidence_type="immutable_release" if grade == "A" else "observed_tag_introduction",
                outcome=outcome, outcome_timestamp=(50 if grade == "A" else 40) * DAY,
                available_after_or_at=10 * DAY, available_by=(10 if grade == "A" else 20) * DAY,
                adopted_sha="adopted-" + name, action="tj-actions/changed-files", comment_identity_state="unverified",
                maintenance_target_episode_links=[dict(episode_id=target)] if target else [],
                maintenance_comparison_focal_episode_links=[dict(focal_episode_id=value,
                    comparison_layer="cross_repository_same_action_supplement") for value in focal])


@pytest.mark.parametrize("exit_kind,days,status", [
    ("unpin", 30, "known_exit"), ("remove", 29, "known_exit"), ("replace", 31, "retained"),
    ("ambiguous_transition", 30, "unknown_interruption"), ("unknown_boundary", 29, "unknown_interruption"),
    ("study_window_censored", 30, "retained"), ("observable_ancestry_censored", 29, "censored_early")])
def test_retention_preserves_horizon_ties_competing_exits_and_unknowns(exit_kind, days, status):
    assert retention_status(episode("1", "e", days=days, exit_kind=exit_kind), 30) == status


def test_persistence_keeps_three_excluded_born_unpins_and_cluster_samples():
    born = [episode("2", str(index), "post_anchor_born_fixed", 20, "unpin") for index in range(3)]
    rows = [episode("1", "base"), episode("1", "pin", "post_anchor_observed_pin", 30, "remove"), *born]
    ctx = context(rows)
    result = summarize_persistence(rows, {"base", "pin"}, ctx)
    assert result["identity"]["episodes"] == 5
    assert result["identity"]["exit_counts"]["unpin"] == 3
    assert result["duration"]["episodes"] == 2
    assert result["by_kind"]["post_anchor_born_fixed"]["duration"]["episodes"] == 0
    assert result["excluded"]["exit_counts"] == {"unpin": 3}
    assert result["duration_quantiles_days"]["post_anchor_born_fixed"]["quantiles"]["p50"] is None
    metric = next(r for r in ctx["metrics"] if r["key"].endswith("baseline_fixed.observed_duration_days.p50"))
    assert metric["clusters"] == {"1": [60.0]}
    assert sum(r["repositories"] for r in result["identity"]["repository_exit_combinations"]) == 2


def test_observed_trajectory_and_determinate_retention_do_not_zero_early_censoring():
    rows = [episode("1", "a", days=10, exit_kind="unpin"), episode("1", "b", days=5),
            episode("2", "c", days=20, exit_kind="ambiguous_transition"), episode("2", "d", days=60)]
    ctx = context(rows)
    result = summarize_persistence(rows, {r["episode_id"] for r in rows}, ctx)
    row = next(r for r in result["retention"] if r["kind"] == "baseline_fixed" and r["horizon_days"] == 30)
    assert (row["retained"], row["known_exit"], row["unknown_interruption"], row["censored_early"]) == (1, 1, 1, 1)
    assert row["determinate"] == 2 and row["estimate"] == .5
    trajectory = next(r for r in result["trajectories"] if r["kind"] == "baseline_fixed" and r["days"] == 30 and r["exit_kind"] == "unpin")
    assert trajectory["observed_fraction"] == .25
    assert trajectory["episodes"] == 4 and trajectory["right_censored_by_time"] == 1
    metric = next(r for r in ctx["metrics"] if r["key"] == row["metric_key"])
    assert metric["clusters"] == {"1": [0.0, 1.0], "2": [1.0, 1.0]}


def test_adoption_point_and_B_open_interval_are_separate_and_not_imputed():
    a, b = entry(), entry(grade="B")
    assert delay_bounds(a) == dict(lower_days=40, upper_days=40, proxy_days=40, upper_inclusive=True)
    assert delay_bounds(b) == dict(lower_days=20, upper_days=30, proxy_days=20, upper_inclusive=False)
    assert delay_bounds(dict(b, outcome="right_censored_no_observed_sha_change")) is None
    assert delay_bounds(dict(b, outcome_timestamp=19 * DAY)) is None
    assert delay_bounds(dict(b, available_after_or_at=None)) is None
    projected = _project_entry(b)
    assert projected["available_by"].endswith("+00:00")
    assert projected["available_by_timestamp"] == b["available_by"]
    assert delay_bounds(projected) == delay_bounds(b)


def test_maintenance_population_masks_provider_target_and_focal_owners():
    rows = [episode("1", "base"), episode("3", "raw")]
    ctx = context(rows, formal=("1",), raw=("1", "3"))
    entries = [_project_entry(entry(target="base", focal=("base", "raw"))),
               _project_entry(entry(name="only-raw-focal", target=None, focal=("raw",))),
               _project_entry(entry("3", "raw", target="raw", focal=("base",)))]
    formal = summarize_maintenance(entries, ctx)
    raw = summarize_maintenance(entries, ctx, population="raw")
    assert formal["target"]["counts"]["entries"] == 1
    assert formal["comparison"]["counts"]["entries"] == 1
    assert formal["comparison"]["counts"]["episodes"] == 1
    assert raw["target"]["counts"]["entries"] == 2
    assert raw["comparison"]["counts"]["entries"] == 3
    assert raw["comparison"]["counts"]["episodes"] == 2
    assert formal["target"]["by_grade"]["B"]["delay_quantiles"]["lower"]["quantiles"]["p50"] is None


def test_local_entry_condition_cross_table_counts_entries_once_and_keeps_mixed():
    rows = [episode("1", "base", coverage="covered"), episode("1", "pin", coverage="no_local_config_observed")]
    ctx = context(rows)
    selected = _project_entry(entry(target="base"))
    selected["target_ids"].append("pin")
    _, cross = _condition_tables(rows, [selected], ctx)
    assert cross["entry_configuration_coverage"][0]["category"] == "mixed_episode_entry_conditions"
    assert cross["entry_configuration_coverage"][0]["entries"] == 1
    assert cross["entry_configuration_coverage"][0]["episodes"] == 2
    assert all(r["key"].startswith(("h1.", "h2.")) for r in ctx["metrics"])


@pytest.fixture
def frozen_fixture(tmp_path):
    root = tmp_path / "study"
    root.mkdir()
    rows = [episode("1", "base"), episode("1", "pin", "post_anchor_observed_pin", 30, "unpin", "no_local_config_observed"),
            episode("2", "born", "post_anchor_born_fixed", 45, "unpin"), episode("3", "raw", "post_anchor_born_fixed")]
    maintenance_rows = [entry(target="base", focal=("base", "raw")), entry(name="b", grade="B", target="pin", focal=("raw",)),
                        entry("3", "raw", target="raw", focal=("base",))]
    all_counts = dict(episodes=3, repositories=2, known_event_episodes=2, known_event_repositories=2)
    duration_counts = dict(episodes=2, repositories=1, known_event_episodes=1, known_event_repositories=1)
    excluded_counts = dict(episodes=1, repositories=1, known_event_episodes=1, known_event_repositories=1)
    def counts(entries, episodes):
        return dict(opportunity_reference_pairs=entries, repository_ids=1, episode_ids=episodes, opportunity_ids=entries,
                    actual_target_sha_adoptions=entries, adopting_repository_ids=1, outcome_counts={"adopted_target": entries})
    expected_horizon = {"entries": 2, "repositories": 1, "episodes": 2}
    empty_horizon = {"entries": 0, "repositories": 0, "episodes": 0}
    reports = {
        "model_eligibility_audit": dict(formal_episode_ids=["base", "pin", "born"], eligible_episode_ids=["base", "pin"],
            counts=dict(all_formal=all_counts, duration_eligible=duration_counts, excluded=excluded_counts)),
        "rq3_scope_counts": dict(formal_scope=dict(riskset_comparisons=[], maintenance_comparisons=[],
            maintenance_counts=dict(target=counts(2, 2), comparison=counts(1, 1)),
            maintenance_by_grade={g: {"opportunity_reference_pairs": 1} for g in ("A", "B")},
            B_pre_upper_exclusions=[{"evidence_rows": 2, "reason": "changed_before_U"}], B_pre_upper_diagnostics={})),
        "maintenance_followup_audit": dict(horizons={str(day): dict(
            calendar_potential=expected_horizon if day == 30 else empty_horizon,
            right_censored_observed_followup=empty_horizon) for day in (30, 90, 180)}),
        "full_replay_report": dict(repository_checks=[dict(repository_id=i, quality=dict(snapshots=n, inheritance_unknown_snapshots=u))
            for i, n, u in (("1", 4, 3), ("2", 2, 2), ("3", 1, 1))]),
        "tool_replay_results": dict(results={tool: dict(version=version, results=[dict(case_id=str(n), covered=bool(n % 2),
            official_result=[{"updates": [{"newDigest": "new"}]}] if n % 2 else None) for n in range(6)])
            for tool, version in (("renovate", "39.200.0"), ("dependabot", "0.301.0"))}),
        "opportunity_join_summary": dict(clock_unalignable_repository_ids=["2"]), "maintenance_scope_summary": {}}
    (root / "reports").mkdir()
    for name, value in reports.items():
        (root / "reports" / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
    with (root / "reports/risksets.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    path = root / INPUTS[-1]
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in maintenance_rows), encoding="utf-8")
    (root / "study.toml").write_text('[study]\nwindow_end = "1970-03-02T00:00:00Z"\n', encoding="utf-8")
    manifest = dict(status="frozen", formal_population=dict(raw_confirmed_repository_ids=["1", "2", "3"],
        formal_eligible_repository_ids=["1", "2"]), input_sha256={name: sha256(root / name) for name in (*INPUTS, "study.toml")})
    (root / "reports/freeze_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_run_writes_only_new_batch_and_is_reproducible(frozen_fixture):
    root = frozen_fixture
    guard = verify_frozen_inputs(root)
    first_context = make_context(root, guard)
    first = run(root, root / "batch-one", first_context)
    second_context = make_context(root, guard)
    second = run(root, root / "batch-two", second_context)
    assert first == second
    for name in first:
        assert (root / "batch-one" / (name + ".json")).read_bytes() == (root / "batch-two" / (name + ".json")).read_bytes()
    persistence = first["rq3_persistence_summary"]
    assert persistence["populations"]["formal"]["identity"]["episodes"] == 3
    assert persistence["populations"]["raw"]["identity"]["episodes"] == 4
    assert persistence["born_fixed_unpin"] == dict(identity=1, duration=0, excluded=1, repository_ids=["2"])
    maintenance = first["rq3_maintenance_results"]["populations"]["formal"]
    assert maintenance["target"]["counts"]["entries"] == 2
    assert maintenance["comparison"]["counts"]["entries"] == 1
    assert maintenance["horizons"]["30"]["calendar_potential"]["entries"] == 2
    assert maintenance["horizons"]["90"]["status"] == "closed_long_maintenance_indicator"
    assert len(maintenance["target"]["by_grade"]["B"]["delay_records"]) == 1
    assert len(first["h1_h2_summary"]["tool_replays"]) == 12
    inheritance = first["h1_h2_summary"]["inheritance_unknown_snapshots"]
    assert inheritance["physical"]["inheritance_unknown_snapshots"] == 6
    assert inheritance["formal"]["inheritance_unknown_snapshots"] == 5
    assert len(first_context["rq3_data"]["episodes"]) == 4
    assert len(first_context["rq3_data"]["maintenance"]) == 3
    assert first_context["rq3_data"]["eligible_episode_ids"] == {"base", "pin"}
    assert len(first_context["metrics"]) == len({r["key"] for r in first_context["metrics"]})
    assert all(sha256(root / name) == expected for name, expected in guard["manifest"]["input_sha256"].items())


def test_run_rejects_changed_input_before_output(frozen_fixture):
    root = frozen_fixture
    ctx = make_context(root, verify_frozen_inputs(root))
    with (root / "reports/model_eligibility_audit.json").open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="not the frozen input"):
        run(root, root / "unwritten", ctx)
    assert not (root / "unwritten").exists()


def test_run_rejects_direct_publish_directory(frozen_fixture):
    root = frozen_fixture
    ctx = make_context(root, verify_frozen_inputs(root))
    with pytest.raises(ValueError, match="before the main orchestrator publishes"):
        run(root, root / "reports", ctx)
