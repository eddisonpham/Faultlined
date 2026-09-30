"""The `just run` port preflight.

It exists because a second `just run` used to die with a bare `WinError 10048`
- a message that reads as a fault in the project rather than as "a server you
started earlier is still listening".
"""

import importlib.util
import socket
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_port.py"
_SPEC = importlib.util.spec_from_file_location("check_port", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_PORT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_PORT)


def _ephemeral_port() -> int:
    """A port the OS just handed out, so the test never collides with a real one."""
    with socket.socket() as probe:
        probe.bind((_PORT.HOST, 0))
        return int(probe.getsockname()[1])


@pytest.mark.unit
def test_a_free_port_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """ "just run" must not print anything extra on the happy path."""
    assert _PORT._is_free(_ephemeral_port()) is True
    assert capsys.readouterr().err == ""


@pytest.mark.unit
def test_an_occupied_port_is_reported_with_a_way_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    port = _ephemeral_port()
    holder = socket.socket()
    holder.bind((_PORT.HOST, port))
    holder.listen(1)
    try:
        assert _PORT._is_free(port) is False
        monkeypatch.setattr(_PORT.os, "environ", {"DE_PORT": str(port)})
        monkeypatch.setattr(_PORT, "_holder_windows", lambda _port: "4242")
        monkeypatch.setattr(_PORT, "_holder_posix", lambda _port: "4242")
        assert _PORT.main() == 1
    finally:
        holder.close()

    err = capsys.readouterr().err
    assert str(port) in err
    assert "4242" in err, "the owning process must be named"
    # A diagnosis with no remedy is the failure mode this replaces.
    assert "taskkill" in err or "kill " in err
    assert "DE_PORT=" in err, "there must be a way to proceed instead"


@pytest.mark.unit
def test_an_unidentifiable_holder_still_gives_a_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No PID found is not a reason to give no advice."""
    port = _ephemeral_port()
    holder = socket.socket()
    holder.bind((_PORT.HOST, port))
    holder.listen(1)
    try:
        monkeypatch.setattr(_PORT.os, "environ", {"DE_PORT": str(port)})
        monkeypatch.setattr(_PORT, "_holder_windows", lambda _port: None)
        monkeypatch.setattr(_PORT, "_holder_posix", lambda _port: None)
        assert _PORT.main() == 1
    finally:
        holder.close()

    err = capsys.readouterr().err
    assert "netstat" in err or "lsof" in err


@pytest.mark.unit
def test_a_malformed_port_does_not_crash_the_recipe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DE_PORT is operator input; a typo must not traceback out of `just run`."""
    monkeypatch.setattr(_PORT.os, "environ", {"DE_PORT": "not-a-number"})
    assert _PORT._port() == 8000
    capsys.readouterr()
