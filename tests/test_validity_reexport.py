import json
import os
from pathlib import Path

import pytest

from pintrace import measurement
from pintrace import validity_reexport as reexport

VOLATILE = reexport.MEASUREMENT_VOLATILE


def write(path, text):
    path.write_bytes(text.encode("utf-8"))
    return path


def document(stamp, body="1"):
    return '{\n  "summary": {\n    "a": 1,\n    "measurement_checked_at_utc": "%s"\n  },\n  "b": %s\n}\n' % (stamp, body)


def test_identical_bytes_are_identical_without_any_normalization(tmp_path):
    first, second = write(tmp_path / "a.json", document("x")), write(tmp_path / "b.json", document("x"))
    result = reexport.compare_files(first, second, VOLATILE)
    assert result["identical_raw"] and result["identical_after_normalization"] and result["differing_lines"] == 0


def test_only_the_declared_timestamp_line_may_differ(tmp_path):
    first, second = write(tmp_path / "a.json", document("2026-10-19")), write(tmp_path / "b.json", document("2026-10-20"))
    result = reexport.compare_files(first, second, VOLATILE)
    assert result["identical_raw"] is False and result["identical_after_normalization"] is True
    assert result["differing_lines"] == result["explained_volatile_lines"] == 1 and result["unexplained_differences"] == 0
    assert result["normalized_sha256"][0] == result["normalized_sha256"][1]


def test_a_real_difference_next_to_the_timestamp_is_not_hidden(tmp_path):
    first, second = write(tmp_path / "a.json", document("t1", "1")), write(tmp_path / "b.json", document("t2", "2"))
    result = reexport.compare_files(first, second, VOLATILE)
    assert result["identical_after_normalization"] is False and result["unexplained_differences"] == 1
    assert result["first_unexplained"][0]["first"].strip() == '"b": 1' and result["first_unexplained"][0]["second"].strip() == '"b": 2'


def test_the_volatile_key_only_counts_at_its_declared_indent(tmp_path):
    nested = '{\n  "measurement_checked_at_utc": "t1"\n}\n'
    other = '{\n  "measurement_checked_at_utc": "t2"\n}\n'
    result = reexport.compare_files(write(tmp_path / "a.json", nested), write(tmp_path / "b.json", other), VOLATILE)
    assert result["identical_after_normalization"] is False, "a same-named key at another depth is data, not the stamp"


def test_a_truncated_or_extended_file_is_a_difference(tmp_path):
    full = document("t")
    longer = write(tmp_path / "long.json", full + "extra\n")
    result = reexport.compare_files(write(tmp_path / "a.json", full), longer, VOLATILE)
    assert result["identical_after_normalization"] is False and result["bytes"][0] < result["bytes"][1]
    shorter = reexport.compare_files(longer, write(tmp_path / "c.json", full), VOLATILE)
    assert shorter["identical_after_normalization"] is False


def test_a_timestamp_line_that_exists_in_only_one_file_is_not_explained(tmp_path):
    with_stamp = write(tmp_path / "a.json", document("t"))
    without = write(tmp_path / "b.json", '{\n  "summary": {\n    "a": 1\n  },\n  "b": 1\n}\n')
    assert reexport.compare_files(with_stamp, without, VOLATILE)["identical_after_normalization"] is False


def test_sample_takes_size_quantiles_under_the_cap_and_always_includes_required_ids():
    sizes = {str(i): i * 10 for i in range(1, 101)}
    sizes.update({"900": 10**9, "901": 10**9 + 1})
    plan = reexport.select_sample(sizes, required=["7", "901", "unknown"], count=10, cap_bytes=10_000)
    assert plan["over_cap"] == ["900", "901"] and plan["candidates"] == 100
    assert "7" in plan["forced"] and "901" not in plan["forced"], "a required repository above the cap cannot be re-exported"
    assert len(plan["selected"]) == len(set(plan["selected"])) and set(plan["forced"]) <= set(plan["selected"])
    assert [sizes[i] for i in plan["selected"]] == sorted(sizes[i] for i in plan["selected"])
    assert plan == reexport.select_sample(sizes, required=["7", "901", "unknown"], count=10, cap_bytes=10_000)
    spread = [sizes[i] for i in plan["selected"]]
    assert min(spread) <= 150 and max(spread) >= 850, "the sample spans small to large files"


def test_sample_of_nothing_under_the_cap_is_empty_not_an_error():
    plan = reexport.select_sample({"1": 10**9}, required=["1"], count=5, cap_bytes=10)
    assert plan["selected"] == [] and plan["over_cap"] == ["1"]


def test_cache_client_reads_the_cache_and_fails_loudly_when_a_blob_is_missing(tmp_path):
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "abc").write_bytes(b"content")
    client = reexport.CacheOnlyClient(tmp_path, "token-that-must-never-be-used")
    assert client.blob("o/r", "commit", "path", "abc") == b"content"
    with pytest.raises(FileNotFoundError):
        client.blob("o/r", "commit", "path", "missing")


def test_offline_context_swaps_the_network_client_and_restores_the_environment(tmp_path):
    os.environ.pop("GIT_NO_LAZY_FETCH", None)
    original = measurement.GitHub
    with reexport.offline(tmp_path / "out"):
        assert measurement.GitHub is reexport.CacheOnlyClient
        assert os.environ["GIT_NO_LAZY_FETCH"] == "1"
        assert measurement.BATCH_DIRECTORY == str(tmp_path / "out")
    assert measurement.GitHub is original and "GIT_NO_LAZY_FETCH" not in os.environ
    os.environ["GIT_NO_LAZY_FETCH"] = "0"
    try:
        with reexport.offline(tmp_path / "out"):
            pass
        assert os.environ["GIT_NO_LAZY_FETCH"] == "0"
    finally:
        os.environ.pop("GIT_NO_LAZY_FETCH", None)


def test_contained_redirects_reports_and_refuses_other_project_writes(tmp_path):
    root, sandbox = tmp_path / "project", tmp_path / "sandbox"
    (root / "reports").mkdir(parents=True)
    (root / "data").mkdir()
    with reexport.contained(root, sandbox) as shadow:
        (root / "reports" / "x.json").write_bytes(b"redirected")
        (root / "reports" / "sub").mkdir()
        (root / "reports" / "sub" / "y.json").write_text("also redirected", encoding="utf-8")
        (sandbox / "runs").mkdir(parents=True)
        (sandbox / "runs" / "z.json").write_bytes(b"inside the sandbox")
        with pytest.raises(PermissionError, match="may not write"):
            (root / "data" / "evil.json").write_bytes(b"x")
        with pytest.raises(PermissionError):
            (root / "study.toml").write_text("x", encoding="utf-8")
    assert (shadow / "x.json").read_bytes() == b"redirected" and (shadow / "sub" / "y.json").read_text(encoding="utf-8") == "also redirected"
    assert not (root / "reports" / "x.json").exists() and not (root / "data" / "evil.json").exists()
    (root / "data" / "after.json").write_bytes(b"patch removed")  # the guard is gone after the block
    assert (root / "data" / "after.json").exists()


def test_fingerprint_detects_a_changed_report_and_a_new_run_directory(tmp_path, monkeypatch):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "a.json").write_bytes(b"1")
    run_directory = tmp_path / "runs"
    run_directory.mkdir()
    monkeypatch.setattr(reexport, "RUN_DIRECTORIES", ("runs",))
    before = reexport.fingerprint(tmp_path)
    assert before == reexport.fingerprint(tmp_path)
    (run_directory / "20260101").mkdir()
    assert reexport.fingerprint(tmp_path) != before
    (run_directory / "20260101").rmdir()
    (tmp_path / "reports" / "a.json").write_bytes(b"22")
    assert reexport.fingerprint(tmp_path) != before


def test_summary_requires_every_check_and_does_not_pass_on_an_empty_run():
    empty = reexport.summarize({}, {}, True, [], [])
    assert not empty["sample_is_non_empty"] and not empty["every_file_identical_after_normalization"] and not empty["every_analysis_repeatable"]
    good_file = dict(identical_after_normalization=True, differing_lines=1, explained_volatile_lines=1)
    measured = {"1": dict(status="measured", comparison=good_file)}
    analyses = {"m": dict(first_vs_published=dict(identical_after_normalization=True), second_vs_first=dict(identical_after_normalization=True),
                          published_report_built_by_current_source=True)}
    assert all(reexport.summarize(measured, analyses, True, ["1"], []).values())
    stale = {"m": dict(analyses["m"], published_report_built_by_current_source=False)}
    assert reexport.summarize(measured, stale, True, ["1"], [])["published_reports_built_by_current_source"] is False
    assert reexport.summarize(measured, analyses, False, ["1"], [])["project_unchanged_by_analysis_runs"] is False
    assert reexport.summarize(measured, analyses, True, ["2"], [])["clock_unaligned_repositories_included"] is False
    drifted = {"1": dict(status="measured", comparison=dict(good_file, identical_after_normalization=False))}
    assert reexport.summarize(drifted, analyses, True, ["1"], [])["every_file_identical_after_normalization"] is False


def test_every_analysis_entry_names_a_real_module_attribute_and_report():
    import importlib
    for name, (constant, keywords, report) in reexport.ANALYSES.items():
        module = importlib.import_module("pintrace." + name)
        assert hasattr(module, constant) and callable(module.run) and report.endswith(".json")
        assert isinstance(keywords, dict)


def test_indent_and_key_reads_only_json_member_lines():
    assert reexport._indent_and_key(b'    "measurement_checked_at_utc": "t",\n') == (4, "measurement_checked_at_utc")
    assert reexport._indent_and_key(b"  }\n") is None and reexport._indent_and_key(b'    "just a string",\n') is None
