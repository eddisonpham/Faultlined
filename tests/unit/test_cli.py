import contextlib
import logging
from collections.abc import Iterator
from typing import Any

import pytest

from data_engine.cli import _monitor_tick, _parent_alive, _worker_loop, main


@pytest.mark.unit
def test_cli_rejects_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["not-a-command"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.unit
def test_cli_requires_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
    assert "required" in capsys.readouterr().err


class _FakeProcess:
    def __init__(self, alive: bool) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


@pytest.mark.unit
def test_a_standalone_worker_has_no_parent_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`parent_process() is None` means "not a multiprocessing child", not "orphaned".

    The guard used to read `None` as dead, so every standalone `de worker` exited on
    its first loop iteration (EXP-0011).
    """
    monkeypatch.setattr("data_engine.cli.multiprocessing.parent_process", lambda: None)
    assert _parent_alive() is True


@pytest.mark.unit
def test_a_live_parent_keeps_the_worker_running(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "data_engine.cli.multiprocessing.parent_process", lambda: _FakeProcess(alive=True)
    )
    assert _parent_alive() is True


@pytest.mark.unit
def test_a_dead_parent_ends_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "data_engine.cli.multiprocessing.parent_process", lambda: _FakeProcess(alive=False)
    )
    assert _parent_alive() is False


class _Stop:
    """Stops the loop after a fixed number of iterations."""

    def __init__(self, iterations: int) -> None:
        self._remaining = iterations

    def is_set(self) -> bool:
        self._remaining -= 1
        return self._remaining <= 0

    def wait(self, _seconds: float) -> None:
        return None


class _Catalog:
    def __init__(self, *, lease: bool = True) -> None:
        self.reaped_deadlines = 0
        self.reaped_orphans = 0
        self.lease_available = lease
        self.monitor_leases = 0

    def reap_expired_deadlines(self) -> int:
        self.reaped_deadlines += 1
        return 0

    def reap_orphaned_jobs(self) -> int:
        self.reaped_orphans += 1
        return 0

    @contextlib.contextmanager
    def monitor_lease(self) -> Iterator[bool]:
        self.monitor_leases += 1
        yield self.lease_available


class _Settings:
    metrics_path: Any = None


def _loop_with(
    monkeypatch: pytest.MonkeyPatch,
    worker: object,
    iterations: int,
    metrics: _Metrics | None = None,
) -> _Metrics:
    recorder = metrics if metrics is not None else _Metrics()
    monkeypatch.setattr("data_engine.cli.load_settings", lambda: _Settings())
    monkeypatch.setattr("data_engine.cli.build_metrics", lambda _settings: recorder)
    monkeypatch.setattr("data_engine.cli.IngestWorker", lambda *_args, **_kwargs: worker)
    monkeypatch.setattr("data_engine.cli._parent_alive", lambda: True)
    _worker_loop(_Stop(iterations), poll_seconds=0.0)
    return recorder


class _Metrics:
    """Counts host samples, and can be told to fail one.

    The monitor emits its own metric family through the same recorder; those calls
    are not what these tests are about, so every other method is a no-op.
    """

    def __init__(self, *, broken: bool = False) -> None:
        self.host_samples = 0
        self.broken = broken

    def sample_host(self) -> None:
        self.host_samples += 1
        if self.broken:
            raise RuntimeError("psutil exploded")

    def __getattr__(self, _name: str) -> Any:
        def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _noop


class _Outage:
    """Fails the first `failures` claims, the way a dropped database would."""

    def __init__(self, failures: int) -> None:
        self.catalog = _Catalog()
        self.remaining = failures
        self.calls = 0

    def process_one(self) -> None:
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise RuntimeError("connection refused")
        return


@pytest.mark.unit
def test_worker_loop_survives_a_database_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blip must not end the worker's life.

    `claim_job` runs before the per-job try/except in `process_one`, so a lost
    connection used to propagate out of this loop and take the process down, leaving
    the queue unprocessed until someone restarted it.
    """
    worker = _Outage(failures=2)

    _loop_with(monkeypatch, worker, iterations=4)

    # Kept going past both failures instead of dying on the first.
    assert worker.calls == 3


@pytest.mark.unit
def test_worker_loop_actually_runs_the_reapers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reapers existed but no caller ever invoked them, so F6 was never enforced."""
    worker = _Outage(failures=0)

    _loop_with(monkeypatch, worker, iterations=2)

    assert worker.catalog.reaped_deadlines >= 1
    assert worker.catalog.reaped_orphans >= 1


@pytest.mark.unit
def test_a_failing_reaper_does_not_end_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reaping is opportunistic; a bad sweep must not stop the worker."""
    worker = _Outage(failures=0)

    def _boom() -> int:
        raise RuntimeError("connection refused")

    worker.catalog.reap_expired_deadlines = _boom  # type: ignore[method-assign]

    _loop_with(monkeypatch, worker, iterations=2)

    assert worker.calls == 1


@pytest.mark.unit
def test_worker_loop_ticks_the_monitor(monkeypatch: pytest.MonkeyPatch) -> None:
    """EXP-0016: `MonitorService.tick` had no production caller, so incidents never
    existed. The worker loop is the scheduler now, under a catalog-held lease."""
    worker = _Outage(failures=0)

    _loop_with(monkeypatch, worker, iterations=2)

    assert worker.catalog.monitor_leases >= 1


@pytest.mark.unit
def test_the_monitor_tick_yields_to_a_lease_held_elsewhere() -> None:
    """With N workers, one tick per interval - not one per worker (ADR 0031)."""
    catalog = _Catalog(lease=False)

    class _Spy:
        ticks = 0

        def tick(self, _catalog: Any) -> Any:
            type(self).ticks += 1
            raise AssertionError("tick must not run without the lease")

    _monitor_tick(_Spy(), catalog, logging.getLogger("test"))
    assert catalog.monitor_leases == 1
    assert _Spy.ticks == 0


@pytest.mark.unit
def test_a_failing_monitor_tick_does_not_end_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0020: a tick that raises is a lost observation, never a lost job."""

    class _Broken:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def tick(self, _catalog: Any) -> Any:
            raise RuntimeError("monitor exploded")

    worker = _Outage(failures=0)
    monkeypatch.setattr("data_engine.cli.MonitorService", _Broken)

    _loop_with(monkeypatch, worker, iterations=2)

    assert worker.calls == 1


@pytest.mark.unit
def test_worker_loop_records_host_gauges_on_its_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `system_*` gauges were documented and never emitted; the loop emits them."""
    metrics = _loop_with(monkeypatch, _Outage(failures=0), iterations=2)

    assert isinstance(metrics, _Metrics)
    assert metrics.host_samples == 1


@pytest.mark.unit
def test_a_failing_host_sample_does_not_end_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0008 applies to the sampler too: telemetry cannot stop the worker."""
    worker = _Outage(failures=0)
    metrics = _Metrics(broken=True)

    # Three loop iterations so the assertion can see work done *after* the sample
    # blew up: `_Stop` ends the loop on the third `is_set` check.
    _loop_with(monkeypatch, worker, iterations=3, metrics=metrics)

    assert metrics.host_samples == 1
    assert worker.calls == 2
