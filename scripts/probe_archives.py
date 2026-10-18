"""Probe three upstreams for dated Software Heritage tag evidence."""

import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/cache/softwareheritage/requests"
BASE = "https://archive.softwareheritage.org/api/1/"


def run():
    config = tomllib.loads((ROOT / "study.toml").read_text(encoding="utf-8"))["study"]
    session = requests.Session()
    CACHE.mkdir(parents=True, exist_ok=True)

    def fetch(url):
        path = CACHE / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        response = session.get(url, timeout=(15, 45))
        response.raise_for_status()
        result = {"url": url, "retrieved_at": response.headers.get("Date"),
                  "next": response.links.get("next", {}).get("url"), "data": response.json()}
        path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    results = []
    for repository, tag in [("actions/checkout", "v4"), ("actions/setup-node", "v4"), ("tj-actions/changed-files", "v45")]:
        item = {"repository": repository, "tag": tag, "update_latency_identified": False,
                "origin_aliases_exhaustively_checked": False, "probes": []}
        try:
            listing = fetch(BASE + "origin/https://github.com/" + repository + "/visits/?per_page=100")
            visits = listing["data"]
            full = [v for v in visits if v.get("snapshot") and v.get("status") == "full"]
            item.update(listed_visits=len(visits),
                        listing_reaches_window_start=not listing["next"] or min(v["date"] for v in visits) <= config["window_start"],
                        origin_listing_complete=listing["next"] is None,
                        oldest_listed_visit=min(v["date"] for v in visits),
                        visits_in_window=sum(config["window_start"] <= v["date"] <= config["window_end"] for v in full))
            for name, bound in [("pre_anchor", config["event_anchor"]), ("at_window_end", config["window_end"])]:
                prior = [v for v in full if v["date"] < bound]
                probe = {"point": name, "bound": bound, "tag_binding_available": False}
                if not prior:
                    probe["reason"] = "No earlier full visit in the queried origin listing"
                    item["probes"].append(probe)
                    continue
                visit = max(prior, key=lambda v: v["date"])
                branch = "refs/tags/" + tag
                url = BASE + "snapshot/" + visit["snapshot"] + "/?" + urlencode({"branches_from": branch, "branches_count": 1})
                snapshot = fetch(url)["data"]
                ref = snapshot.get("branches", {}).get(branch)
                probe.update(visit_date=visit["date"], snapshot=visit["snapshot"], snapshot_url=url,
                             archival_gap_days=(datetime.fromisoformat(bound.replace("Z", "+00:00")) - datetime.fromisoformat(visit["date"])).total_seconds() / 86400)
                if ref:
                    target, kind = ref.get("target"), ref.get("target_type")
                    for _ in range(5):
                        if kind != "release" or not re.fullmatch(r"[0-9a-f]{40}", target or ""):
                            break
                        release = fetch(BASE + "release/" + target + "/")["data"]
                        target, kind = release.get("target"), release.get("target_type")
                    probe.update(target_type=kind, target=target,
                                 tag_binding_available=kind == "revision" and bool(re.fullmatch(r"[0-9a-f]{40}", target or "")))
                else:
                    probe["reason"] = "Requested tag absent in this archived snapshot"
                item["probes"].append(probe)
        except Exception as error:
            item["error"] = f"{type(error).__name__}: {error}"
        results.append(item)
        print(repository, [(p["point"], p["tag_binding_available"]) for p in item["probes"]], flush=True)
    report = {"retrieved_at_utc": datetime.now(timezone.utc).isoformat(), "upstreams": results,
              "limits": ["Archive visits are observations, not exact publication-time tag mappings.",
                         "Only the specified origin URLs were queried; alternate origins may have other coverage.",
                         "No update-lag estimate has been identified by this probe."]}
    (ROOT / "reports/software_heritage_feasibility.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run()

