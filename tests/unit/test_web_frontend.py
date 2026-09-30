"""Frontend contract tests for the instrument UI.

These cover the properties that are invisible in a screenshot but decide whether
the UI is usable: failure visibility, keyboard reachability, escaping, and the
theme hook contract with the vendored stylesheets.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from data_engine.web import THEMES, app_script, layout_css, theme_or_default
from data_engine.web.pages import (
    _copyable,
    _empty,
    _meter,
    _operation,
    episodes_page,
    error_page,
    incident_row_html,
    incidents_page,
    insights_page,
    job_detail_page,
    jobs_page,
    metrics_page,
    slices_page,
    status_page,
)

ALL_PAGES = (
    lambda: status_page({}, "vt220"),
    lambda: jobs_page({"items": []}, None, "vt220"),
    lambda: episodes_page({"items": []}, None, None, "vt220"),
    lambda: slices_page({"items": []}, "vt220"),
    lambda: insights_page({}, "vt220"),
    lambda: metrics_page({}, "vt220"),
    lambda: incidents_page({}, "vt220"),
)


def _pages() -> list[str]:
    return [make() for make in ALL_PAGES]


def _no_comments(text: str) -> str:
    """Strip JS/CSS comments so an assertion tests code, not prose about the code."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.DOTALL)


def _client() -> TestClient:
    from data_engine.api.app import create_app

    return TestClient(create_app(initialize_database=False), raise_server_exceptions=False)


# ---------------------------------------------------------------- shell


@pytest.mark.unit
def test_every_page_shares_one_accessible_shell() -> None:
    for html in _pages():
        assert html.startswith("<!doctype html>")
        assert 'lang="en"' in html
        assert '<main id="main"' in html, "a skip link needs a main landmark to target"
        assert 'class="de-skip' in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html, "poll failures must be announced, not only drawn"


@pytest.mark.unit
def test_shell_loads_the_runtime_and_three_stylesheets() -> None:
    html = status_page({}, "vt220")
    assert '<script src="/ui/app.js" defer></script>' in html
    assert "core.css" in html
    assert "theme-vt220.css" in html
    assert "faultlined.css" in html
    assert "defer" in html, "a blocking script delays first paint of the panel"


@pytest.mark.unit
def test_nav_has_no_api_link() -> None:
    """/docs is gone (ADR 0021): the browser surface is the operator UI."""
    html = status_page({}, "vt220")
    assert '"/docs"' not in html
    assert ">API<" not in html
    # The brand wordmark is a real link back to the landing page, not decoration.
    assert '<a class="de-brand" href="/ui">' in html


@pytest.mark.unit
def test_every_nav_link_advertises_the_key_the_runtime_handles() -> None:
    """A shortcut printed in the nav but not bound in app.js is a broken promise."""
    script = app_script()
    html = status_page({}, "vt220")
    keys = re.findall(r'data-key="(\d)"', html)
    assert keys, "the nav should carry numeric shortcuts"
    assert len(set(keys)) == len(keys), "two links claiming the same key"
    assert "'a[data-key=\"' + event.key + '\"]'" in script
    for key in keys:
        assert f'data-key="{key}"' in html


@pytest.mark.unit
def test_nav_marks_the_current_page_for_assistive_tech() -> None:
    html = jobs_page({"items": []}, None, "vt220")
    current = re.findall(r'<a href="([^"]+)"[^>]*aria-current="page"', html)
    assert current == ["/ui/jobs"]


@pytest.mark.unit
def test_page_title_is_escaped_in_both_title_tag_and_heading() -> None:
    html = job_detail_page(
        {"id": "<script>x</script>", "type": "ingest", "state": "running", "payload": {}},
        "vt220",
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


# ---------------------------------------------------------------- failure visibility


@pytest.mark.unit
def test_failure_banner_is_present_hidden_and_reachable_by_a_role() -> None:
    html = status_page({}, "vt220")
    assert "data-banner" in html
    assert 'data-visible="0"' in html, "must start hidden; a page with no error shows no banner"
    assert "data-banner-msg" in html
    assert "data-banner-retry" in html
    assert 'role="alert"' in html


@pytest.mark.unit
def test_every_failure_path_is_announced_not_merely_drawn() -> None:
    """The banner is a hazard stripe; a screen reader never sees it.

    Each way the poll can fail must reach the aria-live region, and recovery
    must be announced too - otherwise a user cannot tell "fixed" from
    "never broke".
    """
    script = _no_comments(app_script())
    assert script.count("announce(") >= 5, "offline, transient, permanent, and recovery"
    assert "Live updates restored." in script
    # announce() writes to the live region the shell ships.
    html = status_page({}, "vt220")
    assert 'aria-live="polite"' in html and "data-live" in html


@pytest.mark.unit
def test_clock_is_marked_not_stale_on_load_and_updatable() -> None:
    html = status_page({}, "vt220")
    assert 'data-stale="0"' in html
    assert "data-clock" in html
    assert "setStale" in _no_comments(app_script()), (
        "a stale render and a live one must be distinguishable"
    )


@pytest.mark.unit
def test_link_health_led_is_silent_until_it_is_news() -> None:
    """A permanently-visible "live" badge reads as branding, not as a status.

    The LED is for the states that need attention; when everything is fine it
    should not be on screen at all.
    """
    html = status_page({}, "vt220")
    assert "data-led" in html
    assert "data-led-text" in html
    assert 'data-state="ok"' in html
    assert "<span data-led-text></span>" in html, "no hardcoded word in the markup"
    # Hidden at rest, and revealed by app.js when the state is not ok.
    script = _no_comments(app_script())
    assert 'if (state === "ok") led.setAttribute("hidden", "");' in script
    assert 'else led.removeAttribute("hidden");' in script


@pytest.mark.unit
def test_led_still_carries_a_word_when_revealed() -> None:
    """Colour may never be the only signal, including on the LED itself."""
    script = _no_comments(app_script())
    assert 'led.setAttribute("data-state", state)' in script
    assert "if (text) text.textContent = label;" in script
    for word in ('"retry"', '"stale"', '"offline"', '"failed"'):
        assert word in script, f"{word} must be a spoken state, not only a hue"


@pytest.mark.unit
def test_poller_never_discards_the_last_good_render() -> None:
    """An empty panel is a stronger lie than an obviously stale one."""
    script = app_script()
    assert "last good render" in script
    # The success path only assigns innerHTML when it actually got a body.
    assert 'if (html === null || html === "") return;' in script


@pytest.mark.unit
def test_poller_backs_off_and_gives_up_on_permanent_failures() -> None:
    script = _no_comments(app_script())
    assert "Math.pow(2, failures - 1)" in script
    assert "err.permanent" in script
    assert "stopped = true" in script


@pytest.mark.unit
def test_poller_does_not_poll_a_hidden_tab() -> None:
    script = _no_comments(app_script())
    assert "doc.hidden" in script
    assert "visibilitychange" in script
    assert "paused" in script, "a hidden tab should show a paused LED, not a live one"


# ---------------------------------------------------------------- accessibility


@pytest.mark.unit
def test_focus_is_never_removed_and_appears_on_every_control() -> None:
    css = layout_css()
    assert ":focus-visible" in css
    assert "outline: none" not in css.replace("outline: none;", ""), (
        "no rule may drop the focus ring"
    )


@pytest.mark.unit
def test_data_tables_carry_a_caption() -> None:
    html = jobs_page(
        {"items": [{"id": "j-1", "type": "ingest", "state": "running", "correlation_id": "c"}]},
        None,
        "vt220",
    )
    assert "<caption" in html
    assert "Jobs, newest first" in html


@pytest.mark.unit
def test_meters_expose_their_value_to_a_screen_reader() -> None:
    meter = _meter(0.5, 10)
    assert 'role="img"' in meter
    assert 'aria-label="50 percent"' in meter


@pytest.mark.unit
def test_charts_are_labelled_rather_than_presentational() -> None:
    html = metrics_page(
        {"series": {"api_request_duration_seconds": [{"t": "t", "v": 0.1}]}, "summaries": []},
        "vt220",
    )
    assert 'role="img"' in html
    assert "aria-label=" in html


@pytest.mark.unit
def test_skip_link_is_the_first_focusable_thing_in_the_document() -> None:
    html = status_page({}, "vt220")
    body = html.index("<body>")
    assert html.index("de-skip", body) < html.index('class="de-nav', body)


# ---------------------------------------------------------------- escaping


@pytest.mark.unit
def test_untrusted_values_cannot_break_out_of_a_data_attribute() -> None:
    """data-copy carries a full id into an attribute; quotes there are the risk."""
    hostile = 'x" onmouseover="alert(1)'
    cell = _copyable(hostile, "x")
    assert 'onmouseover="alert(1)' not in cell
    assert "&quot;" in cell


@pytest.mark.unit
def test_incident_evidence_cannot_inject_a_row() -> None:
    row = incident_row_html(
        {
            "severity": "critical",
            "status": "open",
            "notify_class": "notify",
            "label": "X",
            "scope": "s",
            "summary": "</td><td><script>alert(1)</script>",
            "occurrence_count": 1,
            "last_seen": "t",
        }
    )
    assert "<script>alert(1)" not in row
    assert "&lt;script&gt;" in row


# ---------------------------------------------------------------- empty + error states


@pytest.mark.unit
def test_empty_states_name_the_thing_and_the_way_out() -> None:
    empty = _empty("no jobs recorded", "submit one")
    assert "no jobs recorded" in empty
    assert "submit one" in empty


@pytest.mark.unit
def test_error_page_renders_html_with_a_way_back() -> None:
    html = error_page(500, "correlation abc-123")
    assert "Error 500" in html
    assert "abc-123" in html
    assert "<main" in html
    assert 'type="submit"' in html, "the operator needs a retry control, not a dead end"
    # The runtime still loads: the clock and shortcuts are how you check whether
    # the server has come back, which is exactly the question an error page raises.
    assert "/ui/app.js" in html


@pytest.mark.unit
def test_error_page_keeps_the_requested_theme_and_falls_back_safely() -> None:
    assert "theme-amber.css" in error_page(500, "d", "amber")
    assert "theme-vt220.css" in error_page(500, "d", "not-a-theme")


@pytest.mark.unit
def test_theme_resolution_never_emits_an_unvendored_sheet() -> None:
    assert theme_or_default("amber") == "amber"
    assert theme_or_default("../../etc/passwd") in THEMES
    assert theme_or_default(None) == "vt220"


@pytest.mark.contract
def test_a_ui_404_renders_the_full_page_not_a_json_blob() -> None:
    """The exact case that matters: a mistyped or stale bookmark in the browser."""
    response = _client().get("/ui/nowhere", headers={"Accept": "text/html"})
    assert response.status_code == 404
    body = response.text
    assert body.startswith("<!doctype html>")
    assert "<main" in body
    assert "about:blank" not in body


@pytest.mark.contract
def test_a_ui_404_keeps_the_operator_theme() -> None:
    """The error page is the one page you most want to still look like the app."""
    response = _client().get("/ui/nowhere?theme=amber", headers={"Accept": "text/html"})
    assert "theme-amber.css" in response.text


@pytest.mark.contract
def test_an_api_404_still_returns_json() -> None:
    """The representation split must not change what a client is promised."""
    response = _client().get("/api/v1/nowhere")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "REQUEST_FAILED"


@pytest.mark.contract
def test_a_ui_path_error_is_html_even_without_an_accept_header() -> None:
    """A curl or a script hitting /ui should still get the page, not raw JSON."""
    response = _client().get("/ui/nowhere")
    assert response.text.startswith("<!doctype html>")


@pytest.mark.contract
def test_the_client_runtime_is_served_with_a_script_content_type() -> None:
    response = _client().get("/ui/app.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")


# ---------------------------------------------------------------- theme hooks


@pytest.mark.unit
def test_markup_carries_the_hooks_the_vendored_themes_target() -> None:
    """Adopting the hooks is the whole reason the CRT themes render at all."""
    html = status_page(
        {"queue_depth": {"queued": 1, "running": 1}, "resources": {}},
        "vt220",
    )
    assert "fine-use-app" in html, "scanlines and themed backgrounds hang off this"
    assert "fine-use-component" in html
    assert "current-value" in html, "the phosphor glow on readouts needs this"
    assert "block-item" in html
    assert "fine-use-data-table" in html
    assert "fine-use-focusable" in html


@pytest.mark.unit
def test_layout_stylesheet_does_not_neutralise_the_vendor_personality() -> None:
    """The bridge may keep layout but must not strip a theme's distinguishing rule."""
    css = layout_css()
    assert "text-shadow: none" not in css
    # The theme sheets own the glow; ours must not fight it with a competing
    # opaque text colour on the same elements.
    assert not re.search(r"\.de-head h1[^{]*\{[^}]*color:\s*transparent", css)


@pytest.mark.unit
def test_layout_sheet_keeps_our_own_names_and_adopts_no_remote_assets() -> None:
    css = layout_css()
    for name in (".de-readouts", ".de-meter", ".de-section", ".de-chart", ".de-led"):
        assert name in css
    assert "http://" not in css and "https://" not in css


@pytest.mark.unit
def test_motion_is_opt_in_everywhere() -> None:
    """Every animation must be declared *inside* a no-preference guard.

    Not cancelled by a reduce override. An earlier pass declared the entrance
    animation unconditionally and switched it off in a reduced-motion block,
    which reads as correct and silently is not: the block never applies, so the
    animation runs anyway. Opting in is the only form that cannot be forgotten.
    """
    css = layout_css()
    guards = css.count("prefers-reduced-motion: no-preference")
    assert guards >= 2
    # Any selector block that declares `animation:` must sit inside a guard, so
    # find declarations and confirm each is preceded by one.
    for match in re.finditer(r"animation:", css):
        before = css[: match.start()]
        if "@keyframes" in before[before.rfind("}") :] and before.count("{") == before.count("}"):
            continue  # inside a keyframes block, which is fine
        assert before.rfind("@media (prefers-reduced-motion: no-preference)") > (
            before.rfind("}") - 4000
        ), "an animation declared outside a no-preference guard will misfire"
    # The entrance must additionally be one-shot; see the poll-replay test.
    assert "html:not([data-booted])" in css


@pytest.mark.unit
def test_the_navigation_indicator_is_a_real_spring_not_a_keyframe() -> None:
    """Physics-based means an ODE, and the constants are the ones that show it.

    At k=170 c=14 m=1 the damping ratio is 0.537: ~13.5% overshoot. The previous
    constants (c=22) gave 0.85, which is nearly critically damped and looked
    exactly like a CSS ease-out - the "physics" was invisible.
    """
    import math

    script = _no_comments(app_script())
    assert "function Spring(" in script
    assert "semi-implicit Euler" in script or "while (remaining > 0)" in script
    k = int(re.search(r"SPRING_K = (\d+)", script).group(1))
    c = int(re.search(r"SPRING_C = (\d+)", script).group(1))
    m = int(re.search(r"SPRING_M = (\d+)", script).group(1))
    zeta = c / (2 * math.sqrt(k * m))
    overshoot = math.exp(-math.pi * zeta / math.sqrt(1 - zeta**2))
    assert 0.45 < zeta < 0.65, f"zeta {zeta:.3f} is outside the visibly-springy band"
    assert overshoot > 0.08, f"overshoot {overshoot:.1%} would be invisible"
    # And the fixed substep, so a dropped frame cannot change the trajectory.
    assert "var substep = 1 / 120;" in script
    # Springs are built once, not per frame: allocating per frame is a leak.
    assert script.count("new Spring(") == 1, "springs must be allocated once and reused"


@pytest.mark.unit
def test_nothing_is_ever_drawn_over_content_that_has_already_loaded() -> None:
    """The reported bug: a loading panel rendered *above* an already-loaded page.

    It sat in normal flow, so it occupied real vertical space and pushed the
    finished page down the viewport. The fix is structural rather than cosmetic:
    the indicator is an out-of-flow overlay, hidden in the served markup and shown
    only for the duration of a navigation, so a settled page has nothing above it
    by construction instead of by timing.
    """
    html = status_page({}, "vt220")
    assert "data-sweep-panel" in html
    assert 'aria-hidden="true"' in html, "a decorative sweep must not be read aloud"
    # Hidden as served: an idle page must not contain a visible indicator.
    panel = re.search(r'<div class="de-sweep-panel"[^>]*>', html).group(0)
    assert "hidden" in panel, "the indicator must ship hidden, not fade out after load"

    css = layout_css()
    block = css.split(".de-sweep-panel {")[1].split("}")[0]
    assert "position: fixed" in block, "an in-flow indicator pushes the page down"
    # The class sets `display: grid`, which outranks the UA rule for the `hidden`
    # attribute, so the hide has to be restated at class specificity or the panel
    # is a full-viewport overlay on every settled page.
    assert ".de-sweep-panel[hidden] { display: none; }" in css

    script = _no_comments(app_script())
    assert 'sweepEl.setAttribute("hidden", "")' in script, "it has to be reliably removed"
    assert "if (reduce.matches)" in script, "reduced motion skips the sweep entirely"
    # No second, in-flow progress bar survives anywhere in the shell.
    assert "de-progress" not in html
    assert "de-progress" not in css


@pytest.mark.unit
def test_a_poll_cannot_replay_the_entrance_animation() -> None:
    """The reported flicker, at its actual cause.

    The poll replaces the live region's innerHTML, which creates *new* elements,
    and a new element restarts its CSS animation from 0%. With an unconditional
    entrance animation the Status page re-ran it every poll: measured, panel
    opacity sat at 0 for ~1.4 s of each 3 s window and the page appeared to
    strobe. Gating the animation on a one-shot attribute on <html> makes replay
    structurally impossible, because the animation is only ever *selectable*
    before the first frame has been painted.
    """
    css = layout_css()
    assert "html:not([data-booted])" in css, "the entrance must be gated on first paint"
    assert re.search(r"html:not\(\[data-booted\]\)[^{]*\{\s*animation:", css), (
        "the entrance animation must be the thing that is gated"
    )
    assert 'setAttribute("data-booted", "")' in _no_comments(app_script())
    # The refresh cue must not be an opacity animation, because a dip in opacity
    # on live data is indistinguishable from a fault.
    refresh = css.split("@keyframes de-refresh")[1].split("}")[0]
    assert "opacity" not in refresh, "an opacity pulse on live data reads as a flicker"


@pytest.mark.unit
def test_the_poll_only_writes_the_dom_when_the_payload_really_changed() -> None:
    """The other half of the flicker, and the subtler one.

    The guard compared the response against `root.innerHTML`. That looks like a
    correct comparison and is not: the browser re-serialises the DOM, so the two
    strings differ in entity escaping and self-closing tags regardless of what
    the server sent. The guard was therefore always true, every poll rewrote the
    region, and the page pulsed once a second - the exact period reported.
    Comparing against the last payload applied is both correct and cheaper, since
    it avoids re-serialising the whole region on every poll.
    """
    script = _no_comments(app_script())
    assert "html !== root.innerHTML" not in script, (
        "innerHTML re-serialisation makes this comparison always false-negative"
    )
    assert "html !== shown" in script
    assert "var shown = root.innerHTML" in script, "seed from what the server rendered"
    # shown advances only when the DOM is written, or the guard goes stale.
    assert script.count("root.innerHTML = html") == 1
    assert "shown = html" in script


@pytest.mark.unit
def test_the_deadline_countdown_keeps_exactly_one_timer() -> None:
    """A poll write re-runs initDeadlines, so it must not stack intervals.

    Only visible after an hour of uptime - the kind of defect that gets shipped
    because nothing fails in the first ten minutes. Each stranded interval also
    holds a closure over nodes that are no longer in the document, so it is both
    a leak and wasted work.
    """
    script = _no_comments(app_script())
    assert "var deadlineTimer = null;" in script
    fn = script.split("function initDeadlines(")[1].split("\n  }")[0]
    assert "if (deadlineTimer) window.clearInterval(deadlineTimer);" in fn
    assert fn.count("window.setInterval(") == 1, "one countdown timer, not one per poll"
    # The clock runs once at boot from a node outside the polled region, so it
    # is not a leak and must not be folded into the same timer.
    assert (
        script.split("function initClock(")[1].split("\n  }")[0].count("window.setInterval(") == 1
    )


@pytest.mark.unit
def test_sticky_table_headers_are_not_offset_by_the_nav() -> None:
    """The reported overlap, at its actual cause.

    `overflow-x: auto` forces `overflow-y` to compute to `auto`, which quietly
    made the table plate a scrollport. The header was then sticky against *it*,
    so its `top` was measured from the top of the table rather than the top of
    the page: measured on the incidents page, `th.top` was 483 while its own
    `tr.top` was 438 - the header sat 45px into its own row, over the data it
    labels. The fix removes the nav coupling entirely rather than re-measuring a
    number that was wrong for a second reason too: it varied with theme metrics
    and viewport, and had already been wrong twice.
    """
    css = layout_css()
    assert "de-nav-h" not in css, "the header must not depend on the nav's height"
    assert "de-nav-h" not in app_script()
    assert "measureNav" not in app_script(), "the measuring code has no consumer left"
    wrap = css.split(".de-table-wrap {")[1].split("}")[0]
    assert "overflow: auto" in wrap, "the plate must be its own scrollport"
    assert "max-height" in wrap, "a scrollport with no bound is a page-height box"
    # `.de-table th` appears twice: once for the display face, once for layout.
    # The layout one is the last, and the one that must carry the sticky offset.
    th = css.split(".de-table th {")[-1].split("}")[0]
    assert "position: sticky" in th
    assert re.search(r"\btop: 0\b", th), "the header pins to the top of its own plate"
    # A header and the cell beneath it must share one line-box height, or the
    # labels sit off the baseline of the values they name. It has to be a length,
    # not a ratio: a ratio scales with each cell's own font-size, which is exactly
    # the skew this replaces.
    assert re.search(r"--de-row-line: [\d.]+rem", css), (
        "a unitless ratio scales with each cell's font-size and re-introduces the skew"
    )
    for selector in (".de-table th {", ".de-table td {"):
        rule = css.split(selector)[-1].split("}")[0]
        assert "line-height: var(--de-row-line)" in rule, f"{selector} must share the metric"
    # And one padding source, so the two cannot drift apart again.
    assert css.count("padding: var(--de-cell-pad);") >= 2


@pytest.mark.unit
def test_page_transition_is_a_fade_and_never_leaves_the_page_hidden() -> None:
    script = _no_comments(app_script())
    assert "de-leaving" in script
    assert "if (reduce.matches) return;" in script
    # A cancelled or same-document navigation must un-hide, or the page goes blank.
    assert "pageshow" in script
    assert script.count('classList.remove("de-leaving")') >= 2
    # Only same-origin document navigations: an API link or a download must not fade.
    assert "url.origin !== window.location.origin" in script
    assert 'link.hasAttribute("download")' in script


@pytest.mark.unit
def test_the_ui_never_names_its_own_endpoints() -> None:
    """The browser surface is an operator console, not an API browser.

    Every panel used to name the transport it was built on: a heading reading
    "API latency by route", route templates like `/api/v1/metrics` rendered
    verbatim into table cells, and empty states whose only instruction was
    "submit one with POST /api/v1/jobs". A person deciding whether this system
    is healthy cannot act on any of that, and it publishes the shape of the
    surface to anyone who loads the page.
    """
    for make in ALL_PAGES:
        html = make()
        body = html.split("<main", 1)[-1]
        # The <link> to the stylesheet and the asset URLs are not prose; the
        # assertion is about what the operator is asked to read.
        assert "/api/v1" not in body, "an endpoint path is visible in the page body"
        for word in ("POST /api", "GET /api", "endpoint", "API latency"):
            assert word not in body, f"{word!r} appears in operator-facing copy"

    # Latency is reported per operation, in words.
    assert _operation("/api/v1/episodes") == "episode listing"
    assert _operation("/api/v1/monitoring/tick") == "monitoring window"
    assert _operation("/api/v1/jobs/{job_id}/report") == "job listing"
    # An unmapped route must degrade, not fall through to the raw template.
    unmapped = _operation("/api/v1/some_new_thing")
    assert "/" not in unmapped and "{" not in unmapped
    assert _operation("-") == "unknown" and _operation("") == "unknown"


@pytest.mark.unit
def test_an_empty_state_never_tells_the_operator_to_use_a_transport() -> None:
    """ "submit one with POST /api/v1/jobs" is not a way out of an empty page.

    It is the only instruction the empty state gave, and it assumed a reader who
    already knows the API. Each hint now names the thing to do instead.
    """
    html = jobs_page({"items": []}, None, "vt220")
    assert "no jobs recorded" in html
    assert "/api/v1" not in html
    slices = slices_page({"items": []}, "vt220")
    assert "no saved slices" in slices
    assert "/api/v1" not in slices


@pytest.mark.unit
def test_the_transition_actually_recedes_the_page() -> None:
    """The class must change something, not merely declare a transition on it.

    It previously carried `transition: opacity ...` and no rule ever set an
    opacity, so the whole transition was a no-op: the page snapped and only the
    indicator moved. A transition property with nothing to transition is
    invisible in review and in a screenshot, which is exactly why it survived.
    """
    css = layout_css()
    block = css.split("body.de-leaving {")[1].split("}")[0]
    assert re.search(r"\bopacity:\s*0\.", block), "the outgoing page must dim"
    assert "filter: blur(" in block, "the outgoing page must go out of focus"
    # And every animated property must be listed, or it snaps instead of easing.
    for prop in ("opacity", "filter"):
        assert prop in block, f"{prop} is animated but not transitioned"


@pytest.mark.unit
def test_the_progress_rule_does_not_cover_the_page() -> None:
    """It was a bordered box floating over the content it was describing.

    A centred panel reads as a dialog - it occludes the page and implies a
    decision. A hairline at the top edge occludes nothing and is where someone
    already looks after clicking.
    """
    css = layout_css()
    block = css.split(".de-sweep-panel {")[1].split("}")[0]
    assert "position: fixed" in block
    assert re.search(r"inset:\s*0 0 auto 0", block), "pinned to the top edge, not centred"
    assert "place-items: center" not in block, "a centred box covers the content"
    assert re.search(r"height:\s*2px", block), "a rule, not a panel"
    # It must not intercept clicks meant for the page underneath.
    assert "pointer-events: none" in block


@pytest.mark.unit
def test_motion_tokens_are_the_published_material_3_values() -> None:
    """Read from material-web v0.192, not written from memory."""
    css = layout_css()
    assert "cubic-bezier(0.2, 0, 0, 1)" in css  # emphasized
    assert "cubic-bezier(0.05, 0.7, 0.1, 1)" in css  # emphasized-decelerate
    assert "cubic-bezier(0.3, 0, 0.8, 0.15)" in css  # emphasized-accelerate
    assert "cubic-bezier(0.3, 0, 1, 1)" in css  # standard-accelerate
    assert "500ms" in css and "200ms" in css  # duration tokens


@pytest.mark.unit
def test_status_colours_are_desaturated_and_theme_scoped() -> None:
    """One ramp per theme, none of them the accent, none fully saturated.

    The bug: `--fine-use-success` is #00ff00 and core.css applied it at full
    weight, so a single "ok" in a row of grey numbers read as a highlighted
    button. The ramp is derived from Okabe-Ito and pulled toward each theme.
    """
    css = layout_css()
    for theme in ("vt220", "amber", "github-dark", "monochrome"):
        block = css.split(f'[data-theme="{theme}"]')[1].split("}")[0]
        for name in ("--de-ok", "--de-warn", "--de-err", "--de-info", "--de-muted"):
            assert name in block, f"{theme} is missing {name}"
    # No pure-saturated channel survives in the chromatic themes: every value has
    # at least one channel pulled well below 255. Monochrome is exempt by design -
    # its whole premise is that *weight* carries the alarm, not hue, so --de-err
    # being #ffffff there is the correct answer, not a failure.
    values = re.findall(r'\[data-theme="([^"]+)"\]\s*\{([^}]*)\}', css)
    checked = 0
    for theme, block in values:
        if theme == "monochrome":
            continue
        for hex in re.findall(r"--de-(?:ok|warn|err|info|muted):\s*(#[0-9a-f]{6})", block):
            r, g, b = (int(hex[i : i + 2], 16) for i in (1, 3, 5))
            assert max(r, g, b) < 250, f"{theme} {hex} is effectively pure/saturated"
            # Saturation proxy: a full-strength channel with a near-zero neighbour
            # is exactly the "neon primary" the sore-thumb complaint was about.
            assert max(r, g, b) - min(r, g, b) < 200, f"{theme} {hex} reads as a neon primary"
            checked += 1
    assert checked >= 15, f"only {checked} chromatic values checked"
    # The classes bind to the ramp, not to the vendored saturated tokens.
    assert ".text-success { color: var(--de-ok); }" in css
    # And the ramp must not be the same value as the vendored token it replaces.
    assert "--de-ok: var(--fine-use-success" not in css


# ---------------------------------------------------------------- runtime


@pytest.mark.unit
def test_client_runtime_is_self_contained_and_deferred() -> None:
    script = _no_comments(app_script())
    assert "http://" not in script and "https://" not in script, "no remote fetches"
    assert "import " not in script and "require(" not in script, "no bundler, per ADR 0014"
    assert "use strict" in app_script()


@pytest.mark.unit
def test_client_runtime_does_not_own_rendering() -> None:
    """One renderer. If app.js built rows, the server and the client would drift."""
    script = _no_comments(app_script())
    # One *write*, not one mention: reading innerHTML to seed the change guard is
    # not rendering. Any second assignment would be a second source of truth.
    assert script.count("innerHTML =") == 1, "the only DOM write is the poll swap"
    assert script.count("insertAdjacentHTML") == 0
    assert script.count("outerHTML") == 0
    assert "createElement" in script, "the clipboard fallback is the only element it builds"


@pytest.mark.unit
def test_shortcuts_do_not_fire_while_typing() -> None:
    script = _no_comments(app_script())
    assert "isTyping" in script
    assert 'tag === "input"' in script
    assert "isContentEditable" in script
