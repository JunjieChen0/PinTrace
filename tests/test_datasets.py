import csv
import gzip
import hashlib
import io
import json
import tarfile

from pintrace.datasets import prepare


def test_candidates_use_pre_incident_state_not_post_incident_survival(tmp_path):
    raw = tmp_path / "data/raw/zenodo-test"
    raw.mkdir(parents=True)
    target = b"jobs: {test: {steps: [{uses: 'tj-actions/changed-files@v45'}]}}\n"
    comment = b"# uses: tj-actions/changed-files@v45\njobs: {test: {steps: [{run: 'echo ok'}]}}\n"
    target_hash, comment_hash = [hashlib.sha256(value).hexdigest() for value in (target, comment)]
    fields = ["repository", "uid", "committed_date", "commit_hash", "file_hash", "file_path", "valid_workflow"]
    rows = [
        ["a/removed-after-event", "a/ci", "1741737600", "a" * 40, target_hash, ".github/workflows/ci.yml", "True"],
        ["a/removed-after-event", "a/ci", "1742083200", "b" * 40, "--", ".github/workflows/ci.yml", "False"],
        ["b/comment-only", "b/ci", "1741737600", "c" * 40, comment_hash, ".github/workflows/ci.yml", "True"],
        ["c/introduced-after-event", "c/ci", "1742083200", "d" * 40, target_hash, ".github/workflows/ci.yml", "True"],
    ]
    with gzip.open(raw / "workflows.csv.gz", "wt", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(fields)
        writer.writerows(rows)
    with gzip.open(raw / "repositories.csv.gz", "wt", encoding="utf-8") as output:
        output.write("name,branch\na/removed-after-event,main\n")
    with tarfile.open(raw / "workflows.tar.gz", "w:gz") as archive:
        for value, digest in [(target, target_hash), (comment, comment_hash)]:
            member = tarfile.TarInfo("workflows/" + digest)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    metadata = {"files": [{"key": path.name, "size": path.stat().st_size,
                           "checksum": "md5:" + hashlib.md5(path.read_bytes()).hexdigest()}
                          for path in raw.glob("*.gz")]}
    (raw / "record.json").write_text(json.dumps(metadata), encoding="utf-8")
    config = {"dataset_record": "test", "event_anchor": "2025-03-14T00:00:00Z",
              "sampling_seed": "fixed", "pilot_size": 50}
    prepare(tmp_path, config)
    with (tmp_path / "reports/pilot_candidates.csv").open(encoding="utf-8", newline="") as source:
        selected = list(csv.DictReader(source))
    assert [row["repository"] for row in selected] == ["a/removed-after-event"]

