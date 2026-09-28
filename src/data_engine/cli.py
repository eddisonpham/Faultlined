"""Command-line entry point for local API and worker processes."""

from __future__ import annotations

import argparse
import logging
import multiprocessing
from collections.abc import Sequence
from typing import Any

import uvicorn

from data_engine.api.app import create_app
from data_engine.catalog.database import initialize_schema
from data_engine.config import load_settings
from data_engine.jobs.worker import IngestWorker
from data_engine.observability.logging import configure_logging


def _worker_loop(stop: Any, poll_seconds: float = 0.25) -> None:
    settings = load_settings()
    worker = IngestWorker(settings)
    while not stop.is_set():
        result = worker.process_one()
        if result is None:
            stop.wait(poll_seconds)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="de", description="Robot episode data engine")
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
        worker = IngestWorker(settings)
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
            target=_worker_loop, args=(stop,), name="data-engine-worker"
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
