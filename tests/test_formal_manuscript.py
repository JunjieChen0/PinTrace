import copy
import json

import pytest

from pintrace.formal_common import sha256
from pintrace.formal_manuscript import SOURCES, Writer, audit_artifacts, audit_text, compose, display, forbidden_assertions, pointer


def source(tmp_path, data, name="stats"):
    path = tmp_path / (name + ".json")
    path.write_text(json.dumps(data), encoding="utf-8")
    return dict(path=path, sha256=sha256(path), data=data)


def test_pointer_escapes_and_array_indices():
    assert pointer({"a/b": [{"~x": 7}]}, "/a~1b/0/~0x") == 7
    with pytest.raises(KeyError):
        pointer({}, "/missing")


@pytest.mark.parametrize("official,expected", [
    ([dict(updates=[dict(newValue="v4.3.1", newDigest="a" * 40, updateType="minor")])], ("v4.3.1", "a" * 40, "minor")),
    ([dict(updates=[dict(newDigest="b" * 40, updateType="digest")])], ("not reported", "b" * 40, "digest")),
    ([dict(latest_version="4.3.1", updated_requirements=[dict(source=dict(ref="a" * 40))])], ("4.3.1", "a" * 40, "not reported")),
    ([dict(latest_version="b" * 40, updated_requirements=[dict(source=dict(ref="b" * 40))])], ("not reported", "b" * 40, "not reported")),
])
def test_candidate_fields_preserve_reported_values_without_inventing_version(tmp_path, official, expected):
    sources = {"stats": source(tmp_path, dict(official_result=official))}
    writer = Writer(sources)
    rendered = [writer.value("stats", "/official_result", fmt) for fmt in ("candidate_version", "candidate_digest", "candidate_type")]
    assert tuple(display(official, fmt) for fmt in ("candidate_version", "candidate_digest", "candidate_type")) == expected
    assert audit_text(" ".join(rendered), writer.claims, sources)["passed"]


def test_registered_value_and_parameter_labels_are_exact(tmp_path):
    sources = {"rq1_summary": source(tmp_path, {"horizons": {"30": {"count": 17}}})}
    writer = Writer(sources)
    text = writer.table("rq1_summary", "/horizons")
    assert audit_text(text, writer.claims, sources)["passed"]
    assert not audit_text(text.replace(">17<", ">18<"), writer.claims, sources)["passed"]


@pytest.mark.parametrize("extra", [" The count was 18.", " Hidden `18`.", "## 18 repositories"])
def test_unregistered_numbers_fail_including_inline_code(tmp_path, extra):
    sources = {"stats": source(tmp_path, {"count": 17})}
    writer = Writer(sources)
    text = writer.value("stats", "/count")
    assert not audit_text(text + extra, writer.claims, sources)["passed"]


def test_changed_pointer_or_duplicate_claim_is_rejected(tmp_path):
    sources = {"stats": source(tmp_path, {"count": 17})}
    writer = Writer(sources)
    text = writer.value("stats", "/count")
    changed = copy.deepcopy(sources)
    changed["stats"]["data"]["count"] = 18
    assert not audit_text(text, writer.claims, changed)["passed"]
    assert not audit_text(text + text, writer.claims, sources)["passed"]
    assert not audit_text(text, writer.claims * 2, sources)["passed"]


@pytest.mark.parametrize("sentence", [
    "We fitted a Cox model.", "Our analysis estimated a causal effect.",
    "We found a PR fatigue effect.", "We achieved 100% classification accuracy.",
    "Release signature verification passed.", "The 90-day maintenance failure rate was high.",
    "Cox regression showed a significant association.", "We verified release signatures.",
])
def test_forbidden_positive_claims(sentence):
    assert forbidden_assertions(sentence)


@pytest.mark.parametrize("sentence", [
    "We did not fit a Cox model.", "Causal effect estimation remains closed.",
    "PR fatigue is uncollected.", "The 90-day maintenance failure rate is not estimable.",
    "Replay consistency does not establish perfect classification accuracy.",
    "Release signature verification did not succeed.",
])
def test_contextual_negative_claims_are_allowed(sentence):
    assert not forbidden_assertions(sentence)


def test_artifact_requires_current_bytes_and_actual_source_pointer(tmp_path):
    image, data = tmp_path / "plot.svg", tmp_path / "data.csv"
    image.write_text("<svg/>", encoding="utf-8")
    data.write_text("count\n17\n", encoding="utf-8")
    sources = {"stats": source(tmp_path, {"count": 17})}
    row = dict(path=str(image), sha256=sha256(image), data_path=str(data), data_sha256=sha256(data),
               kind="figure_svg", headers=["count"], rows=1,
               source_bindings=[dict(report="stats", pointer="/count")], cell_bindings=[dict(row=0, column="count", report="stats", pointer="/count", value=17)])
    manifest = dict(artifacts=[row])
    assert audit_artifacts(tmp_path, manifest, sources)["passed"]
    image.write_text("<svg>changed</svg>", encoding="utf-8")
    assert not audit_artifacts(tmp_path, manifest, sources)["passed"]
    row["sha256"] = sha256(image)
    row["source_bindings"][0]["pointer"] = "/missing"
    assert not audit_artifacts(tmp_path, manifest, sources)["passed"]


def test_csv_numbers_require_bindings_and_values_cannot_be_forged(tmp_path):
    path = tmp_path / "numbers.csv"
    path.write_text("count\n18\n", encoding="utf-8")
    sources = {"stats": source(tmp_path, {"count": 17})}
    row = dict(path=str(path), sha256=sha256(path), data_path=str(path), data_sha256=sha256(path), kind="csv_table", headers=["count"], rows=1,
        source_bindings=[dict(report="stats", pointer="/count")], cell_bindings=[])
    assert not audit_artifacts(tmp_path, dict(artifacts=[row]), sources)["passed"]
    row["cell_bindings"] = [dict(row=0, column="count", report="stats", pointer="/count", value=18)]
    assert not audit_artifacts(tmp_path, dict(artifacts=[row]), sources)["passed"]
    row["cell_bindings"][0]["value"] = 17
    assert not audit_artifacts(tmp_path, dict(artifacts=[row]), sources)["passed"]


def test_markdown_table_is_checked_against_bound_csv_cells(tmp_path):
    data, document = tmp_path / "data.csv", tmp_path / "table.md"
    data.write_text("count\n17\n", encoding="utf-8")
    document.write_text("| count |\n| --- |\n| 18 |\n", encoding="utf-8")
    sources = {"stats": source(tmp_path, {"count": 17})}
    row = dict(path=str(document), sha256=sha256(document), data_path=str(data), data_sha256=sha256(data), kind="markdown_table", headers=["count"], rows=1,
        source_bindings=[dict(report="stats", pointer="/count")], cell_bindings=[dict(row=0, column="count", report="stats", pointer="/count", value=17)])
    assert not audit_artifacts(tmp_path, dict(artifacts=[row]), sources)["passed"]
    document.write_text("| count |\n| --- |\n| 17 |\n", encoding="utf-8")
    row["sha256"] = sha256(document)
    assert audit_artifacts(tmp_path, dict(artifacts=[row]), sources)["passed"]


@pytest.mark.parametrize("bound,value,markdown,passed", [
    (False, None, "NA", True),
    (True, None, "NA", True),
    (True, "", "NA", False),
    (True, "", "", True),
    (True, 17, "NA", False),
    (False, None, "17", False),
])
def test_markdown_missing_value_normalization_preserves_bound_strings_and_numbers(tmp_path, bound, value, markdown, passed):
    data, document = tmp_path / "data.csv", tmp_path / "table.md"
    cell = "" if value is None else str(value)
    data.write_text("label,count\nknown," + cell + "\n", encoding="utf-8")
    document.write_text("| label | count |\n| --- | --- |\n| known | " + markdown + " |\n", encoding="utf-8")
    sources = {"stats": source(tmp_path, dict(label="known", count=value))}
    bindings = [dict(row=0, column="label", report="stats", pointer="/label", value="known")]
    if bound:
        bindings.append(dict(row=0, column="count", report="stats", pointer="/count", value=value))
    artifact = dict(path=str(document), sha256=sha256(document), data_path=str(data), data_sha256=sha256(data), kind="markdown_table",
        headers=["label", "count"], rows=1, cell_bindings=bindings,
        source_bindings=[dict(report=r["report"], pointer=r["pointer"]) for r in bindings])
    assert audit_artifacts(tmp_path, dict(artifacts=[artifact]), sources)["passed"] is passed


def test_raw_rq12_and_object_rq3_metrics_match_their_actual_ci_points(tmp_path):
    sources = {"stats": source(tmp_path, {"raw": .5, "object": {"estimate": .5, "metric_key": "a"}}),
        "uncertainty_intervals": source(tmp_path, {"metrics": {"a": dict(estimate=.5, lower=.2, upper=.8, n_clusters=4, status="sparse")}}, "ci")}
    writer = Writer(sources)
    text = writer.metric("stats", "/raw", key="a") + writer.metric("stats", "/object")
    assert audit_text(text, writer.claims, sources)["passed"]
    sources["uncertainty_intervals"]["data"]["metrics"]["a"]["estimate"] = .6
    with pytest.raises(ValueError, match="point estimate differs"):
        Writer(sources).metric("stats", "/raw", key="a")


def test_duplicate_and_out_of_range_artifact_cells_fail(tmp_path):
    path = tmp_path / "numbers.csv"
    path.write_text("count\n17\n", encoding="utf-8")
    sources = {"stats": source(tmp_path, {"count": 17})}
    cell = dict(row=0, column="count", report="stats", pointer="/count", value=17)
    artifact = dict(path=str(path), sha256=sha256(path), data_path=str(path), data_sha256=sha256(path), kind="csv_table", headers=["count"], rows=1,
        source_bindings=[dict(report="stats", pointer="/count")], cell_bindings=[cell, cell])
    assert not audit_artifacts(tmp_path, dict(artifacts=[artifact]), sources)["passed"]
    artifact["cell_bindings"] = [dict(cell, row=10)]
    assert not audit_artifacts(tmp_path, dict(artifacts=[artifact]), sources)["passed"]


def manuscript_sources(tmp_path):
    metric = dict(estimate=.5, metric_key="a")
    quantile = dict(observations=1, repositories=1, quantiles=dict(p50=.5), metric_keys=dict(p50="a"))
    counts = dict(episodes=1, repositories=1, known_event_episodes=0, exit_counts=dict(remove=0, replace=0, unpin=0))
    entry = dict(entries=1, repositories=1, episodes=1, actual_target_sha_adoptions=1, outcome_counts=dict(adopted_target=1))
    prefixes = ("rq1.formal.main", "rq2.formal.main")
    categories = ("all_full_sha", "mixed", "all_non_sha", "unknown")
    rq1 = dict(repositories=1, metric_prefix=prefixes[0], first_response_counts=dict(pin=1, no_observed_change=0), first_response_proportions=dict(pin=.5, no_observed_change=.5),
        tag_update_semantics="A mutable reference changed; its tag identity remains unresolved.",
        full_window_combination_counts=dict(pin=1, no_observed_change=0), response_time_days_quantiles={"0.5": .5},
        no_observed_change_followup_counts=dict(observed_to_window_end=0, ancestry_censored=0),
        target_state_horizons={day: dict(denominator=1, counts=dict(all_full_sha=1), proportions=dict(all_full_sha=1.0)) for day in ("7", "365")},
        horizons={"30": dict(denominator=1, at_risk_just_before=1, complete_case_denominator=1, observed_response_lower=.5, response_upper_bound=.5)})
    existing = dict(baseline_references=1, repositories_with_baseline_references=1, outcome_counts=dict(pin=1, no_observed_change=0),
        repository_equal=dict(pin=.5, no_observed_change=.5), reference_weighted=dict(pin=.5, no_observed_change=.5),
        window_followup_counts=dict(no_observed_change_complete=0, ancestry_censored=0))
    new = dict(groups=1, known_groups=1, no_new_group_repositories=0, configuration_counts={k: 1 for k in categories},
        repository_equal={**{k: .5 for k in categories}, "all_full_sha_known_groups": .5},
        reference_weighted={**{k: .5 for k in categories}, "full_sha_known_groups": .5})
    grades = {g: dict(counts=entry, outcome_proportions=dict(adopted_target=metric),
        delay_quantiles={k: quantile for k in (("point",) if g == "A" else ("lower", "upper", "proxy"))}) for g in ("A", "B")}
    conditions = [dict(category=c, episodes=1, repositories=1, observed_known_exit_fraction=metric) for c in ("covered", "unknown")]
    data = dict(
        freeze_manifest=dict(formal_repository_count=1, raw_repository_count=2, formal_population=dict(excluded_confirmed_repository_ids=["example"])),
        development_checkpoint=dict(measured_ids_exported=152),
        maintenance_scope=dict(raw_same_tag_retarget_rows=1, restored_normal_entries_with_raw_selected_false=1),
        source_catalog=dict(sources=SOURCES, study=dict(window_start="2024-09-14", event_anchor="2025-03-14", window_end="2026-03-14"), development_checkpoint_repositories=152),
        rq1_summary=dict(formal=rq1), rq2_summary=dict(formal=dict(metric_prefix=prefixes[1], existing=existing, new_actions=new), excluded_target_born_lineage_count=65,
            calendar_eligibility=dict(formal=dict(baseline_references_with_clock_unknown=5, clock_unalignable_repository_ids=["example"], prior_pin_outcomes_recoded=2,
                reasons=dict(nonmonotone_snapshot_calendar=dict(references=3), observation_ended_before_anchor=dict(references=2))))),
        rq3_persistence_summary=dict(populations=dict(formal=dict(identity=counts, duration=counts,
            by_kind=dict(baseline_fixed=dict(identity=counts, duration=counts)), retention=[], duration_quantiles_days=dict(baseline_fixed=quantile))),
            born_fixed_unpin=dict(identity=3, duration=0), comparisons=dict(riskset=[], maintenance=[])),
        rq3_maintenance_results=dict(populations=dict(formal={role: dict(counts=entry, by_grade=grades) for role in ("target", "comparison")}), B_pre_upper_exclusions=[]),
        h1_h2_summary=dict(episode_entry_conditions=dict(identity=dict(entry_configuration_coverage=conditions)), inheritance_unknown_snapshots={}, tool_replays=[]),
        followup=dict(horizons={"30": {field: dict(entries=1, repositories=1, episodes=1) for field in ("calendar_potential", "right_censored_observed_followup")}}, U_bounds=dict(minimum_utc="2025-12-23", maximum_utc="2026-03-03"),
            comparison_temporal_diagnostics=dict(formal_focal_episodes=1, duration_eligible_focal_episodes=1, eligible_riskset_primary_by_kind=dict(post_anchor_observed_pin=1),
                by_kind_layer=[dict(episode_kind=kind, layer="same_repository_baseline_fixed", provider_repositories=0, focal_episodes=0,
                    at_U_alive_and_duration_eligible=dict(providers=0, episodes=0)) for kind in ("post_anchor_observed_pin", "post_anchor_born_fixed")])),
        robustness_checks=dict(scenarios=dict(formal_main=dict(repositories=1, rq2=dict(existing=existing), rq3_persistence=dict(duration=counts)))), figure_manifest=dict(artifacts=[]))
    keys = {"a", prefixes[0] + ".first_response.pin", prefixes[0] + ".horizon.30.lower", prefixes[0] + ".response_days.0.5",
        prefixes[1] + ".existing.repository_equal.pin", prefixes[1] + ".existing.reference_weighted.pin",
        prefixes[1] + ".new.repository_equal.all_full_sha_known_groups", prefixes[1] + ".new.reference_weighted.full_sha_known_groups"}
    keys |= {prefixes[1] + ".new." + weighting + "." + category for weighting in ("repository_equal", "observed_reference_weighted") for category in categories}
    keys |= {prefixes[0] + ".first_response.no_observed_change", prefixes[1] + ".existing.repository_equal.no_observed_change", prefixes[1] + ".existing.reference_weighted.no_observed_change"}
    data["uncertainty_intervals"] = dict(seed=20261018, replicates=2000, confidence_level=.95,
        metrics={key: dict(estimate=.5, lower=.2, upper=.8, n_clusters=1, status="sparse") for key in keys})
    return {name: source(tmp_path, document, name) for name, document in data.items()}


def test_full_draft_has_no_unmapped_numbers_or_unused_claims(tmp_path):
    sources = manuscript_sources(tmp_path)
    writer = Writer(sources)
    text = compose(writer)
    result = audit_text(text, writer.claims, sources)
    assert result["passed"], result["errors"]
    assert "## Conclusion" in text and "## References" in text and "development checkpoint" in text
    assert "not a count with year-long follow-up" in text and "Coverage counts do not establish contemporaneous paired observations" in text
    temporal = [c for c in writer.claims if c["report"] == "followup" and c["json_pointer"].startswith("/comparison_temporal_diagnostics/")]
    assert len(temporal) == 9
    revision = "/calendar_eligibility/formal/prior_pin_outcomes_recoded"
    assert any(c["report"] == "rq2_summary" and c["json_pointer"] == revision for c in writer.claims)
    sources["rq2_summary"]["data"]["calendar_eligibility"]["formal"]["prior_pin_outcomes_recoded"] = 3
    assert not audit_text(text, writer.claims, sources)["passed"]
