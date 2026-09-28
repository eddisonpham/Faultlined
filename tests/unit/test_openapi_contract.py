"""Contract drift check (ADR 0009).

The OpenAPI document is committed at docs/api/openapi.json so an API change cannot land
without the contract being refreshed. This fails the suite when the two diverge.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "docs" / "api" / "openapi.json"
SCRIPT = ROOT / "scripts" / "openapi_contract.py"


@pytest.mark.contract
def test_openapi_contract_is_committed() -> None:
    assert CONTRACT.exists(), (
        "docs/api/openapi.json is missing; generate it with "
        "`python scripts/openapi_contract.py --write`"
    )


@pytest.mark.contract
def test_openapi_contract_does_not_drift() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, (
        "OpenAPI drifted from the committed contract.\n"
        f"{result.stdout}{result.stderr}\n"
        "If intended, refresh it with `python scripts/openapi_contract.py --write`."
    )


@pytest.mark.contract
def test_openapi_contract_covers_the_versioned_api() -> None:
    schema = json.loads(CONTRACT.read_text(encoding="utf-8"))
    paths = set(schema["paths"])
    for required in (
        "/api/v1/health",
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}",
        "/api/v1/episodes/{episode_id}",
    ):
        assert required in paths, f"{required} missing from the contract"
    assert schema["info"]["title"] == "Faultlined"
