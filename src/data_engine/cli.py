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
from data_engine.observability.logging import configure_logging
from data_engine.observability.metrics import JsonlMetricSink, RuntimeMetrics

#: How long to wait after a failed database call before trying the queue again.
DB_ERROR_BACKOFF_SECONDS = 5.0


def build_metrics(settings: Any) -> RuntimeMetrics:
    """Runtime recorder writing to the configured JSONL sink."""
    return RuntimeMetrics(JsonlMetricSink(settings.metrics_path))


def _parent_alive() -> bool:
    """False once the parent process is gone.

    `dev` runs the worker as a child process. If the parent is killed outright the
    child is not reaped, and an orphan keeps claiming jobs from the shared catalog,
    which silently breaks `just test` and steals work from any other worker.
    """
    parent = multiprocessing.parent_process()
    return parent is not None and parent.is_alive()


def _worker_loop(stop: Any, poll_seconds: float = 0.25) -> None:
    settings = load_settings()
    worker = IngestWorker(settings, metrics=build_metrics(settings))
    log = logging.getLogger(__name__)
    # The reapers used to exist but nothing called them, so F6 deadlines were never
    # enforced on a job that was already running and a dead worker's job was stranded.
    next_reap = time.monotonic()
    while not stop.is_set():
        if not _parent_alive():
            log.warning("parent process gone; worker exiting")
            return
        if time.monotonic() >= next_reap:
            _reap(worker, log)
            next_reap = time.monotonic() + REAP_INTERVAL_SECONDS
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


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="de", description="Faultlined robot episode data engine")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("api", "worker", "dev", "doctor", "gc"):
        command = subparsers.add_parser(name)
        command.add_argument(
            "--once", action="store_true", help="process at most one job (worker only)"
        )
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)

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
