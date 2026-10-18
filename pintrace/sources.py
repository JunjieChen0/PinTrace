"""Freeze source metadata and inspect the JSS package without running its code."""

import argparse
import hashlib
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

RECORDS = ["17301952", "13985548", "20340547", "18414913"]


def freeze_record(session, root, record):
    path = root / (record + ".json")
    if not path.exists():
        response = session.get("https://zenodo.org/api/records/" + record, timeout=(15, 60))
        response.raise_for_status()
        with path.open("x", encoding="utf-8") as output:
            json.dump(response.json(), output, indent=2)
    return json.loads(path.read_text(encoding="utf-8"))


def inspect_notebook(content):
    notebook = json.loads(content)
    sources = ["".join(cell.get("source", [])) for cell in notebook.get("cells", [])
               if cell.get("cell_type") == "code"]
    return [line.strip() for source in sources for line in source.splitlines()
            if "read_csv(" in line or "read_parquet(" in line]


def input_gaps(names, input_lines):
    filenames = {Path(name).name for name in names}
    return sorted({filename for filename in ["corrected_workflows.csv.zst", "corrected_workflows.csv.gz"]
                   if any(filename in line and not line.startswith("#") for line in input_lines)
                   and filename not in filenames})


def download_zip(session, item, path):
    if path.exists() and path.stat().st_size < item["size"]:
        raise ValueError("Incomplete cache retained; choose a new --batch rather than overwrite " + str(path))
    if not path.exists():
        with session.get(item["links"]["self"], stream=True, timeout=(15, 90)) as response:
            response.raise_for_status()
            with path.open("xb") as output:
                total, next_update = 0, 100 * 1024 * 1024
                for chunk in response.iter_content(4 * 1024 * 1024):
                    output.write(chunk)
                    total += len(chunk)
                    if total >= next_update:
                        print(f"JSS downloaded {total} / {item['size']} bytes", flush=True)
                        next_update += 100 * 1024 * 1024
    with path.open("rb") as source:
        checksum = "md5:" + hashlib.file_digest(source, "md5").hexdigest()
    if path.stat().st_size != item["size"] or checksum != item["checksum"]:
        raise ValueError("JSS archive checksum failed; file retained")
    return checksum


def inspect_jss(path, output):
    import pyarrow
    import pyarrow.parquet as parquet

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        notebook_inputs = {}
        schemas = {}
        for name in names:
            if name.endswith(".ipynb"):
                notebook_inputs[name] = inspect_notebook(archive.read(name))
            if name in {"code/data/all_diffs_new.parquet", "code/data/path_df.parquet"}:
                local = output / Path(name).name
                if not local.exists():
                    with archive.open(name) as source, local.open("xb") as target:
                        shutil.copyfileobj(source, target, 4 * 1024 * 1024)
                with local.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                data = parquet.ParquetFile(local)
                schemas[name] = {"rows": data.metadata.num_rows,
                                 "schema": str(data.schema_arrow), "columns": data.schema_arrow.names,
                                 "sha256": digest, "bytes": local.stat().st_size}
    gaps = input_gaps(names, [line for lines in notebook_inputs.values() for line in lines])
    return {"entry_names": names, "notebook_input_lines": notebook_inputs,
            "missing_active_notebook_inputs": gaps, "parquet": schemas,
            "pyarrow_version": pyarrow.__version__, "notebooks_executed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", default="source-audit-20261018")
    parser.add_argument("--download-jss", action="store_true")
    args = parser.parse_args()
    if Path(args.batch).name != args.batch or args.batch in {".", ".."}:
        parser.error("batch must be one directory name")
    root = Path(__file__).resolve().parents[1]
    raw = root / "data/raw" / args.batch
    raw.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = "PinTrace research source audit"
    records = {record: freeze_record(session, raw, record) for record in RECORDS}
    result = {"checked_at_utc": datetime.now(timezone.utc).isoformat(), "batch": args.batch,
              "records": {key: {"id": value["id"], "conceptrecid": value.get("conceptrecid"),
                                "title": value["metadata"]["title"], "version": value["metadata"].get("version"),
                                "files": [{k: item[k] for k in ["key", "size", "checksum"]} for item in value["files"]],
                                "metadata_sha256": hashlib.sha256((raw / (key + ".json")).read_bytes()).hexdigest()}
                          for key, value in records.items()},
              "jss_new_candidates": None, "jss_full_archive_verified": False}
    if args.download_jss:
        item = next(item for item in records["18414913"]["files"] if item["key"] == "code.zip")
        result["jss_archive_md5"] = download_zip(session, item, raw / "code.zip")
        result["jss_full_archive_verified"] = True
        result["jss"] = inspect_jss(raw / "code.zip", raw)
    result["implementation_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = root / "reports/source_package_audit.json"
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(report), "records": list(records),
                      "jss_verified": result["jss_full_archive_verified"]}), flush=True)


if __name__ == "__main__":
    main()
