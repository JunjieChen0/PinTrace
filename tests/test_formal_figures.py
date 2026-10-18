import csv
import json

from pintrace.formal_figures import Artifacts, resolve_pointer


def test_json_pointer_preserves_slashes_in_categorical_identity():
    assert resolve_pointer({"actions/checkout": {"count": 3}}, "/actions~1checkout/count") == 3


def test_artifact_cells_are_bound_to_actual_csv_coordinates(tmp_path):
    report = tmp_path / "summary.json"
    report.write_text(json.dumps({"count": 0, "estimate": None, "configuration": "unknown"}), encoding="utf-8")
    artifacts = Artifacts(tmp_path, {"summary": report})
    artifacts.table("bound", "Declared denominator", ["count", "estimate", "state"],
                    [[artifacts.cell("summary", "/count"), artifacts.cell("summary", "/estimate"), artifacts.cell("summary", "/configuration")]])
    artifact = artifacts.artifacts[0]
    with (tmp_path / artifact["data_path"]).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [{"count": "0", "estimate": "", "state": "unknown"}]
    assert len(artifact["cell_bindings"]) == 3
    assert artifact["cell_bindings"][0] == {"row": 0, "column": "count", "report": "summary", "pointer": "/count", "value": 0}
    assert artifact["cell_bindings"][1]["value"] is None
