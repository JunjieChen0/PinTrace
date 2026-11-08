"""Release timing: was the tag that places an adopted SHA on a release already evidenced when the SHA was adopted?

The main analysis (formal_extensions.tag_index) merges tag bindings from every archive visit and release record,
whatever their date, so a commit adopted first and tagged later would count as a release. This audit repeats the
release classifications with a time-aware index that keeps, for each moment, only the bindings evidenced at or
before it: an archive visit that saw the tag at that commit, or the publication time of a release at that commit.

It covers the release routes of the maintenance analysis (what "another SHA" means) and the advisory-line labels of
RQ1 (first responses and the target trajectory). Evidence that arrives later than an adoption does not show the tag
was created later: archive visits are discrete, so the strict rule is a lower bound on what was knowable.

Usage: python -m pintrace.release_timing
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import formal_extensions as fx
from .formal_common import WORKING_MANIFEST, sha256, write_json

RULE_VERSION = "release-timing-v1"
BATCH = "release-timing-v1"
TARGET = "tj-actions/changed-files"


def _time(value):
    """Unix seconds from a Unix-seconds string or an ISO-8601 timestamp; None when empty."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def earliest_evidence(document):
    """(lowercase SHA, tag) -> earliest time a binding of that tag to that commit is evidenced."""
    first = {}
    for row, field in [*((r, "visit_date") for r in document.get("observations", [])),
                       *((r, "published_at") for r in document.get("release_records", []))]:
        when = _time(row.get(field))
        if row.get("target") and row.get("tag") and when is not None:
            key = (row["target"].lower(), row["tag"])
            first[key] = min(first.get(key, when), when)
    return first


class TimedIndex:
    """A tag index restricted to bindings evidenced at or before a moment."""

    def __init__(self, evidence):
        self.evidence = evidence

    def at(self, moment):
        index = defaultdict(set)
        for (sha, tag), when in self.evidence.items():
            if when <= moment:
                index[sha].add(tag)
        return index


def timing(tags, sha, evidence, moment):
    """'before' if any tag bound to ``sha`` is evidenced by ``moment``, 'after' if only later, else 'no_tag'."""
    times = [evidence[(sha, tag)] for tag in tags if (sha, tag) in evidence]
    if not times:
        return "no_tag"
    return "before" if min(times) <= moment else "after"


# ---------------------------------------------------------------- maintenance release routes

def route_timing(rows, entries, indices, timed):
    """Each changed opportunity entry: its route under the merged index and under the index at adoption time."""
    by_key = {(str(e["repository_id"]), e["lineage_id"], e["opportunity_id"]): e for e in entries}
    output = []
    for row in rows:
        entry = by_key[(row["repository_id"], row["lineage_id"], row["opportunity_id"])]
        upstream, outcome = entry["upstream_repository"], row["outcome"]
        merged = fx.adopted_route(entry, outcome, indices[upstream])
        if merged is None:
            continue
        adopted = _time(row["outcome_timestamp"])
        if merged == "opportunity_target":
            # The target is the release itself: it must be available when adopted (grade A exact, grade B upper bound).
            bound = _time(row["available_after_or_at"]) if row["evidence_grade"] == "A" else _time(row["available_by"])
            evidence = "before" if bound is not None and bound <= adopted else "after"
            strict = merged if evidence == "before" else "release_not_yet_evidenced"
        else:
            sha = str(entry.get("adopted_sha") or "").lower()
            evidence = timing(indices[upstream].get(sha, ()), sha, timed[upstream].evidence, adopted)
            strict = fx.adopted_route(entry, outcome, timed[upstream].at(adopted))
        output.append(dict(repository_id=row["repository_id"], role=row["role"], grade=row["evidence_grade"],
                           route=merged, strict_route=strict, evidence=evidence))
    return output


def summarize_routes(detail):
    channel = lambda key: sum(d[key] in {"opportunity_target", "later_release"} for d in detail)
    return dict(changed_entries=len(detail),
                evidence=dict(Counter(f"{d['route']}.{d['evidence']}" for d in detail)),
                routes=dict(Counter(d["route"] for d in detail)), strict_routes=dict(Counter(d["strict_route"] for d in detail)),
                channel=channel("route"), strict_channel=channel("strict_route"),
                unchanged=all(d["route"] == d["strict_route"] for d in detail))


# ---------------------------------------------------------------- RQ1 advisory lines

def first_response_time(record, row):
    times = [e["timestamp"] for e in record["events"] if e["commit"] == row["first_commit"]]
    return min(times) if times else None


def rq1_timing(record, merged, timed):
    """First-response and trajectory labels under the merged index and under the index at each moment."""
    row = fx.describe_repository(record)["rq1"]
    output = dict(repository_id=record["repository_id"], first_response_state=row["first_response_state"])
    if row["first_response_state"] in {"pin", "sha_target_change", "tag_update"}:
        moment = first_response_time(record, row)
        main = fx.response_targets(record, merged)
        strict = fx.response_targets(record, timed.at(moment))
        output.update(first_target=main.get("first_target"), strict_first_target=strict.get("first_target"),
                      after_lines=main.get("after_lines", {}), strict_after_lines=strict.get("after_lines", {}))
    trajectory, strict_trajectory = {}, {}
    for days in fx.TRAJECTORY_DAYS:
        cutoff = record["anchor"] + days * fx.DAY - (1 if days == 0 else 0)
        trajectory[str(days)] = fx.trajectory_state(fx.target_lines_at(record, days, merged))
        strict_trajectory[str(days)] = fx.trajectory_state(fx.target_lines_at(record, days, timed.at(cutoff)))
    output.update(trajectory=trajectory, strict_trajectory=strict_trajectory)
    return output


def summarize_rq1(rows):
    def lines(key, state):
        return dict(sum((Counter(r.get(key, {})) for r in rows if r["first_response_state"] == state), Counter()))

    output = {}
    for state in ("pin", "sha_target_change", "tag_update"):
        chosen = [r for r in rows if r["first_response_state"] == state]
        output[state] = dict(repositories=len(chosen),
                             first_target=dict(Counter(r["first_target"] for r in chosen)),
                             strict_first_target=dict(Counter(r["strict_first_target"] for r in chosen)),
                             changed_repositories=sum(r["first_target"] != r["strict_first_target"] for r in chosen),
                             after_lines=lines("after_lines", state), strict_after_lines=lines("strict_after_lines", state))
    output["trajectory"] = {days: dict(main=dict(Counter(r["trajectory"][days] for r in rows)),
                                       strict=dict(Counter(r["strict_trajectory"][days] for r in rows)),
                                       changed_repositories=sum(r["trajectory"][days] != r["strict_trajectory"][days] for r in rows))
                            for days in (str(d) for d in fx.TRAJECTORY_DAYS)}
    return output


def coverage(document):
    """First dated tag evidence of an upstream: archive visits and release publications."""
    visits = sorted(r["visit_date"] for r in document.get("observations", []) if r.get("visit_date"))
    releases = sorted(r["published_at"] for r in document.get("release_records", []) if r.get("published_at"))
    return dict(first_archive_visit=visits[0] if visits else None, first_release_publication=releases[0] if releases else None)


# ---------------------------------------------------------------- run

def run(root):
    root = Path(root).resolve()
    run_path, run_record = fx.latest_working_run(root)
    results = run_record["output_directory"] + "/"
    bound = fx._verify(root, run_record, WORKING_MANIFEST, results)
    frozen = json.loads((root / WORKING_MANIFEST).read_text(encoding="utf-8"))
    formal = {str(i) for i in frozen["formal_population"]["formal_eligible_repository_ids"]}
    indices, timed = {}, {}
    for path in (p for p in bound if p.startswith(fx.UPSTREAM)):
        document = json.loads((root / path).read_text(encoding="utf-8"))
        name = document["summary"]["repository"]
        indices[name], timed[name] = fx.tag_index(document), TimedIndex(earliest_evidence(document))
    csv.field_size_limit(2**31 - 1)
    with (root / (results + "rq3_maintenance_records.csv")).open(encoding="utf-8", newline="") as stream:
        rows = [row for row in csv.DictReader(stream) if row["population"] == "formal"]
    with (root / fx.ENTRIES).open(encoding="utf-8") as stream:
        entries = [json.loads(line) for line in stream if line.strip()]
    published = json.loads((root / "reports/formal_extensions.json").read_text(encoding="utf-8"))
    routes = summarize_routes(route_timing(rows, entries, indices, timed))
    if routes["changed_entries"] != published["updater_routes"]["changed_entries"]:
        raise ValueError("Route timing does not reproduce the published changed entries")
    with (root / (results + "rq12_repository_records.jsonl")).open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    rq1 = summarize_rq1([rq1_timing(r, indices[TARGET], timed[TARGET]) for r in records if r["repository_id"] in formal])
    if rq1["pin"]["first_target"] != published["response_targets"]["pin"]:
        raise ValueError("Merged-index first responses do not reproduce the published RQ1 pin lines")
    result = dict(rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(),
                  implementation_sha256=sha256(Path(__file__)), source_run=run_path, input_sha256=bound,
                  routes=routes, rq1=rq1,
                  target_coverage=coverage(json.loads((root / fx.UPSTREAM / "tj-actions__changed-files.json").read_text(encoding="utf-8"))),
                  interpretation="'before' means a binding of a tag to the adopted commit, or the release itself, was "
                                 "evidenced at or before adoption. 'after' means the first evidence is later, which can "
                                 "reflect archive coverage rather than a later tag.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / "data/derived" / BATCH / "runs" / stamp
    write_json(directory / "release_timing.json", result)
    target = root / "reports/release_timing.json"
    if target.relative_to(root).as_posix() in frozen["input_sha256"]:
        raise ValueError("Attempt to overwrite a frozen input")
    target.write_bytes((directory / "release_timing.json").read_bytes())
    print(json.dumps(dict(completed=True, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    run(Path(__file__).resolve().parents[1])
