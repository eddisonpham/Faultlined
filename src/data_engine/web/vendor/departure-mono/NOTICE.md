# Departure Mono (vendored)

- **Upstream:** https://departuremono.com/ · https://github.com/rektdeckard/departure-mono
- **Artifact:** `DepartureMono-Regular.woff2` from release `v1.500` (DepartureMono-1.500.zip)
- **Author:** Helena Zhang (helenazhang.com)
- **Licence:** SIL Open Font License 1.1 (full text in `LICENSE`, this directory)
- **Vendored:** 2026-09-29
- **Decision:** [ADR 0014](../../../../../agents/decisions/0014-minimal-ui-server-rendered.md) (addendum)

Static asset only: no JavaScript, no `@font-face` remote URLs, nothing fetched at runtime. It is
served by `GET /ui/vendor/departure-mono/DepartureMono-Regular.woff2` and used as the display/readout
face; body text stays on the vendored terminal-ui stack. Do not edit the font or the licence. See
`agents/implementation/departure-mono-vendored.md` for how to drop it.
