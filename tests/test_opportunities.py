import time

import pytest

from pintrace.opportunities import (Archive, archived_changes, archived_opportunities, channel,
                                    delay_bounds, grade_evidence, release_observations)


def test_current_tag_and_old_publication_time_cannot_be_promoted():
    assert grade_evidence({"target": "a" * 40, "published_at": "2025-01-01T00:00:00Z",
                           "evidence_type": "current_release_listing", "target_commitish": "main"}) == "C"


def test_immutable_release_requires_target_identity():
    item = {"evidence_type": "immutable_release", "immutable": True,
            "published_at": "2025-11-01T00:00:00Z"}
    assert grade_evidence(item) == "C"
    assert grade_evidence({**item, "target": "a" * 40}) == "A"


def test_archive_change_is_interval_not_release_point():
    def row(day, target):
        return {"repository": "actions/checkout", "tag": "v4", "target": target * 40,
                "visit_date": f"2025-01-{day:02}T00:00:00Z", "snapshot": str(day),
                "source_url": f"https://archive.example/{day}", "evidence_type": "archive_snapshot"}
    result = archived_changes([row(3, "b"), row(1, "a"), row(2, "a")])
    assert len(result) == 1
    assert result[0]["available_after"] == "2025-01-02T00:00:00Z"
    assert result[0]["available_by"] == "2025-01-03T00:00:00Z"
    assert result[0]["exact_publication_identified"] is False
    assert delay_bounds("2025-01-04T00:00:00Z", result[0]) == {
        "lower_days": 1, "upper_days": 2, "evidence_grade": "B"}


def test_current_only_and_predating_update_have_no_delay():
    assert delay_bounds("2025-01-01T00:00:00Z", {"evidence_grade": "C"}) is None
    assert delay_bounds("2025-01-01T00:00:00Z", {
        "evidence_grade": "A", "available_at": "2025-01-02T00:00:00Z"}) is None


def test_major_channels_do_not_treat_arbitrary_branches_as_versions():
    assert channel("v4.2.2") == "v4"
    assert channel("v45") == "v45"
    assert channel("main") is None


def test_known_hourly_rate_limit_does_not_issue_more_requests(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    archive.unavailable_until = time.time() + 120
    monkeypatch.setattr("pintrace.opportunities.requests.get", lambda *a, **kw: pytest.fail("Unexpected HTTP request"))
    with pytest.raises(RuntimeError, match="rate limit active"):
        archive.fetch("https://archive.softwareheritage.org/api/1/test/")


def test_release_pages_do_not_stop_at_old_publication_date():
    class Client:
        pages = []

        def api(self, endpoint, **kwargs):
            if "/git/ref/" in endpoint:
                return {"object": {"type": "commit", "sha": "a" * 40}}
            self.pages.append(kwargs["page"])
            if kwargs["page"] == 1:
                return [{"published_at": "2020-01-01T00:00:00Z"}] * 100
            return [{"published_at": "2025-06-01T00:00:00Z", "tag_name": "v1", "prerelease": False,
                     "id": 123, "html_url": "https://github.com/example/action/releases/tag/v1",
                     "url": "https://api.github.com/repos/example/action/releases/123"}]
    client = Client()
    audit = {}
    rows = release_observations(client, "example/action", "2025-01-01T00:00:00Z", "2025-12-31T23:59:59Z", audit)
    assert client.pages == [1, 2]
    assert len(rows) == 1
    assert audit["release_listing_complete"] is True
    assert audit["release_records_examined"] == 101


def snapshot(day, bindings, *, presence=None, complete=True, origin="https://github.com/example/action"):
    return {"repository": "example/action", "origin_url": origin, "snapshot": "snapshot-" + str(day),
            "visit_date": f"2025-01-{day:02}T00:00:00Z", "bindings": bindings,
            "tag_names": sorted(bindings if presence is None else presence),
            "complete": complete, "tag_listing_complete": complete,
            "source_url": f"https://archive.example/snapshot/{day}"}


def test_new_tag_interval_uses_complete_absence_not_only_mutated_tags():
    result = archived_opportunities([snapshot(1, {"v4.1.0": "a" * 40}),
                                     snapshot(2, {"v4.1.0": "a" * 40, "v4.2.0": "b" * 40})])
    row, = result["opportunities"]
    assert row["evidence_type"] == "observed_tag_introduction"
    assert row["lower_snapshot"] == "snapshot-1" and row["upper_snapshot"] == "snapshot-2"
    assert row["lower_complete"] and row["upper_complete"]
    assert row["version_key"] == "4.2.0" and row["version_precision"] == 3
    assert row["version_vs_previous_channel_max"] == "higher"
    assert row["before_current_bindings"] == [{"tag": "v4.1.0", "target": "a" * 40, "version_key": "4.1.0"}]
    assert row["first_ever_publication_identified"] is False


def test_deleted_tag_reappearance_is_reintroduction_and_not_binding_change():
    result = archived_opportunities([snapshot(1, {"v4.1.0": "a" * 40}), snapshot(2, {}),
                                     snapshot(3, {"v4.1.0": "b" * 40})])
    row, = result["opportunities"]
    assert row["evidence_type"] == "observed_tag_reintroduction"
    assert row["available_after"] == "2025-01-02T00:00:00Z"
    assert row["previous_target"] is None


def test_partial_snapshot_cannot_prove_a_tag_absent():
    partial = snapshot(1, {}, complete=False)
    complete = snapshot(2, {"v1.0.0": "a" * 40})
    result = archived_opportunities([partial, complete])
    assert result["opportunities"] == []
    assert len(result["initial_observed_availability"]) == 1


def test_incomplete_intermediate_snapshot_widens_complete_endpoint_interval():
    result = archived_opportunities([snapshot(1, {}), snapshot(2, {}, complete=False),
                                     snapshot(3, {"v1.0.0": "a" * 40})])
    row, = result["opportunities"]
    assert row["lower_snapshot"] == "snapshot-1"
    assert row["intervening_incomplete_snapshots"] == ["snapshot-2"]


def test_unresolved_present_tag_is_not_misclassified_as_introduced():
    result = archived_opportunities([snapshot(1, {}, presence=["v1.0.0"]),
                                     snapshot(2, {"v1.0.0": "a" * 40})])
    assert result["opportunities"] == []


def test_new_same_version_aliases_form_one_opportunity():
    result = archived_opportunities([snapshot(1, {"v4.1.0": "a" * 40}),
                                     snapshot(2, {"v4.1.0": "a" * 40, "v4.2.0": "b" * 40, "4.2.0": "b" * 40})])
    row, = result["opportunities"]
    assert row["tag_aliases"] == ["4.2.0", "v4.2.0"]
    assert row["tag"] == "v4.2.0"


def test_adding_alias_of_existing_version_target_is_diagnostic_only():
    result = archived_opportunities([snapshot(1, {"v4.1.0": "a" * 40}),
                                     snapshot(2, {"v4.1.0": "a" * 40, "4.1.0": "a" * 40})])
    assert result["opportunities"] == []
    row, = result["tag_events"]
    assert row["evidence_type"] == "observed_tag_alias_introduction"
    assert row["opportunity_candidate"] is False


def test_same_sha_new_version_does_not_claim_new_code_or_universal_upgrade():
    result = archived_opportunities([snapshot(1, {"v4.1.0": "a" * 40}),
                                     snapshot(2, {"v4.1.0": "a" * 40, "v4.2.0": "a" * 40})])
    row, = result["opportunities"]
    assert row["target_previously_observed"] is True
    assert row["same_target_lower_tags"] == ["v4.1.0"]
    assert row["downstream_applicability"] == "requires_reference_identity_channel_and_risk_set_join"


def test_backfilled_lower_version_is_retained_without_asserting_upgrade_order():
    result = archived_opportunities([snapshot(1, {"v4.5.0": "a" * 40}),
                                     snapshot(2, {"v4.5.0": "a" * 40, "v4.1.1": "b" * 40})])
    row, = result["opportunities"]
    assert row["version_vs_previous_channel_max"] == "lower"


def test_different_origin_snapshots_cannot_supply_absence_endpoints():
    result = archived_opportunities([snapshot(1, {}, origin="https://example.org/action"),
                                     snapshot(2, {"v1.0.0": "a" * 40})])
    assert result["opportunities"] == []


def test_empty_complete_archive_snapshot_is_saved_explicitly(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    monkeypatch.setattr(archive, "fetch", lambda url: {"data": {"branches": {}, "next_branch": None}})
    result = archive.observation("example/action", {"snapshot": "a" * 40, "date": "2025-01-01T00:00:00Z", "status": "full"})
    state = result["snapshot_visit"]
    assert state["complete"] and state["tag_listing_complete"]
    assert state["tag_names"] == [] and state["bindings"] == {}


def test_alias_and_null_identity_presence_survive_target_resolution(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    branches = {"refs/tags/v1.0.0": {"target_type": "alias", "target": "refs/heads/main"}, "refs/tags/v1.1.0": None}
    monkeypatch.setattr(archive, "fetch", lambda url: {"data": {"branches": branches, "next_branch": None}})
    result = archive.observation("example/action", {"snapshot": "a" * 40, "date": "2025-01-01T00:00:00Z", "status": "full"})
    state = result["snapshot_visit"]
    assert state["complete"] is True
    assert state["tag_names"] == ["v1.0.0", "v1.1.0"]
    assert state["bindings"] == {}
    assert len(state["binding_details"]) == 2
    assert archived_opportunities([state, snapshot(2, {"v1.0.0": "a" * 40, "v1.1.0": "b" * 40})])["opportunities"] == []


def test_failed_second_archive_page_cannot_prove_complete_absence(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    calls = []
    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            return {"data": {"branches": {"refs/tags/v1.0.0": {"target_type": "revision", "target": "a" * 40}},
                             "next_branch": "refs/tags/v2.0.0"}}
        raise RuntimeError("cached object unavailable")
    monkeypatch.setattr(archive, "fetch", fetch)
    result = archive.observation("example/action", {"snapshot": "a" * 40, "date": "2025-01-01T00:00:00Z", "status": "full"})
    state = result["snapshot_visit"]
    assert state["complete"] is False and state["tag_listing_complete"] is False
    assert state["tag_names"] == ["v1.0.0"] and state["bindings"] == {"v1.0.0": "a" * 40}
    assert archived_opportunities([state, snapshot(2, {"v2.0.0": "b" * 40})])["opportunities"] == []


def test_cache_only_mode_never_issues_new_archive_requests(tmp_path, monkeypatch):
    archive = Archive(tmp_path, cache_only=True)
    monkeypatch.setattr("pintrace.opportunities.requests.get", lambda *a, **kw: pytest.fail("Unexpected HTTP request"))
    with pytest.raises(RuntimeError, match="not cached"):
        archive.fetch("https://archive.softwareheritage.org/api/1/snapshot/unknown/")


def test_exhausted_request_budget_preserves_unknown_without_network(tmp_path, monkeypatch):
    archive = Archive(tmp_path, max_requests=0)
    monkeypatch.setattr("pintrace.opportunities.requests.get", lambda *a, **kw: pytest.fail("Unexpected HTTP request"))
    with pytest.raises(RuntimeError, match="budget exhausted"):
        archive.fetch("https://archive.softwareheritage.org/api/1/snapshot/missing/")
    assert archive.requests_issued == 0


def test_any_429_stops_the_round_and_records_true_reset(tmp_path, monkeypatch):
    class Response:
        status_code = 429
        headers = {"X-Ratelimit-Reset": str(int(time.time()) + 120), "X-Ratelimit-Limit": "120"}
        text = "Quota exhausted"
    calls = []
    def get(*args, **kwargs):
        calls.append(args[0])
        return Response()
    monkeypatch.setattr("pintrace.opportunities.requests.get", get)
    archive = Archive(tmp_path, max_requests=90)
    with pytest.raises(RuntimeError, match="rate limit active"):
        archive.fetch("https://archive.example/one")
    with pytest.raises(RuntimeError, match="rate limit active"):
        archive.fetch("https://archive.example/two")
    assert len(calls) == 1
    assert archive.last_rate_limit["headers"]["X-Ratelimit-Limit"] == "120"


def test_partial_origin_visit_cannot_be_promoted_to_complete_snapshot(tmp_path, monkeypatch):
    archive = Archive(tmp_path)
    monkeypatch.setattr(archive, "fetch", lambda url: {"data": {"branches": {}, "next_branch": None}})
    result = archive.observation("example/action", {"snapshot": "a" * 40, "date": "2025-01-01T00:00:00Z", "status": "partial"})
    assert result["snapshot_visit"]["tag_listing_complete"] is True
    assert result["snapshot_visit"]["complete"] is False


def test_same_version_aliases_with_conflicting_targets_remain_explicit():
    result = archived_opportunities([snapshot(1, {"v4.1.0": "a" * 40}),
                                     snapshot(2, {"v4.1.0": "a" * 40, "v4.2.0": "b" * 40, "4.2.0": "c" * 40})])
    assert len(result["opportunities"]) == 2
    assert all(row["upper_version_target_conflict"] for row in result["opportunities"])
    assert len({row["tag_alias_group"] for row in result["opportunities"]}) == 2
