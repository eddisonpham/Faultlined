"""Cross-platform resource telemetry with graceful optional GPU support."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import psutil


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    timestamp: str
    cpu_percent: float | None
    process_rss_bytes: int | None
    memory_used_bytes: int | None
    memory_available_bytes: int | None
    disk_used_bytes: int | None
    disk_free_bytes: int | None
    network_bytes_sent_total: int | None
    network_bytes_recv_total: int | None
    gpu_present: bool
    gpu_utilization_percent: float | None = None
    gpu_memory_used_bytes: int | None = None
    gpu_memory_total_bytes: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sample_resources(*, include_gpu: bool = True) -> TelemetrySample:
    """Take one host snapshot."""
    try:
        memory = psutil.virtual_memory()
        memory_used: int | None = int(memory.used)
        memory_available: int | None = int(memory.available)
    except psutil.Error, OSError:
        memory_used = None
        memory_available = None
    try:
        disk = psutil.disk_usage(".")
        disk_used: int | None = int(disk.used)
        disk_free: int | None = int(disk.free)
    except psutil.Error, OSError:
        disk_used = None
        disk_free = None
    try:
        network = psutil.net_io_counters()
        network_sent: int | None = int(network.bytes_sent) if network else None
        network_received: int | None = int(network.bytes_recv) if network else None
    except psutil.Error, OSError:
        network_sent = None
        network_received = None
    try:
        process_rss: int | None = int(psutil.Process().memory_info().rss)
    except psutil.Error, OSError:
        process_rss = None
    try:
        cpu_percent: float | None = float(psutil.cpu_percent(interval=None))
    except psutil.Error, OSError:
        cpu_percent = None

    gpu_present = False
    gpu_utilization: float | None = None
    gpu_used: int | None = None
    gpu_total: int | None = None

    if include_gpu:
        try:
            import pynvml

            pynvml.nvmlInit()
            try:
                device = pynvml.nvmlDeviceGetHandleByIndex(0)
                utilization = pynvml.nvmlDeviceGetUtilizationRates(device)
                gpu_memory = pynvml.nvmlDeviceGetMemoryInfo(device)
                gpu_present = True
                gpu_utilization = float(utilization.gpu)
                gpu_used = int(gpu_memory.used)
                gpu_total = int(gpu_memory.total)
            finally:
                pynvml.nvmlShutdown()
        except Exception:
            gpu_present = False

    return TelemetrySample(
        timestamp=datetime.now(UTC).isoformat(),
        cpu_percent=cpu_percent,
        process_rss_bytes=process_rss,
        memory_used_bytes=memory_used,
        memory_available_bytes=memory_available,
        disk_used_bytes=disk_used,
        disk_free_bytes=disk_free,
        network_bytes_sent_total=network_sent,
        network_bytes_recv_total=network_received,
        gpu_present=gpu_present,
        gpu_utilization_percent=gpu_utilization,
        gpu_memory_used_bytes=gpu_used,
        gpu_memory_total_bytes=gpu_total,
    )
