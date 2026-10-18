"""Audit bound analysis outputs without changing producer reports or raw data."""

from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pintrace.measurement import BATCH_DIRECTORY
from pintrace.opportunity_join import EPISODE_ENTRY_MODES, RISKSET_RULES_VERSION, RULES_VERSION as JOIN_RULES_VERSION, _opportunity
from pintrace.tables import RULES_VERSION as TABLE_RULES_VERSION


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root):
    root, hashes, checks = Path(root), {}, {}

    def check(name, passed, message=""):
        item = checks.setdefault(name, dict(passed=True, error_count=0, examples=[]))
        if item.get("status") == "not_run":
            item.update(passed=True, status="checked")
        if not passed:
            item["passed"] = False
            item["error_count"] += 1
            if len(item["examples"]) < 5:
                item["examples"].append(message)

    def bind(name, expected=None):
        if name not in hashes:
            hashes[name] = digest(root / name)
        check("input_hashes", expected is None or hashes[name] == expected, name)
        return root / name

    def load(name):
        return json.loads(bind(name).read_text(encoding="utf-8"))

    def rows(name):
        previous_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(2**31 - 1)
            with bind(name).open(encoding="utf-8", newline="") as stream:
                yield from csv.DictReader(stream)
        finally:
            csv.field_size_limit(previous_limit)

    required = ("input_hashes", "table_files", "episodes", "opportunity_links", "summary_counts", "measurement_batch", "maintenance_scope", "inputs_unchanged")
    for name in required:
        checks[name] = dict(passed=False, status="not_run", error_count=0, examples=[])
    counts = Counter()
    try:
        table = load("reports/table_manifest.json")
        risk = load("reports/riskset_summary.json")
        join = load("reports/opportunity_join_summary.json")
        replay = load("reports/full_replay_report.json")
        check("input_hashes", risk.get("rules_version") == RISKSET_RULES_VERSION and join.get("rule_version") == JOIN_RULES_VERSION
              and table.get("rule_version") == TABLE_RULES_VERSION, "analysis rules version mismatch")
        check("table_files", table.get("integrity_gate_passed") is True, "table producer gate is not complete")
        check("table_files", set(table["tables"]) == {"repositories", "snapshots", "references", "lineage_events", "opportunities", "configurations", "pr_events", "checks", "sources"}, "nine-table schema")
        for report, field in ((risk, "source_sha256"), (join, "input_sha256"), (table, "input_sha256"), (replay, "input_sha256")):
            for name, expected in (report.get(field, {}) if report is replay else report[field]).items():
                bind(name, expected)
        opportunity_ids = {}
        for name, descriptor in table["tables"].items():
            bind(descriptor["path"], descriptor["sha256"])
            amount = 0
            for row in rows(descriptor["path"]):
                amount += 1
                if name == "opportunities":
                    record = json.loads(row["payload"])
                    try:
                        canonical = _opportunity(record)
                    except (TypeError, ValueError, OverflowError):
                        canonical = None
                    expected = canonical["opportunity_id"] if canonical else ""
                    if canonical is None:
                        counts["current_only_evidence_rows" if record.get("evidence_grade") == "C" else "unresolved_evidence_rows"] += 1
                    check("opportunity_links", row["opportunity_id"] == expected, "noncanonical opportunity: " + row["opportunity_id"])
                    if expected:
                        opportunity_ids[expected] = canonical
            counts[name + "_rows"] = amount
            check("table_files", amount == table["table_rows"][name], name + " row count")
        check("opportunity_links", True)
        bind("reports/risksets.csv", risk["output_sha256"]["risksets.csv"])
        episodes, comparisons = {}, set()
        for row in rows("reports/risksets.csv"):
            if row["row_kind"] == "episode":
                identity, kind, mode = row["episode_id"], row["episode_kind"], row["entry_mode"]
                check("episodes", bool(identity) and identity not in episodes, "duplicate or empty episode: " + identity)
                check("episodes", mode in EPISODE_ENTRY_MODES.get(kind, set()) and row.get("entry_mode_state") != "unknown", "illegal episode entry: " + identity)
                episodes[identity] = row
            elif row["row_kind"] == "fixed_entry_diagnostic":
                counts["fixed_entry_diagnostic_rows"] += 1
                check("episodes", not row.get("episode_id"), "diagnostic has formal episode ID")
            elif row["row_kind"] == "comparison" and row.get("eligible") == "True":
                comparisons.update((row["repository_id"], lineage, row["focal_episode_id"], row["comparison_layer"])
                                   for lineage in json.loads(row["eligible_lineage_ids"]))
        check("episodes", counts["fixed_entry_diagnostic_rows"] == risk["fixed_entry_diagnostics"]["reference_rows"], "diagnostic count")
        actual_kinds = dict(Counter(row["episode_kind"] for row in episodes.values()))
        check("summary_counts", actual_kinds == risk["risksets"]["episode_kind_counts"], "riskset episode kind counts")
        groups = {(field, value): dict(episodes=set(), repositories=set(), pairs=0, adoptions=set())
                  for field, values in (("episode_kind", EPISODE_ENTRY_MODES), ("entry_mode", set().union(*EPISODE_ENTRY_MODES.values()))) for value in values}
        candidate_episodes, candidate_repositories, candidate_adoptions, selection_keys = set(), set(), set(), set()
        normal_choices, raw_retarget_selected_keys = {}, set()
        descriptor = join["details"]["opportunity_matches"]
        path = bind(descriptor["path"], descriptor["sha256"])
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                counts["opportunity_matches"] += 1
                check("opportunity_links", row["opportunity_id"] in opportunity_ids, "missing opportunity: " + row["opportunity_id"])
                canonical = opportunity_ids.get(row["opportunity_id"], {})
                check("opportunity_links", canonical and (row["upstream_repository"].lower(), row["opportunity_tag"], row["opportunity_target"], row["available_after_or_at"], row["available_by"], row["evidence_grade"])
                      == (canonical["repository"].lower(), canonical["tag"], canonical["target"], canonical["lower"], canonical["upper"], canonical["evidence_grade"]), "opportunity identity fields: " + row["opportunity_id"])
                links, focal = row["rq3_target_episode_links"], row["comparison_focal_episode_links"]
                check("opportunity_links", len(links) == len({link["episode_id"] for link in links}), "duplicate target episode links")
                check("opportunity_links", sorted(row["rq3_target_episode_ids"]) == sorted({link["episode_id"] for link in links}), "target ID/link disagreement")
                check("opportunity_links", sorted(row["comparison_focal_episode_ids"]) == sorted({link["focal_episode_id"] for link in focal}), "focal ID/link disagreement")
                for link in links:
                    episode = episodes.get(link["episode_id"], {})
                    check("opportunity_links", bool(episode) and all(link[key] == episode.get(key) for key in ("episode_kind", "entry_mode", "source_entry_event"))
                          and episode.get("repository_id") == row["repository_id"] and episode.get("lineage_id") == row["lineage_id"], "invalid target episode: " + link["episode_id"])
                for link in focal:
                    episode = episodes.get(link["focal_episode_id"], {})
                    check("opportunity_links", bool(episode) and link["focal_episode_kind"] == episode.get("episode_kind") and link["focal_entry_mode"] == episode.get("entry_mode")
                          and (row["repository_id"], row["lineage_id"], link["focal_episode_id"], link["comparison_layer"]) in comparisons, "invalid focal episode: " + link["focal_episode_id"])
                eligible = row["applicable"] is True and row["selected_for_delay_analysis"] is True
                key = (row["repository_id"], row["lineage_id"], row["reference_target_segment_start_commit"], row["opportunity_target"])
                if row["evidence_type"] == "observed_binding_change":
                    counts["raw_same_tag_retarget_rows"] += 1
                    if row["selected_for_delay_analysis"] is True:
                        counts["raw_selected_same_tag_retarget_rows"] += 1
                        raw_retarget_selected_keys.add(key)
                if row["evidence_type"] == "observed_tag_reintroduction":
                    counts["tag_reintroduction_rows"] += 1
                    counts["tag_reintroduction_applicable_rows"] += row["applicable"] is True
                if row["applicable"] is True and row["evidence_grade"] in {"A", "B"} and row["evidence_type"] in {"immutable_release", "observed_tag_introduction"}:
                    rank = (0 if row["evidence_grade"] == "A" else 1, row["available_by"], row["available_after_or_at"], row["opportunity_id"])
                    if key not in normal_choices or rank < normal_choices[key][0]:
                        normal_choices[key] = rank, row
                if eligible:
                    check("summary_counts", key not in selection_keys, "multiple selected evidence rows for one target segment")
                    selection_keys.add(key)
                candidate = eligible and bool(links)
                check("summary_counts", not row["selected_for_delay_analysis"] or row["applicable"], "selected inapplicable evidence")
                for flag, kind in (("target_delay_model_candidate", None), ("target_pin_delay_model_candidate", "post_anchor_observed_pin"), ("target_born_fixed_delay_model_candidate", "post_anchor_born_fixed"), ("target_baseline_delay_model_candidate", "baseline_fixed")):
                    check("summary_counts", row[flag] == (candidate and (kind is None or any(link["episode_kind"] == kind for link in links))), "candidate flag: " + flag)
                if not candidate:
                    continue
                counts["target_candidate_pairs"] += 1
                candidate_episodes.update(link["episode_id"] for link in links)
                candidate_repositories.add(row["repository_id"])
                adoption = (row["repository_id"], row["lineage_id"], row["outcome_timestamp"], row["adopted_sha"])
                if row["outcome"] == "adopted_target":
                    candidate_adoptions.add(adoption)
                for field, value in groups:
                    selected = [link for link in links if link[field] == value]
                    if selected:
                        group = groups[field, value]
                        group["episodes"].update(link["episode_id"] for link in selected)
                        group["repositories"].add(row["repository_id"])
                        group["pairs"] += 1
                        if row["outcome"] == "adopted_target":
                            group["adoptions"].add(adoption)
        for (field, value), group in groups.items():
            exported = sum(row[field] == value for row in episodes.values())
            expected = dict(exported_episodes=exported, candidate_episodes=len(group["episodes"]),
                candidate_episode_fraction=len(group["episodes"]) / exported if exported else None,
                candidate_repository_ids=len(group["repositories"]), selected_opportunity_reference_pairs=group["pairs"], actual_target_sha_adoptions=len(group["adoptions"]))
            check("summary_counts", join["target_rq3_by_" + field][value] == expected, field + ":" + value)
        check("summary_counts", table["rq3_entry_scope"]["opportunity_episode_kind_coverage"] == join["target_rq3_by_episode_kind"]
              and table["rq3_entry_scope"]["opportunity_entry_mode_coverage"] == join["target_rq3_by_entry_mode"], "table entry-scope counts differ from join")
        for key, expected in (("exported_target_riskset_episodes", len(episodes)), ("target_rq3_episode_candidates", len(candidate_episodes)),
                              ("target_rq3_repository_candidates", len(candidate_repositories)), ("target_rq3_opportunity_pairs", counts["target_candidate_pairs"]), ("actual_target_sha_adoptions", len(candidate_adoptions))):
            check("summary_counts", join[key] == expected, key)
        flow = list(rows("reports/cohort_flow.csv"))
        confirmed = {row["repository_id"] for row in flow if row["status"] == "confirmed"}
        check("measurement_batch", len(flow) == len({row["repository"] for row in flow}) == 580 and all(identity.isdigit() for identity in confirmed), "cohort ledger scope")
        maps = [{name: sha for name, sha in (report.get(field, {}) if report is replay else report[field]).items() if Path(name).parent.as_posix() == "data/derived/" + BATCH_DIRECTORY and Path(name).stem.isdigit() and Path(name).suffix == ".json"}
                for report, field in ((risk, "source_sha256"), (join, "input_sha256"), (table, "input_sha256"), (replay, "input_sha256"))]
        check("measurement_batch", bool(confirmed) and all(mapping == maps[0] for mapping in maps) and {Path(name).stem for name in maps[0]} == confirmed, "four measurement hash maps or confirmed IDs differ")
        check("measurement_batch", replay.get("gate_passed") is True and set(replay["verified_ids"]) == confirmed
              and len(replay["verified_ids"]) == len(confirmed) and set(replay["scope"]["confirmed_repository_ids"]) == confirmed, "full replay is pending or failed")
        counts.update(confirmed_repository_ids=len(confirmed), episodes=len(episodes), canonical_opportunity_ids=len(opportunity_ids))
        checks["measurement_batch"]["measurement_files_by_report"] = [len(mapping) for mapping in maps]
        scope = load("reports/maintenance_scope_summary.json")
        expected_inputs = dict(join["input_sha256"], **{descriptor["path"]: descriptor["sha256"],
                               "reports/opportunity_join_summary.json": hashes["reports/opportunity_join_summary.json"]})
        check("maintenance_scope", scope["rule_version"] == "maintenance-scope-v1" and scope["inputs_unchanged"] is True
              and scope["input_sha256"] == expected_inputs and scope["join_summary_path"] == "reports/opportunity_join_summary.json"
              and scope["join_summary_sha256"] == expected_inputs["reports/opportunity_join_summary.json"]
              and scope["join_rule_version"] == join["rule_version"], "maintenance scope uses another join batch")
        check("maintenance_scope", scope["join_coverage"] == join["riskset_export_coverage"] == risk["coverage"]
              and scope["source_coverage_complete"] == (risk["coverage"].get("all_confirmed_measurements_usable") is True)
              and scope["freeze_ready"] is False and scope["formal_model_frozen"] is False, "maintenance scope coverage or freeze state")
        for name, expected in scope["input_sha256"].items():
            bind(name, expected)
        bind("pintrace/maintenance_scope.py", scope["implementation_sha256"])
        expected_entries = {}
        for key, (_, row) in normal_choices.items():
            target_links = [link for link in row["rq3_target_episode_links"] if link.get("episode_id")
                            and link.get("entry_mode") in EPISODE_ENTRY_MODES.get(link.get("episode_kind"), set())]
            focal_links = [link for link in row["comparison_focal_episode_links"] if link.get("focal_episode_id")
                           and link.get("focal_entry_mode") in EPISODE_ENTRY_MODES.get(link.get("focal_episode_kind"), set())
                           and link.get("comparison_layer") in {"same_repository_baseline_fixed", "same_commit_co_pinned_supplement", "cross_repository_same_action_supplement"}]
            expected_entries[key] = dict(row, maintenance_selected=True, maintenance_target_episode_links=target_links,
                maintenance_comparison_focal_episode_links=focal_links, maintenance_target_delay_model_candidate=bool(target_links),
                maintenance_comparison_delay_model_candidate=bool(focal_links), maintenance_formal_model_frozen=False,
                **{"maintenance_" + name + "_delay_model_candidate": any(link["episode_kind"] == kind for link in target_links)
                   for name, kind in (("baseline", "baseline_fixed"), ("pin", "post_anchor_observed_pin"), ("born_fixed", "post_anchor_born_fixed"))})
        output = scope["details"]["opportunity_entries"]
        check("maintenance_scope", output["path"] == "data/derived/maintenance-scope-v1/opportunity_entries.jsonl", "maintenance output path")
        seen = set()
        with bind(output["path"], output["sha256"]).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                key = tuple(row[field] for field in ("repository_id", "lineage_id", "reference_target_segment_start_commit", "opportunity_target"))
                check("maintenance_scope", key not in seen and row == expected_entries.get(key), "nonoptimal, altered or excluded maintenance entry: " + str(key))
                seen.add(key)
        check("maintenance_scope", seen == set(expected_entries), "normal applicable key coverage is incomplete")

        def scope_counts(records, kind=None, mode=None, comparison=False):
            link_field = "maintenance_comparison_focal_episode_links" if comparison else "maintenance_target_episode_links"
            identifiers = {link["focal_episode_id" if comparison else "episode_id"] for row in records for link in row[link_field]
                           if comparison or ((kind is None or link["episode_kind"] == kind) and (mode is None or link["entry_mode"] == mode))}
            adopted = [row for row in records if row["outcome"] == "adopted_target"]
            return dict(opportunity_reference_pairs=len(records), opportunity_ids=len({row["opportunity_id"] for row in records}),
                repository_ids=len({row["repository_id"] for row in records}), episode_ids=len(identifiers),
                actual_target_sha_adoptions=len({(row["repository_id"], row["lineage_id"], row["outcome_timestamp"], row["adopted_sha"]) for row in adopted}),
                adopting_repository_ids=len({row["repository_id"] for row in adopted}), outcome_counts=dict(Counter(row["outcome"] for row in records)))

        normal = list(expected_entries.values())
        target = [row for row in normal if row["maintenance_target_episode_links"]]
        comparison = [row for row in normal if row["maintenance_comparison_focal_episode_links"]]
        check("maintenance_scope", scope["counts"] == dict(all_entries=scope_counts(normal), target=scope_counts(target), comparison=scope_counts(comparison, comparison=True)), "maintenance core counts")
        for kind in EPISODE_ENTRY_MODES:
            subset = [row for row in target if any(link["episode_kind"] == kind for link in row["maintenance_target_episode_links"])]
            values = scope_counts(subset, kind=kind)
            denominator = sum(row["episode_kind"] == kind for row in episodes.values())
            check("maintenance_scope", scope["target_by_episode_kind"][kind] == dict(values, exported_episodes=denominator,
                  candidate_episode_fraction=values["episode_ids"] / denominator if denominator else None), "maintenance episode-kind denominator: " + kind)
        for field, values in (("evidence_type", {"immutable_release", "observed_tag_introduction"}), ("evidence_grade", {"A", "B"})):
            for value in values:
                for prefix, records in (("all", normal), ("target", target)):
                    check("maintenance_scope", scope[prefix + "_by_" + field][value] == scope_counts([row for row in records if row[field] == value]), "maintenance " + prefix + ":" + value)
        for mode in set().union(*EPISODE_ENTRY_MODES.values()):
            subset = [row for row in target if any(link["entry_mode"] == mode for link in row["maintenance_target_episode_links"])]
            check("maintenance_scope", scope["target_by_entry_mode"][mode] == scope_counts(subset, mode=mode), "maintenance entry mode: " + mode)
        for field in ("raw_same_tag_retarget_rows", "raw_selected_same_tag_retarget_rows", "tag_reintroduction_rows", "tag_reintroduction_applicable_rows"):
            check("maintenance_scope", scope[field] == counts[field], "maintenance raw diagnostics: " + field)
        check("maintenance_scope", scope["raw_selected_retarget_groups_with_normal_replacement"] == len(raw_retarget_selected_keys & set(normal_choices))
              and scope["restored_normal_entries_with_raw_selected_false"] == sum(row["selected_for_delay_analysis"] is not True for row in normal), "maintenance restored-normal diagnostics")
        checks["maintenance_scope"].update(normal_applicable_keys=len(expected_entries), exported_entries=len(seen))
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
        check("readable_inputs", False, type(error).__name__ + ": " + str(error))
    for name, expected in hashes.items():
        try:
            check("inputs_unchanged", digest(root / name) == expected, name)
        except OSError as error:
            check("inputs_unchanged", False, name + ": " + str(error))
    result = dict(rules_version="analysis-verification-v3", checked_at_utc=datetime.now(timezone.utc).isoformat(),
                  gate_passed=all(checks.get(name, {}).get("passed") is True for name in required) and all(item["passed"] for item in checks.values()),
                  checks=checks, counts=dict(counts), input_sha256=hashes, implementation_sha256=digest(Path(__file__)))
    output = root / "reports/analysis_verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    result = verify(Path(__file__).resolve().parents[1])
    print(json.dumps(dict(gate_passed=result["gate_passed"], counts=result["counts"])))
    raise SystemExit(0 if result["gate_passed"] else 1)
