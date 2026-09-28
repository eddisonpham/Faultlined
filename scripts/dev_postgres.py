#!/usr/bin/env python3
"""Start or stop an isolated local PostgreSQL for development and integration tests.

The cluster lives in gitignored var/pgdata on a non-default port, so it never
touches a system PostgreSQL installation and never needs administrator rights or
a password. Intended for local development only.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "var" / "pgdata"
PGDATA_ENV = "FAULTLINED_PGPORT"
DEFAULT_PORT = 55432
ROLE = "data_engine"
DATABASE = "data_engine"
BINARIES = ("initdb", "pg_ctl", "psql")


def port() -> int:
    return int(os.environ.get(PGDATA_ENV, DEFAULT_PORT))


def has_binary(directory: Path, name: str) -> bool:
    return (directory / f"{name}.exe").exists() or (directory / name).exists()


def find_bin_dir() -> Path:
    override = os.environ.get("PGBIN")
    if override:
        candidate = Path(override)
        if all(has_binary(candidate, name) for name in BINARIES):
            return candidate
        raise SystemExit(f"PGBIN is set to {override} but it lacks {', '.join(BINARIES)}")

    on_path = shutil.which("pg_ctl")
    if on_path:
        return Path(on_path).parent

    root = Path("C:/Program Files/PostgreSQL")
    if root.is_dir():
        for version_dir in sorted(root.iterdir(), reverse=True):
            candidate = version_dir / "bin"
            if has_binary(candidate, "pg_ctl"):
                return candidate
    raise SystemExit("Could not find PostgreSQL binaries. Set PGBIN to the bin directory.")


def run(bin_dir: Path, name: str, *args: str) -> subprocess.CompletedProcess[str]:
    suffix = ".exe" if os.name == "nt" else ""
    return subprocess.run(
        [str(bin_dir / f"{name}{suffix}"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def is_listening() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port())) == 0


def init_cluster(bin_dir: Path) -> None:
    if DATA_DIR.exists():
        return
    print(f"Initializing cluster in {DATA_DIR}")
    result = run(
        bin_dir, "initdb", "-D", str(DATA_DIR), "-U", ROLE, "--auth=trust", "--encoding=UTF8"
    )
    if result.returncode != 0:
        raise SystemExit(f"initdb failed:\n{result.stderr}")


def start(bin_dir: Path) -> None:
    if is_listening():
        print(f"PostgreSQL already listening on port {port()}")
        return
    # Detach so the server outlives the invoking shell. Without this, a Ctrl+C or a
    # killed parent tears down the whole cluster: console control events reach the
    # server's children (0xC000013A). CREATE_NEW_PROCESS_GROUP is ignored when
    # DETACHED_PROCESS is set, so it must not be combined with it.
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    log = (DATA_DIR / "server.log").open("a", encoding="utf-8")
    try:
        subprocess.Popen(
            [
                str(bin_dir / ("postgres.exe" if os.name == "nt" else "postgres")),
                "-D",
                str(DATA_DIR),
                "-p",
                str(port()),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **kwargs,  # type: ignore[arg-type]
        )
    finally:
        log.close()

    for _ in range(60):
        if is_listening():
            break
        time.sleep(0.5)
    else:
        raise SystemExit(f"PostgreSQL did not start within 30s; see {DATA_DIR / 'server.log'}")
    print(f"PostgreSQL listening on port {port()}")


def ensure_database(bin_dir: Path) -> None:
    result = run(
        bin_dir,
        "psql",
        "-U",
        ROLE,
        "-h",
        "127.0.0.1",
        "-p",
        str(port()),
        "-d",
        "postgres",
        "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname = '{DATABASE}'",
    )
    if result.returncode == 0 and result.stdout.strip() == "1":
        return
    run(
        bin_dir,
        "psql",
        "-U",
        ROLE,
        "-h",
        "127.0.0.1",
        "-p",
        str(port()),
        "-d",
        "postgres",
        "-c",
        f"CREATE DATABASE {DATABASE} OWNER {ROLE}",
    )


def dsn() -> str:
    return f"postgresql://{ROLE}@127.0.0.1:{port()}/{DATABASE}"


def up() -> int:
    bin_dir = find_bin_dir()
    init_cluster(bin_dir)
    start(bin_dir)
    ensure_database(bin_dir)
    print(f"\nDE_DATABASE_URL={dsn()}\n")
    print("Add that line to .env so `just` recipes pick it up. It contains no password:")
    print("this cluster uses local trust auth and is for development only.")
    return 0


def down() -> int:
    bin_dir = find_bin_dir()
    if not is_listening():
        print("PostgreSQL is not running")
        return 0
    result = run(bin_dir, "pg_ctl", "-D", str(DATA_DIR), "-m", "fast", "stop")
    if result.returncode != 0:
        raise SystemExit(f"pg_ctl stop failed:\n{result.stderr}")
    print("PostgreSQL stopped")
    return 0


def status() -> int:
    running = is_listening()
    print(f"port {port()}: {'listening' if running else 'not listening'}")
    if running:
        print(f"DE_DATABASE_URL={dsn()}")
    return 0 if running else 1


def main() -> int:
    actions = {"up": up, "down": down, "status": status}
    action = sys.argv[1] if len(sys.argv) > 1 else "up"
    if action not in actions:
        raise SystemExit(f"usage: {Path(sys.argv[0]).name} [{'|'.join(actions)}]")
    return actions[action]()


if __name__ == "__main__":
    raise SystemExit(main())
