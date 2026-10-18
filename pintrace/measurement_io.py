"""Read frozen measurements without retaining their repeated raw bodies.

Streaming parser API and numeric options: https://github.com/ICRAR/ijson
"""

from decimal import Decimal
import hashlib
from pathlib import Path

import ijson
from ijson.common import ObjectBuilder

IO_RULES_VERSION = "measurement-io-v1"


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sections(path, prefixes):
    """Yield ``(prefix, value)`` in document order for nonoverlapping prefixes.

    Use ``.item`` to select individual array members. Only the current selected
    value is built; exhausting the iterator validates the complete JSON file.
    Frozen measurement maps have unique keys. Decimal conversion matches
    json.loads floats while keeping integers larger than 64 bits intact.
    """
    prefixes = set(prefixes)
    builder, selected, depth = None, None, 0
    with Path(path).open("rb") as stream:
        for prefix, event, value in ijson.parse(stream):
            if builder is None:
                if prefix not in prefixes or event in {"map_key", "end_map", "end_array"}:
                    continue
                builder, selected = ObjectBuilder(), prefix
            builder.event(event, float(value) if isinstance(value, Decimal) else value)
            if event in {"start_map", "start_array"}:
                depth += 1
            elif event in {"end_map", "end_array"}:
                depth -= 1
            if depth == 0:
                yield selected, builder.value
                builder = None


def load_analysis_measurement(path, project_reference):
    """Load the compact riskset/join view, projecting each reference immediately."""
    result = dict(summary=None, snapshots=[], timeline={}, replay={"passed": False},
                  lineages=dict(conservation={"passed": False}, references=[], events=[]))
    prefixes = {"summary", "snapshots.item", "timeline", "replay.passed",
                "lineages.conservation.passed", "lineages.references.item", "lineages.events.item"}
    has_summary = False
    for prefix, value in sections(path, prefixes):
        if prefix == "summary":
            result["summary"], has_summary = value, True
        elif prefix == "snapshots.item":
            result["snapshots"].append({key: value[key] for key in ("sha", "timestamp", "date", "complete") if key in value})
        elif prefix == "timeline":
            result["timeline"] = value
        elif prefix == "replay.passed":
            result["replay"]["passed"] = value
        elif prefix == "lineages.conservation.passed":
            result["lineages"]["conservation"]["passed"] = value
        elif prefix == "lineages.references.item":
            result["lineages"]["references"].append(project_reference(value))
        elif prefix == "lineages.events.item":
            result["lineages"]["events"].append(dict(value, before=project_reference(value.get("before")),
                                                    after=project_reference(value.get("after"))))
    if not has_summary:
        raise KeyError("summary")
    return result
