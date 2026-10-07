#!/usr/bin/env python3
"""Crawl the live UI and count the clicks between the things an operator needs."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

LANDMARKS = (
    ("status", "/ui"),
    ("jobs_list", "/ui/jobs"),
    ("job_detail", "/ui/jobs/{job_id}"),
    ("episodes_list", "/ui/episodes"),
    ("episode_detail", "/ui/episodes/{episode_id}"),
    ("failures", "/ui/failures"),
    ("vocabulary", "/ui/vocabulary"),
    ("vocabulary_entry", "/ui/vocabulary/entries/{entry_id}"),
    ("slices", "/ui/slices"),
    ("slice_detail", "/ui/slices/{slice_id}"),
    ("builds", "/ui/builds"),
    ("build_detail", "/ui/builds/{build_hash}"),
    ("incidents", "/ui/incidents"),
    ("metrics", "/ui/metrics"),
    ("insights", "/ui/insights"),
    ("artifacts", "/ui/artifacts"),
    ("schema", "/ui/schema"),
    ("benchmarks", "/ui/benchmarks"),
    ("experiments", "/ui/experiments"),
)

WORKFLOWS = (
    ("submit a bag and watch it land", "status", "job_detail"),
    ("read why a job failed", "jobs_list", "job_detail"),
    ("inspect an episode's motion quality", "episodes_list", "episode_detail"),
    ("triage one raw task string", "vocabulary", "vocabulary"),
    ("see what a vocabulary entry covers", "vocabulary", "vocabulary_entry"),
    ("find quarantined episodes", "status", "failures"),
    ("build a dataset", "episodes_list", "builds"),
    ("see the build that contains an episode", "episode_detail", "builds"),
    ("check the system is healthy", "status", "metrics"),
    ("see what broke", "status", "incidents"),
)

_HREF = re.compile(r'href="([^"]+)"')
_BUTTON = re.compile(r"<button\b", re.IGNORECASE)
_FORM = re.compile(r"<form\b", re.IGNORECASE)
_INPUT = re.compile(r"<(?:input|select|textarea)\b", re.IGNORECASE)
_ROW = re.compile(r"<tr\b", re.IGNORECASE)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch(url: str, timeout: float = 60.0) -> tuple[int, str, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.url, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, url, error.read().decode("utf-8", "replace")
    except Exception as error:
        return 0, url, str(error)


def internal_links(base_url: str, html: str, current: str) -> list[str]:
    out: list[str] = []
    for href in _HREF.findall(html):
        if href.startswith(("http://", "https://", "mailto:", "#", "javascript:")):
            continue
        absolute = urljoin(current, href)
        parsed = urlparse(absolute)
        if parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
            continue
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        if path.startswith("/ui") or path.startswith("/api"):
            out.append(path)
    return out


def start_server(dsn: str, port: int, workdir: Path) -> subprocess.Popen[bytes]:
    workdir.mkdir(parents=True, exist_ok=True)
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
        try:
            with urllib.request.urlopen(base_url + "/api/v1/health", timeout=5) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def crawl(base_url: str, seeds: list[str], max_pages: int = 120) -> dict[str, dict[str, Any]]:
    """Breadth-first over rendered links, up to `max_pages` distinct pages."""
    pages: dict[str, dict[str, Any]] = {}
    queue: deque[str] = deque(seeds)
    seen: set[str] = set()
    while queue and len(pages) < max_pages:
        path = queue.popleft()
        if path in seen:
            continue
        seen.add(path)
        status, final_url, html = fetch(base_url + path)
        if status != 200:
            pages[path] = {"status": status, "links": [], "error": html[:200]}
            continue
        links = sorted(set(internal_links(base_url, html, final_url)))
        pages[path] = {
            "status": status,
            "bytes": len(html),
            "buttons": len(_BUTTON.findall(html)),
            "forms": len(_FORM.findall(html)),
            "fields": len(_INPUT.findall(html)),
            "rows": len(_ROW.findall(html)),
            "links": links,
            "title": (re.search(r"<title>(.*?)</title>", html, re.S) or [None, ""])[1].strip()[:60],
        }
        for link in links:
            if link not in seen and len(seen) + len(queue) < max_pages * 3:
                queue.append(link)
    return pages


def shortest_path(pages: dict[str, dict[str, Any]], start: str, goal: str) -> dict[str, Any] | None:
    """Fewest page loads (clicks) from `start` to `goal` over the rendered link graph."""
    if start not in pages or goal not in pages:
        return None
    neighbours: dict[str, set[str]] = {
        path: set(info.get("links", [])) for path, info in pages.items()
    }
    for path, info in pages.items():
        html_links = info.get("links", [])
        groups = {urlparse(link).path for link in html_links if link.split("?")[0] in pages}
        neighbours[path] |= groups
    dist: dict[str, int] = {start: 0}
    parent: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            break
        for nxt in sorted(neighbours.get(node, set())):
            if nxt not in dist and nxt in neighbours:
                dist[nxt] = dist[node] + 1
                parent[nxt] = node
                queue.append(nxt)
    if goal not in dist:
        return {"clicks": None, "path": None, "reachable": False}
    path_nodes: list[str] = []
    cursor: str | None = goal
    while cursor is not None:
        path_nodes.append(cursor)
        cursor = parent[cursor]
    path_nodes.reverse()
    return {"clicks": dist[goal], "path": path_nodes, "reachable": True}


def main() -> None:
    parser = argparse.ArgumentParser(prog="operator_walkthrough")
    parser.add_argument("--episodes", type=int, default=40)
    parser.add_argument("--entries", type=int, default=12)
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()

    from scripts.scale_campaign import _seed_catalog, drop_database, make_database

    name, dsn = make_database("walk")
    workdir = ROOT / "var" / "walk" / name
    server: subprocess.Popen[bytes] | None = None
    try:
        from scripts.feature_latency import seed_metrics, seed_vocabulary

        from data_engine.catalog import vocabulary
        from data_engine.catalog.database import initialize_schema
        from data_engine.catalog.repository import PostgresCatalog
        from data_engine.config import Settings

        settings = Settings(_env_file=None, database_url=dsn)
        initialize_schema(settings)
        workdir.mkdir(parents=True, exist_ok=True)
        seed_metrics(workdir / "runtime.jsonl")
        _seed_seconds, episode_ids = _seed_catalog(dsn, args.episodes)
        seed_vocabulary(dsn, args.entries)
        catalog = PostgresCatalog(settings)
        fixture = ROOT / "var" / "real-data" / "so101_pick_place.mcap"
        job_ids: list[str] = []
        for index in range(3):
            row, _correlation = catalog.submit_job(
                "ingest_source",
                {
                    "source": str(fixture)
                    if index < 2
                    else str(ROOT / "var" / "foreign-corpus" / "not_a_dataset")
                },
                f"walk-{index}",
                str(__import__("uuid").uuid4()),
            )
            job_ids.append(str(row["id"]))
        from data_engine.jobs.worker import IngestWorker

        worker = IngestWorker(settings)
        deadline = time.time() + 180
        while time.time() < deadline:
            if worker.process_one() is None:
                states = {str(catalog.get_job(j)["state"]) for j in job_ids}
                if states <= {"succeeded", "failed", "canceled"}:
                    break
            time.sleep(0.05)
        entries = vocabulary.list_entries(settings)
        entry_id = str(entries[0]["id"]) if entries else ""
        slices = catalog.list_slices()
        slice_id = str(slices[0]["id"]) if slices else ""
        builds = catalog.list_builds()
        build_hash = str(builds[0]["hash"]) if builds else ""

        resolved = {
            "status": "/ui",
            "jobs_list": "/ui/jobs",
            "job_detail": f"/ui/jobs/{job_ids[0]}",
            "episodes_list": "/ui/episodes",
            "episode_detail": f"/ui/episodes/{episode_ids[0]}",
            "failures": "/ui/failures",
            "vocabulary": "/ui/vocabulary",
            "vocabulary_entry": f"/ui/vocabulary/entries/{entry_id}" if entry_id else "",
            "slices": "/ui/slices",
            "slice_detail": f"/ui/slices/{slice_id}" if slice_id else "",
            "builds": "/ui/builds",
            "build_detail": f"/ui/builds/{build_hash}" if build_hash else "",
            "incidents": "/ui/incidents",
            "metrics": "/ui/metrics",
            "insights": "/ui/insights",
            "artifacts": "/ui/artifacts",
            "schema": "/ui/schema",
            "benchmarks": "/ui/benchmarks",
            "experiments": "/ui/experiments",
        }

        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        server = start_server(dsn, port, workdir)
        if not wait_ready(base_url, server):
            raise SystemExit("server did not become ready")

        seeds = sorted({path for path in resolved.values() if path and path not in ("", "/ui")})
        seeds.append("/ui")
        print(f"crawling from {len(seeds)} seeds...", flush=True)
        pages = crawl(base_url, seeds)
        report: dict[str, Any] = {"pages_crawled": len(pages)}

        inventory = {}
        for name, path in resolved.items():
            if not path or path not in pages:
                inventory[name] = {
                    "path": path,
                    "status": (pages.get(path) or {}).get("status", "unreachable"),
                }
                continue
            info = pages[path]
            inventory[name] = {
                "path": path,
                "status": info.get("status"),
                "bytes": info.get("bytes"),
                "buttons": info.get("buttons"),
                "forms": info.get("forms"),
                "fields": info.get("fields"),
                "table_rows": info.get("rows"),
                "outbound_links": len(info.get("links", [])),
            }
        report["pages"] = inventory

        journeys = []
        for label, start, goal in WORKFLOWS:
            start_path = resolved.get(start, "")
            goal_path = resolved.get(goal, "")
            if not start_path or not goal_path or goal_path not in pages:
                journeys.append(
                    {
                        "workflow": label,
                        "start": start,
                        "goal": goal,
                        "clicks": None,
                        "note": "page not present",
                    }
                )
                continue
            result = shortest_path(pages, start_path, goal_path)
            journeys.append(
                {
                    "workflow": label,
                    "start": start,
                    "goal": goal,
                    "clicks": (result or {}).get("clicks"),
                    "path": (result or {}).get("path"),
                    "reachable": (result or {}).get("reachable"),
                }
            )
        report["workflows"] = journeys

        print(f"\npages crawled: {len(pages)}")
        print(f"\n{'workflow':<44} {'clicks':>7}")
        print("-" * 54)
        for journey in journeys:
            print(f"{journey['workflow']:<44} {journey['clicks']!s:>7}")
        head = f"\n{'page':<22} {'bytes':>8} {'btn':>5} {'form':>5}"
        print(f"{head} {'field':>6} {'rows':>6} {'links':>6}")
        print("-" * 68)
        for name, info in inventory.items():
            print(
                f"{name:<22} {info.get('bytes', '')!s:>8} {info.get('buttons', '')!s:>5}"
                f" {info.get('forms', '')!s:>5} {info.get('fields', '')!s:>6}"
                f" {info.get('table_rows', '')!s:>6} {info.get('outbound_links', '')!s:>6}"
            )
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
