"""Read-only frozen inputs and shared estimands for the formal analysis batch."""

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import tomllib


SEED = 20261018
REPLICATES = 2000
FROZEN_MANIFEST = "reports/freeze_manifest.json"
WORKING_MANIFEST = "reports/working_input_manifest.json"


def manifest_file(root, source=None):
    """Path of the input authority a guard or context was verified against."""
    guard = (source or {}).get("guard", source) or {}
    return Path(root).resolve() / guard.get("manifest_path", FROZEN_MANIFEST)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def annotate(result):
    """Flag intervals that are degenerate or built from fewer replicates than requested; they never prove absence of risk."""
    if result["estimate"] is None:
        status = "undefined"
    elif result["lower"] is not None and result["lower"] == result["upper"]:
        status = "zero_degenerate" if result["estimate"] == 0 else "constant_degenerate"
    elif result["valid_replicates"] < result["requested_replicates"]:
        status = "partial_replicates"
    else:
        status = "estimated"
    return dict(result, status=status)


def bound_input(context, pattern):
    """The single input path of the active authority matching ``pattern`` (a regular expression)."""
    found = sorted(name for name in context["freeze_manifest"]["input_sha256"] if re.fullmatch(pattern, name))
    if len(found) != 1:
        raise ValueError(f"The input authority must bind exactly one path matching {pattern!r}; found {len(found)}")
    return found[0]


def bound_measurements(context):
    """Repository id -> bound measurement file; every file must sit in one measurement-vN directory."""
    files = {}
    for name in context["freeze_manifest"]["input_sha256"]:
        match = re.fullmatch(r"data/derived/(measurement-v\d+)/(\d+)\.json", name)
        if match:
            files[match.group(2)] = name
    if len({Path(name).parent for name in files.values()}) > 1:
        raise ValueError("The input authority binds measurements from more than one batch directory")
    return files


def closed_models(context):
    """Closed-analysis declarations of the active authority.

    The frozen manifest holds them at top level; a working-input manifest records the same declarations
    under ``carried_from_original_freeze`` because it inherits them unchanged.
    """
    manifest = context["freeze_manifest"]
    if "closed_models" in manifest:
        return manifest["closed_models"]
    return manifest["carried_from_original_freeze"]["closed_models"]


def expected_counts(context):
    """Population and born-fixed sizes declared by the active authority (None when it does not declare them)."""
    manifest = context["freeze_manifest"]
    born = manifest.get("formal_episode_kind_counts", {}).get("post_anchor_born_fixed", {}).get("episodes")
    return dict(raw=manifest.get("raw_repository_count"), formal=manifest.get("formal_repository_count"), born_fixed=born)


def verify_frozen_inputs(root, *, progress=None, manifest_path=FROZEN_MANIFEST):
    """Rehash every input bound by one authority.

    The default authority is the immutable freeze manifest and must say ``frozen``.
    A working-input manifest (freeze paused) must say ``working_inputs``; neither
    status is accepted at the other's path, so a working batch can never pass for
    a frozen one.
    """
    root = Path(root).resolve()
    path = root / manifest_path
    digest = sha256(path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_status = "frozen" if manifest_path == FROZEN_MANIFEST else "working_inputs"
    if manifest.get("status") != expected_status:
        raise ValueError("Formal analysis requires frozen inputs" if expected_status == "frozen"
                         else "A working-input manifest must declare status working_inputs")
    total_bytes = 0
    for index, (name, expected) in enumerate(manifest["input_sha256"].items(), 1):
        source = (root / name).resolve()
        if not source.is_file() or sha256(source) != expected:
            raise ValueError("Frozen input missing or changed: " + name)
        total_bytes += source.stat().st_size
        if progress is not None and index % 100 == 0:
            progress(f"Frozen inputs verified: {index}/{len(manifest['input_sha256'])}")
    if sha256(path) != digest:
        raise ValueError("Freeze manifest changed during input verification")
    return dict(root=str(root), freeze_sha256=digest, manifest=manifest, manifest_path=manifest_path,
                checked_at_utc=datetime.now(timezone.utc).isoformat(),
                files=len(manifest["input_sha256"]), bytes=total_bytes, verified=True)


def make_context(root, guard):
    root = Path(root).resolve()
    if not guard.get("verified") or guard["root"] != str(root):
        raise ValueError("A successful full frozen-input verification is required")
    manifest = guard["manifest"]
    population = manifest["formal_population"]
    raw = {str(value) for value in population["raw_confirmed_repository_ids"]}
    formal = {str(value) for value in population["formal_eligible_repository_ids"]}
    return dict(root=root, guard=guard, freeze_manifest=manifest, raw_ids=raw, formal_ids=formal,
                populations={"formal": sorted(formal), "raw": sorted(raw)}, metrics=[],
                study=tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"])


def require_context(root, context, inputs=()):
    """Check the shared full-pass receipt and freshly hash each module's inputs."""
    root = Path(root).resolve()
    guard = context.get("guard", {})
    if not guard.get("verified") or guard.get("root") != str(root):
        raise ValueError("Missing full frozen-input guard")
    if sha256(manifest_file(root, guard)) != guard["freeze_sha256"]:
        raise ValueError("Freeze authority changed")
    expected = guard["manifest"]["input_sha256"]
    for name in inputs:
        if name not in expected or sha256(root / name) != expected[name]:
            raise ValueError("Module input is not the frozen input: " + name)


def _append(context, metric):
    if any(row["key"] == metric["key"] for row in context["metrics"]):
        raise ValueError("Duplicate estimand key: " + metric["key"])
    population = set(context["populations"][metric["population"]])
    if not set(metric["clusters"]) <= population:
        raise ValueError("Estimand includes an ineligible repository")
    context["metrics"].append(metric)
    return metric["estimate"]


def add_ratio(context, key, rows, population="formal", binary=False, unit="repository"):
    clusters = defaultdict(lambda: [0.0, 0.0])
    for identity, numerator, denominator in rows:
        if numerator is None or denominator is None:
            continue
        if denominator < 0 or numerator < 0 or numerator > denominator:
            raise ValueError("Invalid numerator/denominator for " + key)
        if denominator == 0:
            continue
        target = clusters[str(identity)]
        target[0] += numerator
        target[1] += denominator
    total = sum(value[1] for value in clusters.values())
    point = sum(value[0] for value in clusters.values()) / total if total else None
    if binary and any(d != 1 or n not in (0, 1) for n, d in clusters.values()):
        raise ValueError("Exact binomial intervals require one complete binary outcome per repository")
    return _append(context, dict(key=key, kind="ratio", population=population, unit=unit,
                                 binary=binary, clusters=dict(clusters), estimate=point))


def add_quantile(context, key, rows, q=0.5, population="formal", unit="episode"):
    import numpy as np
    clusters = defaultdict(list)
    for identity, value in rows:
        if value is not None:
            if not np.isfinite(value):
                raise ValueError("Nonfinite observation for " + key)
            clusters[str(identity)].append(float(value))
    values = [value for rows in clusters.values() for value in rows]
    point = float(np.quantile(values, q, method="linear")) if values else None
    return _append(context, dict(key=key, kind="quantile", population=population, unit=unit,
                                 q=q, clusters=dict(clusters), estimate=point))


def cluster_bootstrap(metrics, populations, *, seed=SEED, replicates=REPLICATES):
    """Use one draw matrix per population for every subgroup, lineage and episode."""
    import numpy as np
    from scipy.stats import beta

    matrices = {}
    for name, ids in sorted(populations.items()):
        ids = sorted(str(identity) for identity in ids)
        rng = np.random.default_rng(seed)
        matrices[name] = (ids, rng.multinomial(len(ids), np.full(len(ids), 1 / len(ids)), size=replicates))
    output = {}
    for metric in sorted(metrics, key=lambda row: row["key"]):
        ids, matrix = matrices[metric["population"]]
        if metric["kind"] == "ratio":
            values = np.array([metric["clusters"].get(identity, [0, 0]) for identity in ids], dtype=float)
            numerator, denominator = matrix @ values[:, 0], matrix @ values[:, 1]
            samples = numerator[denominator > 0] / denominator[denominator > 0]
        else:
            groups = [(index, metric["clusters"][identity]) for index, identity in enumerate(ids) if identity in metric["clusters"]]
            samples = []
            for weights in matrix:
                values = [value for index, rows in groups for value in rows for _ in range(int(weights[index]))]
                if values:
                    samples.append(float(np.quantile(values, metric["q"], method="linear")))
            samples = np.asarray(samples)
        size = len(metric["clusters"])
        bounds = [float(value) for value in np.quantile(samples, [0.025, 0.975], method="linear")] if len(samples) else [None, None]
        status = "no_observations" if not size else "sparse" if size < 10 else "estimated"
        if len(samples) and np.all(samples == samples[0]):
            status = "zero_degenerate" if samples[0] == 0 else "constant_degenerate"
        result = dict(estimate=metric["estimate"], lower=bounds[0], upper=bounds[1],
                      valid_replicates=len(samples), requested_replicates=replicates,
                      n_clusters=size, population_clusters=len(ids), population=metric["population"],
                      kind=metric["kind"], unit=metric["unit"], status=status,
                      interpretation="Descriptive cluster resampling; degenerate intervals do not establish absence of risk.")
        if metric.get("binary") and size:
            successes = int(sum(row[0] for row in metric["clusters"].values()))
            result["exact_binomial"] = dict(successes=successes, denominator=size,
                lower=0.0 if successes == 0 else float(beta.ppf(0.025, successes, size - successes + 1)),
                upper=1.0 if successes == size else float(beta.ppf(0.975, successes + 1, size - successes)))
        output[metric["key"]] = result
    return dict(rule_version="repository-cluster-bootstrap-v1", seed=seed, replicates=replicates,
                confidence_level=0.95, interval="percentile", metrics=output,
                resampling="One whole-repository draw shared by all categories and observations in each population")
