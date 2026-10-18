import os
import subprocess

from pintrace.history import first_parent_history, workflow_commits, workflow_files


def test_side_branch_change_is_observed_at_merge_not_earlier_commit(tmp_path):
    def command(*args, date=None):
        env = dict(os.environ)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        result = subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test",
                                 "-c", "user.email=test@example.invalid", *args],
                                capture_output=True, text=True, env=env, check=True)
        return result.stdout.strip()

    command("init", "-b", "main")
    (tmp_path / "README.md").write_text("test\n", encoding="utf-8")
    command("add", "README.md")
    command("commit", "-m", "root", date="2025-03-01T00:00:00Z")
    command("checkout", "-b", "feature")
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("jobs: {build: {steps: [{uses: 'a/b@v1'}]}}\n", encoding="utf-8")
    command("add", ".github")
    command("commit", "-m", "feature workflow", date="2025-03-13T12:00:00Z")
    feature = command("rev-parse", "HEAD")
    command("checkout", "main")
    command("merge", "--no-ff", "feature", "-m", "merge workflow", date="2025-03-16T12:00:00Z")
    merge = command("rev-parse", "HEAD")
    assert feature not in {r["sha"] for r in first_parent_history(tmp_path)}
    assert workflow_commits(tmp_path) == {merge}
    assert [r["path"] for r in workflow_files(tmp_path, merge)] == [".github/workflows/ci.yml"]

