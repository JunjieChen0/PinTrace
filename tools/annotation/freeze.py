"""Freeze and verify the independent-annotation study: classifier, baseline data, sample, packets and protocol.

Usage:
  python -B -X utf8 tools/annotation/freeze.py freeze-1          write private/freeze_freeze-1.json
  python -B -X utf8 tools/annotation/freeze.py freeze-1 --check  verify the current files against that record

A stage record is written once. Re-running a stage whose file hashes are unchanged only refreshes the Git binding
(for example after the freeze is committed); a stage whose hashes changed is refused, so a later protocol revision
must be frozen as a new stage. A later stage may change only the protocol group: its classifier, baseline data, sample
and packets must equal those of the stage it follows. ``verify`` is what the scorer calls before it reads any answer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pintrace.formal_common import sha256  # noqa: E402

OUT = ROOT / "reports/human_validation_v2"
PRIVATE = OUT / "private"

# Every group is hashed file by file; a missing file is an error, never a silent omission.
GROUPS = {
    "classifier": ["pintrace/workflow.py", "pintrace/lineage.py", "pintrace/measurement.py", "pintrace/configuration.py",
                   "pintrace/validity_goldset.py"],
    "baseline_data": ["data/derived/tables-v5/lineage_events.csv", "data/derived/tables-v5/repositories.csv",
                      "data/derived/tables-v5/manifest.json", "data/derived/validity-v1/goldset/answer_key.json",
                      "data/derived/validity-v1/goldset/packet_round1.json"],
    "sample": ["reports/human_validation_v2/private/key.json", "reports/human_validation_v2/private/practice_key.json",
               "reports/human_validation_v2/private/build.json"],
    "packets": ["reports/human_validation_v2/build/PinTrace_annotation_A1.html",
                "reports/human_validation_v2/build/PinTrace_annotation_A2.html",
                "reports/human_validation_v2/build/PinTrace_practice.html"],
    "protocol": ["tools/annotation/codebook.md", "tools/annotation/guide.md", "tools/annotation/template.html",
                 "tools/annotation/make_packet.py", "tools/annotation/render_docs.py", "tools/annotation/freeze.py",
                 "reports/human_validation_v2/build/PinTrace_编码手册_v1.0.pdf",
                 "reports/human_validation_v2/build/PinTrace_标注工具使用说明_v1.0.pdf"],
}


# Protocol files added by a stage, and the stage each one follows.
STAGE_PROTOCOL = {
    "freeze-2": ["tools/annotation/practice_explanation.md",
                 "reports/human_validation_v2/build/PinTrace_编码手册_v1.1.pdf",
                 "reports/human_validation_v2/build/PinTrace_练习讲解_v1.0.pdf"],
    "freeze-3": ["tools/annotation/score.py", "tests/test_annotation_score.py"],
}
PREVIOUS = {"freeze-2": "freeze-1", "freeze-3": "freeze-2", "freeze-4": "freeze-3", "freeze-5": "freeze-4",
            "freeze-6": "freeze-5"}  # freeze-6: protocol only; the scorer accepts only A3 and A4
FIXED_GROUPS = ("classifier", "baseline_data", "sample", "packets")
# Files a stage adds to a fixed group. Every file the previous stage froze must stay byte-identical; only these may
# appear. freeze-4 adds the packet of A3, who replaces A1 (protocol log, 2026-11-08); freeze-5 adds the packet of A4,
# who replaces A2 (protocol log, 2026-11-09).
STAGE_ADDED = {
    "freeze-4": {"packets": ["reports/human_validation_v2/build/PinTrace_annotation_A3.html"],
                 "sample": ["reports/human_validation_v2/private/build_A3.json"]},
    "freeze-5": {"packets": ["reports/human_validation_v2/build/PinTrace_annotation_A4.html"],
                 "sample": ["reports/human_validation_v2/private/build_A4.json"]},
}


def groups_for(stage: str) -> dict:
    groups = {name: list(paths) for name, paths in GROUPS.items()}
    current = stage
    while current in PREVIOUS or current in STAGE_PROTOCOL:
        groups["protocol"] += STAGE_PROTOCOL.get(current, [])
        for group, paths in STAGE_ADDED.get(current, {}).items():
            groups[group] += paths
        current = PREVIOUS.get(current, "")
    return groups


def rule_versions() -> dict:
    from pintrace import configuration, lineage, measurement, validity_goldset
    sys.path.insert(0, str(ROOT / "tools/annotation"))
    import make_packet
    return dict(lineage=lineage.RULES_VERSION, measurement=measurement.RULES_VERSION,
                configuration=configuration.RULE_VERSION, goldset=validity_goldset.RULE_VERSION,
                annotation_packet=make_packet.RULE_VERSION)


def hashes(root: Path = ROOT, groups: dict = GROUPS) -> dict:
    output = {}
    for group, paths in groups.items():
        output[group] = {}
        for path in paths:
            target = root / path
            if not target.is_file():
                raise FileNotFoundError(f"Freeze input missing: {path}")
            output[group][path] = sha256(target)
    return output


def sent_copies(root: Path = ROOT) -> dict:
    """Each annotator folder must hold byte-identical copies of the frozen build files."""
    problems = {}
    for folder in sorted((root / "reports/human_validation_v2").glob("send_*")):
        for copy in sorted(folder.glob("*")):
            original = root / "reports/human_validation_v2/build" / copy.name
            if not original.is_file() or sha256(copy) != sha256(original):
                problems[copy.relative_to(root).as_posix()] = "differs_from_build_or_unknown"
    return problems


def git_state(root: Path = ROOT, groups: dict = GROUPS) -> dict:
    def git(*args):
        # Only trailing newlines go: porcelain lines start with a status column that may be a space.
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8").stdout.rstrip("\n")
    tracked = [p for p in groups["protocol"] + groups["classifier"] if not p.startswith("reports/")]
    dirty = git("status", "--porcelain", "--", *tracked)
    return dict(head=git("rev-parse", "HEAD"), tracked_inputs_committed=not dirty,
                uncommitted=sorted(line[3:] for line in dirty.splitlines()))


def compare(recorded: dict, current: dict) -> list[str]:
    """Paths whose hash changed, appeared or disappeared."""
    changes = []
    for group in sorted(set(recorded) | set(current)):
        old, new = recorded.get(group, {}), current.get(group, {})
        for path in sorted(set(old) | set(new)):
            if old.get(path) != new.get(path):
                changes.append(f"{group}:{path}")
    return changes


def record_path(stage: str, private: Path = PRIVATE) -> Path:
    return private / f"freeze_{stage}.json"


def freeze(stage: str, note: str = "") -> dict:
    groups = groups_for(stage)
    current = hashes(groups=groups)
    if stage in PREVIOUS:
        before = json.loads(record_path(PREVIOUS[stage]).read_text(encoding="utf-8"))["sha256"]
        moved = compare({g: before[g] for g in FIXED_GROUPS}, {g: {p: current[g].get(p) for p in before[g]} for g in FIXED_GROUPS})
        if moved:
            raise ValueError(f"{stage} may change only the protocol; changed since {PREVIOUS[stage]}: " + ", ".join(moved))
    copies = sent_copies()
    if copies:
        raise ValueError("Annotator folders differ from the build: " + ", ".join(copies))
    path = record_path(stage)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if previous and compare(previous["sha256"], current):
        raise ValueError(f"Stage {stage} is already frozen with different files; freeze a new stage instead: "
                         + ", ".join(compare(previous["sha256"], current)))
    record = dict(stage=stage, note=note or (previous or {}).get("note", ""),
                  frozen_at_utc=(previous or {}).get("frozen_at_utc") or datetime.now(timezone.utc).isoformat(),
                  follows=PREVIOUS.get(stage), git_bound_at_utc=datetime.now(timezone.utc).isoformat(),
                  git=git_state(groups=groups),
                  rule_versions=rule_versions(), sha256=current,
                  policy=["No classifier, sample or packet change after this freeze until scoring is complete.",
                          "A codebook revision is frozen as a new stage before formal labelling starts.",
                          "A later classifier fix is a new rule version, rescored on the same sample and reported apart."])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return record


def verify(stage: str) -> list[str]:
    """Empty list when every frozen file is unchanged and the annotator copies match the build."""
    path = record_path(stage)
    if not path.exists():
        return [f"no freeze record for stage {stage}"]
    recorded = json.loads(path.read_text(encoding="utf-8"))
    return compare(recorded["sha256"], hashes(groups=groups_for(stage))) + [f"copy:{p}" for p in sent_copies()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    if args.check:
        problems = verify(args.stage)
        print(json.dumps(dict(stage=args.stage, unchanged=not problems, problems=problems), ensure_ascii=False, indent=1))
        return 1 if problems else 0
    record = freeze(args.stage, args.note)
    print(json.dumps(dict(stage=record["stage"], git=record["git"], rule_versions=record["rule_versions"],
                          files=sum(len(v) for v in record["sha256"].values())), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
