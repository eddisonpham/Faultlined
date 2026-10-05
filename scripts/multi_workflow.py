#!/usr/bin/env python3
"""Simulate the workflows that actually run at the same time, and measure the interference.

The repo's scaling campaign measures workers against workers: N ingest jobs, one worker
versus two, wall-clock speedup. That is the right question for a batch system and the
wrong one for this product, because an operator does not run one workflow at a time.
On a real machine the catalog is being read by a browser polling six pages, written by
somebody curating task strings, drained by a worker doing ingest, and ticked by the
monitor - all against one Postgres, at the same time.

The question this answers is: **when several of those run together, what does each one
pay?** A single-threaded measurement of every route in isolation cannot see it, and
"it was 90 ms" is not an answer to "it is 90 ms while you are ingesting".

Four legs, each run alone (the control) and then all four at once (the contention run):

| Leg | Actor | What it does |
|---|---|---|
| `ingest` | 2 worker processes | drain real `ingest_source` jobs over real MCAP bags |
| `read` | 1 API process, 1 client | poll the read routes the operator's pages use, every 3 s |
| `curate` | 1 API process, 1 client | map / dismiss / undo strings, as a triaging operator would |
| `monitor` | 1 process | `POST /api/v1/monitoring/tick` on the interval the worker uses |

Latency is reported per leg for the control and the contention run, so the cost of
sharing is a number rather than an impression. It also records whether correctness held
- lost updates, jobs stuck, vocabulary events that will not undo - because a fast wrong
answer is the worst outcome available here.

    uv run python scripts/multi_workflow.py
    uv run python scripts/multi_workflow.py --jobs 12 --seconds 90

Numbers go to stdout as JSON. This script writes nothing to the repository.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

ADMIN_DSN = "postgresql://data_engine@127.0.0.1:55432/postgres"

READ_PATHS = (
    "/api/v1/status",
    "/api/v1/episodes?limit=50",
    "/api/v1/jobs?limit=50",
    "/api/v1/quality/summary",
    "/api/v1/metrics",
    "/api/v1/vocabulary/unmapped?limit=50",
)


# ------------------------------------------------------------------- small helpers


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _decode_error(payload: bytes, status: int, width: int) -> str | None:
    """The first `width` characters of an error body, or None when the call succeeded."""
    if status < 400:
        return None
    return payload[:width].decode("utf-8", "replace")


def percentiles(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"n": 0}
    ordered = sorted(samples)

    def pick(q: float) -> float:
        return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]

    return {
        "n": len(ordered),
        "p50_ms": round(pick(0.50), 2),
        "p95_ms": round(pick(0.95), 2),
        "p99_ms": round(pick(0.99), 2),
        "max_ms": round(ordered[-1], 2),
        "mean_ms": round(statistics.fmean(ordered), 2),
    }


def http(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: float = 120.0,
) -> tuple[int, bytes, float]:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read()
        status = error.code
    except Exception as error:  # a transport failure is a sample, not a crash
        return 0, str(error).encode(), time.perf_counter() - started
    return status, payload, time.perf_counter() - started


def start_server(dsn: str, port: int, workdir: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ)
    env.update(
        {
            "DE_DATABASE_URL": dsn,
            "DE_METRICS_PATH": str(workdir / "runtime.jsonl"),
            "DE_ARTIFACT_ROOT": str(workdir / "artifacts"),
            "DE_EXPORT_ROOT": str(workdir / "exports"),
            "DE_LOG_LEVEL": "ERROR",
        }
    )
    code = (
        "import uvicorn;"
        "from data_engine.api.app import create_app;"
        f"uvicorn.run(create_app(), host='127.0.0.1', port={port}, log_level='error')"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_ready(base_url: str, proc: subprocess.Popen[bytes], timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if http(base_url + "/api/v1/health", timeout=5)[0] == 200:
            return True
        time.sleep(0.25)
    return False


def worker_main(dsn: str, stop_after_idle: int, ready_path: str) -> None:
    from data_engine.config import Settings
    from data_engine.jobs.worker import IngestWorker

    worker = IngestWorker(Settings(_env_file=None, database_url=dsn))
    Path(ready_path).write_text("ready", encoding="utf-8")
    idle = 0
    while idle < stop_after_idle:
        job = worker.process_one()
        if job is None:
            idle += 1
            time.sleep(0.05)
        else:
            idle = 0


# ------------------------------------------------------------------------ the legs


class Sampler:
    """A thread that keeps hitting one thing and records what it paid."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.samples: list[float] = []
        self.statuses: dict[int, int] = {}
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _record(self, status: int, elapsed: float, error: str | None = None) -> None:
        self.samples.append(elapsed * 1000.0)
        self.statuses[status] = self.statuses.get(status, 0) + 1
        if error is not None and len(self.errors) < 5:
            self.errors.append(error)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self) -> None:  # pragma: no cover - overridden per sampler
        raise NotImplementedError


def _reader(base_url: str, seconds: float, interval: float) -> Sampler:
    class Reader(Sampler):
        def _run(self) -> None:
            index = 0
            deadline = time.perf_counter() + seconds
            while time.perf_counter() < deadline and not self._stop.is_set():
                path = READ_PATHS[index % len(READ_PATHS)]
                index += 1
                status, payload, elapsed = http(base_url + path, timeout=60)
                self._record(status, elapsed, _decode_error(payload, status, 120))
                self._stop.wait(interval)

    sampler = Reader("read")
    return sampler


def _curator(base_url: str, seconds: float) -> Sampler:
    """Map / dismiss / undo task strings the way a person triaging a queue would."""

    class Curator(Sampler):
        def _run(self) -> None:
            index = 0
            deadline = time.perf_counter() + seconds
            while time.perf_counter() < deadline and not self._stop.is_set():
                index += 1
                label = f"contention task {index}"
                status, payload, elapsed = http(
                    base_url + "/api/v1/vocabulary",
                    method="POST",
                    body={"preferred_label": label},
                )
                self._record(status, elapsed)
                if status != 201:
                    continue
                entry_id = str(json.loads(payload)["entry"]["id"])
                for step in ("map", "dismiss"):
                    if step == "map":
                        body: dict[str, Any] = {
                            "task_string": f"{label} string",
                            "entry_id": entry_id,
                        }
                        path = "/api/v1/vocabulary/mappings"
                    else:
                        body = {"task_string": f"{label} noise"}
                        path = "/api/v1/vocabulary/dismissals"
                    status, payload, elapsed = http(base_url + path, method="POST", body=body)
                    self._record(status, elapsed, _decode_error(payload, status, 120))

    sampler = Curator("curate")
    return sampler


def _monitor(base_url: str, seconds: float, interval: float) -> Sampler:
    class Monitor(Sampler):
        def _run(self) -> None:
            deadline = time.perf_counter() + seconds
            while time.perf_counter() < deadline and not self._stop.is_set():
                status, payload, elapsed = http(
                    base_url + "/api/v1/monitoring/tick", method="POST", body={}, timeout=60
                )
                self._record(status, elapsed, _decode_error(payload, status, 160))
                self._stop.wait(interval)

    sampler = Monitor("monitor")
    return sampler


# ----------------------------------------------------------------------- the driver


def seed(dsn: str, episodes: int, entries: int) -> dict[str, Any]:
    from scripts.feature_latency import seed_metrics, seed_vocabulary
    from scripts.scale_campaign import _seed_catalog

    workdir = ROOT / "var" / "multi" / Path(dsn.rsplit("/", 1)[-1])
    workdir.mkdir(parents=True, exist_ok=True)
    seed_metrics(workdir / "runtime.jsonl")
    seconds, ids = _seed_catalog(dsn, episodes)
    vocabulary = seed_vocabulary(dsn, entries)
    return {"episodes": len(ids), "seed_s": round(seconds, 2), "vocabulary": vocabulary}


def fixture_paths(count: int) -> list[str]:
    """Real bags to ingest, varied so the workers are not all reading identical bytes."""
    candidates = [
        ROOT / "var" / "real-data" / "so101_pick_place.mcap",
        ROOT / "var" / "foreign-corpus" / "mcap_json" / "bag.mcap",
        ROOT / "var" / "foreign-corpus" / "mcap_ragged" / "bag.mcap",
        ROOT / "var" / "foreign-corpus" / "mcap_flat" / "bag.mcap",
        ROOT / "var" / "foreign-corpus" / "mcap_gripper_topic" / "bag.mcap",
        ROOT / "var" / "real-data" / "so101_pick_place_120s.mcap",
    ]
    available = [str(p) for p in candidates if p.exists()]
    if not available:
        raise SystemExit("no MCAP fixtures found; run scripts/make_mcap_log.py first")
    return [available[i % len(available)] for i in range(count)]


def run_leg(
    dsn: str,
    base_url: str,
    *,
    workers: int,
    jobs: int,
    seconds: float,
    read_interval: float,
    monitor_interval: float,
    active: tuple[str, ...],
) -> dict[str, Any]:
    from data_engine.catalog.repository import PostgresCatalog
    from data_engine.config import Settings

    settings = Settings(_env_file=None, database_url=dsn)
    catalog = PostgresCatalog(settings)
    sources = fixture_paths(jobs)
    submitted = 0
    #: Every job ever submitted that has not been observed terminal yet. New jobs join
    #: this set the moment they are submitted - an earlier version appended them to a
    #: separate list and never polled them, so the leg reported 12 completions for 825
    #: submissions.
    outstanding: set[str] = set()

    def submit_batch(count: int) -> None:
        nonlocal submitted
        for _ in range(count):
            source = sources[submitted % len(sources)]
            submitted += 1
            row, _correlation = catalog.submit_job(
                "ingest_source",
                {"source": source},
                f"multi-{uuid.uuid4().hex[:8]}",
                str(uuid.uuid4()),
            )
            outstanding.add(str(row["id"]))

    concurrent = bool(active)
    if "ingest" in active:
        submit_batch(jobs)
    else:
        # An isolation leg still needs its jobs submitted, it just must not keep a
        # worker draining them, so the sampler is measured against an idle queue.
        submit_batch(0)

    ready_dir = ROOT / "var" / "multi" / "ready"
    ready_dir.mkdir(parents=True, exist_ok=True)
    processes: list[multiprocessing.Process] = []
    if "ingest" in active:
        for _ in range(workers):
            ready = ready_dir / f"r-{uuid.uuid4().hex[:8]}"
            proc = multiprocessing.Process(
                target=worker_main, args=(dsn, 400, str(ready)), daemon=True
            )
            proc.start()
            processes.append(proc)
        deadline = time.time() + 60
        while not all(p.is_alive() for p in processes):
            if time.time() > deadline:
                break
            time.sleep(0.05)
        time.sleep(1.0)

    samplers: list[Sampler] = []
    if "read" in active:
        samplers.append(_reader(base_url, seconds, read_interval))
    if "curate" in active:
        samplers.append(_curator(base_url, seconds))
    if "monitor" in active:
        samplers.append(_monitor(base_url, seconds, monitor_interval))
    for sampler in samplers:
        sampler.start()

    started = time.perf_counter()
    terminal: dict[str, int] = {}
    # Steady state, not a burst. An operator does not hand over 12 jobs and go away;
    # work arrives continuously, so the leg tops the queue back up until the window
    # closes. A burst that drains in nine seconds measures nothing about contention.
    while time.perf_counter() - started < seconds:
        still_open = 0
        for job_id in list(outstanding):
            row = catalog.get_job(job_id)
            state = str(row["state"])
            if state in ("succeeded", "failed", "canceled"):
                terminal[state] = terminal.get(state, 0) + 1
                outstanding.discard(job_id)
            else:
                still_open += 1
        # Keep a small steady backlog - `workers * 2` jobs - and top up by exactly the
        # deficit. Submitting on every tick floods the queue, which measures Postgres
        # insert throughput rather than contention.
        deficit = workers * 2 - still_open
        if deficit > 0:
            submit_batch(deficit)
        time.sleep(0.25)
    for job_id in list(outstanding):
        row = catalog.get_job(job_id)
        state = str(row["state"])
        if state in ("succeeded", "failed", "canceled"):
            terminal[state] = terminal.get(state, 0) + 1
            outstanding.discard(job_id)
    drain_seconds = time.perf_counter() - started

    for sampler in samplers:
        sampler.stop()
    for proc in processes:
        if proc.is_alive():
            proc.terminate()
    for proc in processes:
        proc.join(timeout=15)

    report: dict[str, Any] = {
        "active": list(active),
        "concurrent": concurrent,
        "workers": workers if "ingest" in active else 0,
        "window_seconds": seconds,
        "jobs_submitted": submitted,
        "drain_seconds": round(drain_seconds, 2),
        "terminal": terminal,
        "throughput_jobs_per_s": (
            round(sum(terminal.values()) / drain_seconds, 2) if drain_seconds else None
        ),
        "legs": {
            s.name: {**percentiles(s.samples), "statuses": s.statuses, "errors": s.errors}
            for s in samplers
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(prog="multi_workflow")
    parser.add_argument("--episodes", type=int, default=1000)
    parser.add_argument("--entries", type=int, default=40)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--read-interval", type=float, default=3.0)
    parser.add_argument("--monitor-interval", type=float, default=5.0)
    parser.add_argument("--skip-control", action="store_true")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()

    from scripts.scale_campaign import drop_database, make_database

    name, dsn = make_database("multi")
    workdir = ROOT / "var" / "multi" / name
    workdir.mkdir(parents=True, exist_ok=True)
    server: subprocess.Popen[bytes] | None = None
    try:
        from data_engine.catalog.database import initialize_schema
        from data_engine.config import Settings

        initialize_schema(Settings(_env_file=None, database_url=dsn))
        print("seeding...", flush=True)
        seeded = seed(dsn, args.episodes, args.entries)
        port = free_port()
        server = start_server(dsn, port, workdir)
        base_url = f"http://127.0.0.1:{port}"
        if not wait_ready(base_url, server):
            raise SystemExit("server did not become ready")

        report: dict[str, Any] = {"database": name, "seeded": seeded, "runs": []}
        legs: list[tuple[str, ...]] = []
        if not args.skip_control:
            legs = [
                ("ingest",),
                ("read",),
                ("curate",),
                ("monitor",),
                ("ingest", "read"),
                ("ingest", "curate"),
                ("ingest", "read", "curate", "monitor"),
            ]
        else:
            legs = [("ingest", "read", "curate", "monitor")]
        for leg in legs:
            print(f"\n>>> leg: {' + '.join(leg)}", flush=True)
            report["runs"].append(
                run_leg(
                    dsn,
                    base_url,
                    workers=args.workers,
                    jobs=args.jobs,
                    seconds=args.seconds,
                    read_interval=args.read_interval,
                    monitor_interval=args.monitor_interval,
                    active=leg,
                )
            )
            run = report["runs"][-1]
            print(
                f"    {run['jobs_submitted']} jobs  {run['throughput_jobs_per_s']} jobs/s"
                f"  {run['terminal']}"
            )
            for name, values in run["legs"].items():
                print(
                    f"    {name:<9} n={values.get('n', 0):<5} p50 {values.get('p50_ms', 0):>8.1f}"
                    f"  p95 {values.get('p95_ms', 0):>8.1f}  max {values.get('max_ms', 0):>8.1f}"
                    f"  {values['statuses']}"
                )
                for error in values.get("errors", [])[:2]:
                    print(f"        error: {error[:110]}")
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
            print(f"\nwrote {args.json}")
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        drop_database(name)


if __name__ == "__main__":
    main()
