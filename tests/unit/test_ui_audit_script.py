"""The browser UI audit (`scripts/ui_audit.mjs`) and its contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from data_engine.web import THEMES
from data_engine.web.pages import NAV_LINKS

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ui_audit.mjs"
TEXT = SCRIPT.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_audit_covers_every_theme_not_just_the_default() -> None:
    """A theme that is never checked is a theme nobody sees."""
    for theme in THEMES:
        assert f'"{theme}"' in TEXT, f"{theme} is offered but never audited"
    assert "localStorage.setItem" in TEXT, "the stored theme is how a swap is triggered"
    assert "?theme=" in TEXT, "each pass must force a swap"
    assert "const seed =" in TEXT


@pytest.mark.unit
def test_the_audit_checks_the_cascade_not_just_the_computed_style() -> None:
    """The duplicate stylesheet is the cause; the glow is the symptom."""
    assert "theme-sheet" in TEXT
    assert "cascade-order" in TEXT
    assert "themeSheets.length !== 1" in TEXT


@pytest.mark.unit
def test_the_audit_checks_every_page_the_nav_advertises() -> None:
    """A page added to the nav must be checked, or the audit rots silently."""
    for href, _label, _key, *_group in NAV_LINKS:
        assert f'"{href}"' in TEXT, f"{href} is in the nav but not in the audit"


@pytest.mark.unit
def test_the_audit_measures_the_rendered_page_rather_than_the_source() -> None:
    """The whole reason this tool exists."""
    assert "getComputedStyle" in TEXT
    assert "getBoundingClientRect" in TEXT
    assert "Runtime.evaluate" in TEXT, "it must read the page, not fetch its markup"


@pytest.mark.unit
def test_the_audit_checks_the_state_that_only_exists_during_a_navigation() -> None:
    """The gap that let a full-page blur ship."""
    assert 'classList.add("de-leaving")' in TEXT
    assert "leaving-filter" in TEXT
    assert "leaving-backdrop" in TEXT


@pytest.mark.unit
def test_the_audit_needs_no_installation() -> None:
    """A dev tool requiring `npm install` is a dev tool nobody runs."""
    assert "import " in TEXT
    for line in TEXT.splitlines():
        if not line.startswith("import "):
            continue
        assert 'from "node:' in line, f"a dependency crept in: {line}"
    assert "require(" not in TEXT
    assert "new WebSocket(" in TEXT


@pytest.mark.unit
def test_the_audit_exits_in_ways_a_caller_can_act_on() -> None:
    """0 clean, 1 a check failed, 2 the tool could not run at all."""
    assert "process.exit(failures === 0 ? 0 : 1)" in TEXT
    assert "process.exit(2)" in TEXT
    assert "just run" in TEXT
    assert "CHROME_PATH" in TEXT, "a missing browser needs a way out"


@pytest.mark.unit
def test_the_animation_allowlist_is_explicit() -> None:
    """Anything running unprompted is decoration unless it is named."""
    line = TEXT.split("ALLOWED_ANIMATIONS = new Set([")[1].split("]")[0]
    allowed = {name.strip().strip('"') for name in line.split(",")}
    assert {"de-blink", "de-breathe", "de-arrive"} <= allowed
    assert len(allowed) <= 4, "the allowlist has started absorbing new motion"
