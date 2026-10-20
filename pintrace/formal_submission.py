"""Bind a verified full statistics run, tests, figures and draft to the input freeze."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from .formal_common import sha256, verify_frozen_inputs, write_json


def freeze(root, run_manifest):
    root = Path(root).resolve()
    target = root / "reports/submission_freeze_manifest.json"
    if target.exists():
        raise ValueError("A submission freeze already exists; it will not be overwritten")
    run_path = (root / run_manifest).resolve()
    run = json.loads(run_path.read_text(encoding="utf-8"))
    if not run.get("completed") or run.get("extract_only") or not run.get("frozen_inputs_unchanged"):
        raise ValueError("A completed full formal-statistics run is required")
    required_stages = {"verify_all_frozen_inputs_before", "rq1_rq2", "rq3_h1_h2", "robustness_matrix",
                       "bootstrap_round_one", "bootstrap_round_two", "figures_and_tables",
                       "manuscript_and_claim_audit", "formal_acceptance", "verify_all_frozen_inputs_after"}
    if ({row["name"] for row in run["stages"]} != required_stages or len(run["stages"]) != len(required_stages)
            or any(not row.get("completed") for row in run["stages"])):
        raise ValueError("A stage has not passed")
    directory = root / run["output_directory"]
    original_path = root / "reports/freeze_manifest.json"
    original = json.loads(original_path.read_text(encoding="utf-8"))
    if sha256(original_path) != run["freeze_sha256"] or original["status"] != "frozen":
        raise ValueError("Original input-freeze authority changed")
    final_guard_path = directory / "frozen_input_final_check.json"
    final_guard = json.loads(final_guard_path.read_text(encoding="utf-8"))
    if not final_guard.get("verified") or final_guard["freeze_sha256"] != run["freeze_sha256"] or final_guard["manifest"] != original:
        raise ValueError("Missing matching final full input verification")
    acceptance = json.loads((root / "reports/formal_acceptance.json").read_text(encoding="utf-8"))
    manuscript = json.loads((root / "reports/manuscript_audit.json").read_text(encoding="utf-8"))
    tests = json.loads((root / "reports/formal_test_results.json").read_text(encoding="utf-8"))
    if not acceptance.get("acceptance_passed") or not manuscript.get("audit_passed"):
        raise ValueError("Formal acceptance or manuscript audit did not pass")
    if (not tests.get("passed") or not tests.get("inputs_unchanged") or tests.get("returncode") != 0
            or tests["counts"]["tests"] <= 0 or any(tests["counts"][name] for name in ("failures", "errors", "skipped"))):
        raise ValueError("Current formal-analysis tests did not pass")
    xml = ET.parse(root / tests["junit"]["path"]).getroot()
    xml_counts = {name: sum(int(node.get(name, "0")) for node in xml.iter("testsuite"))
                  for name in ("tests", "failures", "errors", "skipped")}
    if xml_counts != tests["counts"] or len(list(xml.iter("testcase"))) != xml_counts["tests"]:
        raise ValueError("Actual JUnit cases and reported test counts differ")
    current_sources = {path.relative_to(root).as_posix() for path in
                       {*root.glob("pintrace/*.py"), *root.glob("tests/*.py"), *root.glob("scripts/*.py"),
                        root / "pyproject.toml", root / "study.toml", root / "requirements-formal.txt"}}
    if current_sources != set(tests["source_sha256"]):
        raise ValueError("The test run does not bind the complete current source set")
    submission_guard = verify_frozen_inputs(root, progress=print)
    if submission_guard["freeze_sha256"] != run["freeze_sha256"]:
        raise ValueError("Original frozen inputs changed after the tests")
    submission_guard_path = directory / "submission_input_check.json"
    write_json(submission_guard_path, submission_guard)
    expected = {}

    def bind(name, digest):
        path = (root / name).resolve()
        if path in expected and expected[path] != digest:
            raise ValueError("Conflicting output/source binding")
        if not path.is_file() or sha256(path) != digest:
            raise ValueError("Changed output/source: " + str(name))
        expected[path] = digest

    for name, digest in run["used_source_sha256"].items():
        bind(name, digest)
    for name, digest in run["outputs"].items():
        bind(name, digest)
    for name, digest in tests["source_sha256"].items():
        bind(name, digest)
    for key in ("junit", "log"):
        bind(tests[key]["path"], tests[key]["sha256"])
    figures = json.loads((root / "reports/figure_manifest.json").read_text(encoding="utf-8"))
    for artifact in figures["artifacts"]:
        bind(artifact["path"], artifact["sha256"])
        bind(artifact["data_path"], artifact["data_sha256"])
    for name in ("rq1_summary", "rq2_summary", "rq3_persistence_summary", "rq3_maintenance_results", "h1_h2_summary", "robustness_checks", "uncertainty_intervals",
                 "figure_manifest", "claim_evidence_map", "manuscript_audit", "source_catalog", "bootstrap_reproducibility", "formal_acceptance"):
        name = name + ".json"
        if sha256(directory / name) != sha256(root / "reports" / name):
            raise ValueError("Published report differs from verified batch: " + name)
        bind("reports/" + name, sha256(root / "reports" / name))
    if sha256(root / "paper/manuscript.md") != manuscript["manuscript_sha256"]:
        raise ValueError("Published manuscript differs from its audit")
    claim_map = json.loads((root / "reports/claim_evidence_map.json").read_text(encoding="utf-8"))
    if sha256(root / "reports/claim_evidence_map.json") != manuscript["claim_map_sha256"]:
        raise ValueError("The claim map differs from its audit")
    for name, digest in claim_map["input_sha256"].items():
        bind(name, digest)
    additional = (run_path, final_guard_path, submission_guard_path, original_path, root / "reports/formal_test_results.json",
                  root / "paper/manuscript.md", root / "reports/pilot_candidates.csv", root / "requirements-formal.txt",
                  root / "study.toml", root / "docs/formal_analysis_protocol.md", root / "reports/formal_stats_progress.md",
                  *sorted((root / "pintrace").glob("formal_*.py")))
    for path in additional:
        bind(path, sha256(path))
    for directory_name in ("data/derived/formal-stats-v1/method-revisions", "data/derived/formal-stats-v1/extracted-v2"):
        for path in sorted((root / directory_name).rglob("*")):
            if path.is_file():
                bind(path, sha256(path))
    robustness = json.loads((root / "reports/robustness_checks.json").read_text(encoding="utf-8"))
    for name, digest in robustness["additional_input_sha256"].items():
        bind(name, digest)
    def key(path):
        return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
    result = dict(rule_version="submission-freeze-v1", status="draft_evidence_frozen",
                  created_at_utc=datetime.now(timezone.utc).isoformat(), original_freeze_sha256=run["freeze_sha256"],
                  original_frozen_input_sha256=original["input_sha256"],
                  input_sha256={key(path): digest for path, digest in expected.items()},
                  study_sha256=sha256(root / "study.toml"), run_manifest=key(run_path), acceptance_passed=True,
                  manuscript_sha256=manuscript["manuscript_sha256"], test_counts=tests["counts"],
                  full_original_input_verification=dict(files=submission_guard["files"], bytes=submission_guard["bytes"], checked_at_utc=submission_guard["checked_at_utc"],
                                                        receipt=key(submission_guard_path), performed_after_tests=True),
                  scope=dict(formal_repositories=455, raw_repositories=457, original_identity_episodes=307, original_duration_episodes=297),
                  closed_analyses=original["closed_models"], H3_status=original["H3_status"],
                  limits=["This freezes a complete initial manuscript and its numerical evidence; it is not a journal submission or publication decision.",
                          "Observed cumulative fractions retain censoring and unknown bounds; no incident causal effect is established.",
                          "Original frozen histories remain unchanged; the additional pilot roster is separately bound."])
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_manifest")
    args = parser.parse_args()
    result = freeze(Path(__file__).resolve().parents[1], args.run_manifest)
    print(json.dumps(dict(status=result["status"], bound_files=len(result["input_sha256"]))))
