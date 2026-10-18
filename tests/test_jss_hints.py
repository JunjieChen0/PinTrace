from pintrace.jss_hints import references_from_diff


def test_step_literal_yields_direct_reference_without_executing_code():
    refs, error = references_from_diff(["jobs", "<job_id>", "steps", "Number"],
                                      "{'uses': 'tj-actions/changed-files@v45'}")
    assert error is None
    assert refs[0]["action"] == "tj-actions/changed-files"
    refs, error = references_from_diff(["jobs"], "__import__('os').getcwd()")
    assert refs == []
    assert error == "unparsed_literal"


def test_run_string_and_reusable_workflow_are_not_direct_action_hints():
    refs, error = references_from_diff(["jobs", "<job_id>", "steps", "Number"],
                                      "{'run': 'echo tj-actions/changed-files@v45'}")
    assert refs == []
    assert error is None
    refs, error = references_from_diff(["jobs", "<job_id>"],
                                      "{'uses': 'tj-actions/changed-files@v45'}")
    assert refs == []


def test_leaf_uses_is_extracted_but_nested_with_value_is_not():
    refs, error = references_from_diff(["jobs", "<job_id>", "steps", "Number", "uses"],
                                      "tj-actions/changed-files@v45")
    assert len(refs) == 1
    refs, error = references_from_diff(["jobs", "<job_id>", "steps", "Number", "with", "uses"],
                                      "tj-actions/changed-files@v45")
    assert refs == []
