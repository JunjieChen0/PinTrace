"""Time-aware tag evidence (pintrace/release_timing.py) on synthetic upstream records."""

from pintrace import release_timing as rt

SHA, OTHER = "a" * 40, "b" * 40
DOC = dict(
    observations=[dict(tag="v1.2.0", target=SHA.upper(), visit_date="2026-01-10T00:00:00+00:00"),
                  dict(tag="v1.2.0", target=SHA, visit_date="2026-01-05T00:00:00+00:00"),
                  dict(tag="v1", target=OTHER, visit_date="2026-03-01T00:00:00+00:00")],
    release_records=[dict(tag="v1.3.0", target=OTHER, published_at="2026-02-01T00:00:00Z", immutable=True, evidence_grade="A"),
                     # A mutable release: its target was read at collection time, so its publication date does not date the binding.
                     dict(tag="v1.4.0", target=OTHER, published_at="2026-01-01T00:00:00Z", immutable=False, evidence_grade="C")])
JAN5, FEB1, MAR1 = (rt._time(s) for s in ("2026-01-05T00:00:00Z", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"))


def test_earliest_evidence_takes_the_first_visit_or_immutable_publication():
    first = rt.earliest_evidence(DOC)
    assert first[(SHA, "v1.2.0")] == JAN5
    assert first[(OTHER, "v1.3.0")] == FEB1 and first[(OTHER, "v1")] == MAR1


def test_a_mutable_release_does_not_date_its_binding_unless_asked():
    assert (OTHER, "v1.4.0") not in rt.earliest_evidence(DOC)
    assert rt.earliest_evidence(DOC, mutable_releases=True)[(OTHER, "v1.4.0")] == rt._time("2026-01-01T00:00:00Z")


def test_timed_index_keeps_only_bindings_evidenced_by_the_moment():
    timed = rt.TimedIndex(rt.earliest_evidence(DOC))
    assert dict(timed.at(JAN5 - 1)) == {}
    assert timed.at(FEB1)[OTHER] == {"v1.3.0"} and timed.at(MAR1)[OTHER] == {"v1.3.0", "v1"}


def test_timing_separates_before_after_and_untagged():
    evidence = rt.earliest_evidence(DOC)
    assert rt.timing({"v1.2.0"}, SHA, evidence, JAN5) == "before"
    assert rt.timing({"v1.3.0"}, OTHER, evidence, FEB1 - 1) == "after"
    assert rt.timing(set(), "c" * 40, evidence, MAR1) == "no_tag"


def entry(outcome, adopted, tag="v1.1.0"):
    return dict(repository_id="1", lineage_id="l", opportunity_id="o", upstream_repository="u/a",
                adopted_sha=adopted, opportunity_tag=tag, outcome=outcome)


def row(outcome, adopted_at, grade="A", lower="2026-01-01T00:00:00+00:00", upper=""):
    return dict(repository_id="1", lineage_id="l", opportunity_id="o", role="target", outcome=outcome,
                outcome_timestamp=str(adopted_at), evidence_grade=grade, available_after_or_at=lower, available_by=upper)


def routes(outcome, adopted, adopted_at, **row_fields):
    evidence = rt.earliest_evidence(DOC)
    indices = {"u/a": rt.TimedIndex(evidence).at(float("inf"))}
    return rt.route_timing([row(outcome, adopted_at, **row_fields)], [entry(outcome, adopted)], indices,
                           {"u/a": rt.TimedIndex(evidence)})[0]


def test_a_later_release_adopted_before_its_tag_is_evidenced_loses_its_route():
    early = routes("other_sha_change", OTHER, FEB1 - 1)
    assert early["route"] == "later_release" and early["strict_route"] == "untagged_commit" and early["evidence"] == "after"
    late = routes("other_sha_change", OTHER, FEB1)
    assert late["strict_route"] == "later_release" and late["evidence"] == "before"


def test_an_opportunity_target_must_be_available_when_adopted():
    assert routes("adopted_target", SHA, JAN5)["strict_route"] == "opportunity_target"
    grade_b = routes("adopted_target", SHA, JAN5, grade="B", upper="2026-01-06T00:00:00+00:00")
    assert grade_b["evidence"] == "after" and grade_b["strict_route"] == "release_not_yet_evidenced"


def test_summary_reports_whether_any_route_moved():
    summary = rt.summarize_routes([dict(route="later_release", strict_route="untagged_commit", evidence="after"),
                                   dict(route="opportunity_target", strict_route="opportunity_target", evidence="before")])
    assert summary["channel"] == 2 and summary["strict_channel"] == 1 and summary["unchanged"] is False
