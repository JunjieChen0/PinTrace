"""Compare source repository names; this does not establish Action exposure."""

import csv
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import requests


def read_names(path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
        return {row["name"].lower() for row in csv.DictReader(source)}


def main():
    root = Path(__file__).resolve().parents[1]
    raw = root / "data/raw/source-audit-20261018"
    session = requests.Session()
    session.headers["User-Agent"] = "PinTrace source roster audit"
    names, files = {}, {}
    for record in ["13985548", "17301952", "20340547"]:
        metadata = json.loads((raw / (record + ".json")).read_text(encoding="utf-8"))
        item = next(item for item in metadata["files"] if item["key"] == "repositories.csv.gz")
        path = (root / "data/raw/zenodo-17301952/repositories.csv.gz" if record == "17301952"
                else raw / (record + "-repositories.csv.gz"))
        if not path.exists():
            with session.get(item["links"]["self"], stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                with path.open("xb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        output.write(chunk)
        with path.open("rb") as source:
            actual = "md5:" + hashlib.file_digest(source, "md5").hexdigest()
        if path.stat().st_size != item["size"] or actual != item["checksum"]:
            raise ValueError("Roster checksum mismatch, file retained: " + str(path))
        names[record] = read_names(path)
        files[record] = {"bytes": item["size"], "md5": actual,
                         "conceptrecid": metadata["conceptrecid"]}
    records = sorted(names)
    base = names["17301952"]
    result = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "base": "17301952",
        "source_roster_names": {record: len(values) for record, values in names.items()},
        "pairwise_intersections": {a + ":" + b: len(names[a] & names[b])
                                   for i, a in enumerate(records) for b in records[i + 1:]},
        "names_absent_from_base": {record: len(values - base) for record, values in names.items()
                                   if record != "17301952"},
        "union_names": len(set().union(*names.values())), "files": files,
        "action_candidate_union": None,
        "limitations": ["Names are not stable repository IDs; renames can create apparent differences.",
                        "These are repository rosters, not confirmed tj-actions users.",
                        "Different versions share source family 10259013.",
                        "Newer frames use post-incident availability and activity criteria."],
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    with (root / "data/derived/source_roster_membership.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["repository_name_key", "source_frames", "exposure_status"])
        for name in sorted(set().union(*names.values())):
            writer.writerow([name, ";".join(record for record in records if name in names[record]),
                             "not_checked_by_roster_audit"])
    (root / "reports/source_roster_overlap.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
