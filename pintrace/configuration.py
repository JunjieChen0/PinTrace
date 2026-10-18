"""Local configuration intent and source-position annotations, never bot execution."""

import hashlib
import json
import re
from collections import Counter

import json5
import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode
from yaml.tokens import AliasToken, AnchorToken, ScalarToken

from pintrace.workflow import parse_workflow

RULE_VERSION = "configuration-v2"
DEPENDABOT_PATHS = (".github/dependabot.yml", ".github/dependabot.yaml")
RENOVATE_PATHS = (
    "renovate.json", "renovate.jsonc", "renovate.json5",
    ".github/renovate.json", ".github/renovate.jsonc", ".github/renovate.json5",
    ".gitlab/renovate.json", ".gitlab/renovate.jsonc", ".gitlab/renovate.json5",
    ".renovaterc", ".renovaterc.json", ".renovaterc.jsonc", ".renovaterc.json5",
    "package.json",
)
CONFIG_PATHS = DEPENDABOT_PATHS + RENOVATE_PATHS
EXCLUDED = "explicitly_excluded_or_paused"
VERSION = re.compile(r"^(?:version\s*[:=]?\s+)?(v?\d+(?:\.\d+){0,2}(?:-[\w.-]+)?(?:\+[\w.-]+)?)(?=\s|$)", re.I)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node):
    result = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.tag == "tag:yaml.org,2002:merge":
            raise ValueError("non_scalar_or_merge_key")
        name = loader.construct_object(key)
        if not isinstance(name, str) or name in result:
            raise ValueError("non_string_or_duplicate_key")
        result[name] = loader.construct_object(value)
    return result


_UniqueLoader.add_constructor("tag:yaml.org,2002:map", _unique_mapping)


def _json_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key:" + key)
        result[key] = value
    return result


def _has_extends(value):
    if isinstance(value, dict):
        return bool(value.get("extends")) or any(_has_extends(v) for v in value.values())
    return isinstance(value, list) and any(_has_extends(v) for v in value)


def parse_configuration(files, branch=None):
    """Parse {repository_relative_path: UTF-8 text} from one historical tree.

    Missing paths must mean an observed absence, not a failed blob fetch. Callers
    retain fetch failures separately and must not evaluate an incomplete tree.
    Current filename discovery is deliberately separate from historical runtime
    compatibility: a parsed file is evidence of local intent, not an active bot.
    """
    output = {"rule_version": RULE_VERSION, "branch": branch, "entries": [],
              "errors": [], "inheritance_unknown": False,
              "history_semantics": "local_snapshot_only"}
    for path in CONFIG_PATHS:
        if path not in files:
            continue
        text = files[path]
        entry = {"tool": "dependabot" if path in DEPENDABOT_PATHS else "renovate",
                 "path": path, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                 "parse_state": "parsed", "data": None, "errors": [],
                 "inheritance_unknown": False, "syntax_compatibility_unknown": False}
        try:
            if entry["tool"] == "dependabot":
                data = yaml.load(text, Loader=_UniqueLoader)
            elif path == "package.json":
                data = json.loads(text, object_pairs_hook=_json_unique)
                if not isinstance(data, dict):
                    raise ValueError("configuration_root_not_object")
                if "renovate" not in data:
                    continue
                data = data["renovate"]
            else:
                data = json5.loads(text, allow_duplicate_keys=False)
                # JSON5 is a parser superset. Non-strict JSON outside .json5 is
                # retained, but requires a matching historical tool replay.
                if not path.endswith(".json5"):
                    try:
                        json.loads(text)
                    except ValueError:
                        entry["syntax_compatibility_unknown"] = True
            if not isinstance(data, dict):
                raise ValueError("configuration_root_not_object")
            json.dumps(data, allow_nan=False)
            entry["data"] = data
            entry["inheritance_unknown"] = entry["tool"] == "renovate" and _has_extends(data)
        except (yaml.YAMLError, ValueError, TypeError, RecursionError) as error:
            entry["parse_state"] = "invalid_or_unsupported"
            entry["errors"] = [str(error)]
            output["errors"].append({"path": path, "error": str(error)})
        output["entries"].append(entry)
    renovate = [e for e in output["entries"] if e["tool"] == "renovate"]
    output["inheritance_unknown"] = not renovate or any(e["inheritance_unknown"] for e in renovate)
    return output


def _state(status, reason, paths):
    return {"status": status, "reasons": [reason], "paths": paths}


def _name_match(pattern, reference):
    """Dependabot documents '*' only; sub-action naming differences stay unknown."""
    if not isinstance(pattern, str) or any(c in pattern for c in "?[]{}!\\"):
        return None
    regex = "^" + re.escape(pattern.lower()).replace(r"\*", ".*") + "$"
    names = {reference.get("action"), reference.get("repository")} - {None}
    matches = {bool(re.fullmatch(regex, name.lower())) for name in names}
    return matches.pop() if len(matches) == 1 else None


def _dependabot_entry(reference, data, branch):
    if data.get("version") != 2 or not isinstance(data.get("updates"), list):
        return "unknown", "invalid_dependabot_schema"
    updates = data["updates"]
    if any(not isinstance(u, dict) for u in updates):
        return "unknown", "invalid_update_entry"
    candidates, uncertain = [], False
    for update in updates:
        if update.get("package-ecosystem") != "github-actions":
            continue
        target = update.get("target-branch")
        if target is not None:
            if branch is None or not isinstance(target, str):
                uncertain = True
                continue
            if target != branch:
                continue
        directory = update.get("directory")
        if directory is None and update.get("directories") == ["/"]:
            directory = "/"
        if directory != "/":
            uncertain = True
            continue
        candidates.append(update)
    if uncertain or len(candidates) > 1:
        return "unknown", "directory_branch_or_overlapping_entries_unresolved"
    if not candidates:
        return EXCLUDED, "no_github_actions_entry_for_observed_branch"
    update = candidates[0]
    schedule = update.get("schedule")
    if not isinstance(schedule, dict) or not isinstance(schedule.get("interval"), str):
        return "unknown", "missing_or_invalid_schedule"
    if schedule["interval"] not in {"daily", "weekly", "monthly"}:
        return "unknown", "schedule_interval_requires_historical_tool_semantics"
    limit = update.get("open-pull-requests-limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit == 0:
        return EXCLUDED, "version_updates_paused_by_zero_pr_limit"
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
        return "unknown", "invalid_pr_limit"
    if "exclude-paths" in update:
        return "unknown", "exclude_paths_require_historical_tool_semantics"
    ignored = update.get("ignore", [])
    allowed = update.get("allow")
    if not isinstance(ignored, list) or (allowed is not None and not isinstance(allowed, list)):
        return "unknown", "invalid_allow_or_ignore"
    for rule in ignored:
        if not isinstance(rule, dict) or "dependency-name" not in rule:
            return "unknown", "unsupported_ignore_rule"
        matched = _name_match(rule["dependency-name"], reference)
        if matched is None:
            return "unknown", "ignore_dependency_match_unresolved"
        if matched:
            if rule.get("versions") or rule.get("update-types"):
                return "unknown", "opportunity_conditioned_ignore"
            if set(rule) - {"dependency-name"}:
                return "unknown", "unsupported_ignore_rule"
            return EXCLUDED, "dependency_explicitly_ignored"
    if allowed is not None:
        matches = []
        for rule in allowed:
            if not isinstance(rule, dict) or set(rule) - {"dependency-name", "dependency-type"}:
                return "unknown", "unsupported_allow_rule"
            if rule.get("dependency-type", "direct") not in ("direct", "all"):
                return "unknown", "unsupported_allow_dependency_type"
            matches.append(_name_match(rule.get("dependency-name", "*"), reference))
        if None in matches:
            return "unknown", "allow_dependency_match_unresolved"
        if not any(matches):
            return EXCLUDED, "dependency_not_in_allow_list"
    return "covered", "local_github_actions_version_update_entry"


def _path_match(patterns, path):
    """Only literal paths, '*' and directory/**; other minimatch syntax is unknown."""
    if not isinstance(patterns, list) or any(not isinstance(p, str) for p in patterns):
        return None
    unknown = False
    for pattern in patterns:
        if pattern == path or pattern in {"*", "**", "**/*"}:
            return True
        if pattern.endswith("/**") and not any(c in pattern[:-3] for c in "*?[]{}!\\"):
            if path.startswith(pattern[:-3].rstrip("/") + "/"):
                return True
        elif any(c in pattern for c in "*?[]{}!\\"):
            unknown = True
    return None if unknown else False


def _renovate_entry(reference, entry, branch):
    data = entry["data"]
    if entry["syntax_compatibility_unknown"]:
        return "unknown", "non_strict_json_syntax_requires_versioned_replay"
    if data.get("enabled") is False:
        return EXCLUDED, "repository_explicitly_disabled"
    if "enabled" in data and not isinstance(data["enabled"], bool):
        return "unknown", "invalid_enabled_value"
    managers = data.get("enabledManagers")
    if managers is not None:
        if not isinstance(managers, list) or any(not isinstance(m, str) for m in managers):
            return "unknown", "invalid_enabled_managers"
        if "github-actions" not in managers:
            return EXCLUDED, "github_actions_manager_not_enabled"
    manager = data.get("github-actions", {})
    if not isinstance(manager, dict):
        return "unknown", "invalid_github_actions_manager"
    if manager.get("enabled") is False:
        return EXCLUDED, "github_actions_manager_explicitly_disabled"
    if entry["inheritance_unknown"]:
        return "unknown", "unresolved_extends_or_inherited_preset"
    for scope in (data, manager):
        if "packageRules" in scope:
            if not isinstance(scope["packageRules"], list):
                return "unknown", "invalid_package_rules"
            if scope["packageRules"]:
                return "unknown", "package_rule_matching_and_order_require_versioned_replay"
    if any(key in data for key in ("baseBranches", "baseBranchPatterns")):
        return "unknown", "base_branch_patterns_require_versioned_replay"
    if "enabled" in manager and not isinstance(manager["enabled"], bool):
        return "unknown", "invalid_manager_enabled_value"
    for scope in (data, manager):
        if "ignoreDeps" in scope:
            ignored = scope["ignoreDeps"]
            if not isinstance(ignored, list) or any(not isinstance(n, str) for n in ignored):
                return "unknown", "invalid_ignore_deps"
            names = {reference.get("action"), reference.get("repository")} - {None}
            matched = {name in ignored for name in names}
            if len(matched) > 1:
                return "unknown", "sub_action_dependency_name_unresolved"
            if True in matched:
                return EXCLUDED, "dependency_explicitly_ignored"
        for key in ("ignorePaths", "includePaths"):
            if key not in scope:
                continue
            matched = _path_match(scope[key], reference.get("path", ""))
            if matched is None:
                return "unknown", "path_pattern_matching_requires_versioned_replay"
            if (key == "ignorePaths" and matched) or (key == "includePaths" and not matched):
                return EXCLUDED, key + "_excludes_workflow"
        if any(key in scope for key in ("fileMatch", "managerFilePatterns", "matchPaths")):
            return "unknown", "manager_file_patterns_require_versioned_replay"
    return "covered", "local_configuration_allows_default_github_actions_manager"


def coverage_for(reference, configuration, branch=None):
    """Return local configuration coverage; never infer an operational bot.

    'covered' is permitted for one clearly covering local tool even when another
    tool is unknown. 'explicitly_excluded_or_paused' describes observed local
    settings, not proof that every external automation source is disabled.
    """
    branch = configuration.get("branch") if branch is None else branch
    by_tool = {}
    for tool in ("dependabot", "renovate"):
        entries = [e for e in configuration["entries"] if e["tool"] == tool]
        paths = [e["path"] for e in entries]
        if not entries:
            result = _state("no_local_config_observed", "no_local_entry_observed", paths)
        elif len(entries) > 1:
            result = _state("unknown", "multiple_local_entries_historical_precedence_unresolved", paths)
        elif entries[0]["parse_state"] != "parsed":
            result = _state("unknown", "configuration_parse_failed", paths)
        elif reference.get("kind") not in {"action", "reusable_workflow"}:
            result = _state("unknown", "not_a_static_remote_reference", paths)
        else:
            status, reason = (_dependabot_entry(reference, entries[0]["data"], branch)
                              if tool == "dependabot" else _renovate_entry(reference, entries[0], branch))
            result = _state(status, reason, paths)
        by_tool[tool] = result
    statuses = [result["status"] for result in by_tool.values()]
    status = ("covered" if "covered" in statuses else "unknown" if "unknown" in statuses
              else EXCLUDED if EXCLUDED in statuses else "no_local_config_observed")
    return {"status": status, "inheritance_unknown": configuration["inheritance_unknown"],
            "reasons": [tool + ":" + reason for tool, result in by_tool.items() for reason in result["reasons"]],
            "by_tool": by_tool, "scope": "local_configuration_intent",
            "global_configuration_unknown": True}


def _mapping(node):
    return {key.value: value for key, value in node.value} if isinstance(node, MappingNode) else {}


def _canonical(node, active=None):
    active = set() if active is None else active
    if id(node) in active:
        raise ValueError("recursive_yaml_node")
    active = active | {id(node)}
    if isinstance(node, ScalarNode):
        return [node.tag, node.value]
    if isinstance(node, SequenceNode):
        return [_canonical(child, active) for child in node.value]
    return sorted([_canonical(key, active), _canonical(value, active)] for key, value in node.value)


def _comments(text, tokens):
    """Find '#' only outside scalar-token spans, including quoted and block text."""
    lines = text.splitlines()
    spans = {}
    for token in tokens:
        if not isinstance(token, ScalarToken):
            continue
        for line in range(token.start_mark.line, token.end_mark.line + 1):
            start = token.start_mark.column if line == token.start_mark.line else 0
            end = token.end_mark.column if line == token.end_mark.line else len(lines[line])
            spans.setdefault(line, []).append((start, end))
    result = {}
    for index, line in enumerate(lines):
        for match in re.finditer("#", line):
            column = match.start()
            if not any(start <= column < end for start, end in spans.get(index, [])):
                result[index] = (column, line[column + 1:].strip())
                break
    return result


def enrich_workflow(text, path):
    """Keep parse_workflow's result and add comments plus stable step context.

    Context excludes uses and source positions. Empty contexts are None. Aliases
    and multiline scalar comment attachment remain explicitly unresolved.
    Comment version identity is never verified without an upstream mapping.
    """
    output = parse_workflow(text, path)
    output.update(enrichment_rule_version=RULE_VERSION, enrichment_errors=[])
    for reference in output["references"]:
        reference.update(comment_kind="none", comment_text=None, comment_version=None,
                         comment_identity_state="none", comment_attachment_state="ambiguous",
                         step_name=None, step_context=None, context_state="workflow_parse_incomplete")
    if output["errors"] or not output["references"]:
        return output
    root = yaml.compose(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    tokens = list(yaml.scan(text, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)))
    comments = _comments(text, tokens)
    counts, visited = Counter(), set()

    def count(node):
        counts[id(node)] += 1
        if id(node) in visited:
            return
        visited.add(id(node))
        children = [part for pair in node.value for part in pair] if isinstance(node, MappingNode) else node.value if isinstance(node, SequenceNode) else []
        for child in children:
            count(child)

    count(root)
    jobs = _mapping(_mapping(root).get("jobs"))
    contexts = {}
    for job_id, job_node in jobs.items():
        job = _mapping(job_node)
        if "uses" in job:
            contexts[(job_id, None)] = (job_node, job["uses"])
        steps = job.get("steps")
        if isinstance(steps, SequenceNode):
            for index, step_node in enumerate(steps.value):
                step = _mapping(step_node)
                if "uses" in step:
                    contexts[(job_id, index)] = (step_node, step["uses"])
    line_counts = Counter(node.end_mark.line for _, node in contexts.values())
    for reference in output["references"]:
        step_node, node = contexts[(reference["job"], reference["step_index"])]
        step = _mapping(step_node)
        reference.update(comment_kind="none", comment_text=None, comment_version=None,
                         comment_identity_state="none", comment_attachment_state="none",
                         step_name=step["name"].value if isinstance(step.get("name"), ScalarNode) else None,
                         step_context=None, context_state="resolved")
        aliases = counts[id(node)] > 1 or counts[id(step_node)] > 1 or any(
            isinstance(token, (AliasToken, AnchorToken)) and
            step_node.start_mark.index <= token.start_mark.index < step_node.end_mark.index for token in tokens)
        try:
            content = sorted([_canonical(key), _canonical(value)] for key, value in step_node.value if key.value != "uses")
            if content and not aliases:
                reference["step_context"] = hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        except ValueError:
            aliases = True
        if aliases:
            reference["context_state"] = "alias_or_shared_node_unknown"
            reference["comment_attachment_state"] = "ambiguous"
            continue
        if node.start_mark.line != node.end_mark.line or node.style in {"|", ">"}:
            reference["comment_attachment_state"] = "multiline_ambiguous"
            continue
        observed = comments.get(node.end_mark.line)
        if observed is None or observed[0] < node.end_mark.column:
            continue
        column, comment = observed
        reference["comment_text"] = comment
        between = text[node.end_mark.index:node.end_mark.index + column - node.end_mark.column]
        if line_counts[node.end_mark.line] > 1 or between.strip(" \t,}]"):
            reference.update(comment_kind="other", comment_attachment_state="ambiguous")
            continue
        reference["comment_attachment_state"] = "attached"
        ratchet = re.search(r"(?:^|\s)ratchet:(\S+)", comment)
        version = VERSION.match(comment)
        if ratchet:
            reference["comment_kind"] = "ratchet"
            value = ratchet.group(1)
            if "@" in value:
                reference["comment_version"] = value.rsplit("@", 1)[1]
                reference["comment_action"] = value.rsplit("@", 1)[0].lower()
        elif version:
            reference.update(comment_kind="semantic_version", comment_version=version.group(1))
        else:
            reference["comment_kind"] = "other"
        if reference["comment_version"] is not None:
            reference["comment_identity_state"] = "unverified"
    return output
