"""Render an evidence-linked manuscript and audit its numbers and artifacts."""
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re

from .formal_common import closed_models, manifest_file, require_context, sha256, write_json

RULES_VERSION = "formal-manuscript-v1"
IMPORTED_SOURCE_SHA256 = sha256(Path(__file__))
REPORTS = ("rq1_summary", "rq2_summary", "rq3_persistence_summary", "rq3_maintenance_results",
           "h1_h2_summary", "robustness_checks", "uncertainty_intervals", "figure_manifest")
MARKER = re.compile(r"<!--claim:(c\d+)-->(.*?)<!--/claim:\1-->", re.S)
NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?")

# The catalogue records primary sources actually read, not presumed paper contents.
SOURCES = {
    "incident": dict(title="tj-actions changed-files advisory", authors="GitHub Advisory Database", year=2025,
        url="https://github.com/advisories/GHSA-mrrh-fwg8-r2c3", date="2025-03-14",
        supports="The incident involved malicious changed-files code and potential secret exposure through workflow logs; dependency is not proof of exploitation."),
    "policy": dict(title="GitHub Actions policy now supports blocking and SHA pinning actions", authors="GitHub", year=2025,
        url="https://github.blog/changelog/2025-08-15-github-actions-policy-now-supports-blocking-and-sha-pinning-actions/", date="2025-08-15",
        supports="Administrators gained an allowed-actions policy option enforcing full commit identifiers; the announcement does not establish adoption in our repositories."),
    "immutable": dict(title="Immutable releases are now generally available", authors="GitHub", year=2025,
        url="https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/", date="2025-10-28",
        supports="New immutable releases protect assets and tags; existing mutable releases are not retrospectively immutable without republication."),
    "action_required": dict(title="Action Required: A Mixed-Methods Study of Security Practices in GitHub Actions",
        authors="Yusuke Kubo, Fumihiro Kanei, Mitsuaki Akiyama, Takuro Wakai, and Tatsuya Mori", year=2026, venue="NDSS",
        url="https://www.ndss-symposium.org/wp-content/uploads/2026-f483-paper.pdf",
        supports="Repository measurement and a developer survey examine security-practice implementation and reported awareness, applicability, and operational-cost barriers."),
    "evolution": dict(title="An Empirical Study of the Evolution of GitHub Actions Workflows",
        authors="Pooya Rostami Mazrae, Alexandre Decan, Tom Mens, and Mairieli Wessel", year=2026, venue="Journal of Systems and Software",
        url="https://decan.lexpage.net/files/JSS-2026.pdf",
        supports="Qualitative workflow-change categories and large-scale workflow histories characterize maintenance; the source identifies the shared workflow-history dataset."),
    "outdatedness": dict(title="On the Outdatedness of Workflows in the GitHub Actions Ecosystem",
        authors="Alexandre Decan, Tom Mens, and Hassan Onsori Delicheh", year=2023, venue="Journal of Systems and Software",
        url="https://decan.lexpage.net/files/JSS-2023.pdf",
        supports="Action reuse, versioning, technical lag, and opportunity lag are measured; outdatedness and the availability of update opportunities are distinct concepts."),
    "tags": dict(title='Mutating the "Immutable": A Large-Scale Study of Git Tag Alterations',
        authors="Solal Rapaport, Laurent Pautet, Samuel Tardieu, Stefano Zacchiroli, and Théo Zimmermann", year=2026,
        venue="ACM Conference on Reproducibility and Replicability; author preprint",
        url="https://arxiv.org/abs/2606.31354",
        supports="Software Heritage tag histories and a Nixpkgs cross-analysis document tag alterations and reproducibility consequences; tag mutation is not itself an attack classification."),
    "maintenance": dict(title="The Hidden Costs of Automation: An Empirical Study on GitHub Actions Workflow Maintenance",
        authors="Pablo Valenzuela-Toledo, Alexandre Bergel, Timo Kehrer, and Oscar Nierstrasz", year=2024, venue="SCAM; author preprint",
        url="https://arxiv.org/abs/2409.02366",
        supports="Workflow evolution across mature projects identifies maintenance drivers including defect correction and pipeline improvement."),
    "marketplace": dict(title="Time for Actions: A Longitudinal Study of the GitHub Actions Marketplace",
        authors="Narong Chaiwut and Nick Nikiforakis", year=2025, venue="IEEE SecDev; author paper",
        url="https://www.securitee.org/files/timeforactions_secdev2025.pdf",
        supports="Longitudinal marketplace collection characterizes action growth and supply-chain risks, including typosquatting and dangling remote references."),
    "dependabot": dict(title="Automating Dependency Updates in Practice: An Exploratory Study on GitHub Dependabot",
        authors="Runzhi He, Hao He, Yuxia Zhang, and Minghui Zhou", year=2023, venue="IEEE Transactions on Software Engineering; author version",
        url="https://arxiv.org/abs/2206.07230",
        supports="Exploratory repository analysis and a developer survey report both technical-lag benefits and limitations involving configuration, compatibility information, and notifications."),
    "sbom": dict(title="An Empirical Study of SBOM Usage Through GitHub Actions", authors="Yo Kanemoto, Reika Nishimura Arakawa, and Mitsuaki Akiyama", year=2026, venue="IEEE Access",
        url="https://ieeexplore.ieee.org/abstract/document/11543245", verification_scope="official publisher abstract and local publisher-PDF title/abstract; full-method replication not performed",
        supports="The publisher abstract describes workflow-stage classification and longitudinal regression, oscillation, and abandonment of SBOM features."),
    "dependabot_source": dict(title="Historical Dependabot Core source revision", authors="Dependabot maintainers", year=2025,
        url="https://github.com/dependabot/dependabot-core/commit/18e9c1274ede47c78ed532eee072a1bd105167bb",
        supports="Official repository identity for the fixed source revision; executed archive bytes and commands are bound by the local tool audit."),
    "renovate_source": dict(title="Historical Renovate source revision", authors="Renovate maintainers", year=2025,
        url="https://github.com/renovatebot/renovate/commit/476afca3d44459e4614976934834ee1664dbbee5",
        supports="Official repository identity for the fixed source revision; package integrity and executed source bytes are checked separately."),
}


def pointer(document, path):
    value = document
    if path == "":
        return value
    if not path.startswith("/"):
        raise ValueError("JSON Pointer must start with '/'")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def escape(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def display(value, formatter="value", path="", report=""):
    if formatter.startswith("candidate_"):
        result = value[0]
        if "updates" in result:
            update = result["updates"][0]
            field = {"candidate_version": "newValue", "candidate_digest": "newDigest", "candidate_type": "updateType"}[formatter]
            return str(update.get(field, "not reported"))
        if formatter == "candidate_digest":
            return str(result["updated_requirements"][0]["source"]["ref"])
        if formatter == "candidate_version":
            latest = result.get("latest_version")
            return str(latest) if latest is not None and not re.fullmatch(r"[0-9a-f]{40}", str(latest)) else "not reported"
        return "not reported"
    if formatter == "caption_title":
        return str(value).split(". Sources:", 1)[0] + "."
    if formatter == "percent":
        return "not estimable" if value is None else format(value * 100, ".4g") + "%"
    if formatter == "source_report":
        return report
    if formatter == "length":
        return str(len(value))
    if formatter == "pointer":
        return path
    if formatter == "path":
        return path.replace("~1", "/").replace("~0", "~").replace("_", " ")
    if formatter == "key":
        return path.rsplit("/", 1)[-1].replace("~1", "/").replace("~0", "~").replace("_", " ")
    if formatter == "date":
        return value[:10]
    if value is None:
        return "not estimable"
    if isinstance(value, float):
        return format(value, ".6g")
    if isinstance(value, (dict, list, bool)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return str(value).replace("|", "\\|").replace("\n", " ")


class Writer:
    def __init__(self, sources):
        self.sources, self.claims = sources, []

    def value(self, report, path, formatter="value", kind="statistic"):
        source = self.sources[report]
        value = pointer(source["data"], path)
        rendered = display(value, formatter, path, report)
        identity = "c" + str(len(self.claims) + 1).zfill(6)
        self.claims.append(dict(claim_id=identity, report=report, source_path=str(source["path"]), source_sha256=source["sha256"],
            json_pointer=path, value=value, formatter=formatter, rendered=rendered, kind=kind))
        return "<!--claim:" + identity + "-->" + rendered + "<!--/claim:" + identity + "-->"

    def table(self, report, path, columns=None):
        data = pointer(self.sources[report]["data"], path)
        if columns is not None:
            lines = ["| " + " | ".join(column.replace("_", " ") for column in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
            for index, row in enumerate(data):
                lines.append("| " + " | ".join(self.value(report, path + "/" + str(index) + "/" + escape(column)) for column in columns) + " |")
        else:
            lines = ["| Measured field | Value |", "| --- | --- |"]

            def walk(value, current):
                if isinstance(value, dict):
                    for key, item in value.items():
                        walk(item, current + "/" + escape(key))
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        walk(item, current + "/" + str(index))
                else:
                    label = self.value(report, current, "path", "dimension")
                    lines.append("| " + label + " | " + self.value(report, current) + " |")
            walk(data, path)
        return "\n".join(lines) + "\n\nData source: " + self.value(report, path, "source_report", "source_locator") + "; JSON Pointer `" + self.value(report, path, "pointer", "source_locator") + "`.\n"

    def matrix(self, headers, rows):
        return "\n".join(["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
                          *("| " + " | ".join(row) + " |" for row in rows)]) + "\n"

    def metric(self, report, path, metric_key_path=None, key=None):
        node = pointer(self.sources[report]["data"], path)
        point_path = path + "/estimate" if isinstance(node, dict) and metric_key_path is None else path
        metric_key = key or (node["metric_key"] if metric_key_path is None else pointer(self.sources[report]["data"], metric_key_path))
        base = "/metrics/" + escape(metric_key)
        if pointer(self.sources[report]["data"], point_path) != pointer(self.sources["uncertainty_intervals"]["data"], base + "/estimate"):
            raise ValueError("Uncertainty point estimate differs: " + metric_key)
        bounds = [pointer(self.sources["uncertainty_intervals"]["data"], base + "/" + field) for field in ("lower", "upper")]
        if all(bound is not None for bound in bounds) and bounds[0] > bounds[1]:
            raise ValueError("Reversed uncertainty interval: " + metric_key)
        return (self.value(report, point_path) + " [" + self.value("uncertainty_intervals", base + "/lower") + ", "
            + self.value("uncertainty_intervals", base + "/upper") + "]; clusters=" + self.value("uncertainty_intervals", base + "/n_clusters")
            + "; " + self.value("uncertainty_intervals", base + "/status"))


def selected_results(w):
    """Compact result tables; detailed arrays remain in the statistical artifacts."""
    v, metric, matrix = w.value, w.metric, w.matrix
    docs = {name: source["data"] for name, source in w.sources.items()}
    result = {}
    prefix1, prefix2 = docs["rq1_summary"]["formal"]["metric_prefix"], docs["rq2_summary"]["formal"]["metric_prefix"]
    rows = []
    for category in docs["rq1_summary"]["formal"]["first_response_counts"]:
        path = "/formal/first_response_counts/" + escape(category)
        rows.append([v("rq1_summary", path, "key", "dimension"), v("rq1_summary", path), metric("rq1_summary", "/formal/first_response_proportions/" + escape(category), key=prefix1 + ".first_response." + category)])
    result["response"] = matrix(["First-response state", "Repositories", "Fraction [cluster interval]"], rows)
    leading = max(docs["rq1_summary"]["formal"]["first_response_counts"], key=lambda x: (docs["rq1_summary"]["formal"]["first_response_counts"][x], x))
    lp = "/formal/first_response_counts/" + escape(leading)
    result["response"] += "\nThe largest first-response category was " + v("rq1_summary", lp, "key", "dimension") + ", containing " + v("rq1_summary", lp) + " repositories. This ranks observed states; it does not assign incident motivation to their changes.\n"
    common = sorted(docs["rq1_summary"]["formal"]["full_window_combination_counts"], key=lambda x: (-docs["rq1_summary"]["formal"]["full_window_combination_counts"][x], x))[:8]
    result["combinations"] = matrix(["Most common full-window combination", "Repositories"], [[v("rq1_summary", "/formal/full_window_combination_counts/" + escape(key), "key", "dimension"), v("rq1_summary", "/formal/full_window_combination_counts/" + escape(key))] for key in common])
    rows = []
    for day in sorted(docs["rq1_summary"]["formal"]["horizons"], key=int):
        base = "/formal/horizons/" + day
        rows.append([v("rq1_summary", base, "key", "dimension"), v("rq1_summary", base + "/denominator"), v("rq1_summary", base + "/at_risk_just_before"),
            metric("rq1_summary", base + "/observed_response_lower", key=prefix1 + ".horizon." + day + ".lower"), v("rq1_summary", base + "/response_upper_bound")])
    result["response_horizons"] = matrix(["Days", "Initial repositories", "At risk before horizon", "Observed lower fraction [cluster interval]", "Upper outcome bound"], rows)
    result["response_horizons"] += "\nAt the shorter exploratory horizon, the complete-case denominator is " + v("rq1_summary", "/formal/horizons/30/complete_case_denominator") + "; the observed lower and unresolved-outcome upper fractions are " + v("rq1_summary", "/formal/horizons/30/observed_response_lower") + " and " + v("rq1_summary", "/formal/horizons/30/response_upper_bound") + ". The gap is an ascertainment bound, distinct from the cluster interval around an observed fraction.\n"
    rows = []
    for day in ("7", "365"):
        base = "/formal/target_state_horizons/" + day
        for state in docs["rq1_summary"]["formal"]["target_state_horizons"][day]["counts"]:
            rows.append([v("rq1_summary", base, "key", "dimension"), v("rq1_summary", base + "/counts/" + state, "key", "dimension"),
                v("rq1_summary", base + "/denominator"), v("rq1_summary", base + "/counts/" + state), v("rq1_summary", base + "/proportions/" + state)])
    result["target_states"] = matrix(["Days", "Target configuration state", "Initial repositories", "Repositories", "Fixed-denominator fraction"], rows)
    rows = []
    for category in docs["rq2_summary"]["formal"]["existing"]["outcome_counts"]:
        base = "/formal/existing/"
        rows.append([v("rq2_summary", base + "outcome_counts/" + escape(category), "key", "dimension"), v("rq2_summary", base + "outcome_counts/" + escape(category)),
            metric("rq2_summary", base + "repository_equal/" + escape(category), key=prefix2 + ".existing.repository_equal." + category), metric("rq2_summary", base + "reference_weighted/" + escape(category), key=prefix2 + ".existing.reference_weighted." + category)])
    result["existing"] = matrix(["Existing-reference outcome", "Instances", "Repository equal [cluster interval]", "Instance weighted [cluster interval]"], rows)
    result["existing"] += "\nThe initial other-reference risk set is supplied by " + v("rq2_summary", "/formal/existing/repositories_with_baseline_references") + " repositories. Observed first pinning has repository-equal fraction " + metric("rq2_summary", "/formal/existing/repository_equal/pin", key=prefix2 + ".existing.repository_equal.pin") + ", compared with reference-instance-weighted fraction " + metric("rq2_summary", "/formal/existing/reference_weighted/pin", key=prefix2 + ".existing.reference_weighted.pin") + ". These retain the baseline denominator and preserve competing deletions and replacements.\n"
    rows = []
    for category in ("all_full_sha", "mixed", "all_non_sha", "unknown"):
        base = "/formal/new_actions/"
        rows.append([v("rq2_summary", base + "configuration_counts/" + category, "key", "dimension"), v("rq2_summary", base + "configuration_counts/" + category),
            metric("rq2_summary", base + "repository_equal/" + category, key=prefix2 + ".new.repository_equal." + category), metric("rq2_summary", base + "reference_weighted/" + category, key=prefix2 + ".new.observed_reference_weighted." + category)])
    result["new"] = matrix(["First identity configuration", "Identity groups", "Repository equal [cluster interval]", "Observed-instance weighted [cluster interval]"], rows)
    result["new"] += "\nOf the first-identity candidates, " + v("rq2_summary", "/formal/new_actions/known_groups") + " have the known-group identity and calendar conditions used by the primary adoption ratios. " + v("rq2_summary", "/formal/new_actions/no_new_group_repositories") + " repositories have no candidate new group and are not scored as zero adoption. The repository-equal fully-fixed-group share among known groups is " + metric("rq2_summary", "/formal/new_actions/repository_equal/all_full_sha_known_groups", key=prefix2 + ".new.repository_equal.all_full_sha_known_groups") + ". The complete-SHA reference share within those known groups is " + metric("rq2_summary", "/formal/new_actions/reference_weighted/full_sha_known_groups", key=prefix2 + ".new.reference_weighted.full_sha_known_groups") + "; this latter estimand also counts fixed references inside mixed groups. Category weights in the table use observed first-configuration instances and do not assert a complete reference denominator for unknown groups.\n"
    rows, duration_rows = [], []
    p = "/populations/formal/"
    for kind in docs["rq3_persistence_summary"]["populations"]["formal"]["by_kind"]:
        base = p + "by_kind/" + kind
        rows.append([v("rq3_persistence_summary", base, "key", "dimension"), *(v("rq3_persistence_summary", base + "/" + part + "/" + key) for part in ("identity", "duration") for key in ("episodes", "repositories", "known_event_episodes"))])
        q = p + "duration_quantiles_days/" + kind
        duration_rows.append([v("rq3_persistence_summary", base, "key", "dimension"), metric("rq3_persistence_summary", q + "/quantiles/p50", q + "/metric_keys/p50")])
    result["persistence"] = matrix(["Entry kind", "Identity periods", "Identity repositories", "Identity known exits", "Duration periods", "Duration repositories", "Duration known exits"], rows)
    result["persistence"] += "\nThe identity description includes " + v("rq3_persistence_summary", p + "identity/episodes") + " periods from " + v("rq3_persistence_summary", p + "identity/repositories") + " repositories, while duration eligibility retains " + v("rq3_persistence_summary", p + "duration/episodes") + " periods from " + v("rq3_persistence_summary", p + "duration/repositories") + ". Group repository counts overlap. The filter changes the duration denominator rather than retrospectively redefining the observed fixed identities.\n"
    result["durations"] = matrix(["Entry kind", "Median observed days [cluster interval]"], duration_rows)
    result["exits"] = matrix(["Known exit", "Identity-qualified periods", "Duration-qualified periods"],
        [[v("rq3_persistence_summary", p + "identity/exit_counts/" + kind, "key", "dimension"),
          *(v("rq3_persistence_summary", p + part + "/exit_counts/" + kind) for part in ("identity", "duration"))] for kind in ("remove", "replace", "unpin")])
    result["retention"] = matrix(["Entry kind", "Days", "Retained", "Known exits", "Unknown interruption", "Early censored", "Determinate", "Retained / determinate [cluster interval]"],
        [[*(v("rq3_persistence_summary", p + "retention/" + str(i) + "/" + key) for key in ("kind", "horizon_days", "retained", "known_exit", "unknown_interruption", "censored_early", "determinate")), metric("rq3_persistence_summary", p + "retention/" + str(i))] for i, _ in enumerate(docs["rq3_persistence_summary"]["populations"]["formal"]["retention"])])
    rows, delay_rows = [], []
    for role in ("target", "comparison"):
        for grade in ("A", "B"):
            base = p + role + "/by_grade/" + grade
            count = base + "/counts"
            rows.append([role, grade, *(v("rq3_maintenance_results", count + "/" + key) for key in ("entries", "repositories", "episodes", "actual_target_sha_adoptions")), metric("rq3_maintenance_results", base + "/outcome_proportions/adopted_target")])
            if role == "target":
                for clock in (("point",) if grade == "A" else ("lower", "upper", "proxy")):
                    q = base + "/delay_quantiles/" + clock
                    delay_rows.append([grade, clock, v("rq3_maintenance_results", q + "/observations"), v("rq3_maintenance_results", q + "/repositories"), metric("rq3_maintenance_results", q + "/quantiles/p50", q + "/metric_keys/p50")])
    result["maintenance"] = matrix(["Role", "Grade", "Entries", "Provider repositories", "Linked periods", "Distinct adoptions", "Adopted-entry fraction [cluster interval]"], rows)
    result["maintenance_outcomes"] = matrix(["Role", "All entries", "Provider repositories", "Linked periods", "Outcome", "Entries with outcome"],
        [[role, *(v("rq3_maintenance_results", p + role + "/counts/" + field) for field in ("entries", "repositories", "episodes")),
          v("rq3_maintenance_results", p + role + "/counts/outcome_counts/" + outcome, "key", "dimension"),
          v("rq3_maintenance_results", p + role + "/counts/outcome_counts/" + outcome)]
         for role in ("target", "comparison") for outcome in docs["rq3_maintenance_results"]["populations"]["formal"][role]["counts"]["outcome_counts"]])
    result["delays"] = matrix(["Target grade", "Delay clock", "Adopted entries", "Repositories", "Median days [cluster interval]"], delay_rows)
    a = p + "target/by_grade/A/delay_quantiles/point"
    b = p + "target/by_grade/B/delay_quantiles/"
    result["delays"] += "\nAmong observed A-grade adoptions, the median point delay is " + v("rq3_maintenance_results", a + "/quantiles/p50") + " days. The corresponding marginal medians of B-grade lower/proxy and upper bounds are " + v("rq3_maintenance_results", b + "lower/quantiles/p50") + " and " + v("rq3_maintenance_results", b + "upper/quantiles/p50") + " days. Marginal bound summaries and their cluster intervals are not an unconditional average delay or a jointly calibrated interval for every adoption.\n"
    result["early"] = matrix(["Target entry kind at lower visit", "Early outcome", "Evidence rows", "Repositories", "Lineages"],
        [[v("rq3_maintenance_results", "/B_pre_upper_exclusions/" + str(i) + "/" + field) for field in ("episode_kind_at_lower", "outcome", "evidence_rows", "repositories", "reference_lineages")]
         for i, row in enumerate(docs["rq3_maintenance_results"]["B_pre_upper_exclusions"]) if row["action"] == "tj-actions/changed-files"])
    result["followup"] = matrix(["Days after U", "Calendar-potential entries", "Repositories", "Periods", "Actually right-censored through horizon", "Repositories", "Periods"],
        [[v("followup", "/horizons/" + day, "key", "dimension"), *(v("followup", "/horizons/" + day + "/" + field + "/" + key)
            for field in ("calendar_potential", "right_censored_observed_followup") for key in ("entries", "repositories", "episodes"))] for day in sorted(docs["followup"]["horizons"], key=int)])
    diagnostic = "/comparison_temporal_diagnostics/"
    primary = {kind: next(i for i, row in enumerate(docs["followup"]["comparison_temporal_diagnostics"]["by_kind_layer"])
        if row["episode_kind"] == kind and row["layer"] == "same_repository_baseline_fixed")
        for kind in ("post_anchor_observed_pin", "post_anchor_born_fixed")}
    pin = diagnostic + "by_kind_layer/" + str(primary["post_anchor_observed_pin"])
    born = diagnostic + "by_kind_layer/" + str(primary["post_anchor_born_fixed"])
    result["comparison_limits"] = ("For genuinely observed post-anchor pinning, the same-repository primary risk-set coverage has "
        + v("followup", diagnostic + "eligible_riskset_primary_by_kind/post_anchor_observed_pin") + " focal period, while its normal-maintenance primary coverage has "
        + v("followup", pin + "/provider_repositories") + " provider repositories and " + v("followup", pin + "/focal_episodes") + " focal periods. "
        "Across all maintenance comparison layers, " + v("followup", diagnostic + "formal_focal_episodes") + " focal periods are linked, of which "
        + v("followup", diagnostic + "duration_eligible_focal_episodes") + " satisfy duration eligibility. Born-fixed primary coverage links "
        + v("followup", born + "/provider_repositories") + " provider repositories and " + v("followup", born + "/focal_episodes") + " focal periods; requiring survival at U and duration eligibility leaves "
        + v("followup", born + "/at_U_alive_and_duration_eligible/providers") + " providers and " + v("followup", born + "/at_U_alive_and_duration_eligible/episodes")
        + " focal periods. Coverage counts do not establish contemporaneous paired observations, even after these necessary temporal checks.\n")
    rows = []
    for field, cells in docs["h1_h2_summary"]["episode_entry_conditions"]["identity"].items():
        for i, _ in enumerate(cells):
            base = "/episode_entry_conditions/identity/" + field + "/" + str(i)
            rows.append([field.replace("entry_", "").replace("_", " "), v("h1_h2_summary", base + "/category"), v("h1_h2_summary", base + "/episodes"), v("h1_h2_summary", base + "/repositories"), metric("h1_h2_summary", base + "/observed_known_exit_fraction")])
    result["conditions"] = matrix(["Entry condition", "Category", "Periods", "Repositories", "Observed known-exit fraction [cluster interval]"], rows)
    field = "entry_configuration_coverage"
    categories = docs["h1_h2_summary"]["episode_entry_conditions"]["identity"][field]
    configured = next(i for i, row in enumerate(categories) if row["category"] == "covered")
    unresolved = next(i for i, row in enumerate(categories) if row["category"] == "unknown")
    result["conditions"] += "\nVisible covered entry configurations occur in " + v("h1_h2_summary", "/episode_entry_conditions/identity/" + field + "/" + str(configured) + "/episodes") + " fixed periods, while the unresolved category contains " + v("h1_h2_summary", "/episode_entry_conditions/identity/" + field + "/" + str(unresolved) + "/episodes") + ". A contrast between these categories cannot be interpreted as a bot-versus-no-bot effect. The cross-tabulations retain exclusions, no observed local configuration, and unverified comment identities as measured states.\n"
    result["inheritance"] = matrix(["Snapshot population", "Snapshots", "Inherited configuration unknown", "Repositories", "Unknown snapshot share [cluster interval]"],
        [[population, *(v("h1_h2_summary", "/inheritance_unknown_snapshots/" + population + "/" + key) for key in ("snapshots", "inheritance_unknown_snapshots", "repositories")), metric("h1_h2_summary", "/inheritance_unknown_snapshots/" + population + "/snapshot_fraction")]
         for population in docs["h1_h2_summary"]["inheritance_unknown_snapshots"]])
    result["tools"] = w.table("h1_h2_summary", "/tool_replays", columns=("tool", "version", "case_id", "covered", "status"))
    result["tool_candidates"] = matrix(["Tool", "Historical version", "Input case", "Reported candidate version", "Candidate commit", "Reported update type"],
        [[*(v("h1_h2_summary", "/tool_replays/" + str(i) + "/" + field) for field in ("tool", "version", "case_id")),
          *(v("h1_h2_summary", "/tool_replays/" + str(i) + "/official_result", formatter) for formatter in ("candidate_version", "candidate_digest", "candidate_type"))]
         for i, row in enumerate(docs["h1_h2_summary"]["tool_replays"]) if row["covered"] and row["status"] == "official_module_update"])
    rows = []
    for name in docs["robustness_checks"]["scenarios"]:
        base = "/scenarios/" + escape(name)
        rows.append([v("robustness_checks", base, "key", "dimension"), v("robustness_checks", base + "/repositories"), v("robustness_checks", base + "/rq2/existing/baseline_references"), v("robustness_checks", base + "/rq2/existing/repository_equal/pin"), v("robustness_checks", base + "/rq3_persistence/duration/episodes")])
    result["robustness"] = matrix(["Sensitivity scenario", "Repositories", "Baseline other mutable instances", "Observed pin fraction, repository equal", "Duration periods"], rows)
    return result


def forbidden_assertions(text):
    """Check assertions per clause, retaining explicit denials and prior-work context."""
    clean = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    violations = []
    for clause in re.split(r"[;!?\n]|\.(?=\s+[A-Z])", clean):
        negative = bool(re.search(r"\b(?:not|no|never|neither|closed|uncollected|cannot|without|exclude[ds]?)\b", clause, re.I))
        empirical = re.search(r"\b(?:we|our (?:study|results|analysis))\b.{0,100}\b(?:fit|fitted|ran|used|estimated|found|show|demonstrate[ds]?|establish(?:ed)?)\b", clause, re.I)
        closed_model = re.search(r"\b(?:Cox|Fine[ -]?Gray|paired (?:model|regression|inference)|mechanism regression|PR fatigue|H3)\b", clause, re.I)
        causal = re.search(r"\b(?:causal effect|causally (?:improve[ds]?|reduce[ds]?|increase[ds]?)|incident caused)\b", clause, re.I)
        perfect = re.search(r"(?:100\s*%|perfect).{0,35}(?:accuracy|classification)|(?:signature verification|release signatures?).{0,30}(?:passed|succeeded|verified)", clause, re.I)
        positive_model = re.search(r"\b(?:Cox|Fine[ -]?Gray|paired|mechanism).{0,30}(?:regression|model|analysis).{0,45}(?:showed|yielded|significant|estimated|fitted)", clause, re.I)
        verified_signature = re.search(r"\bwe (?:successfully )?verified (?:all )?(?:release )?signatures\b", clause, re.I)
        long_rate = re.search(r"(?:90|180|ninety|one hundred eighty)[ -]?(?:day)?.{0,35}(?:freez(?:e|ing)|maintenance failure|non.updat).{0,25}(?:rate|percent|%)", clause, re.I)
        if not negative and (perfect or long_rate or positive_model or verified_signature or empirical and (closed_model or causal)):
            violations.append(clause.strip())
    return violations


def audit_text(text, claims, sources):
    errors, spans, seen = [], [], Counter()
    registry = {row["claim_id"]: row for row in claims}
    if len(registry) != len(claims):
        errors.append("duplicate_claim_ids")
    for match in MARKER.finditer(text):
        identity, rendered = match.group(1), match.group(2)
        seen[identity] += 1
        claim = registry.get(identity)
        if claim is None:
            errors.append("unregistered_claim:" + identity)
            continue
        try:
            source = sources[claim["report"]]
            actual = pointer(source["data"], claim["json_pointer"])
            if json.dumps(actual, sort_keys=True) != json.dumps(claim["value"], sort_keys=True) or rendered != display(actual, claim["formatter"], claim["json_pointer"], claim["report"]) or rendered != claim["rendered"]:
                errors.append("claim_value_mismatch:" + identity)
            if source["sha256"] != claim["source_sha256"]:
                errors.append("claim_source_hash_mismatch:" + identity)
        except (KeyError, IndexError, ValueError, TypeError):
            errors.append("unresolvable_claim:" + identity)
        spans.append((match.start(2), match.end(2)))
    for identity in registry:
        if seen[identity] != 1:
            errors.append("claim_occurrence_not_one:" + identity)
    masked = list(text)
    for match in re.finditer(r"<!--.*?-->|\]\([^)]*\)", text, re.S):
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    numeric_total, numeric_registered = 0, 0
    for match in NUMBER.finditer("".join(masked)):
        numeric_total += 1
        if not any(start <= match.start() and match.end() <= end for start, end in spans):
            errors.append("unregistered_numeric_token:" + match.group())
        else:
            numeric_registered += 1
    errors.extend("forbidden_assertion:" + value for value in forbidden_assertions(text))
    return dict(passed=not errors, errors=errors, registered_claims=len(claims), matched_claims=sum(seen.values()),
                numeric_tokens=numeric_total, registered_numeric_tokens=numeric_registered,
                numeric_token_coverage=numeric_registered / numeric_total if numeric_total else None)


def audit_artifacts(root, manifest, sources):
    results = []
    input_errors = [name for name, digest in manifest.get("input_sha256", {}).items() if not (Path(root) / name).is_file() or sha256(Path(root) / name) != digest]
    for artifact in manifest["artifacts"]:
        errors = []
        for path_key, hash_key in (("path", "sha256"), ("data_path", "data_sha256")):
            path = Path(root) / artifact[path_key]
            if not path.is_file() or sha256(path) != artifact[hash_key]:
                errors.append("artifact_hash_mismatch:" + path_key)
        bindings = artifact.get("source_bindings", [])
        if not bindings:
            errors.append("missing_source_bindings")
        for binding in bindings:
            try:
                pointer(sources[binding["report"]]["data"], binding["pointer"])
            except (KeyError, IndexError, TypeError, ValueError):
                errors.append("unresolvable_artifact_pointer")
        data_path = Path(root) / artifact["data_path"]
        rows, headers, cells = [], [], {}
        if data_path.is_file():
            with data_path.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                rows, headers = list(reader), reader.fieldnames
        if headers != artifact.get("headers") or len(rows) != artifact.get("rows"):
            errors.append("artifact_data_shape_mismatch")
        for binding in artifact.get("cell_bindings", []):
            location = (binding["row"], binding["column"])
            try:
                actual = pointer(sources[binding["report"]]["data"], binding["pointer"])
                if location in cells or binding["row"] < 0 or binding["column"] not in headers:
                    raise ValueError("duplicate or invalid cell")
                csv_value = "" if actual is None else str(actual)
                if json.dumps(actual, sort_keys=True) != json.dumps(binding["value"], sort_keys=True) or rows[binding["row"]][binding["column"]] != csv_value:
                    raise ValueError("cell differs from JSON")
                cells[location] = actual
            except (KeyError, IndexError, TypeError, ValueError):
                errors.append("artifact_cell_binding_mismatch:" + str(location))
        if {(r["report"], r["pointer"]) for r in bindings} != {(r["report"], r["pointer"]) for r in artifact.get("cell_bindings", [])}:
            errors.append("artifact_source_binding_set_mismatch")
        for index, row in enumerate(rows):
            for column, cell in row.items():
                if (NUMBER.search(cell) or cell in {"True", "False"}) and (index, column) not in cells:
                    errors.append("unbound_artifact_numeric_cell:" + str((index, column)))
        if artifact["kind"] == "markdown_table" and (Path(root) / artifact["path"]).is_file():
            lines = (Path(root) / artifact["path"]).read_text(encoding="utf-8").splitlines()
            actual_rows = [[cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
                           for line in lines if line.startswith("|")]
            expected_rows = [["NA" if cells.get((index, column), "not_null") is None
                or (index, column) not in cells and row[column] == "" else row[column] for column in headers] for index, row in enumerate(rows)]
            if not actual_rows or actual_rows[0] != headers or actual_rows[2:] != expected_rows:
                errors.append("markdown_table_data_mismatch")
        results.append(dict(path=artifact["path"], passed=not errors, errors=errors, source_bindings=bindings, checked_numeric_cells=len(cells)))
    return dict(passed=bool(results) and all(row["passed"] for row in results) and not input_errors, artifacts=results, input_hash_mismatches=input_errors)


def compose(writer):
    """Substantive prose is fixed; every reported value is resolved at generation."""
    value, table = writer.value, writer.table
    literal = lambda path: value("source_catalog", path, kind="method_or_reference_literal")
    ref = lambda key: "[" + SOURCES[key]["title"] + "](" + SOURCES[key]["url"] + ")"
    results = selected_results(writer)
    blocks = ["# From Incident Response to Sustained Maintenance: A Longitudinal Study of SHA Pinning after the tj-actions Compromise\n",
        "## Abstract\n",
        "Supply-chain incidents can trigger rapid workflow changes, but an emergency edit does not by itself establish a maintained dependency practice. "
        "We reconstruct an incident-linked cohort and separate direct response, extension to other dependencies, and subsequent maintenance. "
        "The identity-eligible cohort contains " + value("freeze_manifest", "/formal_repository_count") + " repositories; the archived physical batch retains "
        + value("freeze_manifest", "/raw_repository_count") + ". The design combines source-linked workflow histories, explicit unknown states, and historical official-tool replays. "
        "The first-response pin category contains " + value("rq1_summary", "/formal/first_response_counts/pin") + " repositories. Observed conversion of other baseline references has repository-equal fraction "
        + value("rq2_summary", "/formal/existing/repository_equal/pin", "percent") + " and instance-weighted fraction " + value("rq2_summary", "/formal/existing/reference_weighted/pin", "percent") + ". "
        "Results distinguish fixed-denominator conversion from dependency deletion, first-observed Action identities from reintroductions and copies, and persistence from opportunity-conditioned updating. "
        "The maintenance analysis has a restricted late-window opportunity population and cannot measure long-horizon maintenance-freeze rates. "
        "We report descriptive estimates with repository-clustered uncertainty rather than interpreting temporal associations as incident effects.\n",
        "## Introduction\n",
        "A workflow that invokes an external Action executes code selected through a repository reference. A movable tag is convenient for release tracking, but its target can change independently of the downstream workflow. "
        "The changed-files incident made this distinction concrete: the advisory documents malicious code and potential disclosure through workflow logs (" + ref("incident") + "). "
        "A repository that depended on the Action before disclosure is an incident-relevant observation unit; that dependency alone does not establish that its workflows executed malicious code or that its secrets were exposed.\n",
        "Changing a reference to a complete commit identifier stabilizes code identity. It does not establish that the selected code is safe, that transitive dependencies are fixed, or that the dependency will receive later corrections. "
        "An incident response can therefore have several trajectories: removal, replacement, reference conversion, continued mutable use, or an unresolved transition. Even a retained fixed reference can represent either active maintenance or an unchanged historical configuration. "
        "Distinguishing these trajectories requires dependency-level histories and identifiable upstream opportunities, rather than a before/after percentage of fixed references.\n",
        "Our central question is whether incident-linked configuration response is accompanied by broader adoption and observable continued maintenance. "
        "We separate three questions: how repositories first respond and combine later responses; whether existing other dependencies and newly observed Action identities adopt fixed references; and how fixed episodes persist, exit, and respond to identifiable normal upstream opportunities. "
        "The contribution is this linked operationalization and its bounded empirical account, not a claim that SHA pinning, tag mutability, maintenance burden, or security-practice regression is newly discovered.\n",
        "## Background and Related Work\n",
        "GitHub announced an Actions policy capable of enforcing complete commit identifiers on " + literal("/sources/policy/date") + " (" + ref("policy") + "). "
        "It announced general availability of immutable releases on " + literal("/sources/immutable/date") + " (" + ref("immutable") + "). "
        "These dates define descriptive calendar partitions. They do not reveal which repositories enabled a policy or adopted immutable releases. Existing mutable releases are not retrospectively made immutable merely by enabling the feature; current metadata cannot substitute for a historical binding.\n",
        "Security-practice adoption has already been studied at ecosystem scale. Kubo and colleagues combine repository measurement with a developer survey, identifying implementation gaps and reported barriers involving awareness, applicability, and operational costs (" + ref("action_required") + "). "
        "We do not reuse their developer explanations as evidence of motivation in our cohort. Our focus is the sequence from a pre-incident dependency relationship through response, cross-dependency extension, and observable maintenance.\n",
        "Rostami Mazrae and colleagues derive workflow-change categories and analyze long workflow histories (" + ref("evolution") + "). "
        "Their data family is part of our candidate provenance, so its reuse does not constitute an independent sample expansion. Decan and colleagues distinguish technical lag from opportunity lag when studying Action outdatedness (" + ref("outdatedness") + "). "
        "Our maintenance estimand retains this distinction: a new upstream target must first be linked to the downstream reference and its channel before an observed wait can be interpreted.\n",
        "Rapaport and colleagues use archived tag histories and a package-ecosystem cross-analysis to document alterations and reproducibility consequences (" + ref("tags") + "). "
        "This motivates historical identity checks; it does not justify treating every tag retargeting as malicious or every refusal to follow it as maintenance failure. "
        "Chaiwut and Nikiforakis study marketplace evolution and supply-chain attack surfaces (" + ref("marketplace") + "). "
        "The upstream marketplace and downstream repository maintenance are related populations with different denominators.\n",
        "Workflow maintenance itself has costs. Valenzuela-Toledo and colleagues characterize maintenance drivers across mature projects (" + ref("maintenance") + "). "
        "He and colleagues combine repository analysis with a developer survey and report both benefits and limitations of dependency-update automation (" + ref("dependabot") + "). "
        "The publisher abstract of the SBOM study also describes longitudinal regression, oscillation, and abandonment (" + ref("sbom") + "). "
        "Accordingly, observing a practice exit is not sufficient novelty. Our tool experiment addresses a narrower engineering question: what update decision does a specified historical tool version make for controlled representations and configurations?\n",
        "## Data, Scope, and Measurement\n",
        "### Cohort and observation boundary\n",
        "The observation window starts on " + value("source_catalog", "/study/window_start", "date", "method_or_reference_literal") + ", uses " + value("source_catalog", "/study/event_anchor", "date", "method_or_reference_literal") + " as the disclosure anchor, and ends on "
        + value("source_catalog", "/study/window_end", "date", "method_or_reference_literal") + ". Histories follow observable default-branch first-parent ancestry and use UTC committer timestamps. "
        "Side-branch edits enter the observed state when merged into that ancestry, not at an earlier side-branch timestamp. This is a reconstruction of observable history, not proof of the historical default branch at every instant.\n",
        "Candidate sources are retained by name and provenance before identity deduplication. A current repository name or fork can differ from the entity represented by an older source record. "
        "The frozen physical batch and the formal identity-eligible population therefore remain separate. The formal mask applies to target repositories, comparison providers, and focal episodes. "
        "The necessary birth-date and source checks rule out identified conflicts but cannot establish complete entity continuity. Main analyses use the formal population, with the physical population retained as a sensitivity comparison.\n",
        "### Reference lineages and configuration states\n",
        "Workflow parsing extracts actual external Action invocations rather than matching names in arbitrary text. A complete commit identifier is a reference-form category, not a security verdict. "
        "Lineage reconstruction uses Action identity, file and job context, step identifiers, rename evidence, and source correspondence. Step position alone is insufficient: reordering, comments, and layout changes must not manufacture a pinning event. "
        "Many-to-many rewrites remain ambiguous when a stable correspondence cannot be established. Removal and later return are kept distinct from first observed identity introduction, and copied references do not become independent new Action identities.\n",
        "Workflow and bot-configuration change points are reconstructed jointly. Configuration-only edits can change a measured coverage state without changing a reference or creating a reference transition. "
        "We distinguish visible local ecosystem coverage, explicit exclusions, no observed local configuration, and unresolved inheritance. Unresolved inherited configuration is not evidence that no bot exists. "
        "Version comments are attached through source locations; an unverified comment is not silently upgraded to a verified code identity.\n",
        "### Implementation checks and interpretive limits\n",
        "The frozen evidence binds original objects, input and implementation hashes, conservation checks, replay results, and executable boundary examples. "
        "Golden scenarios cover reorderings, renames, comment-only changes, reintroductions, source markers, invalid YAML, configuration-only changes, inherited configuration, first-parent ordering, and ambiguous rewrites. "
        "A genuinely empty or comment-only workflow can be inactive or removed; missing-content markers and invalid YAML remain unknown. Implementation replay consistency is not evidence of perfect behavioral classification, and we did not create a manual-label accuracy claim.\n",
        "### Estimands and uncertainty\n",
        "Direct response is summarized both as the first observable event and as a full-window combination of behaviors. Time-specific tables retain their at-risk, censored, and unknown denominators. "
        "For expansion, the baseline set of other non-fixed references is held fixed; deletion and replacement are competing observed changes rather than improvements in a shrinking pinning fraction. "
        "New-identity adoption has its own denominator. Repositories with no qualifying new identity are excluded from that adoption denominator, not assigned zero adoption. "
        "Repository-equal and reference-instance-weighted quantities answer different questions and are both reported.\n",
        "Uncertainty uses whole-repository resampling so every selected repository carries its categories, references, and episodes together. "
        "The uncertainty artifact records the actual seed, requested draws, valid draws, percentile bounds, cluster counts, and degenerate statuses. "
        "Exact binomial intervals accompany eligible complete repository-level binary outcomes. They are not substituted for cluster resampling of repeated references or episodes. "
        "Sparse or all-zero cells retain their observed risk sets; a degenerate interval does not establish absence of risk.\n",
        "## Direct Incident-Linked Response\n",
        "The formal direct-response population is " + value("rq1_summary", "/formal/repositories") + " repositories. "
        "First-response categories below are mutually exclusive under the observable-history definition; the subsequent combination table deliberately allows a repository to exhibit several behaviors over the window. "
        "Neither a tag edit nor deletion is relabeled as SHA conversion, and an unresolved early boundary is not replaced by a convenient later event.\n",
        value("rq1_summary", "/formal/tag_update_semantics", kind="definition") + "\n",
        results["response"], results["combinations"],
        "The full-window no-observed-change combination contains " + value("rq1_summary", "/formal/full_window_combination_counts/no_observed_change") + " repositories with no extracted behavior in the available history. This diagnostic can include histories ending before the anchor and is not a count with year-long follow-up. "
        "The first-response no-observed-change category contains " + value("rq1_summary", "/formal/first_response_counts/no_observed_change") + " repositories: "
        + value("rq1_summary", "/formal/no_observed_change_followup_counts/observed_to_window_end") + " are observable to the window end and "
        + value("rq1_summary", "/formal/no_observed_change_followup_counts/ancestry_censored") + " have earlier observable-ancestry censoring. Neither diagnostic should be read as confirmed unchanged behavior throughout unavailable history.\n",
        "The horizon-specific results separate repositories still observable without a response from those censored or interrupted by unknown states. "
        "Observed response-time quantiles describe the eligible observed events, not a population time-to-response for every repository. "
        "Multiple later edits are evidence of a trajectory, not multiple independent repositories responding to the incident.\n",
        results["response_horizons"], "The median among eligible known first observed target-configuration changes is " + writer.metric("rq1_summary", "/formal/response_time_days_quantiles/0.5", key=writer.sources["rq1_summary"]["data"]["formal"]["metric_prefix"] + ".response_days.0.5") + " days. This is a conditional UTC-date-difference distribution with interpolated quantiles, not sub-day response timing or evidence of incident motivation. The complete conditional quantile distribution remains in the response artifact.\n",
        results["target_states"],
        "Observed response and the target's configuration state are different outcomes. The horizon states retain absent dependencies, mutable references, complete-SHA references, mixed configurations, and unresolved observation separately on the initial repository denominator. Similar aggregate fixed counts at separated horizons do not establish that the same repositories stayed fixed or that adoption reached a plateau.\n",
        "## Extension Beyond the Incident Action\n",
        "The existing-reference analysis contains " + value("rq2_summary", "/formal/existing/baseline_references") + " baseline other-dependency instances. "
        "The new-identity analysis contains " + value("rq2_summary", "/formal/new_actions/groups") + " repository/Action first-identity candidates, including unknown-history or unknown-calendar candidates. "
        "The incident Action and its own born-fixed lineages are excluded from both core expansion results. These denominators cannot be replaced by the later maintenance-opportunity subset.\n",
        "The primary-result exclusion audit identifies " + value("rq2_summary", "/excluded_target_born_lineage_count") + " target born-fixed lineages. The separate conservation and contamination checks must pass before this manuscript can be generated.\n",
        results["existing"],
        "The other-reference no-observed-change category contains " + value("rq2_summary", "/formal/existing/outcome_counts/no_observed_change") + " instances, including "
        + value("rq2_summary", "/formal/existing/window_followup_counts/no_observed_change_complete") + " observed to the window end and "
        + value("rq2_summary", "/formal/existing/window_followup_counts/ancestry_censored") + " censored earlier. Absence of an extracted transition is therefore not evidence of complete follow-up for every instance.\n",
        "The fixed baseline denominator makes the difference between conversion and denominator change explicit. A repository can remove an unpinned dependency and thereby increase a current pinning fraction without adopting pinning for that dependency. "
        "The outcome table preserves that behavior separately. Repository-equal estimates emphasize the typical eligible repository; instance weighting emphasizes where references are concentrated. "
        "Their disagreement, when present, should be read as a weighting difference rather than resolved by selecting whichever estimate is larger.\n",
        results["new"],
        "For newly observed identities we distinguish entirely fixed, mixed, entirely non-fixed, and unknown first configurations. "
        "First observed means first established in the available reconstructed history; it does not establish the identity's first-ever use outside that history. "
        "Reintroduced identities and copied steps are separate diagnostics. A repository with no qualifying new identity provides no adoption trial for this estimand.\n",
        "## Persistence and Opportunity-Conditioned Maintenance\n",
        "### Fixed episodes and observed exits\n",
        "Persistence is stratified by baseline-fixed, observed conversion of an existing reference, and introduction already fixed. "
        "The identity-qualified description and duration-qualified analysis are presented side by side. They answer different questions: which states and exits are identifiable, and which observations support a coherent duration calculation. "
        "We retain excluded exits in the identity description rather than allowing a temporal eligibility filter to erase their existence.\n",
        results["persistence"], results["exits"], results["durations"],
        "Known exits include deletion and replacement as well as returning to a mutable reference. Their separate counts prevent a dependency's disappearance from being described as unpinning. The duration medians summarize observed follow-up lengths, including censored observations; they are not median times to failure or estimated survival medians.\n",
        "Unpinning, removal, and replacement are distinct known exits. Ambiguous rewrites and unknown boundaries interrupt ascertainment; they are not evidence of continued retention or known maintenance failure. "
        "Study-window and observable-ancestry censoring are also preserved. Baseline-fixed episodes have an unknown prior fixed age and are not incident-induced adoption episodes. "
        "The reported durations and retention classifications therefore describe observable follow-up under their own entry conditions.\n",
        results["retention"],
        "The born-fixed description retains " + value("rq3_persistence_summary", "/born_fixed_unpin/identity") + " observed unpinning periods, while " + value("rq3_persistence_summary", "/born_fixed_unpin/duration") + " enter the duration-qualified subset. "
        "Their exclusion follows the frozen repository-clock rule; it does not erase their descriptive identity or exit classification.\n",
        "### Normal upstream maintenance opportunities\n",
        "Normal maintenance includes immutable-release evidence and newly observed, later same-channel version tags whose historical bindings can be established. "
        "Filtering precedes deduplication: an earlier-selected tag retargeting must not hide qualifying normal-version evidence for the same new target. "
        "Same-tag retargeting remains a separate diagnostic. Refusing to follow a moved tag is not automatically a maintenance failure, and this study does not classify a specific retargeted commit as malicious.\n",
        results["maintenance"], results["maintenance_outcomes"], results["delays"],
        "The union-level denominators above differ from sums of overlapping grade-specific repository and linked-period counts. A change to another SHA is distinct from adoption of the specified opportunity target, and can reflect a different update route; it is not automatically a stale dependency or maintenance failure. Right censoring with no observed SHA change is likewise not proof of freezing.\n",
        "For a verified immutable-release opportunity, an observed target adoption supports a point delay at the available time resolution. "
        "For a discrete archive interval with availability bounds L and U and adoption time T, the true delay is bounded by [T-U, T-L), while T-U is only an upper-bound-anchored waiting proxy. "
        "We do not pool the proxy with immutable-release point delays into an unconditional mean. Archive availability is an observation, not proof of first-ever publication.\n",
        "A B-grade entry must survive, remain fixed, and avoid its first target change or unknown interruption until U to enter this maintenance subset. "
        "Changes or censoring before U remain in the exclusion diagnostics, even when a later observer might recognize a target adoption. "
        "This conditioning changes the population. It prevents negative proxy waits from being coerced to zero, but also means the retained subset is not all fixed references.\n",
        results["early"], "These target-only early-change rows are evidence diagnostics, including duplicate evidence and only generated candidates. They do not supply a full-cohort failure denominator. The complete upstream/action diagnostic table remains in the maintenance artifact.\n",
        "### Follow-up, comparison coverage, and closed analyses\n",
        "The maintenance opportunities occur late in the observation window. Calendar potential and actually observed right-censored follow-up are separate quantities. "
        "The frozen follow-up audit closes the long-horizon maintenance-freeze indicators; the shorter horizon supports only bounded exploration when its information is sufficient. "
        "It cannot yield a full-cohort maintenance-failure rate. This restriction is about the maintenance opportunity clock, not a prohibition on reporting retention horizons for fixed episodes.\n",
        results["followup"],
        "The target availability upper bounds run from " + value("followup", "/U_bounds/minimum_utc", "date") + " to " + value("followup", "/U_bounds/maximum_utc", "date") + ". "
        "An archive upper bound is not an exact publication timestamp.\n",
        "Comparison layers describe same-repository baseline-fixed providers, same-commit co-pinning supplements, and cross-repository same-Action supplements. "
        "They are not interchangeable controls. Shared repositories, providers, episodes, and release opportunities create dependence, and available coverage does not establish a synchronized common observation interval. "
        "Paired inference, causal effect estimation, Cox regression, Fine-Gray regression, and mechanism regressions remain closed.\n",
        table("rq3_persistence_summary", "/comparisons/riskset", columns=("episode_kind", "layer", "provider_repositories", "focal_repositories", "focal_episodes")),
        table("rq3_persistence_summary", "/comparisons/maintenance", columns=("episode_kind", "layer", "provider_repositories", "focal_repositories", "focal_episodes")),
        results["comparison_limits"],
        "## Configuration and Version-Information Evidence\n",
        "The configuration analysis preserves entry-time measured states rather than backfilling later bot configuration into an earlier episode. "
        "Identity and duration populations are tabulated separately. The maintenance cross-tabulation counts a normal entry once; mixed conditions from multiple linked episodes are retained explicitly. "
        "A visible local configuration is a configuration observation, not proof that a bot ran, created an update, or merged it. Unknown inherited configuration is therefore a substantive measurement limit.\n",
        results["conditions"], results["inheritance"],
        "The entry-condition table describes measured categories, with overlapping repository membership across categories. Full entry-condition-by-grade maintenance cross-tabulations, including mixed linked-episode conditions, remain in the configuration artifact rather than being pooled into an adjusted effect.\n",
        "The historical tool experiment holds an Action and synthetic upstream mapping fixed while varying representation and ecosystem configuration. "
        "Enabled scenarios execute the actual official extraction and update-decision code of the bound tool versions; disabled and missing-ecosystem controls retain their distinct execution boundaries. Tagged SHA references with readable comments, bare SHAs associated with a version tag, and untagged commit SHAs are different inputs. "
        "A tool choosing a branch-head candidate in a bare-SHA scenario must not be re-described as being unable to update that input. These are controlled module decisions, not evidence that a repository's deployed bot behaved identically.\n",
        results["tools"], results["tool_candidates"],
        "The enabled candidate comparison exposes representation-sensitive decisions in the fixed historical versions. A version-comment input and a tag-equivalent bare SHA can yield different candidate commits or version metadata in one tool even when another tool selects the same versioned target for both. An unreported version is shown explicitly rather than inferred from a digest. These are controlled candidate differences; the table alone does not identify a branch-head route or a deployed scheduler's behavior.\n",
        "The fixed revisions are publicly identifiable in the " + ref("dependabot_source") + " and " + ref("renovate_source") + ". "
        "The enabled fixtures do not support a general claim that omitting a version comment freezes a SHA dependency. Candidate selection can instead use another update route. "
        "A configured-disabled Renovate result and a Dependabot fixture that was not scheduled by the harness are different control observations; neither demonstrates how a hosted scheduler would handle an arbitrary repository. "
        "This distinction matters when interpreting an apparent configuration gap: representation, resolver routing, scheduling, update generation, and merging are separate stages, and our experiment directly exercises only the documented module boundary.\n",
        "The experiment supplies a concrete compatibility boundary for interpreting configuration and comment distributions. It does not identify the cause of a maintainer's later edit or prove a subjective review burden. "
        "PR histories were not collected for this frozen stage. An empty PR table is not an absence of PR activity, and the PR-workload hypothesis remains uncollected and closed.\n",
        "## Uncertainty and Robustness\n",
        "The result tables report percentile limits at confidence level " + value("uncertainty_intervals", "/confidence_level", "percent") + ", the effective repository count, and resampling status for each displayed estimand. "
        "Different cells may have different valid resampling counts when their eligible denominators are absent from some draws. "
        "We do not treat a nominal interval as meaningful without its denominator and sparse or degenerate status.\n",
        "The registered seed is " + value("uncertainty_intervals", "/seed") + " and the requested resampling count is " + value("uncertainty_intervals", "/replicates") + ". The complete estimand registry and valid-draw diagnostics remain in the uncertainty artifact.\n",
        "The robustness matrix reports the formal and physical populations, exclusion of the pilot cohort, alternative anchor boundaries, omission of the anchor day, later trajectories after concentrated early edits, and calendar partitions around platform announcements. "
        "These checks evaluate sensitivity to operational choices. They are not independent experiments and do not repair missing comparison overlap or historical selection. "
        "Policy-period changes remain descriptive because repository-specific enforcement, concurrent changes, and broader incident awareness are not experimentally assigned.\n",
        results["robustness"],
        "The later-landmark and platform-calendar rows keep the original baseline reference denominator, and repository-equal estimates average each repository's fraction on its original baseline set. They describe later observed conversions relative to that fixed initial set, not the conditional probability of pinning among references still mutable at the landmark. Earlier conversions, removals, replacements, and unknown observations are not silently discarded to create a fresh risk denominator.\n",
        "## Figures and Reproducible Tables\n",
        "The artifacts below are generated from the same bound statistical JSON. Their data files, field pointers, and rendered-file hashes are checked along with the manuscript. "
        "A visible data-source caption accompanies every artifact; a successful figure hash alone does not establish that its numbers match the statistical source.\n",
        "## Discussion\n",
        "### Incident response is a sequence of engineering decisions\n",
        "The response and expansion tables should be read together. A local incident fix, broader conversion of existing dependencies, and fixed configuration at a new identity's introduction are distinct behaviors. "
        "A repository may exhibit one without the others. This distinction avoids equating an emergency patch with an institutionalized dependency-management practice. "
        "It also makes negative or limited expansion informative: lack of a recorded conversion has to be interpreted alongside removals, unchanged references, unresolved history, and actual opportunities for continued observation.\n",
        "### Stable identity and maintainability require different evidence\n",
        "A full commit identifier stabilizes the selected object, while a readable version and a functioning update route help maintain that choice. "
        "The configuration and historical-tool results identify observable differences in that route. They support practical recommendations to retain human-readable version context, make ecosystem coverage explicit, and review inherited configuration boundaries. "
        "The recommendations concern inspectability and update routing; we did not estimate their causal effect on security or maintenance success. "
        "Missing comments and unknown inherited configuration should be reported as measured conditions, not used to diagnose an individual maintainer's intent.\n",
        "### Opportunity evidence limits claims about long-term maintenance\n",
        "The maintained-code question cannot be answered by an unchanged YAML file alone. A target may have no identifiable later same-channel version, an unresolved historical identity, a deleted upstream, or insufficient downstream follow-up. "
        "The normal-opportunity analysis is consequently a selected, bounded complement to persistence. It is not a substitute for the whole-cohort exit description. "
        "In particular, late-window evidence cannot support a long-term frozen-dependency rate merely because the overall study window spans a year after disclosure.\n",
        "## Threats to Validity and Method Revisions\n",
        "### Construct and implementation validity\n",
        "Observed configuration is not execution. Pinning syntax is not code safety, comments are not verified identities, and local bot configuration is not bot activity. "
        "Lineage ambiguity, missing source states, inherited configuration, and committer-time inversions can interrupt ascertainment. "
        "Object checks, conservation, controlled examples, and replay constrain implementation errors but do not establish semantic classification accuracy against independently labeled real-world behavior. "
        "All unknown and excluded states retain their reasons and original evidence.\n",
        "### Selection, temporal validity, and generalization\n",
        "The candidate frame has its own historical visibility and survival properties; a complete reconstruction within that frame is not a random sample of GitHub. "
        "Current names, forks, source timestamps, and repository identity can disagree. Necessary identity checks reduce known conflicts without proving historical continuity. "
        "Observable first-parent ancestry may omit a historical default branch or rewritten history. Shared templates and organizational practices can induce dependence beyond a repository cluster. "
        "The incident-linked cohort is not a population of confirmed victims, and repositories without the incident Action can still have responded to the same news.\n",
        "### Opportunity ascertainment and controlled-tool external validity\n",
        "Discrete archived snapshots do not reveal every publication, deletion, or retargeting between visits. Current-only release metadata cannot supply an old target identity. "
        "A-grade identity is not a claim that cryptographic release verification succeeded; that verification was incomplete. B-grade upper-survival conditioning excludes early changes from the main maintenance subset. "
        "The tool experiment uses a synthetic upstream and historically fixed versions, so it does not establish present-day routing, hosted bot rollout, repository policy, PR creation, or merge behavior. "
        "The source catalogue identifies the scope of each external reference; an abstract-level comparison is not a replicated methodology.\n",
        "### Revisions after the development checkpoint\n",
        "The measurement design evolved after the " + value("development_checkpoint", "/measured_ids_exported") + "-repository development checkpoint; this was not an untouched preregistration. "
        "Historical entity checks separated the physical batch from the formal identity-qualified population. The normal-maintenance view changed selection order so filtering normal evidence precedes deduplication; retained retargeting diagnostics were not reclassified as failures. "
        "The identity revision excludes " + value("freeze_manifest", "/formal_population/excluded_confirmed_repository_ids", "length") + " current entities from the formal population while preserving their raw histories. "
        "The raw matching evidence retains " + value("maintenance_scope", "/raw_same_tag_retarget_rows") + " same-tag retargeting rows, and filter-before-deduplication restores " + value("maintenance_scope", "/restored_normal_entries_with_raw_selected_false") + " normal entries that raw global selection had hidden. "
        "Subsequent follow-up checks closed long-horizon maintenance-freeze indicators and limited the shorter horizon to bounded exploration. "
        "Frozen raw histories and earlier artifacts remain preserved. These revisions clarify the actual estimands and available evidence; they are disclosed rather than presented as decisions fixed before any data inspection.\n",
        "The first formal-statistics pass also exposed a calendar-eligibility mismatch: a topology-based reference outcome can be identifiable even when its repository does not support the calendar ordering required for an incident-relative event. "
        "The revised formal expansion denominator retains " + value("rq2_summary", "/calendar_eligibility/formal/baseline_references_with_clock_unknown") + " references as clock-unknown: "
        + value("rq2_summary", "/calendar_eligibility/formal/reasons/nonmonotone_snapshot_calendar/references") + " occur in "
        + value("rq2_summary", "/calendar_eligibility/formal/clock_unalignable_repository_ids", "length") + " repositories with nonmonotone snapshot calendars, and "
        + value("rq2_summary", "/calendar_eligibility/formal/reasons/observation_ended_before_anchor/references") + " have observation ending before the event anchor. "
        "The correction recodes " + value("rq2_summary", "/calendar_eligibility/formal/prior_pin_outcomes_recoded") + " previously reported topological pin outcomes rather than counting them as dated incident-following expansion. "
        "The time-based main expansion results normalize those cases to clock-unknown while preserving their original topological kinds as identity diagnostics. "
        "Neither a pin visible only in such a topology nor a previously emitted result is counted as a dated post-incident conversion merely to retain an earlier estimate. "
        "The revised statistical source and extraction-cache audit are separate from the preserved original measurements; this is an additional disclosed method revision, not a change to frozen historical files.\n",
        "## Conclusion\n",
        "This study provides a linked account of incident-relevant response, cross-dependency adoption, and observable persistence and maintenance in GitHub Actions. "
        "Its results separate repository and instance weighting, identity and duration eligibility, known exits and unknown interruptions, and stable code identity and normal updating opportunities. "
        "The direct first-pin category contains " + value("rq1_summary", "/formal/first_response_counts/pin") + " repositories; the broader other-dependency conversion fractions are " + value("rq2_summary", "/formal/existing/repository_equal/pin", "percent")
        + " under repository-equal weighting and " + value("rq2_summary", "/formal/existing/reference_weighted/pin", "percent") + " under instance weighting. "
        "The evidence supports a bounded descriptive study and controlled historical-tool conclusions. It does not establish an incident treatment effect, a general long-term maintenance-failure rate, or maintainer motivations. "
        "The reproducible contribution is the combination of preserved histories, explicit measurement boundaries, source-linked estimates, and a claim-to-evidence audit that makes those limits inspectable.\n",
        "## References\n"]
    for key, source in SOURCES.items():
        blocks.append(source["authors"] + ". " + literal("/sources/" + key + "/year") + ". " + ref(key) + ". " + source.get("venue", "Official advisory or changelog") + ".\n")
    artifacts = []
    for index, artifact in enumerate(writer.sources["figure_manifest"]["data"]["artifacts"]):
        if artifact["kind"] not in {"figure_svg", "markdown_table"}:
            continue
        target = artifact["path"]
        if target.startswith("paper/"):
            target = target[len("paper/"):]
        caption = value("figure_manifest", "/artifacts/" + str(index) + "/caption", "caption_title", "artifact_caption")
        if artifact["kind"] == "figure_svg":
            artifacts.append("![Generated figure](" + target + ")\n\n" + caption + "\n")
        else:
            artifacts.append("[Generated table](" + target + ")\n\n" + caption + "\n")
        grouped_sources = {}
        for binding in artifact["source_bindings"]:
            grouped_sources.setdefault(binding["report"], binding["pointer"])
        for report, first_pointer in grouped_sources.items():
            artifacts.append("Data source: " + value(report, first_pointer, "source_report", "source_locator")
                + "; representative JSON Pointer `" + value(report, first_pointer, "pointer", "source_locator") + "`; complete cell bindings accompany the data CSV.\n")
        data_target = artifact["data_path"]
        artifacts.append("[Bound data CSV](" + (data_target[len("paper/"):] if data_target.startswith("paper/") else data_target) + ")\n")
    return "\n".join(blocks).replace("\n## Discussion\n", "\n" + "\n".join(artifacts) + "\n## Discussion\n")


def run(root, output_dir, context):
    root, output_dir = Path(root).resolve(), Path(output_dir).resolve()
    frozen = ("study.toml", "reports/rq3_scope_counts.json", "reports/model_eligibility_audit.json", "reports/maintenance_followup_audit.json", "reports/maintenance_scope_summary.json")
    require_context(root, context, frozen)
    implementation = sha256(Path(__file__))
    if implementation != IMPORTED_SOURCE_SHA256:
        raise ValueError("Manuscript implementation changed after import; reload the module")
    for filename in ("manuscript.md", "claim_evidence_map.json", "manuscript_audit.json", "source_catalog.json"):
        target = output_dir / filename
        if target.is_relative_to(root) and target.relative_to(root).as_posix() in context["freeze_manifest"]["input_sha256"]:
            raise ValueError("Manuscript output would overwrite frozen evidence")
    output_dir.mkdir(parents=True, exist_ok=True)
    sources = {}

    def load(name, path):
        path = Path(path).resolve()
        content = path.read_bytes()
        sources[name] = dict(path=path, sha256=hashlib.sha256(content).hexdigest(), data=json.loads(content))

    for name in REPORTS:
        default = Path(context.get("report_dir", root / "reports")) / (name + ".json")
        load(name, context.get("report_paths", {}).get(name, default))
        report = sources[name]["data"]
        if "freeze_sha256" in report and report["freeze_sha256"] != context["guard"]["freeze_sha256"]:
            raise ValueError("Statistical report belongs to a different frozen batch: " + name)
        if "checks" in report and not all(value is True for value in report["checks"].values()):
            raise ValueError("Statistical report has failed checks: " + name)
    load("freeze_manifest", manifest_file(root, context))
    for name, filename in (("followup", "maintenance_followup_audit"), ("maintenance_scope", "maintenance_scope_summary")):
        load(name, root / "reports" / (filename + ".json"))
    checkpoint = root / "data/derived/analysis-v4/checkpoints/152-20261018T113552Z/reports/table_manifest.json"
    load("development_checkpoint", checkpoint)
    primary_pdf = root / "paper/P12_An_Empirical_Study_of_SBOM_Usage_Through_GitHub_Actions.pdf"
    additional_files = {str(primary_pdf): sha256(primary_pdf)}
    catalog = dict(rule_version="manuscript-primary-sources-v1", verification_date="2026-10-18", sources=SOURCES,
        study=context["study"], development_checkpoint_repositories=sources["development_checkpoint"]["data"]["measured_ids_exported"],
        literal_policy="External dates, bibliographic years and method identifiers are registered source-catalogue literals, not empirical cohort estimates.",
        local_primary_pdf_sha256=additional_files,
        development_checkpoint_provenance="Frozen historical checkpoint data/derived/analysis-v4/checkpoints/152-20261018T113552Z/; disclosed post-hoc development history.")
    write_json(output_dir / "source_catalog.json", catalog)
    load("source_catalog", output_dir / "source_catalog.json")
    writer = Writer(sources)
    text = compose(writer)
    document = output_dir / "manuscript.md"
    document.write_text(text, encoding="utf-8")
    text_audit = audit_text(document.read_text(encoding="utf-8"), writer.claims, sources)
    artifacts = audit_artifacts(root, sources["figure_manifest"]["data"], sources)
    changed = [name for name, source in sources.items() if sha256(source["path"]) != source["sha256"]]
    changed.extend(name for name, digest in additional_files.items() if sha256(name) != digest)
    if sha256(Path(__file__)) != implementation:
        changed.append("manuscript_implementation")
    require_context(root, context, frozen)
    evidence = dict(rule_version=RULES_VERSION, claims=writer.claims,
        input_sha256={**{str(source["path"]): source["sha256"] for source in sources.values()}, **additional_files},
        implementation_sha256=implementation, freeze_sha256=context["guard"]["freeze_sha256"],
        artifact_evidence=artifacts["artifacts"], numeric_policy="All visible numerical tokens require an evidence marker; link destinations and marker identifiers are metadata.")
    write_json(output_dir / "claim_evidence_map.json", evidence)
    audit = dict(rule_version=RULES_VERSION, audit_passed=text_audit["passed"] and artifacts["passed"] and not changed,
        text=text_audit, figures_and_tables=artifacts, changed_inputs=changed,
        manuscript_sha256=sha256(document), claim_map_sha256=sha256(output_dir / "claim_evidence_map.json"),
        counts=dict(claims=len(writer.claims), artifacts=len(artifacts["artifacts"])),
        visible_english_words=len(re.findall(r"\b[A-Za-z]+\b", re.sub(r"<!--.*?-->", "", text, flags=re.S))),
        closed_analyses=closed_models(context),
        limits=["A programmatic claim audit is not peer review, semantic classification validation, or causal identification."])
    write_json(output_dir / "manuscript_audit.json", audit)
    if not audit["audit_passed"]:
        raise ValueError("Manuscript audit failed: " + json.dumps(audit, ensure_ascii=False))
    return dict(manuscript=str(document), claim_evidence_map=str(output_dir / "claim_evidence_map.json"), audit=str(output_dir / "manuscript_audit.json"))
