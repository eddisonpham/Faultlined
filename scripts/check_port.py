"""Refuse to start when the API port is already taken, and say who took it.

A second `just run` otherwise dies with a bare `WinError 10048`, which reads as a
fault in the project rather than "a server you started earlier is still
listening" - and the fix is a command the reader has to guess.

Implemented in Python rather than as a shell snippet in the recipe for two
reasons. A `#!/usr/bin/env bash` recipe makes `just` resolve the interpreter
through `cygpath`, which is absent from a PowerShell PATH that has Git's `bin`
but not `usr\\bin` - the same trap `windows-shell` in the justfile already
documents. And the check needs different commands per platform (`netstat` on
Windows, `lsof` elsewhere), which is a few lines of Python and an ugly
conditional in a recipe.

The port comes from the same environment the application reads, so this check
can never disagree with the port the server is about to bind.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from portpid import listening_pid, port_from_env, process_image

HOST = "127.0.0.1"


def _is_free(port: int) -> bool:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def main() -> int:
    port = port_from_env()
    if _is_free(port):
        return 0

    pid = listening_pid(port)
    suffix = f" by PID {pid}" if pid else ""
    print(f"Port {port} is already in use{suffix}.", file=sys.stderr)
    print("That is usually an earlier 'just run' that is still going.", file=sys.stderr)
    print("", file=sys.stderr)
    image = process_image(pid) if pid else None
    if image:
        print(f"  that process is:   {image}", file=sys.stderr)
    print("  stop it:   just stop", file=sys.stderr)
    if pid:
        stop = f"taskkill //PID {pid} //F" if sys.platform == "win32" else f"kill {pid}"
        print(f"  or:        {stop}", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"To run on a different port:  DE_PORT={port + 1} just run", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
