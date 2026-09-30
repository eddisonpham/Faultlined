"""The real-data fixture fetcher, tested without a network.

The `network` tests are the only ones in the suite that talk to the Hugging Face
Hub, and a broken build has been traced to them more than once. They are also
the tests whose *failure mode* matters most: the fetcher runs before any reader
code, so a truncated body does not fail as "bad parquet", it fails as something
that looks like a reader bug three layers down.

These tests drive a real HTTP server on localhost, because the behaviours that
matter here - a short body, a reset mid-transfer, a 404 - cannot be produced by
mocking `urlopen`. They are `unit` tests and need no egress.
"""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"
_SPEC = importlib.util.spec_from_file_location("_de_conftest", _CONFTEST)
assert _SPEC is not None and _SPEC.loader is not None
_DOWNLOAD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_DOWNLOAD)


class _Handler(BaseHTTPRequestHandler):
    """Serves whatever the test asked for, including deliberately broken bodies."""

    def log_message(self, *args: object) -> None:
        """Silence the default stderr logging."""

    def do_GET(self) -> None:
        if self.path == "/good":
            body = b"PAR1-parquet-bytes"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/short":
            # The dangerous one: a well-formed 200 whose body stops early. A
            # fetcher that trusts the socket writes a corrupt fixture and calls
            # it success.
            self.send_response(200)
            self.send_header("Content-Length", "9999")
            self.end_headers()
            self.wfile.write(b"PAR1-truncated")
        elif self.path == "/notchunked":
            body = b"x" * 64
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


@pytest.fixture(scope="module")
def server() -> HTTPServer:
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries are real; the backoff between them is not what is under test."""
    monkeypatch.setattr(_DOWNLOAD.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(_DOWNLOAD, "DOWNLOAD_ATTEMPTS", 2)


def _url(server: HTTPServer, path: str) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


@pytest.mark.unit
def test_a_complete_body_is_written(server: HTTPServer, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "file.parquet"
    _DOWNLOAD._download(_url(server, "/good"), target)

    assert target.read_bytes() == b"PAR1-parquet-bytes"
    assert not list(tmp_path.rglob("*.partial")), "no partial file may be left behind"


@pytest.mark.unit
def test_a_truncated_body_is_a_failure_not_a_fixture(server: HTTPServer, tmp_path: Path) -> None:
    """The regression: a short body used to be written as if it were complete."""
    target = tmp_path / "file.parquet"
    with pytest.raises(_DOWNLOAD.FixtureUnavailable, match="truncated"):
        _DOWNLOAD._download(_url(server, "/short"), target)

    assert not target.exists(), "a truncated fixture must never reach the readers"
    assert not list(tmp_path.rglob("*.partial"))


@pytest.mark.unit
def test_a_body_without_content_length_is_accepted(server: HTTPServer, tmp_path: Path) -> None:
    """Chunked responses carry no length; refusing them would refuse real CDNs."""
    target = tmp_path / "file.bin"
    _DOWNLOAD._download(_url(server, "/notchunked"), target)
    assert target.read_bytes() == b"x" * 64


@pytest.mark.unit
def test_a_missing_file_reports_unavailable(server: HTTPServer, tmp_path: Path) -> None:
    target = tmp_path / "file.parquet"
    with pytest.raises(_DOWNLOAD.FixtureUnavailable):
        _DOWNLOAD._download(_url(server, "/missing"), target)
    assert not target.exists()


@pytest.mark.unit
def test_an_existing_fixture_is_never_refetched(tmp_path: Path) -> None:
    """The cache check that keeps a second run offline.

    Asserted here because it is the reason a developer's suite works with no
    network at all, and it is the one line between the fixture layer and every
    reader test.
    """
    assert Path("var") / "real-data" == _DOWNLOAD.REAL_DATA_ROOT
    cached = tmp_path / "meta" / "info.json"
    cached.parent.mkdir(parents=True)
    cached.write_text("{}", encoding="utf-8")
    assert cached.exists() and cached.stat().st_size > 0, "the guard a fetch skips on"
