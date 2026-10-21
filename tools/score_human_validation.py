"""Score the human-labelling packet against the pipeline and the model's first round.

Usage: python -B -X utf8 tools/score_human_validation.py
Reads reports/human_validation/{answers.csv,key.json} and writes human_validation.json beside them.
"""

import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "reports/human_validation"


def kappa(pairs):
    """Cohen's kappa over paired labels."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    left, right = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    expected = sum(left[label] * right[label] for label in set(left) | set(right)) / (n * n)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def main() -> int:
    key = json.loads((DIR / "key.json").read_text(encoding="utf-8"))
    labels = json.loads((ROOT / "data/derived/validity-v1/goldset/labels_round1.json").read_text(encoding="utf-8"))["labels"]
    with (DIR / "answers.csv").open(encoding="utf-8", newline="") as stream:
        answers = [row for row in csv.DictReader(stream) if row["answer"].strip()]

    if not answers:
        print("no answers yet; fill the answer column in reports/human_validation/answers.csv")
        return 0

    pairs, model_pairs, rows = [], [], []
    for row in answers:
        identifier = row["id"]
        human = row["answer"].strip()
        pipeline = key[identifier]["pipeline"]
        model = labels.get(identifier)
        model = model.get("label") if isinstance(model, dict) else model
        pairs.append((human, pipeline))
        if model is not None:
            model_pairs.append((human, model))
        rows.append(dict(id=identifier, task=row["task"], human=human, pipeline=pipeline, model=model, note=row["note"]))

    report = dict(
        answered=len(answers), sampled=len(key),
        agreement_with_pipeline=sum(a == b for a, b in pairs) / len(pairs),
        kappa_with_pipeline=kappa(pairs),
        agreement_with_model=sum(a == b for a, b in model_pairs) / len(model_pairs) if model_pairs else None,
        kappa_with_model=kappa(model_pairs) if model_pairs else None,
        disagreements=[row for row in rows if row["human"] != row["pipeline"]],
        rows=rows,
    )
    (DIR / "human_validation.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({key: report[key] for key in ("answered", "agreement_with_pipeline", "kappa_with_pipeline",
                                                   "agreement_with_model", "kappa_with_model")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
