import json
from pathlib import Path

import pytest

from pintrace.opportunity_batches import gaps, select_upstreams, throttle_wait, write_selection


def row(action, upstream, repositories, references):
    return dict(action=action, upstream_repository=upstream, baseline_repository_count=str(repositories),
                baseline_reference_count=str(references))


ROWS = [row("a/x", "a/x", 10, 20), row("b/y", "b/y", 10, 30), row("c/z", "c/z", 5, 5),
        row("g/c/init", "g/c", 4, 6), row("g/c/analyze", "g/c", 4, 7), row("d/w", "d/w", 1, 1)]


def test_selection_ranks_by_repositories_then_references_then_name_and_groups_sub_actions():
    chosen = select_upstreams(ROWS, 3)
    assert [row["upstream_repository"] for row in chosen] == ["b/y", "a/x", "c/z"]
    grouped = select_upstreams(ROWS, 4)[-1]
    assert grouped["upstream_repository"] == "g/c" and grouped["baseline_references"] == 13 and grouped["actions"] == ["g/c/analyze", "g/c/init"]


def test_required_upstreams_are_kept_without_duplicates_and_unknown_ones_are_dropped():
    chosen = [row["upstream_repository"] for row in select_upstreams(ROWS, 2, required=("d/w", "a/x", "missing/none"))]
    assert chosen == ["b/y", "a/x", "d/w"]


def test_a_recorded_selection_is_not_replaced_by_a_different_one(tmp_path):
    (tmp_path / "reports").mkdir()
    lines = ["action,upstream_repository,baseline_repository_count,baseline_reference_count"]
    lines += [f"{r['action']},{r['upstream_repository']},{r['baseline_repository_count']},{r['baseline_reference_count']}" for r in ROWS]
    (tmp_path / "reports/upstream_frequencies.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    first = write_selection(tmp_path, 2, ())
    assert write_selection(tmp_path, 2, ()) == first
    with pytest.raises(ValueError, match="will not be overwritten"):
        write_selection(tmp_path, 3, ())


def test_throttle_wait_is_zero_without_a_limit_and_positive_while_one_is_active(tmp_path):
    assert throttle_wait(tmp_path, now=1000) == 0
    directory = tmp_path / "data/cache/softwareheritage"
    directory.mkdir(parents=True)
    (directory / "rate-limit.json").write_text(json.dumps({"unavailable_until": 1600}), encoding="utf-8")
    assert throttle_wait(tmp_path, now=1000) == 605
    assert throttle_wait(tmp_path, now=1700) == 0


class ValidationPassed(Exception):
    """Raised by the stub once argument validation has accepted the budget."""


def run_main(monkeypatch, token, budget):
    import sys
    from pintrace import opportunity_batches

    def stop(*args, **kwargs):
        raise ValidationPassed

    monkeypatch.setattr("pintrace.opportunities.TOKEN_FILE", Path("no-such-token-file"))
    monkeypatch.delenv("SWH_TOKEN", raising=False)
    if token:
        monkeypatch.setenv("SWH_TOKEN", token)
    monkeypatch.setattr(sys, "argv", ["opportunity_batches", "--budget", str(budget)])
    monkeypatch.setattr(opportunity_batches, "write_selection", stop)
    opportunity_batches.main()


def test_anonymous_budget_stays_below_the_hourly_limit(monkeypatch):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "", 120)
    with pytest.raises(ValidationPassed):
        run_main(monkeypatch, "", 119)


def test_a_token_raises_the_budget_ceiling_but_not_beyond_its_own_limit(monkeypatch):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, "t", 1200)
    with pytest.raises(ValidationPassed):
        run_main(monkeypatch, "t", 1199)


def test_gaps_lists_missing_upstreams_and_those_with_recorded_errors(tmp_path):
    assert gaps(tmp_path, ["a/x"]) == ["a/x"]
    (tmp_path / "reports").mkdir()
    report = dict(upstreams=[dict(repository="a/x", errors=[]), dict(repository="b/y", errors=[dict(error="budget")])])
    (tmp_path / "reports/opportunity_coverage.json").write_text(json.dumps(report), encoding="utf-8")
    assert gaps(tmp_path, ["a/x", "b/y", "c/z"]) == ["b/y", "c/z"]
