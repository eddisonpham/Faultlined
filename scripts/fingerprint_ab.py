"""A/B the curation claim, end to end through the real entry point (ADR 0032).

just run
uv run --all-extras python scripts/fingerprint_ab.py --behaviours 24 --copies 4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = os.environ.get("DE_API_URL", "http://127.0.0.1:8000")
FRAMES = 90
DIMS = 4
SEED = 20261007
TERMINAL = {"succeeded", "failed", "canceled", "timed_out"}

PROFILE = {"name": "ab-curation", "version": "1"}


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"{API}{path}", data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"{method} {path} -> {exc.code}: {detail}") from exc


def _wait(job_id: str, *, timeout: float = 900.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = _request("GET", f"/api/v1/jobs/{job_id}")
        if job["state"] in TERMINAL:
            return job
        time.sleep(0.25)
    raise SystemExit(f"job {job_id} did not settle in {timeout}s")


def _submit(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _request("POST", "/api/v1/jobs", {"type": kind, "payload": payload})


def _series(behaviour: int, *, jitter: int, rng: list[int]) -> list[list[float]]:
    """One recording of one behaviour as `observations` rows."""
    period = 4.0 + behaviour * 0.75
    scale = 1.0 + 0.012 * jitter
    phase = 0.03 * jitter
    rows: list[list[float]] = []
    for frame in range(FRAMES):
        row = []
        for joint in range(DIMS):
            weight = 0.35 + 0.08 * behaviour + 0.05 * joint
            noise = ((rng[0] * (frame + 1) + joint * 31) % 97) / 9700.0
            row.append(
                scale
                * weight
                * math.sin(2 * math.pi * frame / (period + joint * 0.5) + phase + noise)
            )
        rows.append(row)
    return rows


def _episode(behaviour: int, copy: int, *, task: str) -> dict[str, Any]:
    rng = [17 + behaviour * 7 + copy]
    rows = _series(behaviour, jitter=copy, rng=rng)
    return {
        "task": task,
        "robot": "so101",
        "timestamps": [frame * 0.05 for frame in range(FRAMES)],
        "observations": rows,
        "actions": rows,
    }


class Watch:
    """Polls the platform's own telemetry while the run happens."""

    def __init__(self) -> None:
        self.samples = 0
        self.health: list[dict[str, Any]] = []
        self.failures = 0

    def poll(self) -> None:
        try:
            self.health.append(_request("GET", "/api/v1/monitoring/health"))
            self.samples += 1
        except SystemExit:
            self.failures += 1

    def summary(self) -> dict[str, Any]:
        blind = [item.get("blind") for item in self.health]
        return {
            "samples": self.samples,
            "unreachable": self.failures,
            "blind_values": sorted({str(value) for value in blind}),
            "last_tick_at": self.health[-1].get("last_tick_at") if self.health else None,
        }


def _coverage(build_hash: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    report = _request("GET", f"/api/v1/builds/{build_hash}/coverage")
    return report, (time.perf_counter() - started) * 1000


def _present(report: dict[str, Any]) -> dict[str, int]:
    """Distinct values per axis, taken from the report's own totals rather than from the list."""
    return {str(axis["axis"]): int(axis["present"]) for axis in report["axes"]}


def _print_coverage(label: str, report: dict[str, Any], ms: float) -> None:
    print(
        f"    {label}: {report['episode_count']} episodes · "
        f"{report['coverage_ratio'] * 100:.0f}% of the catalog · "
        f"{report['vocabulary_missing']} of {report['vocabulary_total']} tasks absent · "
        f"{ms:.0f} ms · payload bytes {len(json.dumps(report))}"
    )
    for axis in report["axes"]:
        held = ", ".join(f"{item['value']} x{item['count']}" for item in list(axis["values"])[:4])
        hidden = len(axis["values"]) - 4
        if hidden > 0:
            held += f", +{hidden} more"
        gaps = ", ".join(axis["gaps"][:4])
        if axis["missing"] > len(axis["gaps"]):
            gaps += f", +{axis['missing'] - len(axis['gaps'])} more"
        print(
            f"      {axis['label']:<11s} {axis['present']:>3d} distinct · "
            f"holds {held or 'nothing'} · missing: {gaps or 'nothing on this axis'}"
        )


def _preflight() -> None:
    health = _request("GET", "/api/v1/health")
    episodes = _request("GET", "/api/v1/episodes?limit=1")["items"]
    builds = _request("GET", "/api/v1/builds?limit=1")["items"]
    if episodes or builds:
        count = len(_request("GET", "/api/v1/episodes?limit=500")["items"])
        raise SystemExit(
            f"refusing to run: the catalog already holds {count} episodes and "
            f"{len(builds)} builds. This driver counts absolute state; point it at a fresh "
            f"database (see the docstring)."
        )
    print(f"preflight ok · api {health.get('status', 'up')} · catalog empty")


def _export_size(root: Path, build_hash: str) -> tuple[int, int]:
    target = root / build_hash
    if not target.is_dir():
        return 0, 0
    files = [path for path in target.rglob("*") if path.is_file()]
    return sum(path.stat().st_size for path in files), len(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--behaviours", type=int, default=24)
    parser.add_argument("--copies", type=int, default=4, help="recordings per behaviour")
    parser.add_argument("--export-root", default=os.environ.get("DE_EXPORT_ROOT", ""))
    args = parser.parse_args()

    _preflight()
    watch = Watch()
    started = time.perf_counter()

    print(f"\n[1] ingesting {args.behaviours} behaviours x {args.copies} takes")
    ingest_started = time.perf_counter()
    for behaviour in range(args.behaviours):
        for copy in range(args.copies):
            episode = _episode(behaviour, copy, task=f"behaviour {behaviour}")
            job = _submit("ingest", {"episode": episode})
            settled = _wait(str(job["id"]))
            if settled["state"] != "succeeded":
                raise SystemExit(f"ingest failed: {settled.get('error')}")
            watch.poll()
    ingest_seconds = time.perf_counter() - ingest_started
    episodes = _request("GET", "/api/v1/episodes?limit=500")["items"]
    total = len(episodes)
    print(
        f"    {total} episodes in {ingest_seconds:.1f}s "
        f"({ingest_seconds / max(total, 1) * 1000:.0f} ms/episode)"
    )

    print("\n[2] validating every episode")
    settle_started = time.perf_counter()
    submitted = _submit("validate", {"episode_ids": [], "profile": PROFILE})
    validate = _wait(str(submitted["id"]))
    valid = [
        str(row["id"]) for row in _request("GET", "/api/v1/episodes?state=valid&limit=500")["items"]
    ]
    print(
        f"    {validate['state']} in {time.perf_counter() - settle_started:.1f}s · "
        f"{len(valid)} of {total} valid"
    )
    if not valid:
        raise SystemExit(
            f"nothing validated, so there is nothing to build: {validate.get('error')}"
        )
    watch.poll()

    print("\n[3] arm A (control): build from every valid episode")
    arm_a_started = time.perf_counter()
    build_a = _wait(
        str(
            _submit("build", {"name": "all-episodes", "episode_ids": valid, "profile": PROFILE})[
                "id"
            ]
        )
    )
    hash_a = str((build_a.get("result") or {}).get("build_hash") or "")
    if not hash_a:
        raise SystemExit(f"arm A build failed: {build_a.get('error')}")
    export_a = _wait(str(_submit("export", {"build_hash": hash_a})["id"]))
    arm_a = time.perf_counter() - arm_a_started
    watch.poll()

    print("\n[4] the redundancy report, as the page asks for it")
    report_started = time.perf_counter()
    report = _request("GET", f"/api/v1/builds/{hash_a}/redundancy")
    report_seconds = time.perf_counter() - report_started
    dropped = {
        str(duplicate["episode_id"])
        for group in report["groups"]
        for duplicate in group["duplicates"]
    }
    kept = sorted(episode_id for episode_id in valid if episode_id not in dropped)
    print(
        f"    {report['episode_count']} scored · {report['distinct_count']} distinct · "
        f"{report['redundant_count']} near-duplicate · {report['reduction_ratio'] * 100:.0f}% "
        f"redundant · {report_seconds * 1000:.0f} ms · truncated={report['truncated']}"
    )

    print("\n[5] arm B (treatment): build from the representatives")
    arm_b_started = time.perf_counter()
    build_b = _wait(
        str(
            _submit(
                "build", {"name": "distinct-episodes", "episode_ids": kept, "profile": PROFILE}
            )["id"]
        )
    )
    hash_b = str((build_b.get("result") or {}).get("build_hash") or "")
    if not hash_b:
        raise SystemExit(f"arm B build failed: {build_b.get('error')}")
    export_b = _wait(str(_submit("export", {"build_hash": hash_b})["id"]))
    arm_b = time.perf_counter() - arm_b_started
    watch.poll()

    print("\n[6] the coverage report, for both builds (ADR 0033)")
    coverage_a, coverage_a_ms = _coverage(hash_a)
    coverage_b, coverage_b_ms = _coverage(hash_b)
    _print_coverage("arm A (all)", coverage_a, coverage_a_ms)
    _print_coverage("arm B (representatives)", coverage_b, coverage_b_ms)
    present_a, present_b = _present(coverage_a), _present(coverage_b)
    lost = {
        axis: present_a[axis] - present_b.get(axis, 0)
        for axis in present_a
        if present_b.get(axis, 0) < present_a[axis]
    }
    if lost:
        print(f"    LOST AXIS VALUES between the arms: {lost}")
    else:
        print(
            f"    no axis lost a value: arm B holds {len(present_b)} axes with "
            f"{sum(present_b.values())} distinct values, the same as arm A "
            f"({sum(present_a.values())}) on {present_a['task']} task(s) of {total} episodes"
        )

    print("\n[7] one monitor tick, through the on-demand route")
    tick = _request("POST", "/api/v1/monitoring/tick")
    metrics = _request("GET", "/api/v1/metrics?window_seconds=3600")

    root = Path(args.export_root) if args.export_root else None
    size_a, files_a = _export_size(root, hash_a) if root else (0, 0)
    size_b, files_b = _export_size(root, hash_b) if root else (0, 0)
    total_seconds = time.perf_counter() - started

    print("\n[8] results")
    print(f"    {'':22s} {'arm A (all)':>14s} {'arm B (distinct)':>18s}")
    print(f"    {'episodes':22s} {len(valid):>14d} {len(kept):>18d}")
    print(f"    {'behaviours planted':22s} {args.behaviours:>14d} {args.behaviours:>18d}")
    print(f"    {'build+export seconds':22s} {arm_a:>14.1f} {arm_b:>18.1f}")
    if root:
        print(f"    {'export bytes':22s} {size_a:>14d} {size_b:>18d}")
        print(f"    {'export files':22s} {files_a:>14d} {files_b:>18d}")
    print(f"    {'export job state':22s} {export_a['state']:>14s} {export_b['state']:>18s}")
    print(f"\n    redundancy report: {report_seconds * 1000:.0f} ms")
    print(
        f"    coverage: arm A {coverage_a_ms:.0f} ms · arm B {coverage_b_ms:.0f} ms · "
        f"task values {present_a['task']} -> {present_b['task']}"
    )
    print(f"    tick: {json.dumps(tick)[:120]}")
    print(f"    monitor: {watch.summary()}")
    print(f"    metrics summaries: {len(metrics.get('summaries') or [])}")
    print(f"    wall clock: {total_seconds:.1f}s")

    print(
        f"\n    planted behaviours retained: arm B holds {len(kept)} episodes for "
        f"{args.behaviours} behaviours = {len(kept) / max(args.behaviours, 1):.2f}x the "
        f"distinct count (1.00x would be a perfect compaction)"
    )
    if root and size_a:
        print(f"    export size: arm B is {size_b / size_a * 100:.0f}% of arm A")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
