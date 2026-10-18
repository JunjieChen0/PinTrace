import json

from pintrace.sources import input_gaps, inspect_notebook


def test_notebook_inspection_does_not_execute_code_and_keeps_input_evidence():
    content = json.dumps({"cells": [
        {"cell_type": "markdown", "source": ["read_csv('not-code.csv')"]},
        {"cell_type": "code", "source": ["raise RuntimeError('never execute')\n",
                                            "table = pd.read_csv('corrected_workflows.csv.zst')\n"]},
    ]})
    lines = inspect_notebook(content)
    assert lines == ["table = pd.read_csv('corrected_workflows.csv.zst')"]
    assert input_gaps(["code/data/all_diffs_new.parquet"], lines) == ["corrected_workflows.csv.zst"]


def test_missing_input_audit_distinguishes_bundled_file_and_commented_input():
    assert input_gaps(["data/corrected_workflows.csv.zst"],
                      ["pd.read_csv('corrected_workflows.csv.zst')"]) == []
    assert input_gaps([], ["# pd.read_csv('corrected_workflows.csv.zst')"]) == []
