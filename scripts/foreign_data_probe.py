#!/usr/bin/env python3
"""Run every foreign-corpus fixture through the real ingest path and record the outcome."""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

ADMIN_DSN = "postgresql://data_engine@127.0.0.1:55432/postgres"


def _psycopg() -> Any:
    import psycopg

    return psycopg


def make_database(prefix: str = "foreign") -> tuple[str, str]:
    psycopg = _psycopg()
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    return name, f"postgresql://data_engine@127.0.0.1:55432/{name}"


def drop_database(name: str) -> None:
    psycopg = _psycopg()
    admin = psycopg.connect(ADMIN_DSN, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.close()


def initialize(dsn: str) -> None:
    from data_engine.catalog.database import initialize_schema
    from data_engine.config import Settings

    initialize_schema(Settings(_env_file=None, database_url=dsn))


def probe_reader(path: Path) -> dict[str, Any]:
    """What the reader registry alone does with this path."""
    from data_engine.ingest.readers.registry import READERS, read_episode, reader_for

    reader = reader_for(path)
    claimed = reader.format if reader is not None else None
    started = time.perf_counter()
    try:
        extraction = read_episode(path)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        return {
            "claimed_by": claimed,
            "outcome": "error",
            "error_type": type(exc).__name__,
            "error": str(exc)[:400],
            "latency_ms": round(elapsed * 1000, 2),
        }
    elapsed = time.perf_counter() - started
    return {
        "claimed_by": claimed,
        "outcome": "ok",
        "format": extraction.format,
        "format_version": extraction.format_version,
        "episode_key": extraction.episode_key,
        "robot_type": extraction.robot_type,
        "task": extraction.task,
        "frame_count": extraction.frame_count,
        "duration_seconds": round(extraction.duration_seconds, 3),
        "fps": extraction.fps,
        "channels": len(extraction.channels),
        "quality": extraction.quality.verdict if extraction.quality else None,
        "decoded_channels": (extraction.dataset or {}).get("decoded_channels"),
        "undecodable_channels": (extraction.dataset or {}).get("undecodable_channels"),
        "latency_ms": round(elapsed * 1000, 2),
        "readers_available": [r.format for r in READERS],
    }


def probe_job(catalog: Any, path: Path) -> dict[str, Any]:
    """Submit a real `ingest_source` job and read back the terminal row."""
    started = time.perf_counter()
    try:
        row, _correlation = catalog.submit_job(
            "ingest_source",
            {"source": str(path)},
            f"foreign-probe-{uuid.uuid4().hex[:6]}",
            str(uuid.uuid4()),
        )
    except Exception as exc:
        return {
            "submit": "rejected",
            "error_type": type(exc).__name__,
            "error": str(exc)[:400],
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    job_id = str(row["id"])
    from data_engine.jobs.worker import IngestWorker

    worker = IngestWorker(catalog.settings)
    finished = None
    for _ in range(6000):
        finished = worker.process_one()
        if finished is not None and str(finished.get("id")) == job_id:
            break
        time.sleep(0.01)
    elapsed = time.perf_counter() - started
    if finished is None or str(finished.get("id")) != job_id:
        return {
            "submit": "accepted",
            "state": "no_worker_progress",
            "latency_ms": round(elapsed * 1000, 2),
        }
    state = str(finished.get("state"))
    result: dict[str, Any] = {
        "submit": "accepted",
        "state": state,
        "latency_ms": round(elapsed * 1000, 2),
    }
    if state == "succeeded":
        payload = finished.get("result") or {}
        result["episodes"] = payload.get("episodes")
        result["artifacts"] = payload.get("artifacts")
    else:
        result["reason_code"] = (finished.get("last_error") or "")[:200]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(prog="foreign_data_probe")
    parser.add_argument("corpus", type=Path, nargs="?", default=Path("var/foreign-corpus"))
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="also run each fixture through a real ingest job in a throwaway database",
    )
    parser.add_argument("--traceback", action="store_true", help="print reader tracebacks")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()

    from scripts.foreign_data_corpus import load

    entries = load(args.corpus)
    catalog = None
    dsn = ""
    name = ""
    if args.in_process:
        name, dsn = make_database()
        initialize(dsn)
        from data_engine.catalog.repository import PostgresCatalog
        from data_engine.config import Settings

        catalog = PostgresCatalog(Settings(_env_file=None, database_url=dsn))

    report: list[dict[str, Any]] = []
    try:
        for entry in entries:
            if entry["status"] != "built":
                report.append({"name": entry["name"], "generator": "failed", **entry})
                continue
            path = Path(entry["path"])
            if not path.exists():
                report.append({"name": entry["name"], "error": "fixture missing after build"})
                continue
            row: dict[str, Any] = {
                "name": entry["name"],
                "path": str(path),
                "bytes": entry.get("bytes"),
            }
            try:
                row["reader"] = probe_reader(path)
            except Exception:
                if args.traceback:
                    traceback.print_exc()
                row["reader"] = {"outcome": "probe_crashed"}
            if catalog is not None:
                row["job"] = probe_job(catalog, path)
            report.append(row)
            verdict = row.get("reader", {}).get("outcome")
            claimed = row.get("reader", {}).get("claimed_by")
            job_state = (row.get("job") or {}).get("state", "-")
            print(
                f"{entry['name']:<22} reader={verdict!s:<6} claimed={claimed!s:<8} job={job_state}",
                flush=True,
            )
    finally:
        if name:
            drop_database(name)

    payload = {"corpus": str(args.corpus), "fixtures": report}
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {args.json}")
    if not args.json:
        print(json.dumps(payload, indent=2, sort_keys=True)[:4000])


if __name__ == "__main__":
    main()
