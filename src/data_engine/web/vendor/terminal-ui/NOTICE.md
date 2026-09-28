# Vendored: terminal-ui-components

Third-party CSS vendored into the repository. Not authored by this project.

| Field | Value |
|---|---|
| Upstream | https://github.com/wesellis/terminal-ui-components |
| Pinned commit | `b5d9a832eb6d631e4610cc9662b79707bb1a6bd6` |
| License | MIT — see [LICENSE](LICENSE) |
| Retrieved | 2026-09-28 |
| Reason | ADR 0014 addendum: supplies the terminal/instrument visual vocabulary (themes, ASCII bars, terminal chrome) as a single stylesheet with no build step and no JavaScript, so the platform stays a single `just run` process. |

## Files

| File | Upstream path |
|---|---|
| `core.css` | `components/css/fine-use-core.css` |
| `theme-vt220.css` | `themes/vt220.css` |
| `theme-amber.css` | `themes/amber.css` |
| `theme-github-dark.css` | `themes/github-dark.css` |
| `theme-monochrome.css` | `themes/monochrome.css` |

The upstream project ships more themes; only these four are vendored. To add one, copy it from
`themes/` at the same pinned commit and register it in `data_engine.web.THEMES`.

## Caveats

- The upstream repository had **0 stars and 0 forks** when pinned. It is a young project with no community
  review, and its README lists themes (classic, matrix, dracula, nord) that do not exist in the repository.
  Treat it as unproven.
- None of the files reference external URLs, web fonts, or `@import`, so the UI still works fully offline
  and pulls nothing at runtime.
- MIT requires the copyright notice be retained; it is kept in [LICENSE](LICENSE) and the header comment
  in each file is unmodified.
- **How to drop it:** the vendored CSS is loaded by two routes and one stylesheet link. Removing the
  `vendor/` directory, the vendor routes in `src/data_engine/api/app.py`, and rewriting
  `src/data_engine/web/faultlined.css` reverts to a self-contained UI. No Python logic depends on it.
