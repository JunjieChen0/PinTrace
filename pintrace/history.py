"""First-parent history of the currently observable default-branch ancestry."""

import os
import re
import subprocess
from pathlib import Path


def git(directory, *args):
    result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
                            timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-1000:])
    return result.stdout


def clone(repository, directory):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository identifier")
    directory = Path(directory)
    if not directory.exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "-c", "gc.auto=0", "clone", "--bare", "--filter=blob:none", "--single-branch", "--no-tags",
             "https://github.com/" + repository + ".git", str(directory)],
            capture_output=True, timeout=180,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"})
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace")[-1000:])
    if git(directory, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("Shallow history cannot support this pilot")
    return directory


def first_parent_history(directory):
    records = []
    for line in git(directory, "rev-list", "--first-parent", "--timestamp", "HEAD").decode().splitlines():
        timestamp, sha = line.split()
        records.append({"sha": sha, "timestamp": int(timestamp)})
    return records


def workflow_commits(directory):
    output = git(directory, "log", "--first-parent", "--full-history", "--diff-merges=first-parent",
                 "--no-patch", "--format=%H %ct", "HEAD", "--", ".github/workflows").decode()
    return {line.split()[0] for line in output.splitlines() if line}


def workflow_files(directory, sha):
    output = git(directory, "ls-tree", "-r", "-z", sha, "--", ".github/workflows")
    files = []
    for item in output.split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, kind, blob = metadata.decode().split()
        path = path.decode("utf-8")
        if path.count("/") == 2 and path.lower().endswith((".yml", ".yaml")):
            files.append({"path": path, "blob": blob, "mode": mode, "kind": kind})
    return files

