"""Select normal-release maintenance evidence without changing the raw join."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

RULES_VERSION = "maintenance-scope-v1"
NORMAL_TYPES = {"immutable_release", "observed_tag_introduction"}
ENTRY_MODES = {"baseline_fixed": {"baseline_fixed"}, "post_anchor_observed_pin": {"pin"},
               "post_anchor_born_fixed": {"first_observed_action", "copied_reference", "reintroduced_action", "replace"}}
LAYERS = {"same_repository_baseline_fixed", "same_commit_co_pinned_supplement", "cross_repository_same_action_supplement"}
KEY = ("repository_id", "lineage_id", "reference_target_segment_start_commit", "opportunity_target")


def _key(row):
    return tuple(row[name] for name in KEY)


def select_entries(rows):
    """Filter before target-segment de-duplication; never select by outcome."""
    groups = defaultdict(list)
    for row in rows:
        if row.get("applicable") is True and row.get("evidence_grade") in {"A", "B"} and row.get("evidence_type") in NORMAL_TYPES:
            groups[_key(row)].append(row)
    output = []
    for key in sorted(groups):
        row = min(groups[key], key=lambda r: (r["evidence_grade"] != "A", r["available_by"], r["available_after_or_at"], r["opportunity_id"]))
        episodes = [dict(link) for link in row.get("rq3_target_episode_links", []) if link.get("episode_id")
                    and link.get("entry_mode") in ENTRY_MODES.get(link.get("episode_kind"), set())]
        comparisons = [dict(link) for link in row.get("comparison_focal_episode_links", []) if link.get("focal_episode_id")
                       and link.get("focal_entry_mode") in ENTRY_MODES.get(link.get("focal_episode_kind"), set())
                       and link.get("comparison_layer") in LAYERS]
        flags = {"maintenance_" + name + "_delay_model_candidate": any(link["episode_kind"] == kind for link in episodes)
                 for name, kind in [("baseline", "baseline_fixed"), ("pin", "post_anchor_observed_pin"), ("born_fixed", "post_anchor_born_fixed")]}
        output.append(dict(row, maintenance_selected=True, maintenance_target_episode_links=episodes,
                           maintenance_comparison_focal_episode_links=comparisons, maintenance_target_delay_model_candidate=bool(episodes),
                           maintenance_comparison_delay_model_candidate=bool(comparisons), maintenance_formal_model_frozen=False, **flags))
    return output


def _counts(rows, kind=None, mode=None, comparison=False):
    links = [link for row in rows for link in row["maintenance_comparison_focal_episode_links" if comparison else "maintenance_target_episode_links"]
             if comparison or ((kind is None or link["episode_kind"] == kind) and (mode is None or link["entry_mode"] == mode))]
    adopted = [row for row in rows if row["outcome"] == "adopted_target"]
    return dict(opportunity_reference_pairs=len(rows), opportunity_ids=len({r["opportunity_id"] for r in rows}),
                repository_ids=len({r["repository_id"] for r in rows}), episode_ids=len({l["focal_episode_id" if comparison else "episode_id"] for l in links}),
                actual_target_sha_adoptions=len({(r["repository_id"], r["lineage_id"], r["outcome_timestamp"], r["adopted_sha"]) for r in adopted}),
                adopting_repository_ids=len({r["repository_id"] for r in adopted}), outcome_counts=dict(Counter(r["outcome"] for r in rows)))


def _sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _check(root, hashes):
    for name, expected in hashes.items():
        path = root / name
        if not path.is_file() or _sha(path) != expected:
            raise ValueError("Maintenance input missing or changed: " + name)


def export_scope(root):
    root = Path(root)
    summary_name = "reports/opportunity_join_summary.json"
    summary_path = root / summary_name
    summary_bytes = summary_path.read_bytes()
    join = json.loads(summary_bytes)
    if join.get("rule_version") != "opportunity-join-v5" or join.get("inputs_unchanged") is not True:
        raise ValueError("Maintenance view requires a verified opportunity-join-v5 input")
    detail = join["details"]["opportunity_matches"]
    hashes = dict(join["input_sha256"])
    if detail["path"] in hashes and hashes[detail["path"]] != detail["sha256"]:
        raise ValueError("Conflicting opportunity match input hash")
    hashes.update({detail["path"]: detail["sha256"], summary_name: hashlib.sha256(summary_bytes).hexdigest()})
    risk_name = "reports/riskset_summary.json"
    if risk_name not in hashes:
        raise ValueError("Join does not bind the riskset episode denominators")
    _check(root, hashes)
    risk = json.loads((root / risk_name).read_text(encoding="utf-8"))
    if any(risk.get(key) != join.get(key) for key in ("measurement_batch_directory", "measurement_rules_version")) or risk["coverage"] != join["riskset_export_coverage"]:
        raise ValueError("Riskset denominators do not belong to the joined measurement batch")
    rows = [json.loads(line) for line in (root / detail["path"]).read_text(encoding="utf-8").splitlines() if line]
    entries = select_entries(rows)
    target = [r for r in entries if r["maintenance_target_delay_model_candidate"]]
    comparison = [r for r in entries if r["maintenance_comparison_delay_model_candidate"]]
    selected_keys = {_key(r) for r in entries}
    retargets = [r for r in rows if r.get("evidence_type") == "observed_binding_change"]
    by_kind = {}
    for kind in ENTRY_MODES:
        subset = [r for r in target if any(l["episode_kind"] == kind for l in r["maintenance_target_episode_links"])]
        count = _counts(subset, kind=kind)
        denominator = risk["risksets"]["episode_kind_counts"].get(kind, 0)
        by_kind[kind] = dict(count, exported_episodes=denominator, candidate_episode_fraction=count["episode_ids"] / denominator if denominator else None)
    result = dict(rule_version=RULES_VERSION, scope="normal_release_maintenance_view_not_model_freeze", generated_at_utc=datetime.now(timezone.utc).isoformat(),
        join_rule_version=join["rule_version"], join_summary_path=summary_name, join_summary_sha256=hashes[summary_name],
        input_sha256=hashes, implementation_sha256=_sha(Path(__file__)), inputs_unchanged=True,
        join_coverage=join["riskset_export_coverage"], source_coverage_complete=join["riskset_export_coverage"].get("all_confirmed_measurements_usable") is True,
        freeze_ready=False, formal_model_frozen=False,
        counts=dict(all_entries=_counts(entries), target=_counts(target), comparison=_counts(comparison, comparison=True)),
        raw_same_tag_retarget_rows=len(retargets), raw_selected_same_tag_retarget_rows=sum(r.get("selected_for_delay_analysis") is True for r in retargets),
        raw_selected_retarget_groups_with_normal_replacement=len({_key(r) for r in retargets if r.get("selected_for_delay_analysis") is True and _key(r) in selected_keys}),
        restored_normal_entries_with_raw_selected_false=sum(r.get("selected_for_delay_analysis") is not True for r in entries),
        tag_reintroduction_rows=sum(r.get("evidence_type") == "observed_tag_reintroduction" for r in rows),
        tag_reintroduction_applicable_rows=sum(r.get("evidence_type") == "observed_tag_reintroduction" and r.get("applicable") is True for r in rows),
        all_by_evidence_type={v: _counts([r for r in entries if r["evidence_type"] == v]) for v in sorted(NORMAL_TYPES)},
        all_by_evidence_grade={v: _counts([r for r in entries if r["evidence_grade"] == v]) for v in ("A", "B")},
        target_by_evidence_type={v: _counts([r for r in target if r["evidence_type"] == v]) for v in sorted(NORMAL_TYPES)},
        target_by_evidence_grade={v: _counts([r for r in target if r["evidence_grade"] == v]) for v in ("A", "B")}, target_by_episode_kind=by_kind,
        target_by_entry_mode={v: _counts([r for r in target if any(l["entry_mode"] == v for l in r["maintenance_target_episode_links"])], mode=v) for v in sorted(set().union(*ENTRY_MODES.values()))},
        limits=["Raw fields and retarget evidence are retained unchanged; selected normal entries are a separate view.",
                "B availability is conditional on the upstream bounds and downstream survival; it does not identify first-ever publication.",
                "Target non-adoption may be censored or superseded; it is not a maintenance-failure or safety label."])
    _check(root, hashes)
    if result["implementation_sha256"] != _sha(Path(__file__)):
        raise ValueError("Maintenance selector changed during export")
    path_name = "data/derived/maintenance-scope-v1/opportunity_entries.jsonl"
    output = root / path_name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in entries), encoding="utf-8")
    result["details"] = {"opportunity_entries": dict(path=path_name, sha256=_sha(output))}
    (root / "reports/maintenance_scope_summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    result = export_scope(Path(__file__).resolve().parents[1])
    print(json.dumps(result["counts"], indent=2))
