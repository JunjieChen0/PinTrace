"""Bind the post-freeze supplement in a new manifest: extension analyses, repaired sources and the v2 paper tooling.

The original input freeze and the earlier submission freeze are never changed. This manifest records which inputs of
the earlier submission freeze no longer match the files on disk, so the supersession is explicit rather than silent.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

from .formal_common import sha256, verify_frozen_inputs, write_json

TARGET = "reports/supplement_freeze_manifest.json"
PAPER = "paper/latex"
EXTENSIONS = "reports/formal_extensions.json"
EXTENSION_RUNS = "data/derived/formal-stats-v2/runs"
TEST_RUNS = "data/derived/formal-stats-v2/test-runs"


def _resolve(root, name):
    path = Path(name)
    return path if path.is_absolute() else root / path


def superseded_inputs(root, recorded):
    """Inputs of an earlier freeze whose current file differs from the recorded hash, or is gone."""
    rows = []
    for name, digest in sorted(recorded.items()):
        path = _resolve(Path(root), name)
        current = sha256(path) if path.is_file() else None
        if current != digest:
            rows.append(dict(path=name, recorded_sha256=digest, current_sha256=current))
    return rows


def _sources(root):
    return sorted({*root.glob("pintrace/*.py"), *root.glob("tests/*.py"), *root.glob("scripts/*.py"),
                   root / "pyproject.toml", root / "study.toml", root / "requirements-formal.txt"})


def _run_tests(root):
    before = {p.relative_to(root).as_posix(): sha256(p) for p in _sources(root)}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / TEST_RUNS / stamp
    directory.mkdir(parents=True, exist_ok=False)
    junit, log = directory / "junit.xml", directory / "pytest.log"
    command = [sys.executable, "-B", "-X", "utf8", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=" + str(junit)]
    with log.open("w", encoding="utf-8") as stream:
        done = subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, check=False)
    suites = ET.parse(junit).getroot()
    counts = {name: sum(int(node.get(name, "0")) for node in suites.iter("testsuite")) for name in ("tests", "failures", "errors", "skipped")}
    changed = [name for name, digest in before.items() if sha256(root / name) != digest]
    if done.returncode or counts["tests"] <= 0 or any(counts[k] for k in ("failures", "errors", "skipped")) or changed:
        raise ValueError(f"The test run did not pass cleanly: returncode={done.returncode}, counts={counts}, changed={changed}")
    return dict(directory=directory.relative_to(root).as_posix(), returncode=done.returncode, counts=counts, sources_unchanged_during_run=True,
                junit=dict(path=junit.relative_to(root).as_posix(), sha256=sha256(junit)),
                log=dict(path=log.relative_to(root).as_posix(), sha256=sha256(log))), before


def _extension_run(root):
    """The single run directory whose result equals the published reports/formal_extensions.json."""
    published = (root / EXTENSIONS).read_bytes()
    matches = [d for d in sorted((root / EXTENSION_RUNS).iterdir()) if (d / "formal_extensions.json").is_file()
               and (d / "formal_extensions.json").read_bytes() == published]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one extension run equal to {EXTENSIONS}, found {len(matches)}")
    return matches[0]


def _paper(root):
    paper = root / PAPER
    ledger = json.loads((paper / "claim_ledger.json").read_text(encoding="utf-8"))
    if sha256(paper / "main.tex") != ledger["resolved"]["sha256"] or sha256(paper / "main.src.tex") != ledger["source"]["sha256"]:
        raise ValueError("The v2 manuscript differs from its claim ledger; rebuild it with tools/build.py")
    for name, digest in ledger["result_files_sha256"].items():
        if sha256(root / name) != digest:
            raise ValueError("A result file changed after the claim ledger was written: " + name)
    if sha256(paper / "tools/facts.py") != ledger["facts_module_sha256"]:
        raise ValueError("tools/facts.py changed after the claim ledger was written")
    figures = json.loads((paper / "assets/figure_ledger.json").read_text(encoding="utf-8"))
    for row in figures:
        if sha256(paper / "assets" / row["figure"]) != row["sha256"]:
            raise ValueError("A figure differs from its ledger: " + row["figure"])
        for name, digest in row["sources"].items():
            if sha256(root / "reports" / (name + ".json")) != digest:
                raise ValueError("A figure source changed: " + name)
    integrity = {p for pattern in ("tools/*.py", "assets/*.pdf", "assets/figure_ledger.json") for p in paper.glob(pattern)}
    draft = {paper / name for name in ("main.src.tex", "main.tex", "main.pdf", "claim_ledger.json", "references.bib", "reference_verification.json")}
    return sorted(integrity), sorted(draft), ledger


def freeze(root):
    root = Path(root).resolve()
    target = root / TARGET
    if target.exists():
        raise ValueError("A supplement freeze already exists; it will not be overwritten")
    original_path = root / "reports/freeze_manifest.json"
    original = json.loads(original_path.read_text(encoding="utf-8"))
    prior_path = root / "reports/submission_freeze_manifest.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    if original.get("status") != "frozen" or prior["original_freeze_sha256"] != sha256(original_path):
        raise ValueError("The original input freeze changed")
    extension = json.loads((root / EXTENSIONS).read_text(encoding="utf-8"))
    run_dir = _extension_run(root)
    integrity, draft, ledger = _paper(root)
    guard = verify_frozen_inputs(root, progress=print)
    if guard["freeze_sha256"] != sha256(original_path):
        raise ValueError("The original frozen inputs changed")
    tests, sources = _run_tests(root)
    bound = dict(sources)
    for name, digest in extension["input_sha256"].items():
        bound[name] = digest
    for path in sorted(run_dir.iterdir()):
        bound[path.relative_to(root).as_posix()] = sha256(path)
    bound[EXTENSIONS] = sha256(root / EXTENSIONS)
    for name, digest in ledger["result_files_sha256"].items():
        bound[name] = digest
    for path in integrity:
        bound[path.relative_to(root).as_posix()] = sha256(path)
    for name, digest in bound.items():
        path = _resolve(root, name)
        if not path.is_file() or sha256(path) != digest:
            raise ValueError("Changed or missing bound file: " + name)
    result = dict(
        rule_version="supplement-freeze-v1", status="supplement_evidence_frozen", created_at_utc=datetime.now(timezone.utc).isoformat(),
        original_freeze_sha256=sha256(original_path),
        prior_submission_freeze=dict(path=prior_path.relative_to(root).as_posix(), sha256=sha256(prior_path),
                                     superseded_inputs=superseded_inputs(root, prior["input_sha256"]), inputs_checked=len(prior["input_sha256"])),
        full_original_input_verification=dict(files=guard["files"], bytes=guard["bytes"], checked_at_utc=guard["checked_at_utc"]),
        extension_run=run_dir.relative_to(root).as_posix(), extension_rule_version=extension["rule_version"], tests=tests,
        input_sha256=dict(sorted(bound.items())),
        manuscript_draft_at_freeze={p.relative_to(root).as_posix(): sha256(p) for p in draft},
        limits=["The superseded inputs were edited after the earlier submission freeze to remove a machine-specific path and restore a directory creation; the earlier manifest is unchanged.",
                "The manuscript draft hashes record the draft at freeze time and are expected to change when prose, author details or the data DOI are completed; the numeric evidence is bound by the claim-ledger result hashes and the figure and tool hashes.",
                "The extension analyses are post hoc, were added after the formal statistics, and establish no incident effect."])
    with target.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    outcome = freeze(Path(__file__).resolve().parents[1])
    print(json.dumps(dict(status=outcome["status"], bound_files=len(outcome["input_sha256"]),
                          superseded=[row["path"] for row in outcome["prior_submission_freeze"]["superseded_inputs"]])))
