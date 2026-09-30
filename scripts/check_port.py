"""Refuse to start when the API port is already taken, and say who took it.

A second `just run` otherwise dies with a bare `WinError 10048`, which reads as a
fault in the project rather than "a server you started earlier is still
listening" - and the fix is a command the reader has to guess.

Implemented in Python rather than as a shell snippet in the recipe for two
reasons. A `#!/usr/bin/env bash` recipe makes `just` resolve the interpreter
through `cygpath`, which is absent from a PowerShell PATH that has Git's `bin`
but not `usr\bin` - the same trap `windows-shell` in the justfile already
documents. And the check needs different commands per platform (`netstat` on
Windows, `lsof` elsewhere), which is a few lines of Python and an ugly
conditional in a recipe.

The port comes from the same environment the application reads, so this check
can never disagree with the port the server is about to bind.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys

HOST = "127.0.0.1"


def _port() -> int:
    try:
        return int(os.environ.get("DE_PORT", "8000"))
    except ValueError:
        return 8000


def _holder_windows(port: int) -> str | None:
    """The PID listening on `port`, from netstat -ano."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return None
    for line in out.splitlines():
        parts = line.split()
        # "TCP  127.0.0.1:8000  0.0.0.0:0  LISTENING  1234"
        if len(parts) < 5 or "LISTENING" not in line:
            continue
        if parts[1].rsplit(":", 1)[-1] == str(port):
            return parts[-1]
    return None


def _holder_posix(port: int) -> str | None:
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except OSError, subprocess.SubprocessError:
        return None
    first = out.split()
    return first[0] if first else None


def _is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def main() -> int:
    port = _port()
    if _is_free(port):
        return 0

    pid = _holder_windows(port) if os.name == "nt" else _holder_posix(port)
    suffix = f" by PID {pid}" if pid else ""
    print(f"Port {port} is already in use{suffix}.", file=sys.stderr)
    print("That is usually an earlier 'just run' that is still going.", file=sys.stderr)
    print("", file=sys.stderr)
    if pid:
        stop = f"taskkill //PID {pid} //F" if os.name == "nt" else f"kill {pid}"
        print(f"  stop it:   {stop}", file=sys.stderr)
    else:
        # Still actionable without a PID: a reader who cannot find the process
        # needs the command, not just the diagnosis.
        print(f"  find it:   netstat -ano | findstr :{port}", file=sys.stderr)
        print(f"  or:        lsof -i tcp:{port} -sTCP:LISTEN", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"To run on a different port:  DE_PORT={port + 1} just run", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
