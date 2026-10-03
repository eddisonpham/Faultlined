#!/usr/bin/env python3
"""Stage-5 fault injection: hard-killing a worker mid-ingest (NFR-006, F5/F10).

One throwaway database per trial, a real `de worker` process ingesting the real
hour-long MCAP bag (~43 MiB, ~13 s of ingest to kill inside), and a real hard kill
(`taskkill /F /T`: no `finally`, no failure path, the same visible state a container
restart or OOM-kill leaves). Then a fresh worker's production reaper sweep must
reclaim the orphaned job and finish it, and the catalog must converge to exactly one
episode and one intact artifact no matter where in the ingest the kill landed.

Two kill points per run, sampling the two shapes a mid-ingest death can take:

- ``mid-parse``   killed while the reader is still parsing; nothing published yet.
- ``on-write``    killed at the first sign of artifact-write activity (the
                  ``.pending-*`` temp or the published blob), the publish-then-
                  register window where a half-done ingest can strand bytes.

The 900 s presumed-death window (`ORPHANED_JOB_SECONDS`) is not waited out: the
victim's `started_at` is back-dated past it before recovery starts, so the
production reaper SQL runs with its production constant and the drill measures the
recovery, not the clock. Kill `None` is not simulated anywhere: the worker process
is always really killed.

Checks after recovery, in both trials: the job succeeded on its retry, exactly one
episode row exists for the source, the artifact blob's SHA-256 still matches the
file it claims to be, exactly one quality row exists, and re-ingesting the same
source as a fresh job is a no-op (F26). Stranded ``.pending-*`` temps and any blob
published without a matching episode row are reported, not hidden.

Numbers go to stdout as JSON; this script writes nothing to the repo.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

# Throwaway-database helpers live next door in the scaling-campaign driver; both
# scripts are campaign tooling over the same isolated Postgres conventions.
from scale_campaign import (  # noqa: E402
    catalog_for,
    drop_database,
    initialize,
    make_database,
)

FIXTURE = ROOT / "var" / "real-data" / "so101_pick_place_hour.mcap"
ORPHAN_WINDOW_SECONDS = 900.0  # jobs.state.ORPHANED_JOB_SECONDS, mirrored for back-dating


def _require_fixture() -> None:
    if not FIXTURE.is_file():
        raise SystemExit(f"fixture not found: {FIXTURE} (scripts/make_mcap_log.py --seconds 3600)")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _worker_env(dsn: str, artifact_root: Path, metrics_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Every piece of worker state goes to the trial's throwaway directories: the
    # drill must not leave blobs in var/artifacts or records in the dev metrics sink.
    env["DE_DATABASE_URL"] = dsn
    env["DE_ARTIFACT_ROOT"] = str(artifact_root)
    env["DE_METRICS_PATH"] = str(metrics_path)
    return env


def _worker_command(*cli_args: str) -> list[str]:
    """The worker entry point, run in this interpreter rather than through `uv`.

    `uv run` wraps the worker in a launcher process: `Popen.pid` is then the
    launcher, and `terminate()` kills only that - the real worker survives as an
    orphan and keeps claiming jobs from the throwaway catalog (it happened on the
    first draft of this drill: a "stopped" worker was still running). Killing the
    python process that executes the job loop is the whole point, so the drill
    spawns `data_engine.cli:main` directly - same code path `de worker` runs.
    """
    return [
        sys.executable,
        "-c",
        f"from data_engine.cli import main; main({list(cli_args)!r})",
    ]


def _start_worker(dsn: str, artifact_root: Path, log_path: Path) -> subprocess.Popen[bytes]:
    log = log_path.open("wb")
    proc = subprocess.Popen(
        _worker_command("worker"),
        env=_worker_env(dsn, artifact_root, log_path.parent / "metrics.jsonl"),
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
    )
    return proc


def _hard_kill(proc: subprocess.Popen[bytes]) -> None:
    """TerminateProcess on the worker itself: no cleanup, no failure path runs.

    `Popen.kill()` is TerminateProcess on Windows - the same unclean death as
    `kill -9` or an OOM-kill, which is exactly the state NFR-006 is about.
    """
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - kill does not miss
        raise SystemExit(f"worker {proc.pid} survived kill") from None


def _stop_worker(proc: subprocess.Popen[bytes]) -> None:
    """Drill workers do not need a graceful exit; kill and reap."""
    _hard_kill(proc)


def _connect(dsn: str):
    import psycopg

    return psycopg.connect(dsn, row_factory=psycopg.rows.dict_row, connect_timeout=5)


def _job_row(dsn: str, job_id: str) -> dict[str, object]:
    with _connect(dsn) as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    if row is None:
        raise SystemExit(f"job {job_id} disappeared")
    return dict(row)


def _wait_started(dsn: str, job_id: str, timeout: float) -> float:
    """Poll until the job is claimed; return seconds since submission."""
    deadline = time.perf_counter() + timeout
    created = _job_row(dsn, job_id)["created_at"]
    while time.perf_counter() < deadline:
        row = _job_row(dsn, job_id)
        if row["started_at"] is not None:
            return (row["started_at"] - created).total_seconds()
        time.sleep(0.02)
    raise SystemExit(f"job {job_id} was not claimed within {timeout}s")


def _wait_write_activity(artifact_root: Path, timeout: float) -> str:
    """Return as soon as the ingest starts writing artifacts, naming what appeared.

    The ``.pending-*`` temp is created at the start of the blob copy, so it is the
    earliest visible sign that the write half of the ingest has begun - wide enough
    a window to kill into deterministically, unlike the publish-to-register gap.
    """
    blob_dir = artifact_root / "blobs" / "sha256"
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if blob_dir.is_dir():
            for path in blob_dir.rglob("*"):
                if path.is_file():
                    return path.name
        time.sleep(0.005)
    raise SystemExit(f"no artifact-write activity within {timeout}s")


def _snapshot(dsn: str, artifact_root: Path, source_hash: str) -> dict[str, object]:
    with _connect(dsn) as connection:
        episodes = connection.execute(
            "SELECT count(*) AS n FROM episodes WHERE source_hash = %s", (source_hash,)
        ).fetchone()["n"]
        artifacts = connection.execute(
            "SELECT count(*) AS n FROM artifacts WHERE hash = %s", (source_hash,)
        ).fetchone()["n"]
        quality = connection.execute(
            """SELECT count(*) AS n FROM episode_quality q
               JOIN episodes e ON e.id = q.episode_id WHERE e.source_hash = %s""",
            (source_hash,),
        ).fetchone()["n"]
    blob_dir = artifact_root / "blobs" / "sha256"
    published = [p.name for p in blob_dir.rglob("*") if p.is_file()] if blob_dir.is_dir() else []
    return {
        "episodes": episodes,
        "artifacts_rows": artifacts,
        "quality_rows": quality,
        "blob_files_on_disk": published,
        "stranded_pending_temps": [n for n in published if n.startswith(".pending-")],
    }


def _backdate_orphan(dsn: str, job_id: str) -> None:
    """Age the victim's `started_at` past the presumed-death window.

    The window itself is production (900 s); waiting it out would measure the
    clock, so the drill moves the clock instead and the reaper SQL runs unmodified.
    """
    with _connect(dsn) as connection:
        connection.execute(
            """UPDATE jobs SET started_at = started_at - make_interval(secs => %s)
               WHERE id = %s""",
            (ORPHAN_WINDOW_SECONDS + 1, job_id),
        )
        connection.commit()


def _wait_terminal(dsn: str, job_id: str, timeout: float) -> float:
    """Poll until the job reaches a terminal state; return wall seconds waited."""
    start = time.perf_counter()
    deadline = start + timeout
    while time.perf_counter() < deadline:
        state = str(_job_row(dsn, job_id)["state"])
        if state in ("succeeded", "failed", "canceled", "timed_out"):
            return time.perf_counter() - start
        time.sleep(0.05)
    raise SystemExit(f"job {job_id} did not finish within {timeout}s")


def run_trial(kill_mode: str, source_hash: str, keep_db: bool) -> dict[str, object]:
    name, dsn = make_database("data_engine_scale_fault")
    trial_tmp = ROOT / "var" / "tmp" / f"fault-{uuid.uuid4().hex[:6]}"
    artifact_root = trial_tmp / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    initialize(dsn)
    catalog = catalog_for(dsn)
    try:
        job, _ = catalog.submit_job(
            "ingest_source",
            {"source": str(FIXTURE)},
            f"fault-{kill_mode}-{uuid.uuid4().hex}",
            str(uuid.uuid4()),
        )
        job_id = str(job["id"])
        victim = _start_worker(dsn, artifact_root, trial_tmp / "victim.log")
        try:
            submit_to_start = _wait_started(dsn, job_id, timeout=90)
            if kill_mode == "mid-parse":
                time.sleep(3.0)
                trigger = "fixed 3.0 s after claim (reader parsing)"
            else:
                trigger = _wait_write_activity(artifact_root, timeout=30)
                trigger = f"first artifact-write activity: {trigger}"
                # The write window is short (copy ~43 MiB, then register+finish):
                # kill immediately on sighting, not on the next scheduled step.
            _hard_kill(victim)
        finally:
            _stop_worker(victim)

        after_kill = _job_row(dsn, job_id)
        state_after_kill = str(after_kill["state"])
        killed_state = _snapshot(dsn, artifact_root, source_hash)

        _backdate_orphan(dsn, job_id)
        recovery_started = time.perf_counter()
        rescuer = _start_worker(dsn, artifact_root, trial_tmp / "rescuer.log")
        try:
            _wait_terminal(dsn, job_id, timeout=300)
        finally:
            _stop_worker(rescuer)
        recovery_wall = time.perf_counter() - recovery_started

        finished = _job_row(dsn, job_id)
        result = finished.get("result") or {}

        # F26 under crash conditions: a fresh ingest of the same source must not
        # grow a second episode. `de worker --once` drains exactly the dup job.
        dup, _ = catalog.submit_job(
            "ingest_source",
            {"source": str(FIXTURE)},
            f"fault-dup-{uuid.uuid4().hex}",
            str(uuid.uuid4()),
        )
        subprocess.run(
            _worker_command("worker", "--once"),
            env=_worker_env(dsn, artifact_root, trial_tmp / "metrics.jsonl"),
            capture_output=True,
            cwd=str(ROOT),
            timeout=120,
            check=False,
        )
        final = _snapshot(dsn, artifact_root, source_hash)

        blob_path = artifact_root / "blobs" / "sha256" / source_hash[:2] / source_hash
        blob_ok = (
            blob_path.is_file()
            and hashlib.sha256(blob_path.read_bytes()).hexdigest() == source_hash
        )
        kill_effective = state_after_kill == "running"
        return {
            "kill_mode": kill_mode,
            "database": name,
            "trigger": trigger,
            "submit_to_start_s": round(submit_to_start, 3),
            "state_after_kill": state_after_kill,
            "state_after_kill_snapshot": killed_state,
            "recovery": {
                "wall_s": round(recovery_wall, 2),
                "state": str(finished["state"]),
                "attempts": finished["attempts"],
                "checked": result.get("episode_id") is not None,
            },
            "final_snapshot": final,
            "blob_matches_source_sha256": blob_ok,
            "kill_effective": kill_effective,
            "duplicate_ingest_job_state": str(_job_row(dsn, str(dup["id"]))["state"]),
            "converged": (
                kill_effective
                and final["episodes"] == 1
                and final["quality_rows"] == 1
                and blob_ok
                and str(finished["state"]) == "succeeded"
            ),
            "keep_db": keep_db,
        }
    finally:
        if not keep_db:
            drop_database(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    _require_fixture()
    source_hash = _sha256_file(FIXTURE)
    # Retry each mode until the kill lands mid-flight: the on-write window is
    # short enough that a kill can arrive after the job already finished, and a
    # trial whose victim died with the job done proves nothing about recovery.
    trials = []
    for mode in ("mid-parse", "on-write"):
        for _attempt in range(3):
            trial = run_trial(mode, source_hash, args.keep_db)
            trials.append(trial)
            if trial["kill_effective"]:
                break
        else:  # pragma: no cover - 3 misses means the window moved
            raise SystemExit(f"could not land a {mode} kill mid-flight in 3 attempts")
    report = {
        "fixture": str(FIXTURE.relative_to(ROOT)),
        "source_sha256": source_hash,
        "orphan_window_production_s": ORPHAN_WINDOW_SECONDS,
        "trials": trials,
        "met": all(bool(t["converged"]) for t in trials),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
