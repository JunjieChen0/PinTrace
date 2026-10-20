"""Sequential, source-bound statistics, figures and manuscript production."""

import argparse
from datetime import datetime, timezone
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import traceback

from .formal_common import (FROZEN_MANIFEST, WORKING_MANIFEST, cluster_bootstrap, make_context, sha256,
                            verify_frozen_inputs, write_json)


REPORTS = ("rq1_summary", "rq2_summary", "rq3_persistence_summary", "rq3_maintenance_results", "h1_h2_summary", "robustness_checks", "uncertainty_intervals")


def now():
    return datetime.now(timezone.utc).isoformat()


def publish(root, source, destination, frozen):
    target = (root / destination).resolve()
    if not target.is_relative_to(root) or target.relative_to(root).as_posix() in frozen["input_sha256"]:
        raise ValueError("Attempt to overwrite frozen input or leave the workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if sha256(source) != sha256(target):
        raise ValueError("Published artifact differs from the staged artifact")


WORKING_DIRECTORY = "formal-stats-v3"


def run(root, *, extract_only=False, cache_directory=None, working=False):
    """``working`` runs against reports/working_input_manifest.json into a new batch directory.

    The default keeps the original frozen authority and its formal-stats-v1 directory.
    """
    root = Path(root).resolve()
    manifest_path = WORKING_MANIFEST if working else FROZEN_MANIFEST
    directory = root / "data/derived" / (WORKING_DIRECTORY if working else "formal-stats-v1")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_path = directory / "runs" / stamp / "manifest.json"
    state = dict(rule_version="formal-stats-run-v1", started_at_utc=now(), pid=os.getpid(), completed=False,
                 extract_only=extract_only, manifest_path=manifest_path, stages=[], implementation_sha256=sha256(Path(__file__)))
    write_json(run_path, state)
    sources = {str(Path(__file__).relative_to(root).as_posix()): state["implementation_sha256"],
               "pintrace/formal_common.py": sha256(root / "pintrace/formal_common.py")}

    def stage(name, action):
        row = dict(name=name, started_at_utc=now(), completed=False)
        state["stages"].append(row)
        write_json(run_path, state)
        print("START", name, flush=True)
        value = action()
        row.update(completed=True, ended_at_utc=now())
        write_json(run_path, state)
        print("PASSED", name, flush=True)
        return value

    def module(name):
        imported = importlib.import_module("pintrace." + name)
        sources["pintrace/" + name + ".py"] = sha256(imported.__file__)
        return imported

    try:
        guard = stage("verify_all_frozen_inputs_before", lambda: verify_frozen_inputs(root, progress=print, manifest_path=manifest_path))
        context = make_context(root, guard)
        output = directory / ("extracted" if extract_only else "results")
        output.mkdir(parents=True, exist_ok=True)
        write_json(output / "frozen_input_preflight.json", guard)
        if not extract_only:
            cache = (root / cache_directory).resolve() if cache_directory is not None else directory / "extracted"
            receipt_path = cache / "rq12_record_cache_receipt.json"
            if cache_directory is not None and not receipt_path.is_file():
                raise ValueError("The explicitly requested extraction cache does not exist")
            if receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if sha256(receipt["path"]) != receipt["sha256"]:
                    raise ValueError("Compact extraction cache changed")
                with Path(receipt["path"]).open(encoding="utf-8") as stream:
                    context["rq12_records"] = [json.loads(line) for line in stream if line.strip()]
                context["rq12_record_cache_receipt"] = receipt
        stage("rq1_rq2", lambda: module("formal_rq12").run(root, output, context))
        if not extract_only:
            stage("rq3_h1_h2", lambda: module("formal_rq3").run(root, output, context))
            stage("robustness_matrix", lambda: module("formal_robustness").run(root, output, context))
            write_json(output / "estimand_registry.json", dict(populations=context["populations"], metrics=context["metrics"]))
            first = stage("bootstrap_round_one", lambda: cluster_bootstrap(context["metrics"], context["populations"]))
            write_json(output / "uncertainty_round_one.json", first)
            second = stage("bootstrap_round_two", lambda: cluster_bootstrap(context["metrics"], context["populations"]))
            write_json(output / "uncertainty_round_two.json", second)
            if (output / "uncertainty_round_one.json").read_bytes() != (output / "uncertainty_round_two.json").read_bytes():
                raise ValueError("Bootstrap rounds differ at the byte level")
            write_json(output / "uncertainty_intervals.json", first)
            write_json(output / "bootstrap_reproducibility.json", dict(passed=True, seed=first["seed"], replicates=first["replicates"],
                       metrics=len(first["metrics"]), round_one_sha256=sha256(output / "uncertainty_round_one.json"),
                       round_two_sha256=sha256(output / "uncertainty_round_two.json"), point_and_interval_bytes_identical=True))
            context["report_paths"] = {name: output / (name + ".json") for name in REPORTS}
            context["report_paths"]["freeze_manifest"] = root / manifest_path
            for name in REPORTS:
                publish(root, output / (name + ".json"), "reports/" + name + ".json", guard["manifest"])
            stage("figures_and_tables", lambda: module("formal_figures").run(root, output, context))
            context["report_paths"]["figure_manifest"] = output / "figure_manifest.json"
            stage("manuscript_and_claim_audit", lambda: module("formal_manuscript").run(root, output, context))
            for name in ("figure_manifest", "claim_evidence_map", "manuscript_audit", "source_catalog", "bootstrap_reproducibility"):
                publish(root, output / (name + ".json"), "reports/" + name + ".json", guard["manifest"])
            publish(root, output / "manuscript.md", "paper/manuscript.md", guard["manifest"])
            stage("formal_acceptance", lambda: module("formal_acceptance").run(root, output, context))
            publish(root, output / "formal_acceptance.json", "reports/formal_acceptance.json", guard["manifest"])
        final_guard = stage("verify_all_frozen_inputs_after", lambda: verify_frozen_inputs(root, progress=print, manifest_path=manifest_path))
        if final_guard["freeze_sha256"] != guard["freeze_sha256"]:
            raise ValueError("Input freeze authority changed during the batch")
        changed_sources = [name for name, expected in sources.items() if sha256(root / name) != expected]
        if changed_sources:
            raise ValueError("Analysis implementation changed during the batch: " + ", ".join(changed_sources))
        write_json(output / "frozen_input_final_check.json", final_guard)
        state.update(completed=True, ended_at_utc=now(), frozen_inputs_unchanged=True, freeze_sha256=guard["freeze_sha256"],
                     used_source_sha256=sources, output_directory=output.relative_to(root).as_posix(),
                     outputs={path.relative_to(root).as_posix(): sha256(path) for path in sorted(output.iterdir()) if path.is_file()},
                     runtime={name: importlib.metadata.version(name) for name in ("numpy", "scipy", "matplotlib", "pytest", "ijson")})
        write_json(run_path, state)
        print(json.dumps(dict(completed=True, extract_only=extract_only, manifest=run_path.relative_to(root).as_posix())), flush=True)
        return state
    except BaseException as error:
        state.update(ended_at_utc=now(), error=repr(error), traceback=traceback.format_exc(), used_source_sha256=sources)
        write_json(run_path, state)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract-only", action="store_true", help="Checkpoint the expensive source-bound extraction before producing the remaining artifacts")
    parser.add_argument("--cache-directory", type=Path, help="Use an explicitly verified compact extraction revision")
    parser.add_argument("--working", action="store_true", help="Use reports/working_input_manifest.json (freeze paused) and write formal-stats-v3")
    args = parser.parse_args()
    run(Path(__file__).resolve().parents[1], extract_only=args.extract_only, cache_directory=args.cache_directory, working=args.working)
