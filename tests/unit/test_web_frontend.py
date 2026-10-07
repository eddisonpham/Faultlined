"""Frontend contract tests for the instrument UI."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from data_engine.web import THEMES, app_script, layout_css, theme_or_default
from data_engine.web.pages import (
    CORE_URL,
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
from data_engine.web.records import (
    benchmarks_model,
    benchmarks_page,
    experiments_model,
    experiments_page,
)

ALL_PAGES = (
    lambda: status_page({}, "vt220"),
    lambda: jobs_page({"items": []}, None, "vt220"),
    lambda: episodes_page({"items": []}, None, None, "vt220"),
    lambda: slices_page({"items": []}, "vt220"),
    lambda: insights_page({}, "vt220"),
    lambda: metrics_page({}, "vt220"),
    lambda: incidents_page({}, "vt220"),
    lambda: benchmarks_page(benchmarks_model(), "vt220"),
    lambda: experiments_page(experiments_model(), "vt220"),
)


def _pages() -> list[str]:
    return [make() for make in ALL_PAGES]


def _no_comments(text: str) -> str:
    """Strip JS/CSS comments so an assertion tests code, not prose about the code."""
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.DOTALL)


def _client() -> TestClient:
    from data_engine.api.app import create_app

    return TestClient(create_app(initialize_database=False), raise_server_exceptions=False)


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
    """The banner is a hazard stripe; a screen reader never sees it."""
    script = _no_comments(app_script())
    assert script.count("announce(") >= 5, "offline, transient, permanent, and recovery"
    assert "Live updates restored." in script
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
    """A permanently-visible "live" badge reads as branding, not as a status."""
    html = status_page({}, "vt220")
    assert "data-led" in html
    assert "data-led-text" in html
    assert 'data-state="ok"' in html
    assert "<span data-led-text></span>" in html, "no hardcoded word in the markup"
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
    assert "/ui/app.js" in html


@pytest.mark.unit
def test_error_page_keeps_the_requested_theme_and_falls_back_safely() -> None:
    assert "theme-amber.css" in error_page(500, "d", "amber")
    assert "theme-vt220.css" in error_page(500, "d", "not-a-theme")


@pytest.mark.unit
def test_the_theme_probe_never_outlives_the_swap() -> None:
    """The glow that came back, and the one line that fixes it."""
    script = app_script()
    probe_load = script.split("probe.onload = function () {")[1].split("};")[0]
    assert "probe.remove();" in probe_load, "the probe must go once it has been committed"
    probe_error = script.split("probe.onerror = function () {")[1].split("};")[0]
    assert "probe.remove()" in probe_error
    assert 'probe.setAttribute("data-de-theme"' not in script


@pytest.mark.unit
def test_the_no_halo_override_beats_a_theme_sheet_loaded_after_it() -> None:
    """The override is only as good as its position in the cascade."""
    html = status_page({}, "vt220")
    order = [html.index(href) for href in (CORE_URL, "theme-vt220.css", "faultlined.css")]
    assert order == sorted(order), (
        "faultlined.css must be linked last or the no-halo override loses the cascade"
    )


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


@pytest.mark.unit
def test_markup_carries_the_hooks_the_vendored_themes_target() -> None:
    """Adopting the hooks is the whole reason the CRT themes render at all."""
    html = status_page(
        {"queue_depth": {"queued": 1, "running": 1}, "resources": {}},
        "vt220",
    )
    assert "fine-use-app" in html, "scanlines and themed backgrounds hang off this"
    assert "fine-use-component" in html
    assert "current-value" in html, "the readout styling hangs off this"
    assert "block-item" in html
    assert "fine-use-data-table" in html
    assert "fine-use-focusable" in html


@pytest.mark.unit
def test_layout_stylesheet_kills_the_halo_but_not_the_neon() -> None:
    """The one theme rule we override is the glow - and only the glow."""
    css = layout_css()
    assert css.count("text-shadow: none") >= 1, "the halo override is gone"
    halo_block = css.split("[data-theme] .current-value", 1)[1]
    assert "text-shadow: none" in halo_block
    assert not re.search(r"text-shadow:\s*none[^}]*color:", halo_block), (
        "the halo override must not also set colour; the neon stays"
    )
    assert not re.search(r"\.de-head h1[^{]*\{[^}]*color:\s*transparent", css)


@pytest.mark.unit
def test_layout_sheet_keeps_our_own_names_and_adopts_no_remote_assets() -> None:
    css = layout_css()
    for name in (".de-readouts", ".de-meter", ".de-section", ".de-chart", ".de-led"):
        assert name in css
    assert "http://" not in css and "https://" not in css


@pytest.mark.unit
def test_motion_is_opt_in_everywhere() -> None:
    """Every animation must be declared *inside* a no-preference guard."""
    css = layout_css()
    guards = css.count("prefers-reduced-motion: no-preference")
    assert guards >= 2
    for match in re.finditer(r"animation:", css):
        before = css[: match.start()]
        if "@keyframes" in before[before.rfind("}") :] and before.count("{") == before.count("}"):
            continue
        assert before.rfind("@media (prefers-reduced-motion: no-preference)") > (
            before.rfind("}") - 4000
        ), "an animation declared outside a no-preference guard will misfire"
    assert "html:not([data-booted])" in css


@pytest.mark.unit
def test_nothing_is_drawn_over_the_page_while_it_navigates() -> None:
    """The indicator is gone, and this is why."""
    html = status_page({}, "vt220")
    css = layout_css()
    script = _no_comments(app_script())
    for name in ("de-sweep-panel", "de-sweep", "data-sweep", "data-sweep-panel", "de-progress"):
        assert name not in html, f"{name} is drawn over the page"
        assert name not in css, f"{name} still has styles"
        assert name not in script, f"{name} still has behaviour"
    assert "function Spring(" not in script
    assert "requestAnimationFrame" not in script.split("initShortcuts")[0], (
        "nothing left to animate per frame except the transition itself"
    )


@pytest.mark.unit
def test_a_poll_cannot_replay_the_entrance_animation() -> None:
    """The reported flicker, at its actual cause."""
    css = layout_css()
    assert "html:not([data-booted])" in css, "the entrance must be gated on first paint"
    assert re.search(r"html:not\(\[data-booted\]\)[^{]*\{\s*animation:", css), (
        "the entrance animation must be the thing that is gated"
    )
    assert 'setAttribute("data-booted", "")' in _no_comments(app_script())
    refresh = css.split("@keyframes de-refresh")[1].split("}")[0]
    assert "opacity" not in refresh, "an opacity pulse on live data reads as a flicker"


@pytest.mark.unit
def test_the_poll_only_writes_the_dom_when_the_payload_really_changed() -> None:
    """The other half of the flicker, and the subtler one."""
    script = _no_comments(app_script())
    assert "html !== root.innerHTML" not in script, (
        "innerHTML re-serialisation makes this comparison always false-negative"
    )
    assert "html !== shown" in script
    assert "var shown = root.innerHTML" in script, "seed from what the server rendered"
    assert script.count("root.innerHTML = html") == 1
    assert "shown = html" in script


@pytest.mark.unit
def test_the_deadline_countdown_keeps_exactly_one_timer() -> None:
    """A poll write re-runs initDeadlines, so it must not stack intervals."""
    script = _no_comments(app_script())
    assert "var deadlineTimer = null;" in script
    fn = script.split("function initDeadlines(")[1].split("\n  }")[0]
    assert "if (deadlineTimer) window.clearInterval(deadlineTimer);" in fn
    assert fn.count("window.setInterval(") == 1, "one countdown timer, not one per poll"
    assert (
        script.split("function initClock(")[1].split("\n  }")[0].count("window.setInterval(") == 1
    )


@pytest.mark.unit
def test_sticky_table_headers_are_not_offset_by_the_nav() -> None:
    """The reported overlap, at its actual cause."""
    css = layout_css()
    assert "de-nav-h" not in css, "the header must not depend on the nav's height"
    assert "de-nav-h" not in app_script()
    assert "measureNav" not in app_script(), "the measuring code has no consumer left"
    wrap = css.split(".de-table-wrap {")[1].split("}")[0]
    assert "overflow: auto" in wrap, "the plate must be its own scrollport"
    assert "max-height" in wrap, "a scrollport with no bound is a page-height box"
    th = css.split(".de-table th {")[-1].split("}")[0]
    assert "position: sticky" in th
    assert re.search(r"\btop: 0\b", th), "the header pins to the top of its own plate"
    assert re.search(r"--de-row-line: [\d.]+rem", css), (
        "a unitless ratio scales with each cell's font-size and re-introduces the skew"
    )
    for selector in (".de-table th {", ".de-table td {"):
        rule = css.split(selector)[-1].split("}")[0]
        assert "line-height: var(--de-row-line)" in rule, f"{selector} must share the metric"
    assert css.count("padding: var(--de-cell-pad);") >= 2


@pytest.mark.unit
def test_page_transition_is_a_fade_and_never_leaves_the_page_hidden() -> None:
    script = _no_comments(app_script())
    assert "de-leaving" in script
    assert "if (reduce.matches) return;" in script
    assert "pageshow" in script
    assert script.count('classList.remove("de-leaving")') >= 2
    assert "url.origin !== window.location.origin" in script
    assert 'link.hasAttribute("download")' in script


@pytest.mark.unit
def test_the_ui_never_names_its_own_endpoints() -> None:
    """The browser surface is an operator console, not an API browser."""
    for make in ALL_PAGES:
        html = make()
        body = html.split("<main", 1)[-1]
        assert "/api/v1" not in body, "an endpoint path is visible in the page body"
        prose = re.sub(r"<code>.*?</code>", "", body, flags=re.DOTALL)
        for word in ("POST /api", "GET /api", "endpoint", "API latency"):
            assert word not in prose, f"{word!r} appears in operator-facing copy"

    assert _operation("/api/v1/episodes") == "episode listing"
    assert _operation("/api/v1/monitoring/tick") == "monitoring window"
    assert _operation("/api/v1/jobs/{job_id}/report") == "job listing"
    unmapped = _operation("/api/v1/some_new_thing")
    assert "/" not in unmapped and "{" not in unmapped
    assert _operation("-") == "unknown" and _operation("") == "unknown"


@pytest.mark.unit
def test_an_empty_state_never_tells_the_operator_to_use_a_transport() -> None:
    """ "submit one with POST /api/v1/jobs" is not a way out of an empty page."""
    html = jobs_page({"items": []}, None, "vt220")
    assert "no jobs recorded" in html
    assert "/api/v1" not in html
    slices = slices_page({"items": []}, "vt220")
    assert "no saved slices" in slices
    assert "/api/v1" not in slices


@pytest.mark.unit
def test_the_transition_actually_recedes_the_page() -> None:
    """The class must change something, not merely declare a transition on it."""
    css = layout_css()
    block = css.split("body.de-leaving {")[1].split("}")[0]
    assert re.search(r"\bopacity:\s*0\.", block), "the outgoing page must dim"
    for prop in ("opacity", "transform"):
        assert prop in block, f"{prop} is animated but not transitioned"


@pytest.mark.unit
def test_the_page_is_never_blurred_while_it_is_leaving() -> None:
    """Rule 2 (no halos) applies to motion, not only to text."""
    css = _no_comments(layout_css())
    block = css.split("body.de-leaving {")[1].split("}")[0]
    assert "filter" not in block, "a full-page blur is a glow, not a transition"
    for selector in ("body {", "body.de-leaving {"):
        rule = css.split(selector)[1].split("}")[0]
        assert "filter" not in rule, f"{selector} must not filter the page"
    nav = css.split(".de-nav {")[1].split("}")[0]
    assert "backdrop-filter" not in nav, "the nav must not blur the content under it"
    assert "transparent)" not in nav, "an opaque strip cannot fade to transparent"
    assert "filter:" not in css, "a blur anywhere in the sheet is a glow on screen"


@pytest.mark.unit
def test_the_only_navigation_motion_is_the_fade() -> None:
    """One transition, no companion effect."""
    css = layout_css()
    block = css.split("body.de-leaving {")[1].split("}")[0]
    assert re.search(r"\bopacity:\s*0\.", block)
    for banned in ("filter", "box-shadow", "backdrop-filter"):
        assert banned not in block, f"{banned} on the outgoing page is a glow"
    script = _no_comments(app_script())
    assert script.count('classList.add("de-leaving")') == 1, "one trigger, not several"


@pytest.mark.unit
def test_motion_tokens_are_the_published_material_3_values() -> None:
    """Read from material-web v0.192, not written from memory."""
    css = layout_css()
    assert "cubic-bezier(0.2, 0, 0, 1)" in css
    assert "cubic-bezier(0.05, 0.7, 0.1, 1)" in css
    assert "cubic-bezier(0.3, 0, 0.8, 0.15)" in css
    assert "cubic-bezier(0.3, 0, 1, 1)" in css
    assert "500ms" in css and "200ms" in css


@pytest.mark.unit
def test_status_colours_are_desaturated_and_theme_scoped() -> None:
    """One ramp per theme, none of them the accent, none fully saturated."""
    css = layout_css()
    for theme in ("vt220", "amber", "github-dark", "monochrome"):
        block = css.split(f'[data-theme="{theme}"]')[1].split("}")[0]
        for name in ("--de-ok", "--de-warn", "--de-err", "--de-info", "--de-muted"):
            assert name in block, f"{theme} is missing {name}"
    values = re.findall(r'\[data-theme="([^"]+)"\]\s*\{([^}]*)\}', css)
    checked = 0
    for theme, block in values:
        if theme == "monochrome":
            continue
        for hex in re.findall(r"--de-(?:ok|warn|err|info|muted):\s*(#[0-9a-f]{6})", block):
            r, g, b = (int(hex[i : i + 2], 16) for i in (1, 3, 5))
            assert max(r, g, b) < 250, f"{theme} {hex} is effectively pure/saturated"
            assert max(r, g, b) - min(r, g, b) < 200, f"{theme} {hex} reads as a neon primary"
            checked += 1
    assert checked >= 15, f"only {checked} chromatic values checked"
    assert ".text-success { color: var(--de-ok); }" in css
    assert "--de-ok: var(--fine-use-success" not in css


@pytest.mark.unit
def test_client_runtime_is_self_contained_and_deferred() -> None:
    script = _no_comments(app_script())
    assert "http://" not in script and "https://" not in script, "no remote fetches"
    assert "import " not in script and "require(" not in script, "no bundler, per ADR 0014"
    assert "use strict" in app_script()


@pytest.mark.unit
def test_client_runtime_does_not_own_rendering() -> None:
    """One renderer."""
    script = _no_comments(app_script())
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
