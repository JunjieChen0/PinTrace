"""Build the independent-annotator packets: offline HTML tools, a private answer key and a build record.

Usage: python -B -X utf8 tools/annotation/make_packet.py

Samples
  S1  the 150-item error-oriented gold set (data/derived/validity-v1/goldset), unchanged items;
  S2  a uniform random sample of formal-cohort lineage events (change items) and one reference from each of a
      further set of random events (reference items), drawn by a salted hash ranking;
  P   practice items from the same random frame, disjoint from S1 and S2, used only for the briefing.

Each item shows what a reader of the repository sees: the focus references (path, line, raw ``uses:`` value and
comment), file excerpts read offline from the local bare clones and blob cache, the file diff of the commit, and links
to GitHub. Pipeline labels, strata and sample membership go only to the private key. Every annotator gets the same
items in a different order, under the same opaque item ids.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import heapq
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pintrace.formal_common import WORKING_MANIFEST, sha256  # noqa: E402
from pintrace.validity_goldset import (  # noqa: E402
    EVENTS, pipeline_reference_label, pipeline_transition_label)

RULE_VERSION = "annotation-packet-v1"
GOLD = ROOT / "data/derived/validity-v1/goldset"
REPOSITORIES = ROOT / "data/derived/tables-v5/repositories.csv"
BLOBS = ROOT / "data/cache/blobs"
OUT = ROOT / "reports/human_validation_v2"
BUILD, PRIVATE = OUT / "build", OUT / "private"  # packets to send; key and build record that never leave
TEMPLATE = Path(__file__).with_name("template.html")
SALT = "pintrace-annotation-2026-11-08"
ANNOTATORS = ("A1", "A2")
S2_CHANGES, S2_REFERENCES, PRACTICE_REFERENCES = 200, 50, 6
# Practice change items cover every behaviour the codebook teaches, rare ones included; they are never scored.
PRACTICE_MIX = {"pin": 2, "unpin": 1, "sha_target_change": 2, "mutable_update": 2, "replace": 2, "remove": 2,
                "first_observed_action": 1, "copied_reference": 1, "ambiguous_transition": 1}
PER_EVENT_POOL = 25
CANDIDATE_POOL = 2000
MAX_REFS = 6
EXCERPT_CONTEXT = 6
DIFF_CONTEXT = 3
MAX_DIFF_LINES = 160
REFERENCE_LABELS = ["pinned", "mutable", "not_direct", "unresolvable"]
CHANGE_LABELS = ["pin", "unpin", "sha_change", "tag_change", "replace", "remove", "entry", "no_change", "mixed", "unknown"]
TASKS = {
    "classify_reference": dict(question="这条 uses 引用属于哪一类？ / What kind of reference is this?", labels=REFERENCE_LABELS),
    "classify_change": dict(question="与变更前相比，这些引用发生了什么？ / Relative to before, what happened to these references?",
                            labels=CHANGE_LABELS),
}


# ---------------------------------------------------------------- pure helpers

def rank(text: str, salt: str = SALT) -> str:
    return hashlib.sha256((salt + "\0" + text).encode("utf-8")).hexdigest()


def uid(source_key: str) -> str:
    """Opaque id shared by every annotator's copy; it reveals neither sample nor class."""
    return "i" + rank(source_key, SALT + "|uid")[:9]


def side(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def focus(ref: dict) -> dict:
    """Only what a reader of the file sees; the pipeline's kind, ref type and lineage status are left out."""
    return dict(path=ref.get("path"), line=ref.get("line"), job=ref.get("job"), step_index=ref.get("step_index"),
                step_name=ref.get("step_name"), value=ref.get("value"), comment=ref.get("comment_text"))


def excerpt(lines: list[str], line: int | None, context: int = EXCERPT_CONTEXT) -> dict | None:
    """A window of numbered lines around ``line`` (1-based), with the focus line marked."""
    if not lines or not line or line < 1 or line > len(lines):
        return None
    start, stop = max(1, line - context), min(len(lines), line + context)
    return dict(start=start, focus=line, lines=lines[start - 1:stop])


def _hunk(group: list, before: list[str], after: list[str]) -> list[dict]:
    first, last = group[0], group[-1]
    rows = [dict(kind="hunk", text=f"@@ -{first[1] + 1},{last[2] - first[1]} +{first[3] + 1},{last[4] - first[3]} @@")]
    for tag, i1, i2, j1, j2 in group:
        if tag == "equal":
            rows += [dict(kind="same", old=i + 1, new=j + 1, text=before[i]) for i, j in zip(range(i1, i2), range(j1, j2))]
            continue
        if tag in ("replace", "delete"):
            rows += [dict(kind="del", old=i + 1, new=None, text=before[i]) for i in range(i1, i2)]
        if tag in ("replace", "insert"):
            rows += [dict(kind="add", old=None, new=j + 1, text=after[j]) for j in range(j1, j2)]
    return rows


def diff_rows(before: list[str], after: list[str], context: int = DIFF_CONTEXT, limit: int = MAX_DIFF_LINES,
              focus_old=(), focus_new=()) -> dict:
    """Unified diff as typed rows with old/new line numbers.

    A diff longer than ``limit`` rows keeps whole hunks: first those that contain a focus line, then the others in
    file order, as long as they fit. Hunks are always shown in file order.
    """
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    hunks = [_hunk(group, before, after) for group in matcher.get_grouped_opcodes(context)]
    total = sum(len(h) for h in hunks)
    if total <= limit:
        return dict(rows=[row for h in hunks for row in h], truncated=0)
    old, new = set(focus_old), set(focus_new)
    touches = [any(row.get("old") in old or row.get("new") in new for row in h if row["kind"] != "hunk") for h in hunks]
    keep, used = set(), 0
    for index in sorted(range(len(hunks)), key=lambda i: (not touches[i], i)):
        if used + len(hunks[index]) <= limit or (not keep and touches[index]):
            keep.add(index)
            used += len(hunks[index])
    rows = [row for i, h in enumerate(hunks) if i in keep for row in h]
    return dict(rows=rows, truncated=total - len(rows))


def file_pairs(before_refs: list[dict], after_refs: list[dict], exists_before, exists_after) -> list[tuple]:
    """(before path or None, after path or None) for every file the focus references touch."""
    before_paths = sorted({ref["path"] for ref in before_refs})
    pairs, covered = [], set()
    for path in sorted({ref["path"] for ref in after_refs}):
        if exists_before(path):
            old = path
        elif len(before_paths) == 1 and not exists_after(before_paths[0]):
            old = before_paths[0]  # a rename that the focus references span
        else:
            old = None
        pairs.append((old, path))
        covered.add(old)
    for path in before_paths:
        if path not in covered:
            pairs.append((path, path if exists_after(path) else None))
    return pairs


def pick(ranked: list, count: int, accept) -> tuple[list, dict]:
    """First ``count`` accepted entries of a ranked list, with rejection reasons counted."""
    chosen, skipped = [], {}
    for entry in ranked:
        if len(chosen) == count:
            break
        reason = accept(entry)
        if reason is None:
            chosen.append(entry)
        else:
            skipped[reason] = skipped.get(reason, 0) + 1
    return chosen, skipped


def order(items: list[dict], annotator: str) -> list[dict]:
    return sorted(items, key=lambda item: rank(item["uid"], SALT + "|order|" + annotator))


class Pool:
    """The ``size`` best-ranked entries seen so far, without keeping the whole stream in memory."""

    def __init__(self, size: int):
        self.size, self.heap, self.count = size, [], 0

    def offer(self, key: str, entry) -> None:
        self.count += 1
        # heapq is a min-heap; negating the rank order keeps the worst kept entry at the root.
        item = (_Inverted(key), self.count, entry)
        if len(self.heap) < self.size:
            heapq.heappush(self.heap, item)
        elif key < self.heap[0][0].key:
            heapq.heapreplace(self.heap, item)

    def ranked(self) -> list:
        return [entry for _, _, entry in sorted(self.heap, key=lambda item: item[0].key)]


class _Inverted:
    __slots__ = ("key",)

    def __init__(self, key: str):
        self.key = key

    def __lt__(self, other: "_Inverted") -> bool:
        return self.key > other.key


def script_json(value) -> str:
    """JSON safe to embed in an HTML <script> element."""
    # "<" occurs only inside JSON strings, where < is a valid escape; no "</script>" or "<!--" can survive.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


# ---------------------------------------------------------------- repository access (offline only)

class Files:
    """Workflow files at a commit, from the local bare clone (lazy fetch disabled) and the blob cache only."""

    def __init__(self):
        csv.field_size_limit(2**31 - 1)
        with REPOSITORIES.open(encoding="utf-8", newline="") as stream:
            self.repos = {row["repository_id"]: json.loads(row["payload"]) for row in csv.DictReader(stream)}
        self.env = dict(os.environ, GIT_NO_LAZY_FETCH="1")
        self.cache = {}

    def name(self, repository_id: str) -> str:
        record = self.repos[repository_id]
        return record.get("canonical_repository") or record["repository"]

    def lines(self, repository_id: str, commit: str, path: str) -> list[str] | None:
        key = (repository_id, commit, path)
        if key not in self.cache:
            self.cache[key] = self._read(repository_id, commit, path)
        return self.cache[key]

    def _read(self, repository_id, commit, path):
        directory = ROOT / self.repos[repository_id]["git_directory"]
        out = subprocess.run(["git", "--git-dir", str(directory), "ls-tree", commit, "--", path],
                             capture_output=True, text=True, env=self.env, encoding="utf-8")
        fields = out.stdout.split()
        if out.returncode or len(fields) < 3 or fields[1] != "blob":
            return None
        blob = BLOBS / fields[2]
        if not blob.exists():
            raise FileNotFoundError(f"Blob {fields[2]} for {path} at {commit} is not cached")
        return blob.read_bytes().decode("utf-8", errors="replace").splitlines()


# ---------------------------------------------------------------- items

def read_events(formal: set[str]):
    """Every lineage event, flagged by formal-cohort membership."""
    csv.field_size_limit(2**31 - 1)
    with (ROOT / EVENTS).open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            payload = json.loads(row["payload"])
            yield dict(event_id=row["event_id"], event=row["event"], repository_id=row["repository_id"],
                       commit=row["commit"], previous_commit=row["previous_commit"],
                       before=side(payload.get("before")), after=side(payload.get("after")),
                       formal=row["repository_id"] in formal)


def links(repo: str, commit: str, previous: str | None) -> dict:
    base = f"https://github.com/{repo}"
    output = dict(commit=f"{base}/commit/{commit}")
    if previous:
        output["compare"] = f"{base}/compare/{previous}...{commit}"
    return output


def change_item(row: dict, files: Files) -> dict:
    repo, rid = files.name(row["repository_id"]), row["repository_id"]
    before, after = row["before"][:MAX_REFS], row["after"][:MAX_REFS]
    old_commit, new_commit = row["previous_commit"], row["commit"]

    def ref_view(ref, commit):
        lines = files.lines(rid, commit, ref["path"])
        return dict(focus(ref), excerpt=excerpt(lines or [], ref.get("line")),
                    url=f"https://github.com/{repo}/blob/{commit}/{ref['path']}#L{ref.get('line')}")

    pairs = file_pairs(before, after, lambda p: files.lines(rid, old_commit, p) is not None,
                       lambda p: files.lines(rid, new_commit, p) is not None)
    diffs = []
    for old, new in pairs:
        old_lines = files.lines(rid, old_commit, old) if old else []
        new_lines = files.lines(rid, new_commit, new) if new else []
        focus_old = [ref.get("line") for ref in before if ref["path"] == old]
        focus_new = [ref.get("line") for ref in after if ref["path"] == new]
        diffs.append(dict(before_path=old, after_path=new,
                          **diff_rows(old_lines or [], new_lines or [], focus_old=focus_old, focus_new=focus_new)))
    return dict(task="classify_change", repository=repo, links=links(repo, new_commit, old_commit),
                before=[ref_view(ref, old_commit) for ref in before], after=[ref_view(ref, new_commit) for ref in after],
                diffs=diffs)


def reference_item(ref: dict, rid: str, commit: str, files: Files) -> dict:
    repo = files.name(rid)
    lines = files.lines(rid, commit, ref["path"])
    return dict(task="classify_reference", repository=repo, links=dict(commit=f"https://github.com/{repo}/commit/{commit}"),
                reference=dict(focus(ref), excerpt=excerpt(lines or [], ref.get("line")),
                               url=f"https://github.com/{repo}/blob/{commit}/{ref['path']}#L{ref.get('line')}"))


def readable(row: dict, files: Files) -> str | None:
    """Reason to skip an event, or None when every focus file can be read offline."""
    if not row["formal"]:
        return "outside_formal_cohort"
    if not row["before"] and not row["after"]:
        return "no_references"
    if len(row["before"]) > MAX_REFS or len(row["after"]) > MAX_REFS:
        return "more_than_six_references_per_side"
    for refs, commit in ((row["before"], row["previous_commit"]), (row["after"], row["commit"])):
        for ref in refs:
            if files.lines(row["repository_id"], commit, ref["path"]) is None:
                return "focus_file_not_readable"
    return None


def reference_source(row: dict) -> tuple[dict, str, str]:
    """The after-side reference when there is one, else the before side; the first by a hash ranking."""
    refs, step, commit = (row["after"], "after", row["commit"]) if row["after"] else (row["before"], "before", row["previous_commit"])
    chosen = min(range(len(refs)), key=lambda i: rank(f"{row['event_id']}/{i}"))
    return refs[chosen], f"{step}:{chosen}", commit


def build(root: Path = ROOT) -> dict:
    files = Files()
    manifest = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    formal = {str(i) for i in manifest["formal_population"]["formal_eligible_repository_ids"]}
    gold_key = json.loads((GOLD / "answer_key.json").read_text(encoding="utf-8"))
    gold_events = {entry["source"]["event_id"] for entry in gold_key.values()}

    # Keep only the best-ranked candidates in memory; the frame size is still counted in full.
    pool, by_event, gold_rows = Pool(CANDIDATE_POOL), {event: Pool(PER_EVENT_POOL) for event in PRACTICE_MIX}, {}
    for row in read_events(formal):
        if row["event_id"] in gold_events:
            gold_rows[row["event_id"]] = row
        elif row["formal"]:
            pool.offer(rank(row["event_id"]), row)
            if row["event"] in by_event:
                by_event[row["event"]].offer(rank(row["event_id"], SALT + "|practice"), row)
    rows, frame = pool.ranked(), pool.count

    items, key = [], {}

    def add(item, sample, source_key, pipeline, extra):
        item_id = uid(source_key)
        if item_id in key:
            raise ValueError("Two items share an id: " + source_key)
        items.append(dict(item, uid=item_id, sample=sample))
        key[item_id] = dict(sample=sample, task=item["task"], pipeline=pipeline, source=source_key, **extra)

    # S1: the gold set, same items and pipeline labels as the earlier rounds.
    for gold_id, entry in sorted(gold_key.items()):
        row = gold_rows[entry["source"]["event_id"]]
        source = f"{row['event_id']}/{entry['source']['step']}"
        if entry["kind"] == "transition":
            item = change_item(row, files)
        else:
            refs, commit = (row["after"], row["commit"]) if entry["source"]["step"] == "after" else (row["before"], row["previous_commit"])
            item = reference_item(refs[0], row["repository_id"], commit, files)
        add(item, "S1", source, entry["pipeline"], dict(stratum=entry["stratum"], gold_id=gold_id, event=row["event"]))

    accept = lambda row: readable(row, files)
    s2_changes, skipped_changes = pick(rows, S2_CHANGES, accept)
    used = {row["event_id"] for row in s2_changes}
    rest = [row for row in rows if row["event_id"] not in used]
    s2_refs, skipped_refs = pick(rest, S2_REFERENCES, accept)
    used |= {row["event_id"] for row in s2_refs}
    rest = [row for row in rest if row["event_id"] not in used]
    practice_changes, skipped_practice = [], {}
    for event, count in PRACTICE_MIX.items():
        chosen, skipped = pick([row for row in by_event[event].ranked() if row["event_id"] not in used], count, accept)
        if len(chosen) < count:
            raise ValueError("Too few practice candidates for " + event)
        practice_changes += chosen
        used |= {row["event_id"] for row in chosen}
        for reason, n in skipped.items():
            skipped_practice[reason] = skipped_practice.get(reason, 0) + n
    practice_refs, skipped = pick([row for row in rest if row["event_id"] not in used], PRACTICE_REFERENCES, accept)
    for reason, n in skipped.items():
        skipped_practice[reason] = skipped_practice.get(reason, 0) + n
    if len(practice_refs) < PRACTICE_REFERENCES:
        raise ValueError("The candidate pool is too small; raise CANDIDATE_POOL")
    practice = practice_changes + practice_refs

    for row in s2_changes:
        add(change_item(row, files), "S2", f"{row['event_id']}/event", pipeline_transition_label(row["event"]), dict(event=row["event"]))
    for row in s2_refs:
        ref, step, commit = reference_source(row)
        add(reference_item(ref, row["repository_id"], commit, files), "S2", f"{row['event_id']}/{step}",
            pipeline_reference_label(ref), dict(event=row["event"]))

    practice_items, practice_key = [], {}
    for index, row in enumerate(practice):
        if index < len(practice_changes):
            item, step, label = change_item(row, files), "event", pipeline_transition_label(row["event"])
        else:
            ref, step, commit = reference_source(row)
            item, label = reference_item(ref, row["repository_id"], commit, files), pipeline_reference_label(ref)
        item_id = uid(f"practice/{row['event_id']}/{step}")
        practice_items.append(dict(item, uid=item_id, sample="P"))
        practice_key[item_id] = dict(sample="P", task=item["task"], pipeline=label, event=row["event"],
                                     source=f"{row['event_id']}/{step}")

    return dict(items=items, key=key, practice=practice_items, practice_key=practice_key,
                skipped=dict(S2_changes=skipped_changes, S2_references=skipped_refs, practice=skipped_practice),
                frame=dict(formal_events_outside_gold_set=frame, gold_items=len(gold_key)))


def public(item: dict) -> dict:
    """The annotator's view of an item: no sample membership, stratum or label."""
    return {k: v for k, v in item.items() if k != "sample"}


def render(template: str, packet_id: str, annotator: str, items: list[dict], practice: bool) -> str:
    data = dict(packet_id=packet_id, annotator=annotator, practice=practice, rule_version=RULE_VERSION, tasks=TASKS,
                items=[public(item) for item in items])
    title = "PinTrace 标注练习" if practice else f"PinTrace 标注 · {annotator}"
    return template.replace("{{TITLE}}", html.escape(title)).replace("{{PACKET_JSON}}", script_json(data))


def write(result: dict, out: Path = BUILD, private: Path = PRIVATE) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True)
    template = TEMPLATE.read_text(encoding="utf-8")
    written = {}
    for annotator in ANNOTATORS:
        packet_id = f"pintrace-v2-{annotator}"
        path = out / f"PinTrace_annotation_{annotator}.html"
        path.write_text(render(template, packet_id, annotator, order(result["items"], annotator), False), encoding="utf-8", newline="\n")
        written[annotator] = path
    practice = out / "PinTrace_practice.html"
    practice.write_text(render(template, "pintrace-v2-practice", "practice", order(result["practice"], "practice"), True), encoding="utf-8", newline="\n")
    dump = lambda name, value: (private / name).write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    dump("key.json", result["key"])
    dump("practice_key.json", result["practice_key"])
    counts = {}
    for entry in result["key"].values():
        counts.setdefault(entry["sample"], {}).setdefault(entry["task"], 0)
        counts[entry["sample"]][entry["task"]] += 1
    record = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), salt=SALT,
                  implementation_sha256=sha256(Path(__file__)), template_sha256=sha256(TEMPLATE),
                  event_file=EVENTS, gold_key_sha256=sha256(GOLD / "answer_key.json"),
                  items=len(result["items"]), counts=counts, practice_items=len(result["practice"]),
                  skipped=result["skipped"], frame=result["frame"],
                  packets={name: dict(file=path.name, sha256=sha256(path)) for name, path in written.items()},
                  practice_packet=dict(file=practice.name, sha256=sha256(practice)),
                  offline=dict(git_lazy_fetch_disabled=True, blobs="local cache only; a missing blob is an error"))
    dump("build.json", record)
    return record


def packet_items(page: str) -> dict:
    """The packet data embedded in a rendered annotator page."""
    found = re.search(r'<script id="packet" type="application/json">(.*?)</script>', page, re.S)
    if not found:
        raise ValueError("No packet data in the page")
    return json.loads(found.group(1))


def add_annotator(annotator: str, source: str = "A1", out: Path = BUILD, private: Path = PRIVATE) -> dict:
    """A packet for a replacement annotator, built from a frozen packet so that the items are identical.

    The source page is first re-rendered from its own items in its own order and must equal the frozen file byte
    for byte; only then is the new annotator's order rendered.
    """
    if annotator in ANNOTATORS or (out / f"PinTrace_annotation_{annotator}.html").exists():
        raise ValueError(f"A packet for {annotator} already exists")
    template = TEMPLATE.read_text(encoding="utf-8")
    source_path = out / f"PinTrace_annotation_{source}.html"
    page = source_path.read_text(encoding="utf-8")
    items = packet_items(page)["items"]
    if render(template, f"pintrace-v2-{source}", source, order(items, source), False) != page:
        raise ValueError(f"The {source} packet cannot be reproduced from its own items; refusing to derive a new one")
    path = out / f"PinTrace_annotation_{annotator}.html"
    path.write_text(render(template, f"pintrace-v2-{annotator}", annotator, order(items, annotator), False),
                    encoding="utf-8", newline="\n")
    record = dict(annotator=annotator, source=source, source_sha256=sha256(source_path), file=path.name,
                  sha256=sha256(path), items=len(items), created_at_utc=datetime.now(timezone.utc).isoformat())
    (private / f"build_{annotator}.json").write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n",
                                                     encoding="utf-8", newline="\n")
    return record


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--add":
        print(json.dumps(add_annotator(sys.argv[2]), ensure_ascii=False))
        return
    record = write(build())
    print(json.dumps(dict(items=record["items"], counts=record["counts"], practice=record["practice_items"],
                          skipped=record["skipped"], directory=OUT.relative_to(ROOT).as_posix()), ensure_ascii=False))


if __name__ == "__main__":
    main()
