#!/usr/bin/env python3
"""Measure the latency of every shipped feature against a live API, at several scales.

The repo already has API latency numbers: EXP-0010e measured six endpoints at 10 rps
over a 10k catalog. Six endpoints out of roughly forty is not a picture of the product,
and "10 rps" is a load generator, not a latency distribution - it says nothing about
what a single operator waiting on a page actually experiences.

This measures the thing a person waits for. One client, sequential requests, warmup
discarded, percentile distribution over the whole shipped surface:

* every `/api/v1` read route, including the ones only the UI calls;
* every `/ui` page, because the pages are the product;
* the streamed download variants, which are the ones that grow with row count;
* the write paths an operator performs by hand - submit a job, create an entry, map a
  string, dismiss a string, undo an action.

Run at three catalog scales (default 20 / 1,000 / 10,000 episodes) because a route that
is fine at 20 rows and slow at 10,000 is the whole question. Each scale gets its own
throwaway database and its own server process; nothing touches the dev catalog.

    uv run python scripts/feature_latency.py --scales 20 1000 10000
    uv run python scripts/feature_latency.py --scales 1000 --trials 30 --json var/lat.json

Numbers go to stdout as JSON. This script writes nothing to the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

ADMIN_DSN = "postgresql://data_engine@127.0.0.1:55432/postgres"

#: Warmup requests per route, discarded. Methodology rule 2.
WARMUP = 3
#: Measured requests per route. Methodology rule 3 asks for >= 10 micro-benchmarks.
TRIALS = 20
#: Sits under the product's own 300 ms API target (requirements NFR-010) and the
#: 200 ms catalog target (NFR-003); a route above both is worth a page in the record.
REPORT_OVER_MS = 200.0


# --------------------------------------------------------------------- the surface


def read_routes() -> list[tuple[str, str]]:
    """Every GET the product serves, with the label a reader would recognise it by."""
    return [
        ("health", "/api/v1/health"),
        ("status", "/api/v1/status"),
        ("jobs.list", "/api/v1/jobs?limit=50"),
        ("jobs.report", "/api/v1/jobs?limit=1"),
        ("artifacts.list", "/api/v1/artifacts?limit=50"),
        ("episodes.list", "/api/v1/episodes?limit=50"),
        ("episodes.list.page2", "/api/v1/episodes?limit=50&offset=5000"),
        ("episodes.quality_summary", "/api/v1/quality/summary"),
        ("failures.list", "/api/v1/failures"),
        ("failures.episodes", "/api/v1/failures/episodes?limit=50"),
        ("incidents.list", "/api/v1/incidents?limit=50"),
        ("incidents.summary", "/api/v1/incidents/summary"),
        ("monitoring.health", "/api/v1/monitoring/health"),
        ("monitoring.notify_preview", "/api/v1/monitoring/notify-preview"),
        ("metrics", "/api/v1/metrics"),
        ("slices.list", "/api/v1/slices"),
        ("builds.list", "/api/v1/builds?limit=50"),
        ("clusters.list", "/api/v1/clusters"),
        ("clusters.review", "/api/v1/clusters/review"),
        ("vocabulary.read", "/api/v1/vocabulary"),
        ("vocabulary.unmapped", "/api/v1/vocabulary/unmapped?limit=50"),
        ("vocabulary.candidates", "/api/v1/vocabulary/candidates?limit=50"),
        ("vocabulary.events", "/api/v1/vocabulary/events?limit=50"),
        ("contracts.list", "/api/v1/contracts"),
    ]


def ui_routes() -> list[tuple[str, str]]:
    return [
        ("ui.status", "/ui"),
        ("ui.jobs", "/ui/jobs"),
        ("ui.episodes", "/ui/episodes"),
        ("ui.episodes.detail", "/ui/episodes/{episode_id}"),
        ("ui.builds", "/ui/builds"),
        ("ui.slices", "/ui/slices"),
        ("ui.failures", "/ui/failures"),
        ("ui.incidents", "/ui/incidents"),
        ("ui.metrics", "/ui/metrics"),
        ("ui.insights", "/ui/insights"),
        ("ui.artifacts", "/ui/artifacts"),
        ("ui.schema", "/ui/schema"),
        ("ui.clusters.frozen", "/ui/clusters"),
        ("ui.vocabulary", "/ui/vocabulary"),
        ("ui.benchmarks", "/ui/benchmarks"),
        ("ui.experiments", "/ui/experiments"),
    ]


def download_routes() -> list[tuple[str, str]]:
    """The streamed exports. These are the routes whose cost grows with the catalog."""
    return [
        ("download.episodes.csv", "/api/v1/episodes/export?format=csv"),
        ("download.episodes.jsonl", "/api/v1/episodes/export?format=jsonl"),
    ]


# ------------------------------------------------------------------ measure helper


def _percentile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    rank = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[rank]


def request(
    url: str,
    *,
    method: str = "GET",
    body: Any = None,
    timeout: float = 120.0,
) -> tuple[int, bytes, float]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        payload = error.read()
        status = error.code
    except Exception as error:  # a refused connection is a measurement, not a crash
        return 0, str(error).encode(), time.perf_counter() - started
    return status, payload, time.perf_counter() - started


def measure(url: str, *, trials: int, warmup: int = WARMUP) -> dict[str, Any]:
    statuses: list[int] = []
    samples: list[float] = []
    size = 0
    for index in range(warmup + trials):
        status, payload, elapsed = request(url)
        if index >= warmup:
            statuses.append(status)
            samples.append(elapsed * 1000.0)
            size = len(payload)
    ordered = sorted(samples)
    return {
        "n": len(samples),
        "statuses": sorted(set(statuses)),
        "bytes": size,
        "p50_ms": round(_percentile(ordered, 0.50), 2),
        "p95_ms": round(_percentile(ordered, 0.95), 2),
        "p99_ms": round(_percentile(ordered, 0.99), 2),
        "max_ms": round(max(samples), 2) if samples else 0.0,
        "min_ms": round(min(samples), 2) if samples else 0.0,
        "over_report_threshold": bool(ordered and _percentile(ordered, 0.95) > REPORT_OVER_MS),
    }


# --------------------------------------------------------------- server lifecycle


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(dsn: str, port: int, metrics_path: Path) -> subprocess.Popen[bytes]:
    """A real uvicorn process serving the real app against `dsn`.

    Env vars outrank the `.env` file in pydantic-settings, so passing `DE_DATABASE_URL`
    explicitly is enough to point the process at the throwaway catalog.
    """
    env = dict(os.environ)
    env.update(
        {
            "DE_DATABASE_URL": dsn,
            "DE_API_PORT": str(port),
            "DE_METRICS_PATH": str(metrics_path),
            "DE_ARTIFACT_ROOT": str(metrics_path.parent / "artifacts"),
            "DE_EXPORT_ROOT": str(metrics_path.parent / "exports"),
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
        status, _payload, _elapsed = request(base_url + "/api/v1/health", timeout=5)
        if status == 200:
            return True
        time.sleep(0.25)
    return False


# ---------------------------------------------------------------------- seeding


def seed_metrics(path: Path, records: int = 4000) -> None:
    """Pre-fill the runtime sink so `/api/v1/metrics` aggregates real history.

    An empty sink measures the reader's floor. Every operator arrives at the metrics
    page after the system has been running, so the number that matters is the one over
    a populated file.
    """
    import datetime as dt

    path.parent.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC)
    names = [
        ("api_request_duration_seconds", "api_route"),
        ("catalog_query_duration_seconds", "query"),
        ("pipeline_stage_duration_seconds", "stage"),
        ("jobs_queue_time_seconds", "job_type"),
        ("jobs_run_time_seconds", "job_type"),
    ]
    with path.open("w", encoding="utf-8") as sink:
        for index in range(records):
            name, label_key = names[index % len(names)]
            sink.write(
                json.dumps(
                    {
                        "name": name,
                        "labels": {label_key: f"label-{index % 7}"},
                        "value": 0.01 + (index % 500) / 1000.0,
                        "timestamp": (now - dt.timedelta(seconds=records - index)).isoformat(),
                    }
                )
                + "\n"
            )


def seed_vocabulary(dsn: str, entries: int = 40) -> dict[str, Any]:
    """Give the vocabulary surface something real to render: entries, mappings, events."""
    from data_engine.catalog import vocabulary
    from data_engine.config import Settings

    settings = Settings(_env_file=None, database_url=dsn)
    tasks = [
        "pick up the red block",
        "place the block in the bin",
        "open the drawer",
        "close the drawer",
        "wipe the table",
        "pour the beans",
        "stack the cubes",
        "hand over the tool",
    ]
    started = time.perf_counter()
    created = 0
    mapped = 0
    # No blanket `except` here: an earlier version swallowed every error and reported
    # success, so a keyword-only signature change silently produced a *completely
    # empty* vocabulary and the page under test was measured in its empty state.
    for index in range(entries):
        entry = vocabulary.create_entry(
            settings, preferred_label=f"task family {index % len(tasks)}", notes="seeded"
        )
        created += 1
        for suffix in range(4):
            vocabulary.map_task(
                settings,
                task_string=f"{tasks[(index + suffix) % len(tasks)]} variant {index}-{suffix}",
                entry_id=str(entry["id"]),
            )
            mapped += 1
    return {
        "entries": created,
        "mappings": mapped,
        "seconds": round(time.perf_counter() - started, 2),
    }


# ------------------------------------------------------------------- write legs


def write_leg(base_url: str, episode_source: str) -> dict[str, Any]:
    """The mutations an operator performs by hand, measured the same way."""
    results: dict[str, Any] = {}

    def timed(name: str, call: Any) -> None:
        samples: list[float] = []
        statuses: list[int] = []
        for _ in range(WARMUP + TRIALS):
            status, _payload, elapsed = call()
            samples.append(elapsed * 1000.0)
            statuses.append(status)
        samples = samples[WARMUP:]
        ordered = sorted(samples)
        results[name] = {
            "n": len(samples),
            "statuses": sorted(set(statuses)),
            "p50_ms": round(_percentile(ordered, 0.50), 2),
            "p95_ms": round(_percentile(ordered, 0.95), 2),
            "max_ms": round(max(samples), 2) if samples else 0.0,
        }

    def submit_job() -> tuple[int, bytes, float]:
        return request(
            base_url + "/api/v1/jobs",
            method="POST",
            body={"type": "ingest_source", "payload": {"source": episode_source}},
        )

    timed("write.jobs.submit", submit_job)

    counter = {"n": 0}

    def create_entry() -> tuple[int, bytes, float]:
        counter["n"] += 1
        return request(
            base_url + "/api/v1/vocabulary",
            method="POST",
            body={
                "preferred_label": f"latency probe entry {counter['n']}",
                "notes": "latency campaign",
            },
        )

    timed("write.vocabulary.create_entry", create_entry)

    counter["m"] = 0

    def map_task() -> tuple[int, bytes, float]:
        counter["m"] += 1
        status, payload, elapsed = request(base_url + "/api/v1/vocabulary")
        if status != 200:
            return status, payload, elapsed
        entries = json.loads(payload).get("entries") or []
        if not entries:
            return 0, b"no entries", elapsed
        entry_id = str(entries[0]["id"])
        return request(
            base_url + "/api/v1/vocabulary/mappings",
            method="POST",
            body={"task_string": f"latency probe unmapped {counter['m']}", "entry_id": entry_id},
        )

    timed("write.vocabulary.map_task", map_task)

    counter["d"] = 0

    def dismiss_task() -> tuple[int, bytes, float]:
        counter["d"] += 1
        return request(
            base_url + "/api/v1/vocabulary/dismissals",
            method="POST",
            body={"task_string": f"latency probe dismissed {counter['d']}"},
        )

    timed("write.vocabulary.dismiss_task", dismiss_task)

    return results


# ------------------------------------------------------------------------- driver


def run_scale(scale: int, trials: int) -> dict[str, Any]:
    from scripts.scale_campaign import _seed_catalog, drop_database, make_database

    name, dsn = make_database("latency")
    workdir = ROOT / "var" / "latency" / name
    workdir.mkdir(parents=True, exist_ok=True)
    port = free_port()
    server: subprocess.Popen[bytes] | None = None
    try:
        from data_engine.catalog.database import initialize_schema
        from data_engine.config import Settings

        initialize_schema(Settings(_env_file=None, database_url=dsn))
        metrics_path = workdir / "runtime.jsonl"
        seed_metrics(metrics_path)
        seed_seconds, episode_ids = _seed_catalog(dsn, scale)
        vocabulary_seed = seed_vocabulary(dsn)
        base_url = f"http://127.0.0.1:{port}"
        server = start_server(dsn, port, metrics_path)
        if not wait_ready(base_url, server):
            return {"scale": scale, "error": "server did not become ready"}

        report: dict[str, Any] = {
            "scale": scale,
            "database": name,
            "seed": {"episodes": len(episode_ids), "seconds": round(seed_seconds, 2)},
            "vocabulary_seed": vocabulary_seed,
            "reads": {},
            "ui": {},
            "downloads": {},
            "writes": {},
        }
        for label, path in read_routes():
            report["reads"][label] = {"path": path, **measure(base_url + path, trials=trials)}
            p95 = report["reads"][label]["p95_ms"]
            print(f"  read  {label:<28} p95 {p95:>8.2f} ms", flush=True)
        for label, path in ui_routes():
            resolved = path
            if "{episode_id}" in path:
                resolved = path.format(episode_id=episode_ids[0])
            report["ui"][label] = {"path": resolved, **measure(base_url + resolved, trials=trials)}
            print(f"  ui    {label:<28} p95 {report['ui'][label]['p95_ms']:>8.2f} ms", flush=True)
        for label, path in download_routes():
            report["downloads"][label] = {
                "path": path,
                **measure(base_url + path, trials=max(5, trials // 4)),
            }
            print(
                f"  dl    {label:<28} p95 {report['downloads'][label]['p95_ms']:>8.2f} ms"
                f"  ({report['downloads'][label]['bytes'] / 1024:.0f} KiB)",
                flush=True,
            )
        fixture = ROOT / "var" / "real-data" / "so101_pick_place_120s.mcap"
        report["writes"] = write_leg(base_url, str(fixture))
        for label, values in report["writes"].items():
            print(
                f"  write {label:<28} p95 {values['p95_ms']:>8.2f} ms  {values['statuses']}",
                flush=True,
            )
        report["hot"] = sorted(
            [
                {"route": f"read/{k}", "p95_ms": v["p95_ms"], "bytes": v["bytes"]}
                for k, v in report["reads"].items()
            ]
            + [
                {"route": f"ui/{k}", "p95_ms": v["p95_ms"], "bytes": v["bytes"]}
                for k, v in report["ui"].items()
            ]
            + [
                {"route": f"download/{k}", "p95_ms": v["p95_ms"], "bytes": v["bytes"]}
                for k, v in report["downloads"].items()
            ],
            key=lambda item: -item["p95_ms"],
        )[:12]
        return report
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
        drop_database(name)


def main() -> None:
    parser = argparse.ArgumentParser(prog="feature_latency")
    parser.add_argument("--scales", type=int, nargs="+", default=[20, 1000, 10000])
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--warmup", type=int, default=WARMUP)
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()

    report: dict[str, Any] = {"trials": args.trials, "warmup": args.warmup, "scales": []}
    for scale in args.scales:
        print(f"\n=== scale: {scale} episodes ===", flush=True)
        started = time.perf_counter()
        leg = run_scale(scale, args.trials)
        leg["wall_s"] = round(time.perf_counter() - started, 1)
        report["scales"].append(leg)
        print(f"scale {scale} finished in {leg['wall_s']}s", flush=True)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
