# Resume readiness (final stage)

Use `orchestrator` + `reviewer`.

1. Run the stage-gate review (`11-engineering-review.md`) for Resume/Demo Ready.
2. Extract **only** claims backed by experiment records with provenance. For each: metric, value with CI/percentiles, workload, hardware, commit, experiment link.
   Write `agents/implementation/resume-metrics.md`. No unmeasured superlatives; state baselines and what was compared against.
3. Write `docs/interview-defense.md`: for each major decision (ADR), the problem, the alternatives rejected, the trade-off accepted, and the evidence.
   Include likely questions (scaling, failure handling, exactly-once, GPU scheduling, data versioning, why not Ray/K8s/MLflow, what you'd do with 100× data).
4. Root `README.md`: what/why, architecture diagram, quickstart, demo script (≤ 5 minutes), benchmark summary table, honest limitations.
5. Clean history hygiene: squash only if it improves clarity; verify no secrets anywhere.
6. Provide 4–6 resume bullet drafts, each with a metric traceable to `resume-metrics.md`.
