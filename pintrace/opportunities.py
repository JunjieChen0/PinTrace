"""Collect dated upstream identities without backdating today's mutable tags."""

import argparse
import hashlib
import json
import re
import shutil
import tomllib
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

import requests

from pintrace.github import GitHub, github_token

SHA = re.compile(r"[0-9a-f]{40}\Z")
VERSION = re.compile(r"v?(\d+)(?:\.\d+){0,2}\Z")
SWH = "https://archive.softwareheritage.org/api/1/"


def grade_evidence(record):
    """A requires a single historical binding, not two unrelated timestamps."""
    if not SHA.fullmatch(record.get("target") or ""):
        return "C"
    kind = record.get("evidence_type")
    if kind == "immutable_release" and record.get("immutable") is True and record.get("published_at"):
        return "A"
    if kind == "historical_release_event" and record.get("event_time") and record.get("target_in_event") is True:
        return "A"
    if kind == "archive_snapshot" and record.get("visit_date") and record.get("snapshot"):
        return "B"
    return "C"


def channel(tag):
    match = VERSION.fullmatch(tag or "")
    return "v" + match[1] if match else None


def version_key(tag):
    if not VERSION.fullmatch(tag or ""):
        return None
    return ".".join(str(int(part)) for part in tag.removeprefix("v").split("."))


def archived_opportunities(snapshot_visits):
    """Derive tag-presence intervals from complete snapshots of the same origin.

    Presence is the raw tag-name set, not the subset whose targets were resolved.
    These intervals describe observations, not first-ever publication or a
    universally applicable upgrade for a downstream pinned reference.
    """
    grouped = {}
    for visit in snapshot_visits:
        origin = visit.get("origin_url", "https://github.com/" + visit["repository"])
        grouped.setdefault((visit["repository"], origin), []).append(visit)
    events, initial, gaps = [], [], []
    for (repository, origin), visits in sorted(grouped.items()):
        visits.sort(key=lambda item: (item["visit_date"], item["snapshot"]))
        previous, seen, incomplete = None, set(), []
        for current in visits:
            if not current.get("complete") or not current.get("tag_listing_complete"):
                incomplete.append(current["snapshot"])
                gaps.append({"repository": repository, "snapshot": current["snapshot"], "reason": "incomplete_snapshot"})
                continue
            present = set(current["tag_names"])
            if previous is None:
                for tag, target in current["bindings"].items():
                    initial.append({"repository": repository, "origin_url": origin, "tag": tag,
                                    "channel": channel(tag), "target": target, "evidence_grade": "B",
                                    "evidence_type": "observed_availability", "available_by": current["visit_date"],
                                    "available_after": None, "upper_snapshot": current["snapshot"],
                                    "upper_complete": True, "source_url": current["source_url"],
                                    "exact_publication_identified": False})
                previous, seen, incomplete = current, seen | present, []
                continue
            if previous["visit_date"] == current["visit_date"]:
                if previous["tag_names"] != current["tag_names"] or previous["bindings"] != current["bindings"]:
                    gaps.append({"repository": repository, "snapshot": current["snapshot"], "reason": "equal_time_conflicting_snapshots"})
                    # Neither same-time state can be placed deterministically.
                    previous = None
                continue
            before_names = set(previous["tag_names"])
            for tag, target in current["bindings"].items():
                key = version_key(tag)
                if key is None:
                    continue
                if tag not in before_names:
                    kind = "observed_tag_reintroduction" if tag in seen else "observed_tag_introduction"
                elif tag in previous["bindings"] and previous["bindings"][tag] != target:
                    kind = "observed_binding_change"
                else:
                    continue
                prior_versions = [{"tag": old_tag, "target": old_target, "version_key": version_key(old_tag)}
                                  for old_tag, old_target in previous["bindings"].items()
                                  if channel(old_tag) == channel(tag)]
                same_version = [row for row in prior_versions if row["version_key"] == key]
                upper_same_version = [{"tag": name, "target": sha} for name, sha in current["bindings"].items()
                                      if version_key(name) == key]
                alias_only = kind == "observed_tag_introduction" and any(row["target"] == target for row in same_version)
                precision = len(key.split("."))
                full_versions = [tuple(map(int, row["version_key"].split("."))) for row in prior_versions
                                 if len(row["version_key"].split(".")) == 3]
                order = "not_full_semver" if precision != 3 else "unknown"
                if precision == 3 and full_versions:
                    value, maximum = tuple(map(int, key.split("."))), max(full_versions)
                    order = "higher" if value > maximum else "equal" if value == maximum else "lower"
                row = {
                    "repository": repository, "origin_url": origin, "tag": tag, "channel": channel(tag),
                    "version_key": key, "version_precision": precision, "target": target,
                    "previous_target": previous["bindings"].get(tag),
                    "evidence_grade": "B", "evidence_type": "observed_tag_alias_introduction" if alias_only else kind,
                    "available_after": previous["visit_date"], "available_by": current["visit_date"],
                    "lower_snapshot": previous["snapshot"], "upper_snapshot": current["snapshot"],
                    "lower_complete": True, "upper_complete": True,
                    "lower_snapshot_tags_complete": True, "upper_snapshot_tags_complete": True,
                    "lower_url": previous["source_url"], "upper_url": current["source_url"],
                    "lower_bindings_source_url": previous["source_url"],
                    "same_target_lower_tags": sorted(t for t, sha in previous["bindings"].items() if sha == target),
                    "same_version_lower_bindings": same_version,
                    "upper_same_version_bindings": upper_same_version,
                    "upper_version_target_conflict": len({row["target"] for row in upper_same_version}) > 1,
                    "before_current_bindings": prior_versions,
                    "version_vs_previous_channel_max": order,
                    "intervening_incomplete_snapshots": list(incomplete),
                    "target_previously_observed": target in previous["bindings"].values(),
                    "exact_publication_identified": False, "first_ever_publication_identified": False,
                    "availability_scope": "archived_tag_presence_or_binding_transition",
                    "downstream_applicability": "requires_reference_identity_channel_and_risk_set_join",
                    "latency_model": "interval_only_subject_to_unobserved_reversion_sensitivity",
                    "opportunity_candidate": not alias_only,
                }
                events.append(row)
            seen |= present
            previous, incomplete = current, []
    # v-prefixed and unprefixed aliases of the same version/target/interval are
    # one candidate identity, while preserving all literal names for the join.
    merged = {}
    for event in events:
        group = (event["repository"], event["origin_url"], event["version_key"], event["target"],
                 event["available_after"], event["available_by"], event["opportunity_candidate"])
        if group not in merged:
            merged[group] = {**event, "tag_aliases": [], "alias_event_types": {}, "previous_targets_by_tag": {},
                             "tag_alias_group": hashlib.sha256(json.dumps(group).encode()).hexdigest()}
        merged[group]["tag_aliases"].append(event["tag"])
        merged[group]["alias_event_types"][event["tag"]] = event["evidence_type"]
        merged[group]["previous_targets_by_tag"][event["tag"]] = event["previous_target"]
    for event in merged.values():
        event["tag_aliases"] = sorted(set(event["tag_aliases"]))
        preferred = "v" + event["version_key"]
        event["tag"] = preferred if preferred in event["tag_aliases"] else event["tag_aliases"][0]
        event["previous_target"] = event["previous_targets_by_tag"][event["tag"]]
        kinds = set(event["alias_event_types"].values())
        event["evidence_type"] = next(kind for kind in ["observed_binding_change", "observed_tag_reintroduction",
                                                       "observed_tag_introduction", "observed_tag_alias_introduction"] if kind in kinds)
    rows = list(merged.values())
    return {"opportunities": [row for row in rows if row["opportunity_candidate"]],
            "tag_events": rows, "initial_observed_availability": initial, "snapshot_gaps": gaps}


def archived_changes(observations):
    """Return intervals with different observed bindings; never exact release dates.

    An interval proves at least one change between visits. It does not prove the
    target's first publication date or rule out intervening tag reversions.
    """
    groups = {}
    for item in observations:
        if grade_evidence(item) == "B":
            groups.setdefault((item["repository"], item["tag"]), []).append(item)
    output = []
    for (repository, tag), rows in sorted(groups.items()):
        rows.sort(key=lambda row: (row["visit_date"], row["snapshot"]))
        for before, after in zip(rows, rows[1:]):
            if before["visit_date"] == after["visit_date"] or before["target"] == after["target"]:
                continue
            output.append({
                "repository": repository, "tag": tag, "channel": channel(tag),
                "evidence_grade": "B", "evidence_type": "observed_binding_change",
                "previous_target": before["target"], "target": after["target"],
                "available_after": before["visit_date"], "available_by": after["visit_date"],
                "lower_snapshot": before["snapshot"], "upper_snapshot": after["snapshot"],
                "lower_url": before["source_url"], "upper_url": after["source_url"],
                "exact_publication_identified": False,
                "latency_model": "interval_only_subject_to_unobserved_reversion_sensitivity",
            })
    return output


def delay_bounds(updated_at, opportunity):
    """Nonnegative elapsed-day bounds, or None for C/inapplicable evidence."""
    def date(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if opportunity.get("evidence_grade") == "A":
        lower = upper = date(opportunity["available_at"])
    elif opportunity.get("evidence_grade") == "B" and opportunity.get("available_after"):
        lower, upper = date(opportunity["available_after"]), date(opportunity["available_by"])
    else:
        return None
    updated = date(updated_at)
    if updated < lower:
        return None
    return {"lower_days": max(0, (updated - upper).total_seconds() / 86400),
            "upper_days": (updated - lower).total_seconds() / 86400,
            "evidence_grade": opportunity["evidence_grade"]}


class Archive:
    def __init__(self, cache, cache_only=False, max_requests=None):
        self.cache = Path(cache)
        self.lock = threading.Lock()
        self.last_request = 0
        self.unavailable_until = 0
        self.cache_only = cache_only
        self.max_requests = max_requests
        self.requests_issued = 0
        self.last_rate_limit = None

    def fetch(self, url):
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if self.cache_only:
            raise RuntimeError("Archive object not cached; absence is unknown: " + url)
        with self.lock:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            if self.unavailable_until > time.time():
                raise RuntimeError(f"Archive rate limit active until UTC epoch {self.unavailable_until}; cached evidence retained")
            for attempt in range(4):
                if self.max_requests is not None and self.requests_issued >= self.max_requests:
                    raise RuntimeError("Archive request budget exhausted; missing evidence remains unknown")
                time.sleep(max(0, self.last_request + 0.8 - time.monotonic()))
                self.requests_issued += 1
                response = requests.get(url, headers={"User-Agent": "PinTrace upstream evidence study"}, timeout=(15, 45))
                self.last_request = time.monotonic()
                if response.status_code == 429:
                    reset = response.headers.get("X-Ratelimit-Reset", "")
                    retry = response.headers.get("Retry-After", "")
                    self.unavailable_until = max(time.time() + 1, int(reset) if reset.isdigit() else
                                                 time.time() + (int(retry) if retry.isdigit() else 60))
                    self.last_rate_limit = {"unavailable_until": self.unavailable_until, "url": url,
                                            "status_code": 429, "headers": dict(response.headers),
                                            "body": response.text[:2000]}
                    raise RuntimeError(f"Archive rate limit active until UTC epoch {self.unavailable_until}: {response.text[:300]}")
                if response.status_code in (500, 502, 503, 504) and attempt < 3:
                    retry = response.headers.get("Retry-After", "")
                    time.sleep(min(60, int(retry) if retry.isdigit() else 10 * (attempt + 1)))
                    continue
                response.raise_for_status()
                record = {"url": url, "retrieved_at": response.headers.get("Date"),
                          "next": response.links.get("next", {}).get("url"), "data": response.json()}
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(record, indent=2), encoding="utf-8")
                return record

    def visits(self, repository, start, end):
        url = SWH + "origin/https://github.com/" + repository + "/visits/?per_page=100"
        rows, pages = [], 0
        while url:
            result = self.fetch(url)
            rows.extend(result["data"])
            pages += 1
            if not result["next"] or not result["data"] or min(r["date"] for r in result["data"]) < start:
                break
            url = result["next"]
        full = [r for r in rows if r.get("snapshot") and r.get("status") == "full" and r["date"] <= end]
        before = [r for r in full if r["date"] < start]
        selected = [r for r in full if start <= r["date"] <= end]
        if before:
            selected.append(max(before, key=lambda r: r["date"]))
        return selected, {"listed_visits": len(rows), "pages": pages,
                          "origin_listing_complete": result["next"] is None,
                          "listing_reaches_window_start": bool(before) or result["next"] is None,
                          "origin_aliases_exhaustively_checked": False}

    def observation(self, repository, visit):
        observations = []
        state = {"repository": repository, "origin_url": "https://github.com/" + repository,
                 "snapshot": visit["snapshot"], "visit_date": visit["date"],
                 "visit_status": visit.get("status"), "complete": False, "tag_listing_complete": False,
                 "tag_names": [], "bindings": {}, "binding_details": {}, "source_urls": [], "errors": []}
        query = {"branches_from": "refs/tags/", "branches_count": 1000}
        url = SWH + "snapshot/" + visit["snapshot"] + "/?" + urlencode(query)
        state["source_url"] = url
        identities = {}
        try:
            while url:
                if url in state["source_urls"]:
                    raise ValueError("Repeated archive snapshot pagination cursor")
                result = self.fetch(url)
                state["source_urls"].append(url)
                branches = result["data"]["branches"]
                if not isinstance(branches, dict):
                    raise ValueError("Archive snapshot branches are not a mapping")
                for branch, identity in branches.items():
                    if branch.startswith("refs/tags/"):
                        tag = branch[len("refs/tags/"):]
                        if tag in identities and identities[tag] != identity:
                            raise ValueError("Conflicting bindings inside one immutable snapshot")
                        identities[tag] = identity
                next_branch = result["data"].get("next_branch")
                if next_branch and str(next_branch).startswith("refs/tags/"):
                    query["branches_from"] = next_branch
                    url = SWH + "snapshot/" + visit["snapshot"] + "/?" + urlencode(query)
                else:
                    url = None
            state["tag_listing_complete"] = True
        except Exception as error:
            state["errors"].append({"stage": "tag_listing", "url": url, "error": str(error)})
        state["tag_names"] = sorted(identities)
        for tag, identity in identities.items():
            if not VERSION.fullmatch(tag):
                continue
            detail = {"raw_identity": identity, "resolution_status": "unresolved", "peel_urls": []}
            state["binding_details"][tag] = detail
            if not isinstance(identity, dict):
                continue
            target, kind = identity.get("target"), identity.get("target_type")
            try:
                for _ in range(8):
                    if kind != "release" or not SHA.fullmatch(target or ""):
                        break
                    peel_url = SWH + "release/" + target + "/"
                    peeled = self.fetch(peel_url)["data"]
                    detail["peel_urls"].append(peel_url)
                    target, kind = peeled.get("target"), peeled.get("target_type")
                if kind == "revision" and SHA.fullmatch(target or ""):
                    detail.update(resolution_status="resolved_revision", target=target)
                    state["bindings"][tag] = target
                    observations.append({
                        "repository": repository, "tag": tag, "channel": channel(tag),
                        "target": target, "evidence_type": "archive_snapshot", "evidence_grade": "B",
                        "visit_date": visit["date"], "snapshot": visit["snapshot"],
                        "source_url": state["source_url"], "peel_urls": detail["peel_urls"],
                        "exact_publication_identified": False,
                    })
                else:
                    detail.update(resolution_status="unsupported_or_unresolved_identity", target_type=kind)
            except Exception as error:
                detail["error"] = str(error)
                state["errors"].append({"stage": "target_resolution", "tag": tag, "error": str(error)})
        state["complete"] = state["tag_listing_complete"] and not state["errors"] and visit.get("status") == "full"
        return {"observations": observations, "snapshot_visit": state}


def release_observations(client, repository, start, end, audit=None):
    rows, page, examined = [], 1, 0
    while True:
        releases = client.api(f"repos/{repository}/releases", per_page=100, page=page)
        examined += len(releases)
        for release in releases:
            published = release.get("published_at")
            if not published or not start <= published <= end or release.get("draft"):
                continue
            record = {"repository": repository, "tag": release["tag_name"],
                      "channel": channel(release["tag_name"]), "published_at": published,
                      "immutable": release.get("immutable") is True, "prerelease": release["prerelease"],
                      "release_id": release["id"], "source_url": release["html_url"],
                      "release_api_url": release["url"],
                      "tag_ref_url": f"https://api.github.com/repos/{repository}/git/ref/tags/" + quote(release["tag_name"], safe=""),
                      "target": None, "evidence_type": "current_release_listing"}
            try:
                ref = client.api(f"repos/{repository}/git/ref/tags/" + quote(release["tag_name"], safe=""))
                obj = ref["object"]
                for _ in range(8):
                    if obj["type"] != "tag":
                        break
                    obj = client.api(f"repos/{repository}/git/tags/{obj['sha']}")["object"]
                if obj["type"] == "commit":
                    record["target"] = obj["sha"]
                    if record["immutable"]:
                        record["evidence_type"] = "immutable_release"
                        record["immutable_since"] = "publication_under_GitHub_immutable_release_semantics"
                        record["immutability_rule_url"] = "https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes"
            except requests.RequestException as error:
                record["target_error"] = str(error)
            record["evidence_grade"] = grade_evidence(record)
            rows.append(record)
        if len(releases) < 100:
            break
        page += 1
    if audit is not None:
        audit.update(release_pages=page, release_records_examined=examined,
                     release_listing_complete=True, release_listing_truncated=False)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--repositories", nargs="+", default=["actions/checkout", "tj-actions/changed-files", "actions/setup-node"])
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--cached-archives-only", action="store_true", help="Derive archive evidence without new SWH requests")
    parser.add_argument("--archive-max-requests", type=int, default=90, help="Bound new SWH requests, including retries, per run")
    args = parser.parse_args()
    root = args.root
    study = tomllib.loads((root / "study.toml").read_text(encoding="utf-8"))["study"]
    archive = Archive(root / "data/cache/softwareheritage/requests", cache_only=args.cached_archives_only,
                      max_requests=args.archive_max_requests)
    throttle = root / "data/cache/softwareheritage/rate-limit.json"
    if throttle.exists():
        throttle_data = json.loads(throttle.read_text(encoding="utf-8"))
        archive.unavailable_until = throttle_data.get("unavailable_until", 0)
        archive.last_rate_limit = throttle_data
    client = GitHub(root / "data/cache", github_token())
    output = root / "data/derived/upstream-opportunities"
    output.mkdir(parents=True, exist_ok=True)
    prior_report = root / "reports/opportunity_coverage.json"
    if prior_report.exists() and json.loads(prior_report.read_text(encoding="utf-8")).get("rule_version") == "opportunities-v1":
        preserved = root / "reports/opportunity_coverage_v1.json"
        if not preserved.exists():
            shutil.copyfile(prior_report, preserved)
    summaries = []
    for repository in args.repositories:
        filename = repository.replace("/", "__") + ".json"
        previous_payload = output / filename
        if previous_payload.exists() and "snapshot_visits" not in json.loads(previous_payload.read_text(encoding="utf-8")):
            preserved = output / "diagnostics-v1" / filename
            if not preserved.exists():
                preserved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(previous_payload, preserved)
        item = {"repository": repository, "errors": []}
        observations, snapshot_visits = [], []
        try:
            visits, coverage = archive.visits(repository, study["window_start"], study["window_end"])
            item.update(coverage)
            # Fetch boundary/anchor observations first when a bounded network
            # round cannot fill every historical visit. Existing cache is free.
            bounds = [study["window_start"], study["event_anchor"], study["window_end"]]
            priority = {min(visits, key=lambda row: abs(datetime.fromisoformat(row["date"]) -
                            datetime.fromisoformat(bound.replace("Z", "+00:00"))))["snapshot"]
                        for bound in bounds} if visits else set()
            visits.sort(key=lambda row: (row["snapshot"] not in priority, row["date"]))
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = [(visit, pool.submit(archive.observation, repository, visit)) for visit in visits]
                for visit, future in futures:
                    try:
                        result = future.result()
                        observations.extend(result["observations"])
                        snapshot_visits.append(result["snapshot_visit"])
                        item["errors"].extend({"snapshot": visit["snapshot"], **error}
                                              for error in result["snapshot_visit"]["errors"])
                    except Exception as error:
                        item["errors"].append({"snapshot": visit["snapshot"], "error": str(error)})
        except Exception as error:
            item["errors"].append({"stage": "archive_visits", "error": str(error)})
        releases = release_observations(client, repository, study["window_start"], study["window_end"], item)
        derived = archived_opportunities(snapshot_visits)
        intervals = derived["opportunities"]
        exact = [{**r, "available_at": r["published_at"], "latency_model": "point",
                  "availability_scope": "release_tag_publication_not_first_commit_availability"}
                 for r in releases if r["evidence_grade"] == "A" and not r["prerelease"]]
        item.update(archive_bindings=len(observations), archived_tags=len({r["tag"] for r in observations}),
                    archive_complete_visits=sum(row["complete"] for row in snapshot_visits),
                    archive_incomplete_visits=sum(not row["complete"] for row in snapshot_visits),
                    release_grade_counts=dict(Counter(r["evidence_grade"] for r in releases)),
                    observed_binding_change_intervals=sum(row["evidence_type"] == "observed_binding_change" for row in intervals),
                    observed_tag_introduction_intervals=sum(row["evidence_type"] == "observed_tag_introduction" for row in intervals),
                    observed_tag_reintroduction_intervals=sum(row["evidence_type"] == "observed_tag_reintroduction" for row in intervals),
                    alias_introduction_diagnostics=sum(not row["opportunity_candidate"] for row in derived["tag_events"]),
                    initial_availability_records=len(derived["initial_observed_availability"]),
                    archive_opportunity_intervals=len(intervals), exact_release_opportunities=len(exact))
        payload = {"rule_version": "opportunities-v2", "summary": item, "observations": observations,
                   "snapshot_visits": snapshot_visits, "release_records": releases,
                   "opportunities": exact + intervals, "tag_events": derived["tag_events"],
                   "initial_observed_availability": derived["initial_observed_availability"],
                   "snapshot_gaps": derived["snapshot_gaps"]}
        (output / filename).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        summaries.append(item)
        print(json.dumps({key: item[key] for key in ["repository", "archive_bindings", "release_grade_counts",
                                                    "observed_binding_change_intervals", "observed_tag_introduction_intervals",
                                                    "observed_tag_reintroduction_intervals", "exact_release_opportunities"]}
                         | {"evidence_gaps": len(item["errors"])}), flush=True)
    report = {"rule_version": "opportunities-v2", "checked_at": datetime.now(timezone.utc).isoformat(),
              "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "archive_requests_issued": archive.requests_issued, "archive_request_budget": archive.max_requests,
              "archive_cache_only": archive.cache_only,
              "window": study, "upstreams": summaries,
              "limits": ["Archived observations are not exact tag publication times.",
                         "Different bindings bracket at least one change; first availability and unseen reversions remain unknown.",
                         "New-tag presence intervals require complete same-origin snapshots; unresolved target presence is never treated as absence.",
                         "Tag aliases for the same version/target/interval are grouped; new tag identities are not automatically applicable downstream upgrades.",
                         "Non-immutable release timestamps plus current tags are grade C and excluded from delay models.",
                         "Cohort-wide frequency and risk-set coverage must be joined after the complete cohort is available."]}
    if archive.unavailable_until > time.time():
        throttle.parent.mkdir(parents=True, exist_ok=True)
        throttle.write_text(json.dumps(archive.last_rate_limit or {"unavailable_until": archive.unavailable_until}), encoding="utf-8")
    (root / "reports/opportunity_coverage.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
