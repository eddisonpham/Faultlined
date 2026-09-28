# Vendored terminal stylesheet

Faultlined's UI uses a third-party CSS file vendored into the tree. This page records what it is, why, and
what the risk is, so nobody has to reverse-engineer it later.

| Field | Value |
|---|---|
| Upstream | https://github.com/wesellis/terminal-ui-components |
| Pinned commit | `b5d9a832eb6d631e4610cc9662b79707bb1a6bd6` |
| Licence | MIT (notice retained in the vendored `LICENSE`) |
| Vendored | 2026-09-28 |
| Decision | [ADR 0014](../decisions/0014-minimal-ui-server-rendered.md) (addendum) |

## What it provides

Four of upstream's themes (`vt220` as default, plus `amber`, `github-dark`, `monochrome`) and one core
stylesheet. It supplies the visual vocabulary — colour variables, type scale, status colours, terminal
chrome — while `src/data_engine/web/faultlined.css` supplies Faultlined's own layout on top, and
`src/data_engine/web/pages.py` owns all markup and escaping.

## Why it does not break ADR 0014

That ADR forbids a frontend **framework, bundler, and build step**, and requires the platform to stay a
single `just run` process. This is a static stylesheet with no JavaScript and no `@import`, so nothing
builds and nothing is fetched at runtime. Verified by a test that asserts the vendored files contain no
`@import` and no absolute URL.

## Risks

- **Unproven upstream.** The repository had **0 stars and 0 forks** when pinned, and its README lists
  themes (`classic`, `matrix`, `dracula`, `nord`) that are not present in the repository. Treat it as
  unvetted third-party code, even under a permissive licence.
- **Single theme source of truth.** Theme names are hard-coded in `data_engine.web.THEMES`; a name that
  is not vendored silently falls back to `vt220` rather than erroring.
- **Vendored files are not ours to edit.** `faultlined.css` overrides where needed; do not patch the
  vendored files, or the next re-vendor silently discards the change.

## How to drop it

1. Delete `src/data_engine/web/vendor/terminal-ui/`.
2. Remove `ui_vendor_css` and the two vendor `<link>` tags in `src/data_engine/api/app.py` / `pages.py`.
3. Rewrite `src/data_engine/web/faultlined.css` to be self-contained.

No Python logic depends on the vendored CSS, so this is contained.
