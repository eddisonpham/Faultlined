import sys
from types import SimpleNamespace

import pytest

from data_engine.observability import telemetry


@pytest.mark.unit
def test_sample_resources_includes_host_values_and_degrades_without_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        telemetry.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(used=20, available=80, total=100),
    )
    monkeypatch.setattr(
        telemetry.psutil, "disk_usage", lambda _path: SimpleNamespace(used=30, free=70)
    )
    monkeypatch.setattr(
        telemetry.psutil, "net_io_counters", lambda: SimpleNamespace(bytes_sent=40, bytes_recv=50)
    )
    monkeypatch.setattr(
        telemetry.psutil,
        "Process",
        lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=10)),
    )
    monkeypatch.setattr(telemetry.psutil, "cpu_percent", lambda **_kwargs: 12.5)
    monkeypatch.setitem(sys.modules, "pynvml", None)

    sample = telemetry.sample_resources()

    assert sample.process_rss_bytes == 10
    assert sample.memory_used_bytes == 20
    assert sample.disk_free_bytes == 70
    assert sample.network_bytes_recv_total == 50
    assert sample.gpu_present is False
    assert sample.gpu_memory_total_bytes is None
    assert sample.to_dict()["cpu_percent"] == 12.5


@pytest.mark.unit
def test_sample_resources_marks_unavailable_host_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        telemetry.psutil,
        "virtual_memory",
        lambda: (_ for _ in ()).throw(telemetry.psutil.AccessDenied()),
    )
    monkeypatch.setattr(
        telemetry.psutil, "disk_usage", lambda _path: SimpleNamespace(used=30, free=70)
    )
    monkeypatch.setattr(
        telemetry.psutil, "net_io_counters", lambda: SimpleNamespace(bytes_sent=40, bytes_recv=50)
    )
    monkeypatch.setattr(
        telemetry.psutil,
        "Process",
        lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=10)),
    )
    monkeypatch.setattr(telemetry.psutil, "cpu_percent", lambda **_kwargs: 0.0)

    sample = telemetry.sample_resources(include_gpu=False)

    assert sample.memory_used_bytes is None
    assert sample.memory_available_bytes is None
    assert sample.disk_free_bytes == 70
    assert sample.network_bytes_recv_total == 50
    assert sample.process_rss_bytes == 10


@pytest.mark.unit
def test_sample_resources_can_skip_gpu_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        telemetry.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(used=20, available=80, total=100),
    )
    monkeypatch.setattr(
        telemetry.psutil, "disk_usage", lambda _path: SimpleNamespace(used=30, free=70)
    )
    monkeypatch.setattr(
        telemetry.psutil, "net_io_counters", lambda: SimpleNamespace(bytes_sent=40, bytes_recv=50)
    )
    monkeypatch.setattr(
        telemetry.psutil,
        "Process",
        lambda: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=10)),
    )
    monkeypatch.setattr(telemetry.psutil, "cpu_percent", lambda **_kwargs: 0.0)
    sample = telemetry.sample_resources(include_gpu=False)
    assert sample.gpu_present is False
