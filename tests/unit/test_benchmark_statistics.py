import pytest
from benchmarks.statistics import bootstrap_ci95, nearest_rank, summarize


@pytest.mark.unit
def test_nearest_rank_percentiles() -> None:
    values = [4.0, 1.0, 3.0, 2.0]
    assert nearest_rank(values, 0.5) == 2.0
    assert nearest_rank(values, 0.95) == 4.0
    with pytest.raises(ValueError):
        nearest_rank([], 0.5)
    with pytest.raises(ValueError):
        nearest_rank(values, 0.0)


@pytest.mark.unit
def test_bootstrap_ci_is_deterministic_and_bounds_mean() -> None:
    values = [0.9, 1.0, 1.1, 1.2, 1.3]
    first = bootstrap_ci95(values, seed=7)
    second = bootstrap_ci95(values, seed=7)
    assert first == second
    assert first[0] <= sum(values) / len(values) <= first[1]


@pytest.mark.unit
def test_summary_preserves_trial_count_failures_and_samples() -> None:
    summary = summarize([0.1, 0.2, 0.3], warmup_count=3, failures=1, seed=1)
    assert summary.n == 3
    assert summary.warmup_count == 3
    assert summary.failure_count == 1
    assert summary.p50_seconds == 0.2
