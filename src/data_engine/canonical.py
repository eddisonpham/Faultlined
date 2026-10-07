"""Canonical JSON: the one serialization the platform hashes, compares, and dedupes."""

from __future__ import annotations

import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Stable UTF-8 JSON bytes; rejects non-finite numbers by default."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
