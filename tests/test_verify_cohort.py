import hashlib

from pintrace.verify_cohort import verify_blobs


def test_empty_blob_is_verified_as_real_object_not_missing_marker(tmp_path):
    blob = hashlib.sha1(b"blob 0\0").hexdigest()
    (tmp_path / blob).write_bytes(b"")
    state = {"files": [{"kind": "blob", "mode": "100644", "blob": blob}]}
    assert verify_blobs(state, tmp_path) == {"required_blobs": 1, "missing_blobs": [], "mismatched_blobs": []}


def test_missing_and_corrupt_blobs_are_different_failures(tmp_path):
    (tmp_path / ("a" * 40)).write_bytes(b"wrong")
    state = {"files": [{"kind": "blob", "mode": "100644", "blob": "a" * 40},
                       {"kind": "blob", "mode": "100644", "blob": "b" * 40}]}
    result = verify_blobs(state, tmp_path)
    assert result["missing_blobs"] == ["b" * 40]
    assert result["mismatched_blobs"] == ["a" * 40]
