"""Audit necessary historical-entity eligibility without rewriting the raw cohort."""

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import tomllib


RULE_VERSION = "entity-identity-v1"
ROSTERS = ("data/raw/zenodo-17301952/repositories.csv.gz",
           "data/raw/source-audit-20261018/13985548-repositories.csv.gz",
           "data/raw/source-audit-20261018/20340547-repositories.csv.gz")
FLOW = "reports/cohort_flow.csv"
CANDIDATES = "reports/candidate_source_membership.csv"
OUTPUT_FLOW = "reports/entity_identity_flow.csv"
OUTPUT_SUMMARY = "reports/entity_identity_summary.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("API creation timestamp requires a timezone")
    return parsed.astimezone(timezone.utc)


def classify_group(rows, births, metadata, anchor):
    """Check every alias/source value; Git ancestry and fork parents never assign identity."""
    aliases = sorted({row["repository"].lower() for row in rows})
    identity = str(rows[0].get("repository_id") or "")
    evidence = [item for name in aliases for item in births.get(name, [])]
    reasons, source_dates, clocks, differences = [], [], [], []
    if identity:
        for name in aliases:
            if not births.get(name):
                reasons.append("source_created_missing:" + name)
        for item in evidence:
            try:
                source_dates.append((item, datetime.fromisoformat(item["created"]).date()))
            except (TypeError, ValueError):
                reasons.append("source_created_invalid:" + item["repository"])
        if not metadata:
            reasons.append("current_metadata_missing")
        for item in metadata:
            try:
                clocks.append(_timestamp(item.get("created_at")))
            except (AttributeError, TypeError, ValueError):
                reasons.append("current_created_invalid")
        signatures = {(str(item.get("id")), str(item.get("full_name", "")).lower(),
                       item.get("fork"), str((item.get("parent") or {}).get("id")),
                       str((item.get("source") or {}).get("id"))) for item in metadata}
        if len(signatures) > 1 or len(set(clocks)) > 1:
            reasons.append("current_metadata_conflict")
        if any(not item.get("full_name") or str(item.get("id")) != identity for item in metadata):
            reasons.append("current_metadata_identity_invalid")
        if any(clock >= _timestamp(anchor) for clock in clocks):
            reasons.append("current_entity_created_not_before_anchor")
        for item, date in source_dates:
            for current_date in sorted({clock.date() for clock in clocks}):
                delta = (date - current_date).days
                differences.append(dict(repository=item["repository"], source_path=item["source_path"],
                                        source_row=item["source_row"], source_date=date.isoformat(),
                                        current_date=current_date.isoformat(), days=delta))
                if abs(delta) > 1:
                    reasons.append("source_current_birth_date_conflict")
    else:
        reasons.append("current_repository_unavailable")
    passed = bool(identity) and not reasons
    return dict(identity_state="necessary_identity_checks_passed" if passed else
                "historical_identity_unknown" if identity else "inaccessible_unknown",
                explicit_reasons=sorted(set(reasons)), aliases=aliases,
                current_created_at=clocks[0].isoformat() if clocks and len(set(clocks)) == 1 else None,
                current_created_values=[item.get("created_at") for item in metadata],
                source_birth_values=evidence, source_birth_dates=sorted({date.isoformat() for _, date in source_dates}),
                date_differences_days=differences, metadata_evidence=metadata,
                evidence_paths=sorted({item["source_path"] for item in evidence} |
                                      {item["path"] for item in metadata}))


def audit(root):
    root = Path(root)
    inputs, problems = {}, []
    implementation = {"pintrace/entity_identity.py": digest(Path(__file__))}

    def read(name):
        data = (root / name).read_bytes()
        inputs[name] = hashlib.sha256(data).hexdigest()
        return data

    def read_csv(name):
        return list(csv.DictReader(io.StringIO(read(name).decode("utf-8-sig"))))

    flow, candidates = read_csv(FLOW), read_csv(CANDIDATES)
    anchor = tomllib.loads(read("study.toml").decode("utf-8"))["study"]["event_anchor"]
    names = {row["repository"].lower() for row in flow}
    candidate_names = {row["repository"].lower() for row in candidates}
    births = defaultdict(list)
    for name in ROSTERS:
        text = gzip.decompress(read(name)).decode("utf-8-sig")
        for number, row in enumerate(csv.DictReader(io.StringIO(text)), 2):
            if row["name"].lower() in names:
                births[row["name"].lower()].append(dict(repository=row["name"], created=row.get("created"),
                                                       source_path=name, source_row=number))
    groups = defaultdict(list)
    for row in flow:
        groups[row.get("repository_id") or "unavailable:" + row["repository"].lower()].append(row)
    wanted = {row["repository_id"] for row in flow if row.get("repository_id")}
    metadata = defaultdict(list)
    api_paths = sorted((root / "data/cache/api").glob("*.json"))
    for path in api_paths:
        try:
            raw = path.read_bytes()
            cached = json.loads(raw)
            data = cached.get("data")
            if not isinstance(data, dict) or "full_name" not in data or str(data.get("id")) not in wanted:
                continue
            name = path.relative_to(root).as_posix()
            inputs[name] = hashlib.sha256(raw).hexdigest()
            item = {key: data.get(key) for key in ("id", "full_name", "created_at", "fork")}
            for key in ("parent", "source"):
                value = data.get(key)
                item[key] = ({field: value.get(field) for field in ("id", "full_name", "created_at")}
                             if isinstance(value, dict) else None)
            metadata[str(data["id"])].append(dict(item, path=name, url=cached.get("url"),
                                                retrieved_at=cached.get("retrieved_at")))
        except (OSError, ValueError, AttributeError) as error:
            problems.append(dict(path=path.relative_to(root).as_posix(), reason="unreadable_api_cache",
                                 error=type(error).__name__))
    rows = []
    for group in groups.values():
        identity = group[0].get("repository_id") or ""
        result = classify_group(group, births, metadata.get(identity, []), anchor)
        for row in group:
            raw_status = row["status"]
            main = ("inaccessible_unknown" if raw_status == "inaccessible_unknown" else
                    "historical_identity_unknown" if result["identity_state"] != "necessary_identity_checks_passed"
                    else raw_status)
            rows.append(dict(repository=row["repository"], repository_id=identity, current_id=identity,
                             raw_status=raw_status, main_status=main,
                             formal_eligible=raw_status == "confirmed" and main == "confirmed", **result))
    rows.sort(key=lambda row: row["repository"].lower())
    unchanged = all((root / name).exists() and digest(root / name) == value for name, value in inputs.items())
    unchanged = unchanged and api_paths == sorted((root / "data/cache/api").glob("*.json"))
    unchanged = unchanged and digest(Path(__file__)) == implementation["pintrace/entity_identity.py"]
    if len(flow) != len(names) or len(candidates) != len(candidate_names) or names != candidate_names:
        problems.append(dict(reason="candidate_flow_name_coverage_differs"))
    if any(row["raw_status"] not in {"confirmed", "not_at_baseline", "parse_incomplete", "inaccessible_unknown"}
           or (not row["repository_id"] and row["raw_status"] != "inaccessible_unknown") for row in rows):
        problems.append(dict(reason="raw_cohort_status_or_identity_unresolved"))
    raw_ids = sorted({row["repository_id"] for row in rows if row["raw_status"] == "confirmed"})
    eligible = sorted({row["repository_id"] for row in rows if row["formal_eligible"]})
    report = dict(rule_version=RULE_VERSION, checked_at_utc=datetime.now(timezone.utc).isoformat(),
                  event_anchor_utc=anchor, candidate_names=len(candidates), audited_names=len(rows),
                  assessed_repository_ids=sorted(wanted), observed_unique_repository_ids=len(wanted),
                  metadata_repository_ids=len(metadata), raw_status_counts=dict(Counter(row["raw_status"] for row in rows)),
                  main_status_counts=dict(Counter(row["main_status"] for row in rows)),
                  identity_state_counts=dict(Counter(row["identity_state"] for row in rows)),
                  raw_confirmed_repository_ids=raw_ids, formal_eligible_repository_ids=eligible,
                  excluded_confirmed_repository_ids=sorted(set(raw_ids) - set(eligible)),
                  audit_passed=len(rows) == len(candidates) == len(names) == 580 and len(wanted) == 549
                               and unchanged and not problems,
                  inputs_unchanged=unchanged, input_sha256=inputs, implementation_sha256=implementation,
                  problems=problems,
                  eligibility_scope="Necessary temporal/entity checks only; no complete historical name or branch guarantee. "
                                    "Current fork parents and inherited Git history do not reassign historical identities.")
    return report, rows


def write_reports(root, report, rows):
    path = root / OUTPUT_FLOW
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["repository"])
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, sort_keys=True)
                             if isinstance(value, (list, dict)) else value for key, value in row.items()})
    report["flow"] = dict(path=OUTPUT_FLOW, sha256=digest(path), row_count=len(rows))
    (root / OUTPUT_SUMMARY).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    root = Path(__file__).resolve().parents[1]
    report, rows = audit(root)
    write_reports(root, report, rows)
    print(json.dumps({key: report[key] for key in ("rule_version", "audit_passed", "main_status_counts",
                                                  "excluded_confirmed_repository_ids", "flow")}, indent=2))
    raise SystemExit(0 if report["audit_passed"] else 1)


if __name__ == "__main__":
    main()
