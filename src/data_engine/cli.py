"""Command-line entry point for local API and worker processes."""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import time
from collections.abc import Sequence
from typing import Any

import uvicorn

from data_engine.api.app import create_app
from data_engine.catalog.database import initialize_schema
from data_engine.config import load_settings
from data_engine.jobs.state import REAP_INTERVAL_SECONDS
from data_engine.jobs.worker import IngestWorker
from data_engine.monitoring.features import DEFAULT_WINDOW_SECONDS
from data_engine.monitoring.service import MonitorService
from data_engine.observability.logging import configure_logging
from data_engine.observability.metrics import JsonlMetricSink, RuntimeMetrics

#: How long to wait after a failed database call before trying the queue again.
DB_ERROR_BACKOFF_SECONDS = 5.0

#: How often the worker records host gauges (`system_*`) into the runtime sink.
#: Once a minute is often enough to notice a filling disk or a growing RSS, and
#: rare enough that eight gauges a minute do not bury the job metrics in the JSONL.
HOST_SAMPLE_INTERVAL_SECONDS = 60.0

#: How often the worker loop evaluates a monitoring window. It matches the notifier's
#: own window (`DEFAULT_WINDOW_SECONDS`): a shorter cadence re-evaluates records the
#: previous tick already saw, and a longer one lets a sustained fault sit unobserved
#: between ticks. The monitor was fully implemented, tested, and inert - nothing in
#: any running deployment called `MonitorService.tick` (EXP-0016), so `/ui/incidents`
#: rendered zero rows forever. See ADR 0031.
MONITOR_INTERVAL_SECONDS = DEFAULT_WINDOW_SECONDS


def build_metrics(settings: Any) -> RuntimeMetrics:
    """Runtime recorder writing to the configured JSONL sink."""
    return RuntimeMetrics(JsonlMetricSink(settings.metrics_path))


def _parent_alive() -> bool:
    """False once the parent process is gone.

    `dev` runs the worker as a child process. If the parent is killed outright the
    child is not reaped, and an orphan keeps claiming jobs from the shared catalog,
    which silently breaks `just test` and steals work from any other worker.

    A worker started standalone (`de worker`, `just worker`) has no multiprocessing
    parent at all - `parent_process()` returning `None` means "not a child of a
    multiprocessing spawn", not "orphaned". Reading `None` as dead made every
    standalone worker exit on its first loop iteration; only `de dev`'s children
    ever ran. Found by the worker-kill drill (EXP-0011), which starts workers the
    way the runbook says to.
    """
    parent = multiprocessing.parent_process()
    return parent is None or parent.is_alive()


def _worker_loop(stop: Any, poll_seconds: float = 0.25) -> None:
    settings = load_settings()
    metrics = build_metrics(settings)
    worker = IngestWorker(settings, metrics=metrics)
    log = logging.getLogger(__name__)
    # The reapers used to exist but nothing called them, so F6 deadlines were never
    # enforced on a job that was already running and a dead worker's job was stranded.
    monitor = MonitorService(worker.catalog, metrics=metrics, metrics_path=settings.metrics_path)
    next_reap = time.monotonic()
    next_sample = time.monotonic()
    next_monitor = time.monotonic()
    while not stop.is_set():
        if not _parent_alive():
            log.warning("parent process gone; worker exiting")
            return
        if time.monotonic() >= next_reap:
            _reap(worker, log)
            next_reap = time.monotonic() + REAP_INTERVAL_SECONDS
        if time.monotonic() >= next_sample:
            _sample_host(metrics, log)
            next_sample = time.monotonic() + HOST_SAMPLE_INTERVAL_SECONDS
        if time.monotonic() >= next_monitor:
            _monitor_tick(monitor, worker.catalog, log)
            next_monitor = time.monotonic() + MONITOR_INTERVAL_SECONDS
        try:
            result = worker.process_one()
        except Exception:
            # A database blip must not end the worker's life. `claim_job` runs before
            # the job try/except in `process_one`, so a lost connection used to
            # propagate out of this loop and take the process down, leaving the queue
            # unprocessed until someone restarted it. Back off and keep going.
            log.exception("worker iteration failed; continuing")
            stop.wait(max(poll_seconds, DB_ERROR_BACKOFF_SECONDS))
            continue
        if result is None:
            stop.wait(poll_seconds)


def _sample_host(metrics: Any, log: Any) -> None:
    """Record host gauges on an interval; telemetry must not stop the worker."""
    try:
        metrics.sample_host()
    except Exception:
        log.exception("host sample failed; continuing")


def _monitor_tick(monitor: MonitorService, catalog: Any, log: Any) -> None:
    """Evaluate one monitoring window, if this worker holds the tick lease.

    Out of band by construction (ADR 0020): a tick that raises is a lost observation,
    never a lost job, so every failure is logged and swallowed the way a host sample
    is. The lease is the catalog's, not this process's, so a pool of workers produces
    one tick per interval rather than one per worker.
    """
    try:
        with catalog.monitor_lease() as acquired:
            if not acquired:
                return
            report = monitor.tick(catalog)
    except Exception:
        log.exception("monitor tick failed; continuing")
        return
    if report.written:
        log.warning(
            "monitoring opened incidents",
            extra={"event": "incidents_opened", "count": len(report.written)},
        )


def _reap(worker: IngestWorker, log: Any) -> None:
    """Sweep expired deadlines and jobs abandoned by a dead worker."""
    try:
        timed_out = worker.catalog.reap_expired_deadlines()
        reclaimed = worker.catalog.reap_orphaned_jobs()
    except Exception:
        # Reaping is opportunistic maintenance. If the database is unreachable the
        # next sweep will catch up, so this must not end the loop.
        log.exception("reaper sweep failed; continuing")
        return
    if timed_out or reclaimed:
        log.warning(
            "reaper swept the queue",
            extra={
                "event": "jobs_reaped",
                "timed_out": timed_out,
                "reclaimed_from_dead_worker": reclaimed,
            },
        )


def _run_migrate(settings: Any, *, status_only: bool) -> None:
    """Apply pending catalog migrations, or just report them (ADR 0028)."""
    from data_engine.catalog import migrations as migration_runner

    if status_only:
        state = migration_runner.status(settings)
        print("applied: " + (", ".join(state["applied"]) or "(none)"))
        print("pending: " + (", ".join(state["pending"]) or "(none)"))
        if state["unknown"]:
            print(
                "unknown: " + ", ".join(state["unknown"]),
                "-- the database is newer than this code",
            )
            raise SystemExit(1)
        print("unknown: (none)")
        return
    applied = migration_runner.upgrade(settings)
    if applied:
        print("applied: " + ", ".join(applied))
    else:
        print("catalog migrations: nothing to apply")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="de", description="Faultlined robot episode data engine")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("api", "worker", "dev", "doctor", "gc"):
        command = subparsers.add_parser(name)
        command.add_argument(
            "--once", action="store_true", help="process at most one job (worker only)"
        )
    migrate = subparsers.add_parser("migrate", help="apply pending catalog migrations (ADR 0028)")
    migrate.add_argument(
        "--status",
        action="store_true",
        help="print applied and pending versions; exit nonzero if ahead of the code",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

    if args.command == "migrate":
        _run_migrate(settings, status_only=bool(args.status))
        return
    if args.command == "doctor":
        initialize_schema(settings)
        print("PostgreSQL connection and catalog schema: OK")
        return
    if args.command == "gc":
        print("No garbage-collection work is implemented in the vertical slice.")
        return
    if args.command == "worker":
        initialize_schema(settings)
        worker = IngestWorker(settings, metrics=build_metrics(settings))
        if args.once:
            worker.process_one()
            return
        stop = multiprocessing.Event()
        try:
            _worker_loop(stop)
        except KeyboardInterrupt:
            stop.set()
        return
    if args.command == "api":
        initialize_schema(settings)
        uvicorn.run(
            create_app(settings, initialize_database=False),
            host=settings.api_host,
            port=settings.api_port,
        )
        return
    if args.command == "dev":
        initialize_schema(settings)
        stop = multiprocessing.Event()
        worker_process = multiprocessing.Process(
            target=_worker_loop, args=(stop,), name="data-engine-worker", daemon=True
        )
        worker_process.start()
        try:
            uvicorn.run(
                create_app(settings, initialize_database=False),
                host=settings.api_host,
                port=settings.api_port,
            )
        finally:
            stop.set()
            worker_process.join(timeout=5)
            if worker_process.is_alive():
                worker_process.terminate()
                worker_process.join(timeout=2)
        return
    logging.getLogger(__name__).error("unknown command")
