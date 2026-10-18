"""Parse only syntactic GitHub Actions references; no security inference."""

import re
from collections import Counter

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

TARGET = "tj-actions/changed-files"
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


def classify_uses(value, reusable=False):
    result = {"value": value, "kind": "unsupported", "action": None,
              "repository": None, "ref": None, "ref_type": None}
    if not isinstance(value, str):
        return result
    if value.startswith("./"):
        return dict(result, kind="local")
    if value.startswith("docker://"):
        return dict(result, kind="docker")
    if "${{" in value:
        return dict(result, kind="dynamic")
    if "@" not in value:
        return result
    name, ref = value.rsplit("@", 1)
    parts = name.split("/")
    if len(parts) < 2 or not ref or any(not p or re.search(r"\s", p) for p in parts):
        return result
    repository = "/".join(parts[:2]).lower()
    action = "/".join([repository, *parts[2:]])
    ref_type = "full_sha" if FULL_SHA.fullmatch(ref) else "mutable_or_unresolved"
    if ref_type == "full_sha":
        ref = ref.lower()
    return dict(result, kind="reusable_workflow" if reusable else "action",
                action=action, repository=repository, ref=ref, ref_type=ref_type)


def parse_workflow(text, path):
    output = {"path": path, "references": [], "errors": []}
    try:
        root = yaml.compose(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except yaml.YAMLError as error:
        output["errors"].append("invalid_yaml: " + str(error))
        return output
    if root is None:
        output["inactive_empty_document"] = True
        return output

    def mapping(node):
        if not isinstance(node, MappingNode):
            return {}
        values = {}
        for key, value in node.value:
            if not isinstance(key, ScalarNode):
                output["errors"].append("non_scalar_mapping_key")
                continue
            if key.value in values:
                output["errors"].append(f"duplicate_key:{key.value}:{key.start_mark.line + 1}")
            if key.value == "<<":
                output["errors"].append("yaml_merge_key_requires_review")
            values[key.value] = value
        return values

    # Inspect the YAML node graph without constructing application objects.
    seen = set()

    def check(node):
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, MappingNode):
            mapping(node)
            for key, value in node.value:
                check(key)
                check(value)
        elif isinstance(node, SequenceNode):
            for child in node.value:
                check(child)

    check(root)
    if output["errors"]:
        return output
    jobs = mapping(root).get("jobs")
    if not isinstance(jobs, MappingNode) or not jobs.value:
        output["errors"].append("missing_or_invalid_jobs")
        return output

    def reference(node, job, step_id, step_index, reusable=False):
        if not isinstance(node, ScalarNode) or node.tag != "tag:yaml.org,2002:str":
            output["errors"].append("non_string_uses")
            return
        item = classify_uses(node.value, reusable)
        item.update(path=path, job=job, step_id=step_id, step_index=step_index,
                    line=node.start_mark.line + 1)
        output["references"].append(item)
        if item["kind"] == "unsupported":
            output["errors"].append("unsupported_uses:" + node.value)
        if item["kind"] == "dynamic":
            output["errors"].append("dynamic_uses_requires_review")

    for job_id, job_node in mapping(jobs).items():
        if not isinstance(job_node, MappingNode):
            output["errors"].append("invalid_job:" + job_id)
            continue
        job = mapping(job_node)
        if "uses" in job:
            reference(job["uses"], job_id, None, None, reusable=True)
        steps = job.get("steps")
        if steps is not None and not isinstance(steps, SequenceNode):
            output["errors"].append("invalid_steps:" + job_id)
        elif steps is not None:
            for index, step_node in enumerate(steps.value):
                if not isinstance(step_node, MappingNode):
                    output["errors"].append("invalid_step:" + job_id)
                    continue
                step = mapping(step_node)
                step_id = step.get("id")
                step_id = step_id.value if isinstance(step_id, ScalarNode) else None
                if "uses" in step:
                    reference(step["uses"], job_id, step_id, index)
    return output


def action_counts(references):
    counts = {}
    for ref in references:
        if ref["kind"] != "action":
            continue
        item = counts.setdefault(ref["action"], Counter())
        item[ref["ref_type"]] += 1
    return {key: dict(value) for key, value in sorted(counts.items())}


def changes(before, after):
    """Repository/action aggregates, not step-level replacement claims."""
    previous, current = action_counts(before), action_counts(after)
    events = []
    for action in sorted(previous.keys() | current.keys()):
        old, new = previous.get(action, {}), current.get(action, {})
        if old == new:
            old_sha = Counter(r["ref"] for r in before if r["kind"] == "action" and r["action"] == action and r["ref_type"] == "full_sha")
            new_sha = Counter(r["ref"] for r in after if r["kind"] == "action" and r["action"] == action and r["ref_type"] == "full_sha")
            if old_sha != new_sha:
                events.append({"action": action, "event": "sha_targets_changed", "before": old, "after": new})
            elif Counter(r["ref"] for r in before if r["kind"] == "action" and r["action"] == action) != Counter(r["ref"] for r in after if r["kind"] == "action" and r["action"] == action):
                events.append({"action": action, "event": "mutable_refs_changed", "before": old, "after": new})
            continue
        kind = "action_added" if not old else "action_removed" if not new else "pinning_counts_changed"
        events.append({"action": action, "event": kind, "before": old, "after": new})
    return events

