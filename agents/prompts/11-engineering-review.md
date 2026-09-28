# Repository-wide engineering review (stage gate)

Use the `reviewer` role. Read-mostly. Stage under review: `<stage from definition-of-done.md>`.

1. Work from a clean clone in a temp directory. Follow README only. Run setup, fmt, lint, typecheck, test, coverage, hygiene, bench.
2. Compare docs to code: architecture docs vs actual modules, API docs vs actual endpoints, status vs reality. List every drift.
3. Audit decisions: every significant technology/interface choice has an ADR; no orphan decisions; superseded ADRs are marked.
4. Audit dependencies: each is justified; none unused (run the ecosystem's unused-dep checker where available).
5. Audit code: dead/duplicated code, speculative abstractions, missing timeouts, non-idempotent retries, swallowed errors, secret leakage in logs.
6. Audit tests: failure-mode catalog vs actual tests; flaky tests; assertion-free tests; coverage gate still enforced and not lowered.
7. Audit performance claims: each claim → experiment record → provenance → baseline. Flag unsupported claims.
8. Audit secrets: working tree, `git log -p`, docs, fixtures, CI config.
9. Audit the definition of done: each criterion checked with evidence.
10. Write `agents/reviews/YYYY-MM-DD-<stage>.md` from the template with severity-rated findings and a verdict.

Do not fix findings except trivial ones. Return the record and a prioritized action list.
