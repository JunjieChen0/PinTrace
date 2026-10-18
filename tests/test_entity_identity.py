import csv
import gzip
import json

import pytest

from pintrace import entity_identity as audit


ANCHOR = "2025-03-14T00:00:00Z"


def row(name="owner/repo", identity="1", status="confirmed", **extra):
    return dict(repository=name, repository_id=identity, status=status, **extra)


def birth(name="owner/repo", created="2020-01-02 03:04:05", path="source-a", number=2):
    return dict(repository=name, created=created, source_path=path, source_row=number)


def metadata(created="2020-01-02T15:04:05Z", **extra):
    return dict(id=1, full_name="owner/repo", created_at=created, fork=False, path="cache-a", **extra)


def classify(rows=None, births=None, observations=None):
    return audit.classify_group(rows or [row()], {"owner/repo": [birth()]} if births is None else births,
                                [metadata()] if observations is None else observations, ANCHOR)


def test_post_anchor_fork_cannot_inherit_repository_identity():
    observation = metadata("2026-08-01T18:05:44Z")
    observation.update(fork=True, parent={"id": 99, "created_at": "2020-01-01T00:00:00Z"})
    result = classify(rows=[row(baseline_date="2025-03-06T00:00:00Z", baseline_commit="inherited")],
                      observations=[observation])
    assert result["identity_state"] == "historical_identity_unknown"
    assert "current_entity_created_not_before_anchor" in result["explicit_reasons"]
    assert result["metadata_evidence"][0]["parent"]["id"] == 99


def test_post_anchor_nonfork_is_also_unknown():
    assert classify(observations=[metadata("2026-01-01T00:00:00Z")])["identity_state"] == "historical_identity_unknown"


def test_exact_anchor_is_not_strictly_before_anchor():
    result = classify(births={"owner/repo": [birth(created="2025-03-14 00:00:00")]},
                      observations=[metadata(ANCHOR)])
    assert result["explicit_reasons"] == ["current_entity_created_not_before_anchor"]


@pytest.mark.parametrize("created", ["2020-01-01 23:59:59", "2020-01-02 03:04:05", "2020-01-03 00:00:00"])
def test_date_precision_tolerates_both_adjacent_days(created):
    assert classify(births={"owner/repo": [birth(created=created)]})["identity_state"] == "necessary_identity_checks_passed"


def test_twelve_hour_source_format_does_not_create_conflict():
    result = classify()
    assert result["explicit_reasons"] == []
    assert result["date_differences_days"][0]["days"] == 0


def test_all_source_versions_are_checked_without_selecting_matching_one():
    evidence = [birth(), birth(created="2019-01-02", path="source-b")]
    result = classify(births={"owner/repo": evidence})
    assert result["identity_state"] == "historical_identity_unknown"
    assert result["source_birth_values"] == evidence
    assert {item["source_path"] for item in result["date_differences_days"]} == {"source-a", "source-b"}


def test_same_current_id_alias_conflict_masks_the_entire_group():
    rows = [row(), row("renamed/repo")]
    sources = {"owner/repo": [birth()], "renamed/repo": [birth("renamed/repo", "2024-01-01")]}
    result = classify(rows, sources)
    assert result["identity_state"] == "historical_identity_unknown"
    assert result["aliases"] == ["owner/repo", "renamed/repo"]


def test_missing_alias_source_is_not_filled_from_other_alias():
    result = classify(rows=[row(), row("renamed/repo")])
    assert "source_created_missing:renamed/repo" in result["explicit_reasons"]


@pytest.mark.parametrize("births,observations,reason", [({}, None, "source_created_missing:owner/repo"),
                                                       (None, [], "current_metadata_missing")])
def test_missing_evidence_remains_unknown(births, observations, reason):
    result = classify(births=births, observations=observations)
    assert result["identity_state"] == "historical_identity_unknown"
    assert reason in result["explicit_reasons"]
    assert result["date_differences_days"] == []


@pytest.mark.parametrize("created", [None, "bad", ""])
def test_invalid_source_birth_remains_unknown(created):
    result = classify(births={"owner/repo": [birth(created=created)]})
    assert result["explicit_reasons"] == ["source_created_invalid:owner/repo"]


def test_conflicting_metadata_cannot_select_compatible_observation():
    result = classify(observations=[metadata(), dict(metadata("2026-01-01T00:00:00Z"), path="cache-b")])
    assert "current_metadata_conflict" in result["explicit_reasons"]
    assert result["current_created_at"] is None
    assert len(result["metadata_evidence"]) == 2


@pytest.mark.parametrize("created", [None, "bad", "2020-01-02T15:04:05"])
def test_missing_invalid_or_timezone_free_api_birth_stays_unknown(created):
    result = classify(observations=[metadata(created)])
    assert result["identity_state"] == "historical_identity_unknown"
    assert "current_created_invalid" in result["explicit_reasons"]
    assert result["current_created_at"] is None
    assert result["date_differences_days"] == []


def test_pre_anchor_fork_is_not_automatically_excluded():
    observation = metadata()
    observation["fork"] = True
    observation["parent"] = {"id": 999}
    assert classify(observations=[observation])["identity_state"] == "necessary_identity_checks_passed"


def test_inaccessible_entity_is_unknown_even_when_historical_birth_exists():
    result = classify(rows=[row(identity="", status="inaccessible_unknown")], observations=[])
    assert result["identity_state"] == "inaccessible_unknown"
    assert result["explicit_reasons"] == ["current_repository_unavailable"]
    assert result["source_birth_values"] == [birth()]


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fixture(root):
    rows = [row("owner/r" + str(number), str(number),
                "confirmed" if number <= 457 else "not_at_baseline" if number <= 548 else "parse_incomplete")
            for number in range(1, 550)]
    rows += [row("owner/alias" + str(number), str(number)) for number in range(1, 24)]
    rows += [row("owner/alias" + str(number), str(number), "not_at_baseline") for number in (458, 459)]
    rows += [row("unavailable/r" + str(number), "", "inaccessible_unknown") for number in range(6)]
    write_csv(root / audit.FLOW, rows)
    write_csv(root / audit.CANDIDATES, [{"repository": item["repository"]} for item in rows])
    (root / "study.toml").write_text('[study]\nevent_anchor="' + ANCHOR + '"\n', encoding="utf-8")
    sources = [dict(name=item["repository"], created="2020-01-02 03:04:05") for item in rows]
    for index, name in enumerate(audit.ROSTERS):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["name", "created"])
            writer.writeheader()
            writer.writerows(sources if index == 0 else [])
    cache = root / "data/cache/api"
    cache.mkdir(parents=True)
    for number in range(1, 550):
        data = dict(id=number, full_name="OWNER/R" + str(number), fork=number == 457,
                    created_at="2026-01-01T00:00:00Z" if number in (456, 457, 548) else "2020-01-02T15:04:05Z")
        (cache / (str(number) + ".json")).write_text(json.dumps(dict(data=data, url="different-case-or-alias-url")), encoding="utf-8")
    return rows


def test_full_ledger_binds_evidence_keeps_raw_files_and_uses_ids_not_urls(tmp_path):
    fixture(tmp_path)
    before = {name: (tmp_path / name).read_bytes() for name in (audit.FLOW, audit.CANDIDATES)}
    old = tmp_path / "data/derived/measurement-v3/457.json"
    old.parent.mkdir(parents=True)
    old.write_bytes(b'{"physical_measurement":"preserved"}')
    report, rows = audit.audit(tmp_path)
    audit.write_reports(tmp_path, report, rows)
    assert report["audit_passed"] and report["inputs_unchanged"]
    assert len(report["raw_confirmed_repository_ids"]) == 457
    assert len(report["formal_eligible_repository_ids"]) == 455
    assert report["excluded_confirmed_repository_ids"] == ["456", "457"]
    assert report["main_status_counts"] == dict(confirmed=478, not_at_baseline=92, parse_incomplete=1,
                                                inaccessible_unknown=6, historical_identity_unknown=3)
    assert report["flow"] == dict(path=audit.OUTPUT_FLOW, sha256=audit.digest(tmp_path / audit.OUTPUT_FLOW), row_count=580)
    assert all((tmp_path / name).read_bytes() == content for name, content in before.items())
    assert old.read_bytes() == b'{"physical_measurement":"preserved"}'
    assert all(audit.digest(tmp_path / name) == value for name, value in report["input_sha256"].items())
    exported = list(csv.DictReader((tmp_path / audit.OUTPUT_FLOW).open(encoding="utf-8", newline="")))
    assert {item["repository_id"] for item in exported if item["formal_eligible"] == "True"} == set(report["formal_eligible_repository_ids"])
    assert len(json.loads(next(item for item in exported if item["repository_id"] == "1")["source_birth_values"])) == 2
    assert len([name for name in report["input_sha256"] if name.startswith("data/cache/api/")]) == 549


def test_flow_candidate_mismatch_fails_audit(tmp_path):
    fixture(tmp_path)
    write_csv(tmp_path / audit.CANDIDATES, [{"repository": "other/repo"}])
    report, _ = audit.audit(tmp_path)
    assert not report["audit_passed"]
    assert {item["reason"] for item in report["problems"]} == {"candidate_flow_name_coverage_differs"}


def test_input_change_during_classification_fails_guard(tmp_path, monkeypatch):
    fixture(tmp_path)
    original = audit.classify_group
    changed = False

    def classify_and_change(*args):
        nonlocal changed
        if not changed:
            with (tmp_path / audit.FLOW).open("ab") as stream:
                stream.write(b"\n")
            changed = True
        return original(*args)

    monkeypatch.setattr(audit, "classify_group", classify_and_change)
    report, _ = audit.audit(tmp_path)
    assert not report["inputs_unchanged"]
    assert not report["audit_passed"]
