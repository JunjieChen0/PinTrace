"""Render publication artifacts directly from the frozen formal-statistics JSON."""

import csv
import json
from pathlib import Path

from .formal_common import require_context, sha256, write_json


def resolve_pointer(value, pointer):
    for token in pointer.strip("/").split("/") if pointer else []:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def escape(value):
    return str(value).replace("~", "~0").replace("/", "~1")


class Artifacts:
    def __init__(self, root, reports):
        self.root = Path(root)
        self.reports = {key: json.loads(Path(path).read_text(encoding="utf-8")) for key, path in reports.items()}
        self.artifacts = []

    def cell(self, report, pointer):
        return dict(report=report, pointer=pointer, value=resolve_pointer(self.reports[report], pointer))

    def table(self, name, title, headers, rows):
        directory = self.root / "paper/tables"
        directory.mkdir(parents=True, exist_ok=True)
        data_path = directory / (name + ".csv")
        bindings = []
        plain = []
        for index, row in enumerate(rows):
            values = []
            for column, cell in enumerate(row):
                if isinstance(cell, dict) and "pointer" in cell:
                    bindings.append(dict(row=index, column=headers[column], **cell))
                    values.append(cell["value"])
                else:
                    values.append(cell)
            plain.append(values)
        with data_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows(plain)
        grouped = {}
        for binding in bindings:
            parts = binding["pointer"].strip("/").split("/")
            common = grouped.setdefault(binding["report"], parts)
            length = 0
            while length < min(len(common), len(parts)) and common[length] == parts[length]:
                length += 1
            grouped[binding["report"]] = common[:length]
        sources = [report + "#" + ("/" + "/".join(parts) if parts else "") for report, parts in sorted(grouped.items())]
        caption = title + ". Sources: " + "; ".join(sources)
        markdown = directory / (name + ".md")
        lines = [title, "", "| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
        for row in plain:
            lines.append("| " + " | ".join("NA" if value is None else str(value).replace("|", "\\|") for value in row) + " |")
        lines += ["", "Sources: " + "; ".join(f"`{source}`" for source in sources), ""]
        markdown.write_text("\n".join(lines), encoding="utf-8")
        shared = dict(caption=caption, source_bindings=[{k: value[k] for k in ("report", "pointer")} for value in bindings],
                      data_path=data_path.relative_to(self.root).as_posix(), data_sha256=sha256(data_path),
                      cell_bindings=bindings, headers=headers, rows=len(rows))
        for path, kind in ((data_path, "csv_table"), (markdown, "markdown_table")):
            self.artifacts.append(dict(path=path.relative_to(self.root).as_posix(), sha256=sha256(path), kind=kind, **shared))
        return plain, shared

    def figure(self, name, figure, source):
        import matplotlib.pyplot as plt
        directory = self.root / "paper/figures"
        directory.mkdir(parents=True, exist_ok=True)
        figure.text(.01, .005, "Data: " + source["data_path"] + "; cell-level JSON provenance in figure_manifest.json", fontsize=6)
        figure.tight_layout(rect=(0, .025, 1, 1))
        for suffix in ("svg", "png"):
            path = directory / (name + "." + suffix)
            kwargs = dict(dpi=300, bbox_inches="tight")
            if suffix == "svg":
                kwargs["metadata"] = {"Date": None, "Description": source["caption"]}
            figure.savefig(path, **kwargs)
            self.artifacts.append(dict(path=path.relative_to(self.root).as_posix(), sha256=sha256(path), kind="figure_" + suffix, **source))
        plt.close(figure)


def run(root, output_dir, context):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    require_context(root, context)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "svg.hashsalt": "pintrace-formal-v1",
                         "axes.spines.top": False, "axes.spines.right": False})
    builder = Artifacts(root, context["report_paths"])
    builder.reports["figure_manifest"] = {"method_constants": {"horizons": [7, 30, 90, 180, 365]}}
    c, reports = builder.cell, builder.reports
    rows = [["Raw repositories", c("freeze_manifest", "/raw_repository_count")],
            ["Formal repositories", c("freeze_manifest", "/formal_repository_count")],
            ["Identity-eligible fixed periods", c("rq3_persistence_summary", "/populations/formal/identity/episodes")],
            ["Duration-eligible fixed periods", c("rq3_persistence_summary", "/populations/formal/duration/episodes")]]
    data, source = builder.table("sample_flow", "Sample units and eligibility", ["unit", "count"], rows)
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.5))
    for ax, subset in zip(axes, (data[:2], data[2:])):
        ax.barh([row[0] for row in subset], [row[1] for row in subset], color=["#4477aa", "#66ccee"])
        ax.set_xlabel("Distinct repositories" if ax is axes[0] else "Reference fixed periods")
        ax.invert_yaxis()
        for index, (_, value) in enumerate(subset):
            ax.text(value, index, " " + str(value), va="center")
    builder.figure("sample_flow", fig, source)

    rq1 = reports["rq1_summary"]["formal"]
    rows = [[key, c("rq1_summary", "/formal/first_response_counts/" + escape(key))] for key in rq1["first_response_counts"]]
    data, source = builder.table("rq1_first_response", "First observed target response; simultaneous changes stay combined", ["state", "repositories"], rows)
    fig, ax = plt.subplots(figsize=(10, max(4, len(rows) * .26)))
    ax.barh([row[0] for row in data], [row[1] for row in data], color="#4477aa")
    ax.invert_yaxis(); ax.set_xlabel("Repositories")
    builder.figure("rq1_first_response", fig, source)

    rows = []
    for index, day in enumerate(("7", "30", "90", "180", "365")):
        base = "/formal/horizons/" + day
        metric = rq1["metric_prefix"] + ".horizon." + day + ".lower"
        rows.append([c("rq1_summary", base + "/denominator"), c("figure_manifest", f"/method_constants/horizons/{index}"),
                     c("rq1_summary", base + "/observed_response_lower"),
                     c("rq1_summary", base + "/response_upper_bound"),
                     c("rq1_summary", base + "/at_risk_just_before"),
                     c("uncertainty_intervals", "/metrics/" + escape(metric) + "/lower"),
                     c("uncertainty_intervals", "/metrics/" + escape(metric) + "/upper")])
    data, source = builder.table("rq1_response_trajectory", "Observed response, cluster intervals and missing-outcome bounds on the fixed cohort", ["denominator", "days", "observed_lower", "response_upper", "at_risk_before", "bootstrap_lower", "bootstrap_upper"], rows)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot([r[1] for r in data], [r[2] for r in data], "o-", label="Observed first response / initial cohort")
    ax.fill_between([r[1] for r in data], [r[2] for r in data], [r[3] for r in data], alpha=.18, label="Unknown/censored outcome bounds")
    ax.fill_between([r[1] for r in data], [r[5] for r in data], [r[6] for r in data], alpha=.3, label="Repository-cluster percentile interval")
    ax.set(xlabel="Days since event-date boundary", ylabel="Cohort fraction", ylim=(0, 1)); ax.legend(fontsize=8)
    builder.figure("rq1_response_trajectory", fig, source)

    rq2 = reports["rq2_summary"]["formal"]
    rows = []
    for index, day in enumerate(("7", "30", "90", "180", "365")):
        base = "/formal/existing/horizons/" + day
        metric = "rq2.formal.main.existing.horizon." + day + ".pin.lower"
        rows.append([c("figure_manifest", f"/method_constants/horizons/{index}"), c("rq2_summary", base + "/observed_pin_cumulative"),
                     c("rq2_summary", base + "/upper_bound"),
                     c("rq2_summary", base + "/repository_equal_observed_pin"),
                     c("uncertainty_intervals", "/metrics/" + escape(metric) + "/lower"),
                     c("uncertainty_intervals", "/metrics/" + escape(metric) + "/upper")])
    data, source = builder.table("rq2_existing_extension", "First pinning of baseline non-SHA dependencies; target Action excluded", ["days", "reference_weighted_lower", "upper_bound", "repository_equal_lower", "bootstrap_lower", "bootstrap_upper"], rows)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot([r[0] for r in data], [r[1] for r in data], "o-", label="Reference weighted")
    ax.plot([r[0] for r in data], [r[3] for r in data], "s--", label="Repository equal")
    ax.fill_between([r[0] for r in data], [r[1] for r in data], [r[2] for r in data], alpha=.15, label="Reference outcome bounds")
    ax.fill_between([r[0] for r in data], [r[4] for r in data], [r[5] for r in data], alpha=.25, label="Repository-cluster interval")
    ax.set(xlabel="Days since event-date boundary", ylabel="Observed pinning fraction", ylim=(0, 1)); ax.legend(fontsize=8)
    builder.figure("rq2_existing_extension", fig, source)
    rows = [[key, c("rq2_summary", "/formal/new_actions/configuration_counts/" + key),
             c("rq2_summary", "/formal/new_actions/repository_equal/" + key)]
            for key in ("all_full_sha", "mixed", "all_non_sha", "unknown")]
    data, source = builder.table("rq2_new_action_configuration", "First observable Action identities; no-new-dependency repositories excluded", ["configuration", "identity_groups", "repository_equal_share"], rows)
    fig, axes = plt.subplots(1, 2, figsize=(10, 4))
    axes[0].bar([r[0] for r in data], [r[1] for r in data], color="#4477aa"); axes[0].set_ylabel("Identity groups")
    axes[1].bar([r[0] for r in data], [r[2] for r in data], color="#228833"); axes[1].set(ylabel="Repository-equal share", ylim=(0, 1))
    for ax in axes: ax.tick_params(axis="x", rotation=25)
    builder.figure("rq2_new_action_configuration", fig, source)

    persistence = reports["rq3_persistence_summary"]["populations"]["formal"]
    rows = []
    for i, row in enumerate(persistence["trajectories"]):
        cells = [c("rq3_persistence_summary", f"/populations/formal/trajectories/{i}/{key}") for key in ("kind", "days", "exit_kind", "observed_fraction", "at_risk_before")]
        cells += [c("uncertainty_intervals", "/metrics/" + escape(row["metric_key"]) + "/" + bound) if row["metric_key"] else None for bound in ("lower", "upper")]
        rows.append(cells)
    data, source = builder.table("rq3_exit_trajectories", "Observed first exits and unknown interruptions; duration-eligible periods", ["kind", "days", "exit_kind", "fraction", "at_risk_before", "bootstrap_lower", "bootstrap_upper"], rows)
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), sharey=True)
    for ax, kind in zip(axes, persistence["by_kind"]):
        for exit_kind, style in (("unpin", "-"), ("remove", "-"), ("replace", "-"), ("unknown_boundary", "--"), ("ambiguous_transition", ":")):
            selected = sorted([r for r in data if r[0] == kind and r[2] == exit_kind], key=lambda r: r[1])
            line, = ax.step([r[1] for r in selected], [r[3] for r in selected], where="post", linestyle=style, label=exit_kind)
            bounded = [row for row in selected if row[5] is not None and row[6] is not None]
            ax.fill_between([r[1] for r in bounded], [r[5] for r in bounded], [r[6] for r in bounded], step="post", color=line.get_color(), alpha=.07)
        ax.set(title=kind.replace("post_anchor_", ""), xlabel="Observed days after entry", ylim=(0, 1)); ax.legend(fontsize=6)
    axes[0].set_ylabel("Observed events / initial fixed periods")
    builder.figure("rq3_exit_trajectories", fig, source)
    rows = [[c("rq3_persistence_summary", f"/populations/formal/retention/{i}/{key}") for key in ("kind", "horizon_days", "retained", "known_exit", "unknown_interruption", "censored_early", "determinate", "estimate")]
            for i, row in enumerate(persistence["retention"])]
    builder.table("rq3_retention", "Fixed-period retention only where the horizon is decidable", ["kind", "days", "retained", "known_exit", "unknown", "early_censor", "determinate", "retained_among_determinate"], rows)

    maintenance = reports["rq3_maintenance_results"]["populations"]["formal"]
    rows, delay_rows = [], []
    for role in ("target", "comparison"):
        for grade in ("A", "B"):
            base = f"/populations/formal/{role}/by_grade/{grade}"
            counts = maintenance[role]["by_grade"][grade]["counts"]
            for outcome in counts["outcome_counts"]:
                rows.append([role, grade, outcome, c("rq3_maintenance_results", base + "/counts/outcome_counts/" + escape(outcome))])
            if role == "target":
                for i, record in enumerate(maintenance[role]["by_grade"][grade]["delay_records"]):
                    delay_rows.append([grade, *[c("rq3_maintenance_results", base + f"/delay_records/{i}/" + field) for field in ("repository_id", "lower_days", "upper_days", "proxy_days")]])
    builder.table("maintenance_outcomes", "Normal-release maintenance outcomes by role and evidence grade", ["role", "grade", "outcome", "entries"], rows)
    data, source = builder.table("maintenance_delay_intervals", "Conditional adoption delay: A points and B [T-U,T-L) intervals", ["grade", "repository", "lower_days", "upper_days", "proxy_days"], delay_rows)
    fig, axes = plt.subplots(1, 2, figsize=(11, 6))
    for ax, grade in zip(axes, ("A", "B")):
        selected = sorted([r for r in data if r[0] == grade], key=lambda r: (r[2], r[3]))
        for index, row in enumerate(selected):
            ax.plot([row[2], row[3]], [index, index], color="#4477aa", alpha=.6)
            ax.scatter([row[2]], [index], s=8, color="#4477aa")
            if grade == "B": ax.scatter([row[3]], [index], s=10, facecolors="white", edgecolors="#4477aa")
        ax.set(title="Grade " + grade, xlabel="Days after true availability (interval for B)", ylabel="Adopted entries, ordered by delay")
    builder.figure("maintenance_delay_intervals", fig, source)

    h1h2 = reports["h1_h2_summary"]
    rows = []
    for field, cells in h1h2["episode_entry_conditions"]["identity"].items():
        for index, cell in enumerate(cells):
            rows.append([field, c("h1_h2_summary", f"/episode_entry_conditions/identity/{field}/{index}/category"),
                         c("h1_h2_summary", f"/episode_entry_conditions/identity/{field}/{index}/episodes"),
                         c("h1_h2_summary", f"/episode_entry_conditions/identity/{field}/{index}/known_event_episodes")])
    data, source = builder.table("h1_h2_entry_conditions", "Visible entry configuration and inline comments; descriptive associations", ["field", "category", "fixed_periods", "known_exit_periods"], rows)
    fig, axes = plt.subplots(1, 3, figsize=(13, 5))
    for ax, field in zip(axes, h1h2["episode_entry_conditions"]["identity"]):
        selected = [r for r in data if r[0] == field]
        ax.barh([r[1] for r in selected], [r[2] for r in selected], color="#66ccee")
        ax.set_title(field.replace("entry_", ""), fontsize=8); ax.set_xlabel("Identity-eligible fixed periods")
    builder.figure("h1_h2_entry_conditions", fig, source)
    rows = [[c("h1_h2_summary", f"/tool_replays/{i}/{key}") for key in ("tool", "version", "case_id", "covered", "status")]
            for i, row in enumerate(h1h2["tool_replays"])]
    builder.table("historical_tool_replays", "Controlled official-module behavior; version and routing remain explicit", ["tool", "version", "case", "covered", "observed_module_status"], rows)
    rows = [[key, c("robustness_checks", "/scenarios/" + key + "/repositories"),
             c("robustness_checks", "/scenarios/" + key + "/rq2/existing/baseline_references"),
             c("robustness_checks", "/scenarios/" + key + "/rq3_persistence/duration/episodes")]
            for key in reports["robustness_checks"]["scenarios"]]
    builder.table("robustness_matrix", "Population, boundary and calendar-stratum sensitivity matrix", ["scenario", "repositories", "baseline_mutable_references", "duration_eligible_periods"], rows)
    manifest = dict(rule_version="formal-figures-v1", generated_from_statistics=True, artifacts=builder.artifacts,
                    method_constants=builder.reports["figure_manifest"]["method_constants"],
                    input_sha256={str(Path(path)): sha256(path) for path in context["report_paths"].values()},
                    freeze_sha256=context["guard"]["freeze_sha256"],
                    limits=["A/B adoption-delay plots condition on observable applicable entries; B upper endpoints are open.",
                            "Observed cumulative fractions do not estimate censor-adjusted population incidence."])
    write_json(Path(output_dir) / "figure_manifest.json", manifest)
    return manifest
