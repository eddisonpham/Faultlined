#!/usr/bin/env python3
"""Stage-4 scaling campaign driver: NFR-005, NFR-008 concurrency leg, NFR-010.

One throwaway database per leg, real Postgres from the environment, real queue
rows - the black-box counterpart to the in-memory benchmark workloads. The legs:
- ``job-scaling``        submit N ingest_source jobs for one real MCAP, then drain
   with 1 vs 2 worker processes: wall-clock speedup (NFR-008) and
   enqueue->start latency at 100 queued (NFR-005a).
- ``cancel``           enqueue->start p95 with 100 already-queued jobs ahead of
   the measured one (NFR-005a) and cancel-effect latency
   for a queued job (NFR-005b).
- ``api-latency``      10 rps against a live API for the CRUD/list contract
                       (NFR-010). Needs `just api` running separately.

Numbers go to stdout as JSON; this script writes nothing to the repo. It refuses
a non-throwaway database name so a mistake can never touch the dev catalog.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sys
import time
import uuid
from datetime import UTC, timedelta
from datetime import datetime as DateTime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "var" / "real-data" / "so101_pick_place_120s.mcap"


def _require_fixture():
    if not FIXTURE.is_file():
        raise SystemExit(f"fixture not found: {FIXTURE} (scripts/make_mcap_log.py --seconds 120)")


def _psycopg():
    import psycopg

    return psycopg


def _connect(dsn: str):
    psycopg = _psycopg()
    return psycopg.connect(dsn)


def make_database(prefix: str) -> tuple[str, str]:
    """Create `data_engine_scale_<hex>` and return (name, dsn)."""
    psycopg = _psycopg()
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect("postgresql://data_engine@127.0.0.1:55432/postgres", autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    dsn = f"postgresql://data_engine@127.0.0.1:55432/{name}"
    return name, dsn


def drop_database(name: str) -> None:
    psycopg = _psycopg()
    admin = psycopg.connect("postgresql://data_engine@127.0.0.1:55432/postgres", autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.close()


def initialize(dsn: str) -> None:
    from data_engine.catalog.database import initialize_schema
    from data_engine.config import Settings

    initialize_schema(Settings(_env_file=None, database_url=dsn))


def catalog_for(dsn: str):
    from data_engine.catalog.repository import PostgresCatalog
    from data_engine.config import Settings

    return PostgresCatalog(Settings(_env_file=None, database_url=dsn))


def worker_process_main(dsn: str, stop_after_idle: int, ready_path: str) -> None:
    """One real worker process; exits after `stop_after_idle` idle polls."""
    from data_engine.config import Settings
    from data_engine.jobs.worker import IngestWorker

    settings = Settings(_env_file=None, database_url=dsn)
    worker = IngestWorker(settings)
    Path(ready_path).write_text("ready", encoding="utf-8")
    idle = 0
    while idle < stop_after_idle:
        job = worker.process_one()
        if job is None:
            idle += 1
            time.sleep(0.05)
        else:
            idle = 0


def _start_workers(dsn: str, count: int) -> tuple[list[multiprocessing.Process], list[Path]]:
    ready_dir = Path(os.environ["DE_SCALE_TMP"])
    processes: list[multiprocessing.Process] = []
    ready_paths: list[Path] = []
    for _index in range(count):
        ready = ready_dir / f"ready-{uuid.uuid4().hex[:6]}"
        ready_paths.append(ready)
        proc = multiprocessing.Process(
            target=worker_process_main,
            args=(dsn, 20, str(ready)),
            daemon=True,
        )
        proc.start()
        processes.append(proc)
    deadline = time.time() + 30
    for ready in ready_paths:
        while not ready.exists():
            if time.time() > deadline:
                raise SystemExit("worker did not become ready in 30s")
            time.sleep(0.05)
    return processes, ready_paths


def _stop_workers(processes: list[multiprocessing.Process]) -> None:
    for proc in processes:
        if proc.is_alive():
            proc.terminate()
    for proc in processes:
        proc.join(timeout=10)


def _p(numbers: list[float], q: float) -> float:
    ordered = sorted(numbers)
    if not ordered:
        return 0.0
    rank = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[rank]


def _enqueue(catalog, count: int, *, prefix: str) -> list[str]:
    ids: list[str] = []
    for _i in range(count):
        row, _ = catalog.submit_job(
            "ingest_source",
            {"source": str(FIXTURE)},
            f"{prefix}-{uuid.uuid4().hex}",
            str(uuid.uuid4()),
        )
        ids.append(str(row["id"]))
    return ids


def _cancel_one(
    job_id: str,
    catalog,
    deadline: float,
) -> tuple[dict[str, object] | None, float | None]:
    """Cooperatively cancel a running job; return (row, effective_latency) or (None, None)."""
    canceled = False
    effective = None
    started = time.perf_counter()
    while time.perf_counter() < deadline:
        row = catalog.get_job(job_id)
        if str(row.get("state")) == "canceled":
            canceled = True
            effective = time.perf_counter() - started
            break
        if str(row.get("state")) == "failed":
            break
        time.sleep(0.01)
    return (row if canceled else None, effective)


def select_one(catalog) -> str:
    """Return one queued job id (oldest first)."""
    candidates = catalog.list_jobs(state="queued", limit=20)
    if not candidates:
        raise RuntimeError("no queued jobs found")
    return min(
        (j for j in candidates if str(j.get("state")) == "queued"),
        key=lambda j: j.get("created_at") or j.get("id"),
    )


def _drain(catalog, job_ids: list[str], timeout: float) -> tuple[float, int]:
    """Wait until every job is terminal; return (wall, failed_count)."""
    start = time.perf_counter()
    remaining = set(job_ids)
    failed = 0
    while remaining and time.perf_counter() - start < timeout:
        for job_id in list(remaining):
            row = catalog.get_job(job_id)
            state = str(row["state"])
            if state in ("succeeded", "failed", "canceled"):
                if state == "failed":
                    failed += 1
                remaining.discard(job_id)
        time.sleep(0.05)
    wall = time.perf_counter() - start
    return wall, failed


def leg_job_scaling(catalog, dsn: str, _tmpdir: Path) -> dict[str, object]:
    """1 vs 2 workers draining 8 real ingest_source jobs (NFR-008 leg)."""
    outcomes: dict[str, object] = {}
    for workers in (1, 2):
        jobs = _enqueue(catalog, 8, prefix=f"scale-w{workers}")
        procs, _ready = _start_workers(dsn, workers)
        try:
            wall, failed = _drain(catalog, jobs, timeout=600.0)
        finally:
            _stop_workers(procs)
        outcomes[f"workers_{workers}"] = {
            "jobs": 8,
            "wall_seconds": round(wall, 3),
            "failed": failed,
            "throughput_jobs_per_second": round(8 / wall, 3) if wall > 0 else None,
        }
    one = outcomes["workers_1"]["wall_seconds"]  # type: ignore[index]
    two = outcomes["workers_2"]["wall_seconds"]  # type: ignore[index]
    outcomes["speedup_1_to_2"] = round(float(one) / float(two), 3) if two else None
    return outcomes


def leg_enqueue_start(catalog, dsn: str, _tmpdir: Path) -> dict[str, object]:
    """Enqueue->start latency at depth 100 (NFR-005a corner)."""
    _enqueue(catalog, 100, prefix="enq-start-backlog")
    measured = _enqueue(catalog, 20, prefix="enq-start-measured")
    samples: list[float] = []
    procs, _ready = _start_workers(dsn, 1)
    try:
        for job_id in measured:
            started = time.perf_counter()
            while time.perf_counter() - started < 30.0:
                row = catalog.get_job(job_id)
                state = str(row.get("state"))
                if state == "succeeded":
                    samples.append(time.perf_counter() - started)
                    break
                if state == "failed" or state == "canceled":
                    break
                time.sleep(0.01)
    finally:
        _stop_workers(procs)
    return {
        "n": len(samples),
        "p50_s": round(_p(samples, 0.50), 4) if samples else None,
        "p95_s": round(_p(samples, 0.95), 4) if samples else None,
        "max_s": round(max(samples), 4) if samples else None,
        "target_s": 1.0,
        "met": bool(samples) and max(samples) <= 1.0,
    }


def leg_depth_curve(catalog, dsn: str, _tmpdir: Path) -> dict[str, object]:
    """Queue-position wait as a function of depth (NFR-005 E3)."""
    depths = (0, 10, 50, 100, 200)
    results = []
    for depth in depths:
        _enqueue(catalog, depth, prefix=f"depth-{depth}-backlog")
        measured = _enqueue(catalog, 5, prefix=f"depth-{depth}-measured")
        samples = []
        procs, _ready = _start_workers(dsn, 1)
        try:
            for job_id in measured:
                started = time.perf_counter()
                deadline = time.perf_counter() + 60.0
                while time.perf_counter() < deadline:
                    row = catalog.get_job(job_id)
                    state = str(row.get("state"))
                    if state in ("succeeded", "failed", "canceled"):
                        samples.append(time.perf_counter() - started)
                        break
                    time.sleep(0.01)
        finally:
            _stop_workers(procs)
        results.append(
            {
                "depth": depth,
                "wait_p95_s": round(_p(samples, 0.95), 3) if samples else None,
                "n": len(samples),
            }
        )
    return {
        "target_s": 2.0,
        "results": results,
    }


def leg_catalog_scale(catalog, dsn: str, _tmpdir: Path) -> dict[str, object]:
    """10k-episode catalog (NFR-003, NFR-008 read side): query p95 over the
    real repository, and a real validate job over all 10k episodes through a
    real worker process (NFR-002's system-scale reading)."""
    initialize(dsn)
    seed_seconds, episode_ids = _seed_catalog(dsn, 10_000)

    validate_row, _ = catalog.submit_job(
        "validate",
        {
            "episode_ids": [],
            "profile": {
                "name": "scale-campaign",
                "version": "1",
                "required_channels": ["action", "observation.state"],
                "min_frames": 10,
                "max_frames": 100_000,
            },
        },
        f"validate-10k-{uuid.uuid4().hex}",
        str(uuid.uuid4()),
    )
    validate_ids = [str(validate_row["id"])]
    procs, _ready = _start_workers(dsn, 1)
    try:
        validate_wall, _validate_failed = _drain(catalog, validate_ids, timeout=3600.0)
    finally:
        _stop_workers(procs)
    finished = catalog.get_job(validate_ids[0])
    result = finished.get("result") or {}

    queries = _sample_catalog_queries(catalog, episode_ids)

    return {
        "seed": {"episodes": len(episode_ids), "seed_seconds": round(seed_seconds, 2)},
        "validate_job": {
            "episodes_requested": 10_000,
            "wall_s": round(validate_wall, 2),
            "state": str(finished["state"]),
            "checked": result.get("checked"),
            "passed": result.get("passed"),
            "failed_count": result.get("failed"),
            "per_episode_ms": round(validate_wall / 10_000 * 1000, 3),
        },
        "queries_p95_target_ms": 200.0,
        "queries": queries,
        "database": dsn.split("/")[-1],
    }


def leg_cancel(catalog, dsn: str, _tmpdir: Path) -> dict[str, object]:
    """Cancel a queued job; effect <= 2s (NFR-005b). Real effect via the worker."""
    backlogs = _enqueue(catalog, 60, prefix="cancel-backlog")
    measured = _enqueue(catalog, 10, prefix="cancel-measured")
    samples: list[float] = []
    canceled_ok = 0
    procs, _ready = _start_workers(dsn, 1)
    try:
        for job_id in measured:
            catalog.request_cancel(job_id)
            deadline = time.perf_counter() + 30.0
            requested = time.perf_counter()
            effective = None
            while time.perf_counter() < deadline:
                row = catalog.get_job(job_id)
                if str(row["state"]) == "canceled":
                    effective = time.perf_counter() - requested
                    break
                time.sleep(0.01)
            if effective is not None:
                samples.append(effective)
                canceled_ok += 1
    finally:
        _stop_workers(procs)
    _drain(catalog, backlogs, timeout=300.0)
    return {
        "n": len(samples),
        "canceled_ok": canceled_ok,
        "p50_s": round(_p(samples, 0.50), 4) if samples else None,
        "p95_s": round(_p(samples, 0.95), 4) if samples else None,
        "max_s": round(max(samples), 4) if samples else None,
        "target_s": 2.0,
        "met": bool(samples) and max(samples) <= 2.0,
    }


def leg_api_latency(base_url: str, seconds: float) -> dict[str, object]:
    """10 rps across the CRUD/list contract (NFR-010)."""
    import urllib.error
    import urllib.request

    paths = [
        "/api/v1/health",
        "/api/v1/episodes?limit=50",
        "/api/v1/jobs?limit=50",
        "/api/v1/failures",
        "/api/v1/incidents?limit=50",
        "/api/v1/metrics",
    ]
    latencies: dict[str, list[float]] = {path: [] for path in paths}
    statuses: dict[str, list[int]] = {path: [] for path in paths}
    interval = 1.0 / 10.0
    deadline = time.time() + seconds
    index = 0
    while time.time() < deadline:
        path = paths[index % len(paths)]
        index += 1
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(base_url + path, timeout=10) as response:
                response.read()
                statuses[path].append(response.status)
        except urllib.error.HTTPError as error:
            statuses[path].append(error.code)
        latencies[path].append(time.perf_counter() - started)
        time.sleep(max(0.0, interval - (time.perf_counter() - started)))
    report: dict[str, object] = {"duration_s": seconds, "rate_rps": 10.0, "targets_ms": 300}
    all_ok = True
    per_path: dict[str, object] = {}
    for path in paths:
        samples_ms = [x * 1000 for x in latencies[path]]
        ok = statuses[path] and max(statuses[path]) < 500 and _p(samples_ms, 0.95) <= 300.0
        all_ok = all_ok and bool(ok)
        per_path[path] = {
            "n": len(samples_ms),
            "p50_ms": round(_p(samples_ms, 0.50), 2),
            "p95_ms": round(_p(samples_ms, 0.95), 2),
            "statuses": sorted(set(statuses[path])),
        }
    report["per_path"] = per_path
    report["met"] = all_ok
    return report


def _sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def _seed_catalog(dsn: str, count: int = 10_000) -> tuple[float, list[str]]:
    """Seed `count` episodes the way real rows look, in batches, one connection.

    Artifacts, episodes (with reader-shaped metadata so the validation path can
    run), quality rows, and lineage edges, all with spread created_at so the
    newest-first reads see a realistic ordering. Batching is the seeding
    method, not the measured subject; the per-row repository path is what the
    latency samples then read.
    """
    from psycopg.types.json import Jsonb

    job_id = str(uuid.uuid4())
    episode_ids: list[str] = []
    artifact_rows: list[tuple[str, int]] = []
    episode_rows: list[tuple[object, ...]] = []
    quality_rows: list[tuple[object, ...]] = []
    lineage_rows: list[tuple[str, str, str, str, str]] = []
    now = DateTime.now(UTC)
    for i in range(count):
        episode_id = str(uuid.uuid4())
        episode_ids.append(episode_id)
        artifact_hash = _sha(f"seed-artifact-{i}")
        source_hash = _sha(f"seed-source-{i}")
        digest = _sha(f"seed-signal-{i}")
        b = digest.encode()
        created = now - timedelta(seconds=count - i)
        artifact_rows.append((artifact_hash, 4096 + (b[0] % 1024)))
        metadata = {
            "format": "lerobot-v3",
            "format_version": "v3.0",
            "episode_key": f"episode_index={i}",
            "robot": "so100_follower",
            "task": "pick",
            "tasks": ["pick"],
            "frame_count": 280 + b[0] % 40,
            "duration_seconds": 9.3 + (b[1] % 100) / 100.0,
            "fps": 30.0,
            "channel_stats": {
                "action": {
                    "name": "action",
                    "dtype": "float32",
                    "count": 280 + b[0] % 40,
                    "min": -2.0,
                    "max": 80.0,
                    "mean": 0.4 + b[2] % 7 / 10.0,
                    "std": 0.5 + b[3] % 5 / 10.0,
                    "shape": [280 + b[0] % 40, 12],
                },
                "observation.state": {
                    "name": "observation.state",
                    "dtype": "float32",
                    "count": 280 + b[0] % 40,
                    "min": -2.0,
                    "max": 80.0,
                    "mean": 0.3 + b[4] % 7 / 10.0,
                    "std": 0.4 + b[5] % 5 / 10.0,
                    "shape": [280 + b[0] % 40, 12],
                },
            },
        }
        episode_rows.append(
            (
                episode_id,
                source_hash,
                f"episode_index={i}",
                artifact_hash,
                "lerobot-v3",
                Jsonb(metadata),
                "ingested",
                created,
            )
        )
        verdict = ("smooth", "moderate", "jerky")[b[6] % 3]
        quality_rows.append(
            (
                episode_id,
                280 + b[0] % 40,
                0.02 + (b[1] % 100) / 1000.0,
                0.01 + (b[2] % 60) / 1000.0,
                0.05 + (b[3] % 200) / 1000.0,
                verdict,
                Jsonb(
                    [
                        {
                            "name": "action[0]",
                            "active": True,
                            "discrete": False,
                            "gripper": False,
                            "norm_delta_std": 0.02 + (b[7] % 40) / 1000.0,
                            "mean_abs_delta_norm": 0.01 + (b[8] % 30) / 1000.0,
                        },
                        {
                            "name": "action[1]",
                            "active": True,
                            "discrete": False,
                            "gripper": True,
                            "norm_delta_std": 0.01 + (b[9] % 20) / 1000.0,
                            "mean_abs_delta_norm": 0.005 + (b[10] % 15) / 1000.0,
                        },
                    ]
                ),
                0,
                0.033,
                0.0,
                "ok",
                "smooth",
                "action[0]",
                12,
                # `motion_trace` is a list of *runs*, each run a list of
                # (seconds, score) pairs - the shape `EpisodeQuality.to_dict()` writes
                # and `web/pages._motion_trace` iterates. Seeding a flat list of floats
                # instead produced rows the episode-detail page could not render, so
                # every seeded episode was a 500 on `/ui/episodes/{id}`; the campaign
                # never noticed because it only ever measured repository queries.
                Jsonb([[((i + d) / 50.0, ((i + d) % 13) / 500.0) for d in range(12)]]),
            )
        )
        lineage_rows.append(("episode", episode_id, "job", job_id, "produced_by"))

    started = time.perf_counter()
    with _connect(dsn) as connection:
        with connection.cursor() as cur:
            with cur.copy("COPY artifacts (hash, size_bytes) FROM STDIN") as copy:
                for row in artifact_rows:
                    copy.write_row(row)
            with cur.copy(
                """COPY episodes
                       (id, source_hash, episode_key, artifact_hash, format,
                        metadata, state, created_at)
                   FROM STDIN"""
            ) as copy:
                for row in episode_rows:
                    copy.write_row(row)
            with cur.copy(
                """COPY episode_quality
                       (episode_id, frame_count, movement_score, jerk_score,
                        stall_ratio, verdict, dims, nonfinite, max_gap_seconds,
                        gap_ratio, integrity, worst_verdict, worst_dim,
                        judged_dims, motion_trace)
                   FROM STDIN"""
            ) as copy:
                for row in quality_rows:
                    copy.write_row(row)
            with cur.copy(
                """COPY lineage_edges
                       (from_type, from_ref, to_type, to_ref, relation)
                   FROM STDIN"""
            ) as copy:
                for row in lineage_rows:
                    copy.write_row(row)
        connection.commit()
    return time.perf_counter() - started, episode_ids


def _sample_catalog_queries(
    catalog, episode_ids: list[str], samples: int = 200
) -> dict[str, object]:
    """NFR-003 leg: the read paths an operator hits, over the 10k catalog."""
    from data_engine.catalog.repository import JobState

    probes: dict[str, list[float]] = {
        "list_episodes(limit=50)": [],
        "get_episode": [],
        "get_episode_quality": [],
        "quality_summary": [],
        "list_jobs": [],
        "count_jobs": [],
    }
    step = max(1, len(episode_ids) // samples)
    picks = episode_ids[::step][:samples]
    for index, episode_id in enumerate(picks):
        started = time.perf_counter()
        catalog.list_episodes(limit=50)
        probes["list_episodes(limit=50)"].append(time.perf_counter() - started)
        started = time.perf_counter()
        catalog.get_episode(episode_id)
        probes["get_episode"].append(time.perf_counter() - started)
        started = time.perf_counter()
        catalog.get_episode_quality(episode_id)
        probes["get_episode_quality"].append(time.perf_counter() - started)
        if index % 20 == 0:
            started = time.perf_counter()
            catalog.quality_summary()
            probes["quality_summary"].append(time.perf_counter() - started)
        started = time.perf_counter()
        catalog.list_jobs(state=JobState.SUCCEEDED, limit=50)
        probes["list_jobs"].append(time.perf_counter() - started)
        started = time.perf_counter()
        catalog.count_jobs(JobState.QUEUED)
        probes["count_jobs"].append(time.perf_counter() - started)
    report: dict[str, object] = {}
    for name, values in probes.items():
        ms = [v * 1000 for v in values]
        report[name] = {
            "n": len(ms),
            "p50_ms": round(_p(ms, 0.50), 2),
            "p95_ms": round(_p(ms, 0.95), 2),
            "max_ms": round(max(ms), 2),
        }
    return report


dsn_global: str = ""


def main() -> int:
    global dsn_global
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--leg",
        choices=(
            "job-scaling",
            "enqueue-start",
            "cancel",
            "catalog-scale",
            "api",
            "depth-curve",
            "production-workers",
            "cancel-running",
        ),
        required=True,
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=float, default=30.0, help="api leg duration")
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    _require_fixture()
    os.environ["DE_SCALE_TMP"] = str(Path(os.environ.get("DE_SCALE_TMP", ROOT / "var" / "tmp")))
    Path(os.environ["DE_SCALE_TMP"]).mkdir(parents=True, exist_ok=True)

    name, dsn = make_database("data_engine_scale")
    dsn_global = dsn
    initialize(dsn)
    catalog = catalog_for(dsn)
    report = _run_leg(catalog, dsn, args)
    report["database"] = name
    report["leg"] = args.leg
    print(json.dumps(report, indent=2))
    return 0


def _run_leg(catalog, dsn: str, args: argparse.Namespace) -> dict[str, object]:
    leg = args.leg
    if leg == "job-scaling":
        return leg_job_scaling(catalog, dsn, Path(os.environ["DE_SCALE_TMP"]))
    if leg == "enqueue-start":
        return leg_enqueue_start(catalog, dsn, Path(os.environ["DE_SCALE_TMP"]))
    if leg == "cancel":
        return leg_cancel(catalog, dsn, Path(os.environ["DE_SCALE_TMP"]))
    if leg == "catalog-scale":
        return leg_catalog_scale(catalog, dsn, Path(os.environ["DE_SCALE_TMP"]))
    if leg == "api":
        return leg_api_latency(args.base_url, args.seconds)
    if leg == "depth-curve":
        return leg_depth_curve(catalog, dsn, Path(os.environ["DE_SCALE_TMP"]))
    if leg == "production-workers":
        return leg_production_workers(catalog, dsn, Path(os.environ["DE_SCALE_TMP"]))
    if leg == "cancel-running":
        return leg_cancel_running(catalog, dsn, Path(os.environ["DE_SCALE_TMP"]))
    raise AssertionError(f"unhandled leg: {args.leg}")


def leg_production_workers(catalog, dsn: str, _tmpdir: Path) -> dict[str, object]:
    """NFR-005 E5: re-run the depth-100 enqueue->start point with 2 workers so the
    queue-position wait is also depth-capacity confirmed (wait ratio 1w : 2w).

    Uses the same shape as the existing enqueue-start leg's first-claim measurement
    but with 2 workers and a fresh backlog.
    """
    _enqueue(catalog, 100, prefix="backlog-2w")
    measured = _enqueue(catalog, 1, prefix="two-worker")
    job_id = measured[0]
    procs, _ready = _start_workers(dsn, 2)
    try:
        first_claim = None
        deadline = time.perf_counter() + 60.0
        ready_at = time.perf_counter()
        while time.perf_counter() < deadline:
            if catalog.get_job(job_id)["started_at"] is not None:
                first_claim = time.perf_counter()
                break
            time.sleep(0.01)
        row = catalog.get_job(job_id)
        created = row.get("created_at")
        started = row.get("started_at")
        raw_wait = None
        if started is not None and created is not None:
            delta = started - created
            if delta.total_seconds() >= 0:
                raw_wait = delta.total_seconds()
    finally:
        _stop_workers(procs)
    return {
        "worker_count": 2,
        "depth_100": {
            "first_claim_after_worker_ready_s": (
                round(first_claim - ready_at, 4) if first_claim else None
            ),
            "queue_position_wait_raw_s": round(raw_wait, 3) if raw_wait else None,
        },
        "note": "depth-100 point repeated with 2 workers to confirm the wait is capacity-bound",
    }


def leg_cancel_running(catalog, dsn: str, _tmpdir: Path) -> dict[str, object]:
    """NFR-005 E6b: cancel takes effect for a *running* job (cooperative path),
    in addition to the already-measured queued-job cancel.

    Enqueues 30 small jobs, lets 1 worker start the first, then cancels a
    running job (find one in state=running via list_jobs), and records the
    effect latency. Also re-confirms the queued path on the same battery.
    """
    backlogs = _enqueue(catalog, 28, prefix="cancel-running-backlog")
    procs, _ready = _start_workers(dsn, 1)
    try:
        # wait until one job is actually running
        running_job = None
        deadline = time.time() + 60.0
        while time.time() < deadline:
            running = [
                j
                for j in catalog.list_jobs(state="running", limit=20)
                if str(j["state"]) == "running"
            ]
            if running:
                running_job = str(running[0]["id"])
                break
            time.sleep(0.05)
        measured_running: list[float] = []
        measured_queued: list[float] = []
        if running_job is not None:
            row, effective = _cancel_one(running_job, catalog, time.perf_counter() + 30.0)
            if effective is not None:
                measured_running.append(effective)
        # queued cancels: submit fresh, cancel immediately, before the worker can reach them
        for _i in range(12):
            job_id = select_one(catalog)
            requested = time.perf_counter()
            catalog.request_cancel(job_id)
            effective = None
            deadline = requested + 30.0
            while time.perf_counter() < deadline:
                row = catalog.get_job(job_id)
                if str(row["state"]) == "canceled":
                    effective = time.perf_counter() - requested
                    break
                time.sleep(0.01)
            if effective is not None:
                measured_queued.append(effective)
    finally:
        _stop_workers(procs)
        _drain(catalog, backlogs, timeout=300.0)
    return {
        "running_cancel": {
            "n": len(measured_running),
            "p50_s": round(_p(measured_running, 0.50), 4) if measured_running else None,
            "p95_s": round(_p(measured_running, 0.95), 4) if measured_running else None,
            "max_s": round(max(measured_running), 4) if measured_running else None,
            "target_s": 2.0,
            "met": bool(measured_running) and max(measured_running) <= 2.0,
        },
        "queued_cancel": {
            "n": len(measured_queued),
            "p50_s": round(_p(measured_queued, 0.50), 4) if measured_queued else None,
            "p95_s": round(_p(measured_queued, 0.95), 4) if measured_queued else None,
            "max_s": round(max(measured_queued), 4) if measured_queued else None,
            "target_s": 2.0,
            "met": bool(measured_queued) and max(measured_queued) <= 2.0,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
