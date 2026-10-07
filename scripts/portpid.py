"""Port/PID lookup shared by the dev scripts (`check_port`, `stop_server`)."""

from __future__ import annotations

import os
import subprocess

HOST = "127.0.0.1"


def port_from_env() -> int:
    try:
        return int(os.environ.get("DE_API_PORT", "8000"))
    except ValueError:
        return 8000


def listening_pid(port: int) -> str | None:
    """The PID listening on `port`, or None when the port is free."""
    if os.name == "nt":
        return _from_netstat(port)
    return _from_lsof(port)


def process_image(pid: str) -> str | None:
    """The image/process name for `pid`, or None when it cannot be determined."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        else:
            out = subprocess.run(
                ["ps", "-p", pid, "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
    except OSError, subprocess.SubprocessError:
        return None
    if os.name == "nt":
        return _parse_tasklist(out, pid)
    return out.strip() or None


def _from_netstat(port: int) -> str | None:
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
    return _parse_netstat(out, port)


def _from_lsof(port: int) -> str | None:
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
    pids = out.split()
    return pids[0] if pids else None


def _parse_netstat(text: str, port: int) -> str | None:
    """The PID from `netstat -ano` output listening on `port`."""
    for line in text.splitlines():
        if "LISTENING" not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[1].rsplit(":", 1)[-1] == str(port):
            return parts[-1]
    return None


def _parse_tasklist(text: str, pid: str) -> str | None:
    """The image name from `tasklist /FO CSV /NH` output for `pid`."""
    for line in text.splitlines():
        fields = [field.strip('"') for field in line.split('","')]
        if len(fields) >= 2 and fields[1] == pid:
            return fields[0]
    return None
