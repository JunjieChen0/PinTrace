"""Record the actual complete test run and bind every current source file."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    files = sorted({*ROOT.glob("pintrace/*.py"), *ROOT.glob("tests/*.py"), *ROOT.glob("scripts/*.py"),
                    ROOT / "pyproject.toml", ROOT / "study.toml", ROOT / "requirements-formal.txt"})
    before = {path.relative_to(ROOT).as_posix(): sha(path) for path in files}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "data/derived/formal-stats-v1/test-runs" / stamp
    directory.mkdir(parents=True, exist_ok=False)
    junit, log = directory / "junit.xml", directory / "pytest.log"
    command = [sys.executable, "-B", "-X", "utf8", "-m", "pytest", "-q", "--junitxml=" + str(junit)]
    started = datetime.now(timezone.utc).isoformat()
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, check=False)
    suites = ET.parse(junit).getroot()
    counts = {name: sum(int(node.get(name, "0")) for node in suites.iter("testsuite"))
              for name in ("tests", "failures", "errors", "skipped")}
    changed = [name for name, digest in before.items() if sha(ROOT / name) != digest]
    report = dict(rule_version="formal-test-run-v1", passed=result.returncode == 0 and counts["tests"] > 0
                  and not any(counts[name] for name in ("failures", "errors", "skipped")) and not changed,
                  started_at_utc=started, ended_at_utc=datetime.now(timezone.utc).isoformat(), command=command,
                  returncode=result.returncode, counts=counts, source_sha256=before, inputs_unchanged=not changed,
                  changed_sources=changed, junit=dict(path=junit.relative_to(ROOT).as_posix(), sha256=sha(junit)),
                  log=dict(path=log.relative_to(ROOT).as_posix(), sha256=sha(log)))
    (ROOT / "reports").mkdir(parents=True, exist_ok=True)
    (ROOT / "reports/formal_test_results.json").write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(log.read_text(encoding="utf-8"), end="")
    print(json.dumps({"passed": report["passed"], "counts": counts, "changed_sources": changed}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
