"""Canonical JSON: the one serialization the platform hashes, compares, and dedupes.

It lives at the package root rather than in `catalog/` because idempotency requests,
build manifests, and validation profiles all hash it, and `catalog.repository` importing
`validation.profile` would otherwise close a cycle.
"""

from __future__ import annotations

import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    """Stable UTF-8 JSON bytes; rejects non-finite numbers by default."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
