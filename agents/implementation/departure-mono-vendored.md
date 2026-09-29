# Vendored font: Departure Mono

Faultlined's UI uses one third-party font, vendored into the tree, as the visual anchor of the
instrument-panel design (run-intelligence slice). This page records what it is, why, and how to drop it.

| Field | Value |
|---|---|
| Upstream | https://departuremono.com/ · https://github.com/rektdeckard/departure-mono |
| Artifact | `DepartureMono-Regular.woff2`, release `v1.500` (pinned by release tag) |
| Author | Helena Zhang |
| Licence | SIL OFL 1.1 (text retained in `src/data_engine/web/vendor/departure-mono/LICENSE`) |
| Vendored | 2026-09-29 |
| Decision | [ADR 0014](../decisions/0014-minimal-ui-server-rendered.md) (addendum) |

## What it provides

A monospaced pixel face with a lo-fi technical vibe, designed for crisp legibility at 11 px increments
(source-log #47). It is used for display headings, numeric readouts, meters, and chart labels — the
"low-level instrument" register the owner asked for. Body prose stays on the vendored terminal-ui
monospace stack, so the font carries ~14 KB and only one weight.

## Why it does not break ADR 0014

Static asset only: no build step, no bundler, no runtime fetch, no JavaScript. It is served from
`/ui/vendor/departure-mono/DepartureMono-Regular.woff2` by the same single `just run` process.

## Risks

- **Display face, not text face.** Pixel fonts degrade in long prose; it must stay on labels/readouts.
  `faultlined.css` scopes it to `.de-display`, `.de-readout`, table numerics, and chart text.
- **Single weight.** v1.500 has one weight; emphasis is done with color and letterspacing, not bold.

## How to drop it

1. Delete `src/data_engine/web/vendor/departure-mono/`.
2. Remove the font route in `src/data_engine/api/app.py` and the `@font-face` + `.de-display`/
   `.de-readout` rules in `src/data_engine/web/faultlined.css`.
3. Nothing in Python depends on the font; the UI falls back to the terminal-ui monospace stack.
