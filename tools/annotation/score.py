"""Score the independent annotation: annotator agreement, the disagreement sheet, then consensus against the pipeline.

Usage:
  python -B -X utf8 tools/annotation/score.py --a1 A1.csv --a2 A2.csv
      check the freeze, validate both answer files, report agreement and write the disagreement sheet
  python -B -X utf8 tools/annotation/score.py --a1 A1.csv --a2 A2.csv --consensus consensus.csv
      also compare the consensus labels with the pipeline

Nothing is read before ``freeze.verify`` passes. The disagreement sheet carries the two answers only, never the
pipeline label, so the consensus discussion stays blind. S1 (the gold set) and S2 (the uniform sample) are always
reported apart, and so are the two tasks. Outputs go to reports/human_validation_v2/private/score/.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/annotation"))

import freeze  # noqa: E402
from make_packet import TASKS  # noqa: E402

PRIVATE = ROOT / "reports/human_validation_v2/private"
STAGE = "freeze-3"
CONFIDENCE = {"high", "medium", "low"}
FAST_GAP_SECONDS = 2.0
Z95 = 1.959963984540054


# ---------------------------------------------------------------- answers

def read_answers(path: Path, annotator: str, key: dict) -> dict:
    """uid -> row for one annotator's export; every problem is collected and raised together."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    problems, answers = [], {}
    for number, row in enumerate(rows, start=2):
        item = row.get("uid", "")
        if row.get("packet_id") != f"pintrace-v2-{annotator}" or row.get("annotator") != annotator:
            problems.append(f"line {number}: packet {row.get('packet_id')!r} is not {annotator}'s")
        if item in answers:
            problems.append(f"line {number}: {item} appears twice")
        elif item not in key:
            problems.append(f"line {number}: {item} is not in the sample")
        elif row.get("task") != key[item]["task"]:
            problems.append(f"line {number}: {item} task {row.get('task')!r} differs from the key")
        elif row.get("answer") not in TASKS[key[item]["task"]]["labels"]:
            problems.append(f"line {number}: {item} answer {row.get('answer')!r} is not a label of its task")
        elif row.get("confidence") not in CONFIDENCE:
            problems.append(f"line {number}: {item} confidence {row.get('confidence')!r} is missing or invalid")
        answers[item] = row
    missing = sorted(set(key) - set(answers))
    if missing:
        problems.append(f"{len(missing)} items unanswered, e.g. {', '.join(missing[:5])}")
    if problems:
        raise ValueError(f"{annotator} answers rejected:\n  " + "\n  ".join(problems))
    return answers


def timing(answers: dict) -> dict:
    """Answer times are the last save per item, so a revisit moves an item later; the figures flag, they do not prove."""
    times = sorted(datetime.fromisoformat(row["updated_at_utc"].replace("Z", "+00:00")) for row in answers.values())
    gaps = [(later - earlier).total_seconds() for earlier, later in zip(times, times[1:])]
    return dict(first_utc=times[0].isoformat(), last_utc=times[-1].isoformat(),
                span_hours=round((times[-1] - times[0]).total_seconds() / 3600, 2),
                median_gap_seconds=round(statistics.median(gaps), 2) if gaps else None,
                gaps_under_2s=sum(gap < FAST_GAP_SECONDS for gap in gaps), gaps=len(gaps))


# ---------------------------------------------------------------- statistics

def kappa(pairs: list[tuple]) -> float | None:
    """Cohen's kappa; None when chance agreement is total (a single label on both sides)."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[label] * right[label] for label in left) / (n * n)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def wilson(hits: int, n: int) -> list | None:
    if not n:
        return None
    p = hits / n
    centre = (p + Z95 ** 2 / (2 * n)) / (1 + Z95 ** 2 / n)
    half = Z95 * math.sqrt(p * (1 - p) / n + Z95 ** 2 / (4 * n * n)) / (1 + Z95 ** 2 / n)
    return [round(centre - half, 4), round(centre + half, 4)]


def proportion(hits: int, n: int) -> dict:
    return dict(hits=hits, n=n, rate=round(hits / n, 4) if n else None, ci95=wilson(hits, n))


def cells(key: dict) -> dict:
    """(sample, task) -> uids, in a stable order."""
    groups = {}
    for item in sorted(key):
        groups.setdefault((key[item]["sample"], key[item]["task"]), []).append(item)
    return dict(sorted(groups.items()))


# ---------------------------------------------------------------- reports

def agreement(key: dict, first: dict, second: dict) -> dict:
    report = {}
    for (sample, task), items in cells(key).items():
        pairs = [(first[i]["answer"], second[i]["answer"]) for i in items]
        value = kappa(pairs)
        report[f"{sample}/{task}"] = dict(raw=proportion(sum(a == b for a, b in pairs), len(pairs)),
                                          kappa=None if value is None else round(value, 4))
    return report


def disagreements(key: dict, first: dict, second: dict) -> list[dict]:
    """Blind sheet: the uid, each annotator's position, answer, confidence and note, and an empty consensus column."""
    sheet = []
    for (_, task), items in cells(key).items():
        for item in items:
            a, b = first[item], second[item]
            if a["answer"] != b["answer"]:
                sheet.append(dict(uid=item, task=task, position_A1=a["position"], answer_A1=a["answer"],
                                  confidence_A1=a["confidence"], note_A1=a["note"], position_A2=b["position"],
                                  answer_A2=b["answer"], confidence_A2=b["confidence"], note_A2=b["note"],
                                  consensus="", reason=""))
    return sheet


def read_consensus(path: Path, key: dict, first: dict, second: dict) -> dict:
    """Agreed items keep the shared answer; each disagreement needs a consensus label of its task."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        given = {row["uid"]: row.get("consensus", "").strip() for row in csv.DictReader(stream)}
    labels, problems = {}, []
    for item in sorted(key):
        if first[item]["answer"] == second[item]["answer"]:
            labels[item] = first[item]["answer"]
            if given.get(item) and given[item] != labels[item]:
                problems.append(f"{item}: both annotators agreed, the consensus sheet may not change it")
        elif given.get(item) not in TASKS[key[item]["task"]]["labels"]:
            problems.append(f"{item}: consensus {given.get(item)!r} is missing or not a label of its task")
        else:
            labels[item] = given[item]
    if problems:
        raise ValueError("consensus rejected:\n  " + "\n  ".join(problems))
    return labels


def against_pipeline(key: dict, consensus: dict) -> dict:
    """Accuracy over all items and over the items the humans could decide; a human unknown lowers coverage."""
    report = {}
    for (sample, task), items in cells(key).items():
        decided = [i for i in items if consensus[i] != "unknown"]
        matrix = Counter((consensus[i], key[i]["pipeline"]) for i in items)
        report[f"{sample}/{task}"] = dict(
            accuracy=proportion(sum(consensus[i] == key[i]["pipeline"] for i in items), len(items)),
            accuracy_decided=proportion(sum(consensus[i] == key[i]["pipeline"] for i in decided), len(decided)),
            human_coverage=proportion(len(decided), len(items)),
            confusion=[dict(human=h, pipeline=p, n=n) for (h, p), n in sorted(matrix.items())])
    return report


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def score(a1: Path, a2: Path, consensus: Path | None, stage: str = STAGE, private: Path = PRIVATE) -> dict:
    problems = freeze.verify(stage)
    if problems:
        raise ValueError(f"{stage} check failed, no answer read: " + ", ".join(problems))
    key = json.loads((private / "key.json").read_text(encoding="utf-8"))
    first, second = read_answers(a1, "A1", key), read_answers(a2, "A2", key)
    out = private / "score"
    sheet = disagreements(key, first, second)
    write_csv(out / "disagreements.csv", sheet, list(sheet[0]) if sheet else ["uid", "consensus", "reason"])
    report = dict(stage=stage, inputs={"A1": freeze.sha256(Path(a1)), "A2": freeze.sha256(Path(a2))},
                  timing={"A1": timing(first), "A2": timing(second)},
                  agreement=agreement(key, first, second), disagreements=len(sheet))
    if consensus:
        report["inputs"]["consensus"] = freeze.sha256(Path(consensus))
        report["against_pipeline"] = against_pipeline(key, read_consensus(consensus, key, first, second))
    (out / "score.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a1", required=True, type=Path)
    parser.add_argument("--a2", required=True, type=Path)
    parser.add_argument("--consensus", type=Path)
    parser.add_argument("--stage", default=STAGE)
    args = parser.parse_args()
    report = score(args.a1, args.a2, args.consensus, args.stage)
    print(json.dumps({k: report[k] for k in ("timing", "agreement", "disagreements")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
