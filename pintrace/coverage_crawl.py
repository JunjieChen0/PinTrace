"""Crawl the public dependents list ("Used by") of the target Action, slowly and resumably.

GitHub offers no API for this list, so the HTML pages are read one by one, unauthenticated, at a fixed delay. Each
raw page is kept gzipped with its SHA-256, so the parsed list can be re-derived. The list shows current dependents
only; repositories that removed the Action are absent from it.

Usage: python -m pintrace.coverage_crawl [--resume data/derived/coverage-v1/crawl-<stamp>] [--delay 2.5]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

RULE_VERSION = "coverage-crawl-v1"
BASE = "https://github.com/tj-actions/changed-files/network/dependents"
OUTPUT = "data/derived/coverage-v1"
HEADERS = {"User-Agent": "PinTrace research pilot"}
ROW = re.compile(r'data-test-id="dg-repo-pkg-dependent"(.*?)(?=data-test-id="dg-repo-pkg-dependent"|<div class="paginate-container|\Z)', re.S)
REPOSITORY = re.compile(r'data-hovercard-type="repository"[^>]*href="/([^"/]+/[^"/]+)"')
NEXT = re.compile(r'<a[^>]*href="([^"]*dependents_after=[^"]*)"[^>]*>\s*Next\s*</a>')
TOTAL = re.compile(r'([\d,]+)\s+Repositories')
BACKOFF_SECONDS = 60
MAX_ATTEMPTS = 5


def parse_page(page):
    """(repositories in page order, absolute URL of the next page or None, total shown or None)."""
    repositories = []
    for block in ROW.findall(page):
        match = REPOSITORY.search(block)
        if match:
            repositories.append(match.group(1))
    following = NEXT.search(page)
    total = TOTAL.search(page)
    nxt = html.unescape(following.group(1)) if following else None
    if nxt and nxt.startswith("/"):
        nxt = "https://github.com" + nxt
    return repositories, nxt, int(total.group(1).replace(",", "")) if total else None


def fetch(session, url, delay):
    """The page body; waits ``delay`` before each request and backs off on 429 or 5xx."""
    for attempt in range(MAX_ATTEMPTS):
        time.sleep(delay if attempt == 0 else BACKOFF_SECONDS * attempt)
        response = session.get(url, headers=HEADERS, timeout=60)
        if response.status_code == 200:
            return response.text
        if response.status_code != 429 and response.status_code < 500:
            response.raise_for_status()
    raise RuntimeError(f"Gave up after {MAX_ATTEMPTS} attempts: {url}")


def resume_point(directory):
    """(next page index, next URL) from an existing crawl directory; (0, BASE) for a new one."""
    pages = directory / "pages.jsonl"
    if not pages.exists():
        return 0, BASE
    lines = [json.loads(line) for line in pages.read_text(encoding="utf-8").splitlines() if line.strip()]
    return (len(lines), lines[-1]["next"]) if lines else (0, BASE)


def crawl(directory, delay=2.5, limit=None, session=None):
    """Append pages to ``directory`` until the list ends (or ``limit`` pages this run). Returns pages written."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "raw").mkdir(exist_ok=True)
    session = session or requests.Session()
    index, url = resume_point(directory)
    written = 0
    new_list = not (directory / "dependents.csv").exists()
    with (directory / "pages.jsonl").open("a", encoding="utf-8") as pages, \
            (directory / "dependents.csv").open("a", encoding="utf-8", newline="") as rows:
        table = csv.writer(rows)
        if new_list:
            table.writerow(["page", "position", "repository"])
        while url and (limit is None or written < limit):
            body = fetch(session, url, delay)
            raw = body.encode("utf-8")
            repositories, following, total = parse_page(body)
            if not repositories:
                raise RuntimeError(f"No dependents parsed on page {index}: {url}")
            (directory / "raw" / f"{index:05d}.html.gz").write_bytes(gzip.compress(raw, mtime=0))
            table.writerows([index, position, name] for position, name in enumerate(repositories))
            pages.write(json.dumps(dict(page=index, url=url, sha256=hashlib.sha256(raw).hexdigest(), rows=len(repositories),
                                        total_shown=total, next=following,
                                        fetched_at_utc=datetime.now(timezone.utc).isoformat())) + "\n")
            pages.flush()
            rows.flush()
            index, url, written = index + 1, following, written + 1
    if url is None:
        (directory / "complete.json").write_text(json.dumps(dict(rule_version=RULE_VERSION, pages=index,
                                                                 completed_at_utc=datetime.now(timezone.utc).isoformat()),
                                                            indent=2), encoding="utf-8")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", help="existing crawl directory to continue")
    parser.add_argument("--delay", type=float, default=2.5)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.delay < 1:
        parser.error("delay must be at least 1 second")
    root = Path(__file__).resolve().parents[1]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(args.resume) if args.resume else root / OUTPUT / f"crawl-{stamp}"
    written = crawl(directory if directory.is_absolute() else root / directory, args.delay, args.limit)
    print(json.dumps(dict(directory=str(directory), pages_written=written)), flush=True)


if __name__ == "__main__":
    main()
