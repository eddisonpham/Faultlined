import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_repo_hygiene.py"
_SPEC = importlib.util.spec_from_file_location("check_repo_hygiene", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_HYGIENE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HYGIENE)


@pytest.mark.unit
def test_link_checker_reports_broken_relative_link(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text("[missing](not-here.md)\n", encoding="utf-8")
    errors = _HYGIENE.check_links(tmp_path)
    assert len(errors) == 1
    assert "broken link" in errors[0]


@pytest.mark.unit
def test_link_checker_ignores_external_and_code_block_links(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "[external](https://example.com)\n\n```md\n[hidden](not-here.md)\n```\n",
        encoding="utf-8",
    )
    assert _HYGIENE.check_links(tmp_path) == []


@pytest.mark.unit
def test_adr_checker_rejects_invalid_names_and_duplicate_numbers(tmp_path: Path) -> None:
    adr_dir = tmp_path / "agents" / "decisions"
    adr_dir.mkdir(parents=True)
    (adr_dir / "bad-name.md").write_text("x", encoding="utf-8")
    (adr_dir / "0001-one.md").write_text("x", encoding="utf-8")
    (adr_dir / "0001-two.md").write_text("x", encoding="utf-8")
    errors = _HYGIENE.check_adrs(tmp_path)
    assert any("filename" in error for error in errors)
    assert any("duplicate ADR number" in error for error in errors)


@pytest.mark.unit
def test_secret_checker_detects_private_key_marker(tmp_path: Path) -> None:
    marker = "-----BEGIN " + "PRIVATE KEY-----"
    (tmp_path / "example.txt").write_text(marker, encoding="utf-8")
    errors = _HYGIENE.check_secrets(tmp_path)
    assert len(errors) == 1
    assert "Private key block" in errors[0]
