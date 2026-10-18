import hashlib
import io
import tarfile

import pytest

from pintrace.frames import candidate_evidence, pre_event_states, scan_archive, usable_archive_state


def row(repo, date, digest, commit="a", uid="ci"):
    return dict(repository=repo, committed_date=str(date), file_hash=digest,
                commit_hash=commit * 40, uid=uid, valid_workflow="True",
                file_path=".github/workflows/ci.yml")


def test_archive_state_ignores_post_event_removal_and_new_adoption():
    states, counts = pre_event_states([
        row("a/repo", 9, "old"), row("a/repo", 11, "--", "b"),
        row("b/repo", 10, "new"), row("c/repo", 8, "another"),
    ], 10)
    assert {value["repository"]: value["file_hash"] for value in states} == {
        "a/repo": "old", "c/repo": "another"}
    assert counts["metadata_rows"] == 4


def test_uid_is_scoped_to_repository_and_timestamp_ties_are_reported():
    states, counts = pre_event_states([
        row("a/repo", 8, "old"), row("a/repo", 8, "other", "b"),
        row("b/repo", 8, "different"),
    ], 10)
    assert len(states) == 2
    assert counts["latest_timestamp_tie_histories"] == 1
    assert next(value for value in states if value["repository"] == "a/repo")["file_hash"] == "other"


def test_older_timestamp_conflict_does_not_mark_unambiguous_latest_state():
    states, counts = pre_event_states([
        row("a/repo", 8, "old"), row("a/repo", 8, "other", "b"),
        row("a/repo", 9, "latest", "c"),
    ], 10)
    assert states[0]["latest_time_ambiguous"] is False
    assert counts["latest_timestamp_tie_histories"] == 0


@pytest.mark.parametrize("digest,path", [("", ""), ("--", ".github/workflows/ci.yml"),
                                       ("not-a-content-hash", ".github/workflows/ci.yml"),
                                       ("a" * 64, "")])
def test_source_valid_flag_does_not_make_deleted_or_invalid_markers_live(digest, path):
    state = row("a/repo", 8, digest)
    state["file_path"] = path
    assert not usable_archive_state(state)


def test_complete_source_state_can_be_a_candidate_hint():
    assert usable_archive_state(row("a/repo", 8, "a" * 64))


def test_archive_distinguishes_comment_only_and_missing_content(tmp_path):
    target = b"jobs: {test: {steps: [{uses: 'tj-actions/changed-files@v45'}]}}\n"
    comment = b"# tj-actions/changed-files@v45\njobs: {test: {steps: [{run: 'true'}]}}\n"
    values = {hashlib.sha256(value).hexdigest(): value for value in [target, comment]}
    path = tmp_path / "workflows.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for digest, value in values.items():
            member = tarfile.TarInfo("workflows/" + digest)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    content, audit = scan_archive(path, set(values) | {"missing"})
    states = [row("a/repo", 8, hashlib.sha256(target).hexdigest()),
              row("b/repo", 8, hashlib.sha256(comment).hexdigest())]
    evidence = candidate_evidence(pre_event_states(states, 10)[0], content, "test")
    assert [value["repository"] for value in evidence] == ["a/repo"]
    assert evidence[0]["baseline_status"] == "archive_hint_requires_git_confirmation"
    assert evidence[0]["identity_status"] == "name_only_unverified"
    assert audit["missing_required_content_hashes"] == 1
    assert audit["missing_content_hashes"] == ["missing"]


def test_archive_rejects_content_not_matching_published_filename(tmp_path):
    path = tmp_path / "corrupt.tar.gz"
    digest = "a" * 64
    with tarfile.open(path, "w:gz") as archive:
        value = b"not the announced object"
        member = tarfile.TarInfo("workflows/" + digest)
        member.size = len(value)
        archive.addfile(member, io.BytesIO(value))
    with pytest.raises(ValueError, match="content hash mismatch"):
        scan_archive(path, {digest})
