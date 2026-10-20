import random

import numpy as np
import pytest

from pintrace.formal_competing import CODES, cumulative_incidence, eligible_rows, summarize_stratum
from pintrace.formal_extensions import Clustered

DAY = 86400


def reference(times, causes, grid, n_causes, weights=None):
    """Aalen-Johansen written with plain loops, one cause at a time, as an independent check."""
    weights = weights or [1.0] * len(times)
    order = sorted(set(t for t, c in zip(times, causes) if c > 0))
    survival, cif = 1.0, [0.0] * n_causes
    path = []
    for t in order:
        at_risk = sum(w for tt, w in zip(times, weights) if tt >= t)
        events = [sum(w for tt, c, w in zip(times, causes, weights) if tt == t and c == k) for k in range(1, n_causes + 1)]
        if at_risk > 0:
            for k in range(n_causes):
                cif[k] += survival * events[k] / at_risk
            survival *= 1 - sum(events) / at_risk
        path.append((t, list(cif)))
    out = []
    for g in grid:
        last = [p for t, p in path if t <= g]
        out.append(last[-1] if last else [0.0] * n_causes)
    return np.array(out).T  # (causes, grid)


def test_hand_computed_example_with_two_causes_and_censoring():
    times, causes = [1, 2, 3, 4, 5], [1, 2, 0, 1, 0]
    cif, valid = cumulative_incidence(times, causes, [[1] * 5], [0.5, 1, 2, 3, 4, 5], 2)
    # t=1: 1 of 5 has cause 1; t=2: 1 of 4 has cause 2 (survival .8); t=4: 1 of 2 has cause 1 (survival .6)
    assert cif[0, 0].tolist() == pytest.approx([0, .2, .2, .2, .5, .5])
    assert cif[0, 1].tolist() == pytest.approx([0, 0, .2, .2, .2, .2])
    assert valid[0].tolist() == [True] * 6


def test_without_censoring_the_incidence_is_the_empirical_fraction():
    times, causes = [1, 2, 2, 3, 6], [1, 2, 1, 2, 1]
    cif, _ = cumulative_incidence(times, causes, [[1] * 5], [2, 6], 2)
    assert cif[0, 0].tolist() == pytest.approx([2 / 5, 3 / 5])
    assert cif[0, 1].tolist() == pytest.approx([1 / 5, 2 / 5])


def test_censoring_at_an_event_time_keeps_the_period_at_risk_for_that_event():
    cif, _ = cumulative_incidence([2, 2], [1, 0], [[1, 1]], [2], 1)
    assert cif[0, 0, 0] == pytest.approx(0.5)  # the censored period was still at risk at t=2


def test_matches_an_independent_reference_on_random_data_with_ties_and_weights():
    rng = random.Random(7)
    checked = 0
    for _ in range(60):
        n = rng.randint(3, 40)
        times = [rng.randint(1, 12) for _ in range(n)]
        causes = [rng.choice([0, 0, 1, 2, 3, 4]) for _ in range(n)]
        weights = [float(rng.randint(0, 3)) for _ in range(n)]
        grid = [0, 1, 3, 6, 9, 12, 15]
        if sum(weights) == 0:
            continue
        mine, valid = cumulative_incidence(times, causes, [weights], grid, 4)
        assert mine[0] == pytest.approx(reference(times, causes, grid, 4, weights), abs=1e-12)
        assert valid[0].tolist() == [sum(w for t, w in zip(times, weights) if t >= g) > 0 for g in grid]
        checked += 1
    assert checked > 40


def test_integer_weight_equals_repeating_the_episode():
    weighted, _ = cumulative_incidence([1, 3, 4, 7], [1, 2, 0, 1], [[2, 1, 3, 1]], [1, 3, 7], 2)
    repeated, _ = cumulative_incidence([1, 1, 3, 4, 4, 4, 7], [1, 1, 2, 0, 0, 0, 1], [[1] * 7], [1, 3, 7], 2)
    assert weighted == pytest.approx(repeated)


def test_horizon_beyond_all_follow_up_is_marked_not_estimable():
    _, valid = cumulative_incidence([1, 2], [1, 0], [[1, 1]], [2, 10], 1)
    assert valid[0].tolist() == [True, False]


def test_cumulative_incidence_never_exceeds_one_and_never_decreases():
    rng = random.Random(3)
    times = [rng.randint(1, 50) for _ in range(200)]
    causes = [rng.choice([0, 1, 2, 3, 4]) for _ in range(200)]
    cif, _ = cumulative_incidence(times, causes, [[1] * 200], list(range(0, 55, 5)), 4)
    assert (np.diff(cif[0], axis=1) >= -1e-12).all()
    assert (cif[0].sum(axis=0) <= 1 + 1e-12).all()


def row(repository, exit_kind, days, kind="baseline_fixed"):
    return dict(repository_id=repository, exit_kind=exit_kind, duration=float(days), episode_kind=kind, episode_id=f"{repository}-{exit_kind}-{days}")


def test_stratum_reports_bounds_in_the_expected_order_and_counts_causes():
    kinds = ["unpin", "remove", "replace", "ambiguous_transition", "study_window_censored", "observable_ancestry_censored"]
    rows = [row(str(i), kinds[i % 6], 20 + 30 * (i % 7)) for i in range(60)]
    result = summarize_stratum(rows, Clustered(sorted({r["repository_id"] for r in rows})))
    assert result["episodes"] == 60 and result["censored"] == 20 and not result["meets_planning_threshold"]
    assert result["cause_counts"] == {"unpin": 10, "remove": 10, "replace": 10, "unknown": 10}
    estimable = 0
    for horizon in result["horizons"].values():
        ci = horizon["cumulative_incidence"]
        if horizon["at_risk"] == 0:
            # nobody is observed that long (longest follow-up here is 200 days), so no incidence is claimed
            assert all(series["estimate"] is None and series["status"] == "undefined" for series in ci.values())
            continue
        estimable += 1
        assert ci["any_exit_including_unknown"]["estimate"] >= ci["known_exit_unknown_competing"]["estimate"] - 1e-12
        assert ci["known_exit_unknown_competing"]["estimate"] == pytest.approx(sum(ci[c]["estimate"] for c in ("unpin", "remove", "replace")))
    assert estimable == 3 and result["horizons"]["365"]["at_risk"] == 0
    first = result["horizons"]["30"]["cumulative_incidence"]["unpin"]
    assert first["requested_replicates"] == 2000 and first["seed"] == 20261018


def test_stratum_is_reproducible_and_flags_a_sparse_tail():
    rows = [row(str(i), "remove" if i % 2 else "study_window_censored", 10 + i) for i in range(30)]
    cluster = Clustered(sorted({r["repository_id"] for r in rows}))
    first, second = summarize_stratum(rows, cluster), summarize_stratum(rows, cluster)
    assert first == second
    tail = first["horizons"]["365"]
    assert tail["sparse_tail"] is True and tail["at_risk"] == 0
    assert tail["cumulative_incidence"]["remove"]["status"] == "undefined"


def test_stratum_with_no_events_is_degenerate_not_a_claim_of_safety():
    rows = [row(str(i), "study_window_censored", 400) for i in range(12)]
    result = summarize_stratum(rows, Clustered(sorted({r["repository_id"] for r in rows})))
    ci = result["horizons"]["365"]["cumulative_incidence"]["any_exit_including_unknown"]
    assert ci["estimate"] == 0 and ci["status"] == "zero_degenerate"


def test_empty_stratum_is_reported_as_empty():
    assert summarize_stratum([], Clustered(["1"])) == dict(episodes=0, repositories=0)


def test_eligible_rows_filters_and_rejects_bad_input():
    base = dict(formal="True", duration_eligible="True", entry_timestamp="0", exit_timestamp=str(10 * DAY), exit_kind="remove", repository_id=5)
    kept = eligible_rows([base, dict(base, formal="False"), dict(base, duration_eligible="False")])
    assert len(kept) == 1 and kept[0]["repository_id"] == "5" and kept[0]["duration"] == 10
    with pytest.raises(ValueError, match="Unexpected exit kind"):
        eligible_rows([dict(base, exit_kind="mystery")])
    with pytest.raises(ValueError, match="positive observed duration"):
        eligible_rows([dict(base, exit_timestamp="0")])
    assert set(CODES.values()) == {0, 1, 2, 3, 4}
