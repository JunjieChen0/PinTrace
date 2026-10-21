"""Deterministic re-export check: the same inputs must give the same bytes.

Two stages, both strictly offline and neither able to overwrite an existing output:

* Measurement. A deterministic size-stratified sample of repositories (plus every repository whose commit clock cannot
  be aligned) is measured again with the unchanged ``measurement.collect`` into a separate directory. Git objects come
  from the local bare clones with lazy fetching disabled, and blobs come from the local cache only, so a missing object
  is an error and never a download. Each result is compared line by line with the stored ``measurement-v4`` file; the
  only line allowed to differ is the wall-clock ``measurement_checked_at_utc`` stamp.
* Analyses. ``formal_extensions``, ``formal_activity``, ``formal_competing``, ``formal_placebo`` and
  ``formal_sensitivity`` are run end to end twice with their ``Path.write_text`` / ``Path.write_bytes`` writes redirected
  into a sandbox (a write to any other place inside the project raises). Their output is compared with the published
  report and with each other; only the top-level ``created_at_utc`` line may differ. Afterwards the project must show no
  new run directory and no changed report, which also covers any write route the redirect does not intercept.

A repository above the size cap is not re-exported (building its full JSON needs far more memory than the machine has);
the per-snapshot verification covers it instead. Equal bytes show that the pipeline from cached Git objects to the
files is deterministic for the sampled repositories. They do not show that the cache holds the right objects (the
blob hashes are checked against Git separately) or that unsampled repositories reproduce.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib
import itertools
import json
import os
from pathlib import Path
import time
import tomllib
from unittest import mock

from . import measurement
from .formal_common import WORKING_MANIFEST, sha256, write_json

RULE_VERSION = "validity-reexport-v1"
OUTPUT = "data/derived/validity-v1/runs"
MEASUREMENT_VOLATILE = ((4, "measurement_checked_at_utc"),)
REPORT_VOLATILE = ((2, "created_at_utc"),)
CAP_MB = 450
COUNT = 12
RUN_DIRECTORIES = ("data/derived/formal-stats-v3/runs", "data/derived/formal-stats-v4/runs")
# module -> (module attribute that names the run directory, run keyword arguments, published report)
ANALYSES = {
    "formal_extensions": ("WORKING_EXTENSIONS", dict(working=True), "formal_extensions.json"),
    "formal_activity": ("OUTPUT", {}, "formal_activity.json"),
    "formal_competing": ("OUTPUT", {}, "formal_competing_risks.json"),
    "formal_placebo": ("OUTPUT", {}, "formal_placebo.json"),
    "formal_sensitivity": ("OUTPUT", {}, "formal_sensitivity.json"),
}


def _indent_and_key(line):
    stripped = line.lstrip(b" ")
    if not stripped.startswith(b'"'):
        return None
    end = stripped.find(b'":', 1)
    return None if end < 0 else (len(line) - len(stripped), stripped[1:end].decode("utf-8", "replace"))


def compare_files(first, second, volatile=()):
    """Line-by-line comparison; lines whose (indent, key) is listed in ``volatile`` are explained when they differ."""
    volatile = set(volatile)
    digests = (hashlib.sha256(), hashlib.sha256())
    raw = unexplained = explained = lines = 0
    examples = []
    sizes = [0, 0]
    with Path(first).open("rb") as a, Path(second).open("rb") as b:
        for number, (left, right) in enumerate(itertools.zip_longest(a, b), 1):
            lines = number
            for index, line in enumerate((left, right)):
                if line is not None:
                    sizes[index] += len(line)
                    marker = _indent_and_key(line)
                    digests[index].update(b"<volatile>\n" if marker in volatile else line)
            if left == right:
                continue
            raw += 1
            marker = _indent_and_key(left) if left is not None else None
            if left is not None and right is not None and marker in volatile and marker == _indent_and_key(right):
                explained += 1
            else:
                unexplained += 1
                if len(examples) < 3:
                    examples.append(dict(line=number, first=None if left is None else left[:120].decode("utf-8", "replace").rstrip(),
                                         second=None if right is None else right[:120].decode("utf-8", "replace").rstrip()))
    return dict(lines=lines, bytes=sizes, differing_lines=raw, explained_volatile_lines=explained, unexplained_differences=unexplained,
                identical_raw=raw == 0, identical_after_normalization=unexplained == 0 and digests[0].digest() == digests[1].digest(),
                normalized_sha256=[digests[0].hexdigest(), digests[1].hexdigest()], first_unexplained=examples)


def select_sample(sizes, required, count=COUNT, cap_bytes=CAP_MB * 10**6):
    """Evenly spaced quantiles of the size-ordered candidates under the cap, plus the required ids that fit under it."""
    candidates = sorted((size, identity) for identity, size in sizes.items() if size <= cap_bytes)
    chosen = {candidates[min(len(candidates) - 1, int((index + .5) * len(candidates) / count))][1] for index in range(count)} if candidates else set()
    forced = {identity for identity in required if identity in sizes and sizes[identity] <= cap_bytes}
    selected = sorted(chosen | forced, key=lambda identity: (sizes[identity], identity))
    return dict(selected=selected, forced=sorted(forced), over_cap=sorted((i for i, s in sizes.items() if s > cap_bytes), key=lambda i: (sizes[i], i)),
                candidates=len(candidates))


class CacheOnlyClient:
    """Serve blobs from the local cache; a blob that is not cached is an error, never a download."""

    def __init__(self, cache, token=""):
        self.cache = Path(cache)

    def blob(self, repository, commit, path, blob_sha):
        return (self.cache / "blobs" / blob_sha).read_bytes()


@contextmanager
def offline(batch_directory):
    """Measure into ``batch_directory`` (absolute) with neither lazy Git fetches nor network blob reads."""
    previous = os.environ.get("GIT_NO_LAZY_FETCH")
    os.environ["GIT_NO_LAZY_FETCH"] = "1"
    try:
        with mock.patch.object(measurement, "GitHub", CacheOnlyClient), mock.patch.object(measurement, "BATCH_DIRECTORY", str(batch_directory)):
            yield
    finally:
        if previous is None:
            os.environ.pop("GIT_NO_LAZY_FETCH", None)
        else:
            os.environ["GIT_NO_LAZY_FETCH"] = previous


@contextmanager
def contained(root, sandbox):
    """Redirect writes to ``<root>/reports`` into the sandbox and refuse every other write inside the project."""
    root, sandbox = Path(root).resolve(), Path(sandbox).resolve()
    shadow, reports = sandbox / "shadow_reports", root / "reports"
    write_bytes, write_text = Path.write_bytes, Path.write_text

    def destination(path):
        resolved = Path(os.path.abspath(path))
        if resolved.is_relative_to(sandbox):
            return resolved
        if resolved.is_relative_to(reports):
            return shadow / resolved.relative_to(reports)
        if resolved.is_relative_to(root):
            raise PermissionError("The re-export check may not write inside the project: " + str(resolved))
        return resolved

    def guarded_bytes(self, data):
        target = destination(self)
        target.parent.mkdir(parents=True, exist_ok=True)
        return write_bytes(target, data)

    def guarded_text(self, data, *args, **kwargs):
        target = destination(self)
        target.parent.mkdir(parents=True, exist_ok=True)
        return write_text(target, data, *args, **kwargs)

    with mock.patch.object(Path, "write_bytes", guarded_bytes), mock.patch.object(Path, "write_text", guarded_text):
        yield shadow


def fingerprint(root):
    """Size and mtime of every file in reports/ plus the names of the run directories the analyses write to."""
    root = Path(root)
    files = {}
    for path in sorted((root / "reports").rglob("*")):
        if path.is_file():
            status = path.stat()
            files[path.relative_to(root).as_posix()] = (status.st_size, status.st_mtime_ns)
    runs = {name: sorted(entry.name for entry in (root / name).iterdir()) if (root / name).is_dir() else [] for name in RUN_DIRECTORIES}
    return dict(reports=files, runs=runs)


def implementation_hashes(root):
    paths = [root / "study.toml", *[root / "pintrace" / name for name in measurement.SOURCE_NAMES]]
    return {path.relative_to(root).as_posix(): measurement.digest(path) for path in paths}


def reexport_measurements(root, ids, sandbox, log=print):
    root = Path(root).resolve()
    batch = Path(sandbox).resolve() / "measurement"
    config = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    records = {row["repository_id"]: row for row in measurement.confirmed_records(root)}
    implementation = implementation_hashes(root)
    results = {}
    for identity in ids:
        stored, again = root / "data/derived" / measurement.BATCH_DIRECTORY / (identity + ".json"), batch / (identity + ".json")
        if again.exists():
            raise FileExistsError("A re-export would overwrite " + str(again))
        started = time.monotonic()
        with offline(batch):
            summary = measurement.collect(records[identity], root, config, "", implementation_hashes=implementation)
        seconds = time.monotonic() - started
        comparison = compare_files(stored, again, MEASUREMENT_VOLATILE) if again.is_file() else None
        results[identity] = dict(repository=records[identity]["repository"], stored_bytes=stored.stat().st_size, seconds=round(seconds, 1),
                                 status=summary["status"], error=summary.get("error"), snapshots=summary.get("snapshots"),
                                 comparison=comparison)
        log(json.dumps(dict(id=identity, seconds=round(seconds, 1), status=summary["status"],
                            identical=comparison and comparison["identical_after_normalization"])), flush=True)
    return results


def reexport_analyses(root, sandbox, modules=None, log=print):
    root = Path(root).resolve()
    sandbox = Path(sandbox).resolve()
    before = fingerprint(root)
    results = {}
    for name, (constant, keywords, report) in (modules or ANALYSES).items():
        module = importlib.import_module("pintrace." + name)
        outputs = []
        for attempt in ("first", "second"):
            directory = sandbox / "analysis" / name / attempt
            with contained(root, directory), mock.patch.object(module, constant, str(directory / "runs")):
                module.run(root, **keywords)
            outputs.append(directory / "shadow_reports" / report)
        published = root / "reports" / report
        recorded = json.loads(published.read_text(encoding="utf-8")).get("implementation_sha256")
        results[name] = dict(report=report, published_sha256=sha256(published),
                             published_report_built_by_current_source=recorded == sha256(Path(module.__file__)),
                             first_vs_published=compare_files(outputs[0], published, REPORT_VOLATILE),
                             second_vs_first=compare_files(outputs[1], outputs[0], REPORT_VOLATILE))
        log(json.dumps(dict(module=name, identical_to_published=results[name]["first_vs_published"]["identical_after_normalization"],
                            second_identical=results[name]["second_vs_first"]["identical_after_normalization"])), flush=True)
    return results, before == fingerprint(root)


def summarize(measurement_results, analysis_results, project_unchanged, forced, over_cap):
    compared = {i: r["comparison"] for i, r in measurement_results.items()}
    checks = dict(
        sample_is_non_empty=bool(measurement_results),
        every_repository_measured=all(r["status"] == "measured" for r in measurement_results.values()),
        every_file_identical_after_normalization=bool(compared) and all(c and c["identical_after_normalization"] for c in compared.values()),
        only_the_check_timestamp_differs=bool(compared) and all(c and c["differing_lines"] == c["explained_volatile_lines"] <= 1 for c in compared.values()),
        clock_unaligned_repositories_included=bool(forced) and set(forced) <= set(measurement_results),
        every_analysis_identical_to_published=bool(analysis_results) and all(r["first_vs_published"]["identical_after_normalization"] for r in analysis_results.values()),
        published_reports_built_by_current_source=bool(analysis_results) and all(r["published_report_built_by_current_source"] for r in analysis_results.values()),
        every_analysis_repeatable=bool(analysis_results) and all(r["second_vs_first"]["identical_after_normalization"] for r in analysis_results.values()),
        project_unchanged_by_analysis_runs=project_unchanged)
    return checks


def run(root, count=COUNT, cap_mb=CAP_MB, ids=None, skip_measurement=False, skip_analysis=False):
    root = Path(root).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = root / OUTPUT / stamp
    directory.mkdir(parents=True)
    batch = root / "data/derived" / measurement.BATCH_DIRECTORY
    sizes = {path.stem: path.stat().st_size for path in batch.glob("*.json") if path.stem.isdigit()}
    clock = [str(i) for i in json.loads((root / "reports/opportunity_join_summary.json").read_text(encoding="utf-8"))["clock_unalignable_repository_ids"]]
    plan = select_sample(sizes, clock, count, cap_mb * 10**6)
    chosen = [str(i) for i in ids] if ids else plan["selected"]
    print(json.dumps(dict(directory=directory.relative_to(root).as_posix(), selected=len(chosen), over_cap=len(plan["over_cap"]))), flush=True)
    measured = {} if skip_measurement else reexport_measurements(root, chosen, directory)
    write_json(directory / "measurement_partial.json", measured)
    analyses, unchanged = ({}, True) if skip_analysis else reexport_analyses(root, directory)
    checks = summarize(measured, analyses, unchanged, plan["forced"], plan["over_cap"])
    result = dict(
        rule_version=RULE_VERSION, created_at_utc=datetime.now(timezone.utc).isoformat(), implementation_sha256=sha256(Path(__file__)),
        input_sha256={WORKING_MANIFEST: sha256(root / WORKING_MANIFEST), "study.toml": sha256(root / "study.toml")},
        offline=dict(git_lazy_fetch_disabled=True, blobs="local cache only; a missing blob is an error", network_client_replaced=True),
        measurement=dict(batch=measurement.BATCH_DIRECTORY, repositories_in_batch=len(sizes), size_cap_mb=cap_mb, candidates_under_cap=plan["candidates"],
                         sample=chosen, required_clock_unaligned=plan["forced"], not_reexported_over_cap=plan["over_cap"],
                         volatile_lines=[key for _, key in MEASUREMENT_VOLATILE], results=measured,
                         snapshots_reexported=sum(r.get("snapshots") or 0 for r in measured.values()),
                         bytes_compared=sum(r["stored_bytes"] for r in measured.values())),
        analyses=dict(volatile_lines=[key for _, key in REPORT_VOLATILE], modules=analyses),
        checks=checks,
        limits=["Equal bytes show the pipeline from cached Git objects to the files is deterministic for the sampled repositories only.",
                "Repositories above the size cap are not re-exported; the per-snapshot verification covers them.",
                "The blob cache and local clones are the inputs; their content is checked against Git hashes by the replay, not by this check.",
                "formal_stats is not rerun here: it would overwrite published reports, and it has its own two-round byte gate.",
                "The measurement timestamp line and the analyses' created_at_utc line are the only normalized differences."])
    write_json(directory / "validity_reexport.json", result)
    (root / "reports/validity_reexport.json").write_bytes((directory / "validity_reexport.json").read_bytes())
    print(json.dumps(dict(completed=True, checks=checks, directory=directory.relative_to(root).as_posix())), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=COUNT, help="evenly spaced size quantiles to re-export")
    parser.add_argument("--cap-mb", type=int, default=CAP_MB)
    parser.add_argument("--ids", nargs="*", help="re-export exactly these repository ids instead of the sample")
    parser.add_argument("--skip-measurement", action="store_true")
    parser.add_argument("--skip-analysis", action="store_true")
    arguments = parser.parse_args()
    run(Path(__file__).resolve().parents[1], arguments.count, arguments.cap_mb, arguments.ids, arguments.skip_measurement, arguments.skip_analysis)
