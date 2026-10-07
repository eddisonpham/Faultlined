"""Stop the dev server that is listening on the API port - `just stop`."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from portpid import listening_pid, port_from_env, process_image

_OWN_IMAGE_PARTS = ("de", "python", "uvicorn")


def _looks_like_ours(image: str | None) -> bool:
    if image is None:
        return True
    lowered = image.lower()
    return any(part in lowered for part in _OWN_IMAGE_PARTS)


def _terminate(pid: str) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", pid, "/F"], check=False, capture_output=True)
    else:
        subprocess.run(["kill", pid], check=False, capture_output=True)


def main() -> int:
    port = port_from_env()
    pid = listening_pid(port)
    if pid is None:
        print(f"Nothing is listening on port {port}.")
        return 0

    image = process_image(pid)
    if not _looks_like_ours(image):
        manual = f"taskkill //PID {pid} //F" if os.name == "nt" else f"kill {pid}"
        print(
            f"Port {port} is held by PID {pid} ({image}), which does not look like "
            "this project's server. Not stopping it.",
            file=sys.stderr,
        )
        print(f"Stop it yourself if you are sure:  {manual}", file=sys.stderr)
        return 1

    _terminate(pid)
    if listening_pid(port) is None:
        print(f"Stopped PID {pid}" + (f" ({image})" if image else "") + f"; port {port} is free.")
        return 0
    print(f"Sent the stop to PID {pid}, but port {port} is still listening.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
