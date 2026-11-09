"""Dependents-page parsing and resumable crawling (pintrace/coverage_crawl.py) on synthetic HTML."""

import json

import pytest

from pintrace import coverage_crawl as cc


def row(owner, name):
    return (f'<div class="Box-row" data-test-id="dg-repo-pkg-dependent">'
            f'<a data-hovercard-type="user" href="/{owner}">{owner}</a> / '
            f'<a class="text-bold" data-hovercard-type="repository" data-hovercard-url="/{owner}/{name}/hovercard" '
            f'href="/{owner}/{name}">{name}</a></div>')


def page(names, cursor=None):
    following = (f'<a class="btn" href="https://github.com/tj-actions/changed-files/network/dependents?dependents_after={cursor}&amp;x=1">Next</a>'
                 if cursor else '<button disabled>Next</button>')
    return ('<a class="btn-link selected">41,038 Repositories</a>' + "".join(row(*n.split("/")) for n in names)
            + f'<div class="paginate-container"><button disabled>Previous</button>{following}</div>')


class Session:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url, headers, timeout):
        self.urls.append(url)
        body = self.pages[len(self.urls) - 1]
        return type("Response", (), dict(status_code=200, text=body, raise_for_status=lambda self: None))()


def test_parse_page_reads_rows_cursor_and_total():
    repositories, nxt, total = cc.parse_page(page(["a/one", "b/two"], cursor="Q1"))
    assert repositories == ["a/one", "b/two"] and total == 41038
    assert nxt.endswith("dependents_after=Q1&x=1")


def test_last_page_has_no_next():
    assert cc.parse_page(page(["a/one"]))[1] is None


def test_crawl_writes_pages_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.time, "sleep", lambda seconds: None)
    pages = [page(["a/one", "b/two"], "Q1"), page(["c/three"], "Q2"), page(["d/four"])]
    session = Session(pages)
    assert cc.crawl(tmp_path, delay=1, limit=1, session=session) == 1
    assert not (tmp_path / "complete.json").exists()
    session.pages = pages[1:]
    session.urls = []
    assert cc.crawl(tmp_path, delay=1, session=session) == 2
    assert session.urls[0].endswith("dependents_after=Q1&x=1")
    lines = (tmp_path / "dependents.csv").read_text(encoding="utf-8").splitlines()
    assert lines == ["page,position,repository", "0,0,a/one", "0,1,b/two", "1,0,c/three", "2,0,d/four"]
    records = [json.loads(l) for l in (tmp_path / "pages.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["page"] for r in records] == [0, 1, 2] and records[-1]["next"] is None
    assert json.loads((tmp_path / "complete.json").read_text(encoding="utf-8"))["pages"] == 3
    assert len(list((tmp_path / "raw").glob("*.html.gz"))) == 3
