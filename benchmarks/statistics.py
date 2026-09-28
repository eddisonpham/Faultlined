"""Deterministic summary statistics for small local benchmark samples."""

from __future__ import annotations

import math
import random
from statistics import mean, stdev

from benchmarks.schema import ResultSummary


def nearest_rank(samples: list[float], percentile: float) -> float:
    if not samples:
        raise ValueError("at least one sample is required")
    if not 0 < percentile <= 1:
        raise ValueError("percentile must be in (0, 1]")
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def bootstrap_ci95(
    samples: list[float], *, seed: int, resamples: int = 2_000
) -> tuple[float, float]:
    if not samples:
        raise ValueError("at least one sample is required")
    if resamples < 100:
        raise ValueError("at least 100 bootstrap resamples are required")
    generator = random.Random(seed)
    boot_means = sorted(mean(generator.choices(samples, k=len(samples))) for _ in range(resamples))
    return nearest_rank(boot_means, 0.025), nearest_rank(boot_means, 0.975)


def summarize(
    samples: list[float],
    *,
    warmup_count: int,
    failures: int,
    seed: int,
    warmup_failure_count: int = 0,
) -> ResultSummary:
    if not samples:
        return ResultSummary(
            n=0,
            warmup_count=warmup_count,
            warmup_failure_count=warmup_failure_count,
            p50_seconds=0.0,
            p95_seconds=0.0,
            p99_seconds=0.0,
            mean_seconds=0.0,
            stddev_seconds=0.0,
            ci95_mean_seconds=(0.0, 0.0),
            failure_count=failures,
        )
    return ResultSummary(
        n=len(samples),
        warmup_count=warmup_count,
        warmup_failure_count=warmup_failure_count,
        p50_seconds=nearest_rank(samples, 0.50),
        p95_seconds=nearest_rank(samples, 0.95),
        p99_seconds=nearest_rank(samples, 0.99),
        mean_seconds=mean(samples),
        stddev_seconds=stdev(samples) if len(samples) > 1 else 0.0,
        ci95_mean_seconds=bootstrap_ci95(samples, seed=seed),
        failure_count=failures,
    )
