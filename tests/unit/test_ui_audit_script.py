"""The browser UI audit (`scripts/ui_audit.mjs`) and its contract.

The audit is the only thing in this repository that can see a computed style or
a pixel of geometry, so its own failure modes matter more than usual. These
tests pin the properties that make it trustworthy: it checks every page the nav
advertises, it measures rather than reads the source, it has no dependencies to
install, and it exits in a way a caller can act on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_engine.web.pages import NAV_LINKS

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ui_audit.mjs"
TEXT = SCRIPT.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_audit_checks_every_page_the_nav_advertises() -> None:
    """A page added to the nav must be checked, or the audit rots silently.

    The list of pages is a literal in the script, which is exactly the kind of
    duplication that drifts. This asserts the two lists agree, so adding a page
    to the nav fails here rather than quietly going unmeasured.
    """
    for href, _label, _key in NAV_LINKS:
        assert f'"{href}"' in TEXT, f"{href} is in the nav but not in the audit"


@pytest.mark.unit
def test_the_audit_measures_the_rendered_page_rather_than_the_source() -> None:
    """The whole reason this tool exists.

    Every visual defect fixed in this project was invisible in the source, so an
    audit that asserted on CSS text would be asserting the thing that has
    already lied. It has to read computed styles out of a live document.
    """
    assert "getComputedStyle" in TEXT
    assert "getBoundingClientRect" in TEXT
    assert "Runtime.evaluate" in TEXT, "it must read the page, not fetch its markup"


@pytest.mark.unit
def test_the_audit_checks_the_state_that_only_exists_during_a_navigation() -> None:
    """The gap that let a full-page blur ship.

    The reported defect lived between a link click and the next document, so a
    tool that only loads pages and measures them sees nothing. The script has to
    apply the leaving class itself and measure the page in that state.
    """
    assert 'classList.add("de-leaving")' in TEXT
    assert "leaving-filter" in TEXT
    assert "leaving-backdrop" in TEXT


@pytest.mark.unit
def test_the_audit_needs_no_installation() -> None:
    """A dev tool requiring `npm install` is a dev tool nobody runs.

    The UI has no build step and no npm runtime (ADR 0014). This reaches for the
    standard library only: Node's own WebSocket against Chrome's own DevTools
    protocol. An import of anything third-party would make the tool optional in
    exactly the way that loses defects.
    """
    assert "import " in TEXT
    for line in TEXT.splitlines():
        if not line.startswith("import "):
            continue
        assert 'from "node:' in line, f"a dependency crept in: {line}"
    assert "require(" not in TEXT
    # The standard-library WebSocket is the whole trick; Node 21+ ships it.
    assert "new WebSocket(" in TEXT


@pytest.mark.unit
def test_the_audit_exits_in_ways_a_caller_can_act_on() -> None:
    """0 clean, 1 a check failed, 2 the tool could not run at all.

    The distinction that matters is 1 versus 2: a failing check is a defect in
    the UI, an unreachable server or a missing browser is a defect in the
    invocation, and conflating them sends the reader to the wrong place.
    """
    assert "process.exit(failures === 0 ? 0 : 1)" in TEXT
    assert "process.exit(2)" in TEXT
    # And it says what to do rather than only what went wrong.
    assert "just run" in TEXT
    assert "CHROME_PATH" in TEXT, "a missing browser needs a way out"


@pytest.mark.unit
def test_the_animation_allowlist_is_explicit() -> None:
    """Anything running unprompted is decoration unless it is named.

    A threshold ("more than three animations is too many") would pass a page
    with three glows. An allowlist fails a page with one new animation, which is
    the behaviour a reviewer wants.
    """
    line = TEXT.split("ALLOWED_ANIMATIONS = new Set([")[1].split("]")[0]
    allowed = {name.strip().strip('"') for name in line.split(",")}
    assert {"de-blink", "de-breathe", "de-arrive"} <= allowed
    # The three are the LED, the clock cursor and the one-shot panel entrance.
    assert len(allowed) <= 4, "the allowlist has started absorbing new motion"
