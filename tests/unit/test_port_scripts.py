"""Tests for the dev port/PID scripts (`check_port`, `stop_server`).

The parsers are tested against synthetic tool output; `listening_pid` and
`process_image` are tested against a real bound socket, which is the whole
point of the scripts - agreeing with what actually holds the port.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import portpid  # noqa: E402
import stop_server  # noqa: E402

from data_engine.config import Settings  # noqa: E402


@pytest.fixture()
def bound_port() -> tuple[int, socket.socket]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    return port, sock


class TestPortFromEnv:
    def test_defaults_to_8000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DE_API_PORT", raising=False)
        assert portpid.port_from_env() == 8000

    def test_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DE_API_PORT", "9123")
        assert portpid.port_from_env() == 9123

    def test_garbage_falls_back_to_8000(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DE_API_PORT", "not-a-port")
        assert portpid.port_from_env() == 8000

    def test_agrees_with_the_port_the_server_binds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """F19: the check must read the same variable the application binds.

        It used to read `DE_PORT`, which the settings never read, so the advice
        it printed was a port the server would not use.
        """
        monkeypatch.setenv("DE_API_PORT", "9124")
        assert portpid.port_from_env() == Settings().api_port

    def test_the_old_variable_name_is_not_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DE_API_PORT", raising=False)
        monkeypatch.setenv("DE_PORT", "9125")
        assert portpid.port_from_env() == 8000


class TestParseNetstat:
    def test_finds_the_listener(self) -> None:
        text = (
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       7240\n"
            "  TCP    127.0.0.1:5354         0.0.0.0:0              LISTENING       4321\n"
        )
        assert portpid._parse_netstat(text, 8000) == "7240"
        assert portpid._parse_netstat(text, 5354) == "4321"

    def test_ignores_non_listening_rows(self) -> None:
        text = "  TCP    127.0.0.1:8000         127.0.0.1:5000         ESTABLISHED     99\n"
        assert portpid._parse_netstat(text, 8000) is None

    def test_ignores_short_and_foreign_rows(self) -> None:
        text = "  UDP    0.0.0.0:5353           *:*                                    1234\n"
        assert portpid._parse_netstat(text, 5353) is None

    def test_no_match_is_none(self) -> None:
        assert portpid._parse_netstat("garbage", 8000) is None

    def test_suffix_ports_do_not_collide(self) -> None:
        # Port 080 would rsplit to "080" != "80"; likewise 80000 vs 8000.
        text = "  TCP    127.0.0.1:80000        0.0.0.0:0              LISTENING       5\n"
        assert portpid._parse_netstat(text, 8000) is None


class TestParseTasklist:
    def test_finds_the_pid(self) -> None:
        text = '"python.exe","7240","Console","1","123,456 K"\n'
        assert portpid._parse_tasklist(text, "7240") == "python.exe"

    def test_other_pid_is_none(self) -> None:
        text = '"python.exe","7240","Console","1","123,456 K"\n'
        assert portpid._parse_tasklist(text, "999") is None

    def test_empty_is_none(self) -> None:
        assert portpid._parse_tasklist("", "1") is None


class TestListeningPid:
    def test_finds_ourselves_on_a_real_socket(self, bound_port: tuple[int, socket.socket]) -> None:
        port, sock = bound_port
        try:
            pid = portpid.listening_pid(port)
        finally:
            sock.close()
        assert pid == str(os.getpid())

    def test_free_port_is_none(self) -> None:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
        sock.close()
        # Closed: nobody is listening on it.
        assert portpid.listening_pid(free) is None


class TestProcessImage:
    def test_image_for_a_live_pid(self) -> None:
        image = portpid.process_image(str(os.getpid()))
        assert image is not None
        assert "python" in image.lower()

    def test_image_for_a_bogus_pid_is_none(self) -> None:
        assert portpid.process_image("999999999") is None


class TestLooksLikeOurs:
    @pytest.mark.parametrize(
        ("image", "expected"),
        [
            ("python.exe", True),
            ("python3.14.exe", True),
            ("de.exe", True),
            ("uvicorn.exe", True),
            ("my-python-notebook.exe", True),  # substring match, deliberately loose
            ("chrome.exe", False),
            ("nginx.exe", False),
        ],
    )
    def test_image_names(self, image: str, expected: bool) -> None:
        assert stop_server._looks_like_ours(image) is expected

    def test_unknown_image_is_assumed_ours(self) -> None:
        assert stop_server._looks_like_ours(None) is True
