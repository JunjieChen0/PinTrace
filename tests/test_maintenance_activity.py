from pintrace.maintenance_activity import DAY, bin_counts, longest_gap_days, profile

ANCHOR = 1_000 * DAY
START, END = ANCHOR - 180 * DAY, ANCHOR + 360 * DAY


def at(day):
    return ANCHOR + day * DAY


def test_bins_assign_commits_to_thirty_day_windows_by_start_day():
    counts = bin_counts([at(-1), at(0), at(29), at(30), at(359)], ANCHOR, -30, 360)
    assert counts[-30] == 1 and counts[0] == 2 and counts[30] == 1 and counts[330] == 1
    assert sum(counts.values()) == 5


def test_longest_gap_counts_the_edges_of_the_interval():
    assert longest_gap_days([at(100)], at(0), at(360)) == 260
    assert longest_gap_days([], at(0), at(360)) == 360


def test_profile_counts_before_and_after_the_anchor_and_flags_end_inactivity():
    times = [at(-200), at(-10), at(5), at(40)]
    result = profile(times, ANCHOR, START, END, max(times))
    assert result["commits_pre_180d"] == 1 and result["commits_pre_365d"] == 2
    assert result["commits_post_180d"] == 2 and result["commits_post_to_end"] == 2
    assert result["days_since_last_commit_at_anchor"] == 10 and result["first_commit_after_anchor_days"] == 5
    assert result["days_since_last_commit_at_end"] == 320
    assert result["no_commit_last_90d_at_end"] is True and result["no_commit_last_180d_at_end"] is True
    assert result["head_before_window_end"] is True


def test_a_commit_inside_the_final_ninety_days_clears_the_inactivity_flag():
    times = [at(-5), at(300)]
    result = profile(times, ANCHOR, START, END, max(times))
    assert result["no_commit_last_90d_at_end"] is False and result["no_commit_last_180d_at_end"] is False


def test_commits_after_the_window_end_do_not_count_toward_end_state():
    times = [at(-5), at(10), at(500)]
    result = profile(times, ANCHOR, START, END, max(times))
    assert result["days_since_last_commit_at_end"] == 350 and result["commits_post_to_end"] == 1
    assert result["head_before_window_end"] is False


import csv
import json
import os
import subprocess

from pintrace.maintenance_activity import collect, commit_times, write


def make_repository(path):
    def git(*args, date=None):
        env = dict(os.environ)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        done = subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
                              capture_output=True, text=True, env=env, check=True)
        return done.stdout.strip()

    git("init", "-b", "main")
    for index, date in enumerate(("2025-01-01T00:00:00Z", "2025-03-20T00:00:00Z")):
        (path / "file.txt").write_text(str(index), encoding="utf-8")
        git("add", "file.txt")
        git("commit", "-m", f"commit {index}", date=date)
    git("checkout", "-b", "side")
    (path / "side.txt").write_text("side", encoding="utf-8")
    git("add", "side.txt")
    git("commit", "-m", "side", date="2025-03-25T00:00:00Z")
    git("checkout", "main")
    git("merge", "--no-ff", "side", "-m", "merge", date="2025-04-01T00:00:00Z")
    return git("rev-parse", "HEAD")


def test_commit_times_follow_the_first_parent_chain_newest_first(tmp_path):
    head = make_repository(tmp_path)
    times = commit_times(tmp_path, head)
    assert times == sorted(times, reverse=True) and len(times) == 3
    # the side-branch commit of 2025-03-25 is not on the first-parent chain
    assert all(not 1742860800 <= t < 1742947200 for t in times)


def test_collect_reports_counts_without_author_or_message_fields(tmp_path):
    head = make_repository(tmp_path)
    config = dict(window_start="2024-09-14T00:00:00Z", event_anchor="2025-03-14T00:00:00Z", window_end="2026-09-14T23:59:59Z")
    record = dict(repository_id="1", repository="owner/repo", group="control", git_directory=".", observed_head=head)
    [row] = collect(tmp_path, [record], config)
    assert row["observed_commits"] == 3 and row["commits_pre_180d"] == 1 and row["commits_post_to_end"] == 2
    assert row["first_commit_after_anchor_days"] == 6.0
    assert row["no_commit_last_90d_at_end"] is True and row["head_before_window_end"] is True
    banned = {"author", "author_name", "author_email", "committer", "committer_name", "committer_email", "message", "subject", "sha", "commit"}
    assert not banned & set(row)
    summary = write(tmp_path, [row], config, [])
    header = next(csv.reader((tmp_path / "data/derived/maintenance-activity-v1/activity.csv").open(encoding="utf-8")))
    assert not banned & set(header)
    assert summary["counts"] == {"treated": 0, "control": 1}
    assert json.loads((tmp_path / "data/derived/maintenance-activity-v1/activity_bins.json").read_text(encoding="utf-8")).keys() == {"1"}
