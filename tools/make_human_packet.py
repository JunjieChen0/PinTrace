"""Draw a 40-item human-labelling packet from the 150-item gold set, without any pipeline or model labels.

Writes reports/human_validation/{packet.md,answers.csv,key.json} with a deterministic stratified sample.
The answer sheet stays empty until the author fills it; tools/score_human_validation.py then scores it.
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "data/derived/validity-v1/goldset"
OUT = ROOT / "reports/human_validation"
SAMPLE = 40
SEED = 20261022
TASKS = {
    "classify_reference": ("这个 uses 引用是什么？", ["pinned", "mutable", "not_direct", "unresolvable"]),
    "classify_change": ("按 before/after 两列证据，相对 before 发生了什么？",
                        ["pin", "unpin", "sha_change", "tag_change", "no_change", "replace", "remove", "entry", "unknown"]),
}


def allocate(counts, total):
    """Largest-remainder allocation with at least one item per stratum."""
    keys = sorted(counts)
    base = {key: 1 for key in keys}
    remaining = total - len(keys)
    quota = {key: counts[key] / sum(counts.values()) * total for key in keys}
    for key in sorted(keys, key=lambda k: -(quota[k] - base[k])):
        if remaining <= 0:
            break
        if base[key] < counts[key]:
            base[key] += 1
            remaining -= 1
    while remaining > 0:
        for key in sorted(keys, key=lambda k: -(counts[k] - base[k])):
            if remaining <= 0:
                break
            if base[key] < counts[key]:
                base[key] += 1
                remaining -= 1
    return base


def render(item, number):
    task, labels = TASKS[item["task"]]
    evidence = item["evidence"]
    lines = [f"## {number:02d}. `{item['id']}`", "", f"**任务**：{task}", ""]
    if "before" in evidence or "after" in evidence:
        for side, caption in (("before", "变更前"), ("after", "变更后")):
            for row in evidence.get(side, []):
                lines.append(f"- {caption}：`{row['path']}:{row['line']}` job `{row['job']}` step {row['step_index']} `{row['value']}` 注释 `{row['comment']}`")
    else:
        lines.append(f"- 文件：`{evidence['path']}:{evidence['line']}`")
        lines.append(f"- job：`{evidence['job']}`；step {evidence['step_index']} `{evidence.get('step_name')}`")
        lines.append(f"- 值：`{evidence['value']}`；注释：`{evidence.get('comment')}`")
    lines += ["", "选项：" + " / ".join(f"`{label}`" for label in labels), ""]
    return "\n".join(lines)


def main() -> None:
    packet = json.loads((GOLD / "packet_round1.json").read_text(encoding="utf-8"))["items"]
    key = json.loads((GOLD / "answer_key.json").read_text(encoding="utf-8"))
    import numpy as np

    by_stratum = defaultdict(list)
    for item in packet:
        by_stratum[key[item["id"]]["stratum"]].append(item)
    quota = allocate({k: len(v) for k, v in by_stratum.items()}, SAMPLE)

    chosen = []
    for stratum in sorted(by_stratum):
        pool = sorted(by_stratum[stratum], key=lambda x: x["id"])
        rng = np.random.default_rng(SEED + int.from_bytes(stratum.encode()[:4], "little"))
        picks = rng.choice(len(pool), size=min(quota[stratum], len(pool)), replace=False)
        chosen += [pool[i] for i in sorted(picks)]
    chosen = sorted(chosen, key=lambda x: x["id"])

    OUT.mkdir(parents=True, exist_ok=True)
    body = [
        "# 人工标注包（40 条）",
        "",
        "本包从 150 条金标集按分层比例抽取，**不含任何管线或模型标签**。",
        "请逐条在 `answers.csv` 的 `answer` 列填写其中一个选项；不确定时填 `unknown` 并在 `note` 写明原因。",
        "填写完成后运行 `python -B -X utf8 tools/score_human_validation.py`。",
        "",
        f"条目数：{len(chosen)}；随机种子：{SEED}。",
        "",
    ]
    body += [render(item, i) for i, item in enumerate(chosen, 1)]
    (OUT / "packet.md").write_text("\n".join(body), encoding="utf-8", newline="\n")

    with (OUT / "answers.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "task", "answer", "note"])
        for item in chosen:
            writer.writerow([item["id"], item["task"], "", ""])

    (OUT / "key.json").write_text(json.dumps({item["id"]: key[item["id"]] for item in chosen}, indent=1, ensure_ascii=False) + "\n",
                                  encoding="utf-8", newline="\n")
    print(json.dumps({"items": len(chosen), "strata": quota, "directory": OUT.relative_to(ROOT).as_posix()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
