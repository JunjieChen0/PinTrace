import subprocess

from pintrace.tool_replay import cases, git_fixture, validate_results


def test_paired_inputs_change_only_comments_and_coverage():
    upstream = {"tags": {"v4.2.2": "a" * 40}, "untagged": "c" * 40}
    rows = cases(upstream)
    assert len(rows) == 6
    assert [r["covered"] for r in rows] == [True, False] * 3
    assert rows[0]["workflow"].replace(" # v4.2.2", "") == rows[2]["workflow"]
    assert rows[0]["workflow"] == rows[1]["workflow"]
    assert "package-ecosystem: pip" in rows[1]["dependabot_config"]


def test_fixture_has_real_tagged_and_untagged_commits(tmp_path):
    result = git_fixture(tmp_path / "fixture")
    assert result == git_fixture(tmp_path / "fixture")
    assert result["untagged"] not in result["tags"].values()
    command = ["git", "-C", result["fixture_repo"], "merge-base", "--is-ancestor", result["untagged"], result["head"]]
    assert subprocess.run(command, capture_output=True).returncode == 0


def test_incomplete_replay_cannot_be_reported_as_success():
    assert validate_results({"renovate": None}, {"cases": [{"case_id": "missing"}]})


def test_config_off_case_must_reach_disabled_engine_output():
    payload = {"cases": [{"case_id": str(i)} for i in range(6)]}
    result = {"results": [{"case_id": str(i), "covered": False,
                           "official_result": [{"updates": [{"newDigest": "a" * 40}]}]} for i in range(6)]}
    assert len(validate_results({"renovate": result}, payload)) == 6
