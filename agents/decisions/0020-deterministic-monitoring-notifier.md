# ADR 0020: The monitoring notifier is deterministic

- **Status:** accepted, 2026-09-29
- **Deciders:** owner + implementer
- **Supersedes:** the machine-learning framing of
  [`agents/implementation/automated-monitoring-plan.md`](../implementation/automated-monitoring-plan.md).
  Amends the direction in [ADR 0008](0008-observability.md); complements
  [ADR 0017](0017-runtime-metrics-aggregation.md) (the signals this consumes).

## Context

The platform's original ask was machine monitoring for unattended curation: flag anomalies in a queue, have a
model interpret them, and eventually notify the owner. The proposal that followed proposed an Isolation
Forest over a ~30-feature windowed observation, with a budgeted LLM interpreting triage entries, and a
chaos harness producing labelled ground truth.

Working the design through to the ground-truth methodology made the machine-learning parts indefensible on
their own terms, and the honest conclusion was reached while writing them down:

1. **Nearly every real afk failure has a known mechanism.** A suspended laptop, a dead worker, a full disk, a
   stalled run, a quarantine rate far above its baseline, a dataset that returned too few episodes — each is
   a rule with a threshold.
2. **There is no data to train on.** Real incidents are rare and unlabelled. The only labelled data is the
   injected catalogue from the chaos harness, and that catalogue is the authors' own idea of what goes
   wrong. A model trained on it learns those assumptions and reports them back as accuracy. This is a
   labelling problem; no modelling choice fixes it.
3. **Volume is far below what is needed.** Idle periods produce no ticks, 60-second ticks are strongly
   autocorrelated, and effective sample size — not tick count — is the binding constraint.
4. **Latency and operability are free at zero.** A rule over thirty features costs microseconds and is
   inspectable. A learned component adds an artifact to version, a schema to keep in sync with it, a
   non-deterministic output to explain, and a failure mode not reproducible from its inputs.

Production practice agrees: metric-spike detection is handled with median/MAD and EWMA control limits
rather than learned models, Netflix checks generic metadata at the source before business metrics, and
Dynatrace treats over-alerting — not missed anomalies — as the primary failure mode.

The taxonomy also proved to be imported from the wrong context. Cloud SRE targets a 24/7 multi-host fleet
with an on-call rotation; Faultlined is a single-user, local-first, batch system on a laptop that suspends.
`QUEUE_BACKLOG` and `WORKLOAD_ETA_SHIFT` are fleet-scale concerns that mostly mean "I submitted too much"
here, while suspend, disk exhaustion, clock drift, and silent partial success — the highest-probability afk
failures — had no label at all.

## Decision

**The notifier is deterministic. No trained model, no LLM call, and no network dependency in the detection
path.**

1. **Detectors are rules and control limits.** `monitoring/detectors.py` holds explicit threshold rules over
   a versioned feature vector; `monitoring/baselines.py` supplies per-scope EWMA centres and median/MAD
   spreads. Both are inspectable and reproducible from their evidence.
2. **Baseline fitting is not model fitting.** Control limits are *fitted* per (feature, scope) from observed
   history, which is robust statistics and is what lets "this source is normally slower" pass unflagged. The
   line is drawn at fitted parameters, deliberately, and no neural or statistical model crosses it.
3. **Completion contracts are the anchor.** During unattended curation the operative question is whether the
   requested work happened. A declared expectation compared against an observed outcome is exact,
   deterministic, and needs no threshold tuning, so it carries the highest precision per incident.
4. **Severity is the cost of the night, not the size of the deviation.** A 3σ wobble in a metric nobody is
   waiting on is less urgent than a run that will not finish. Notification is limited to three classes
   (wasted night, data being lost, commitment missed); everything else batches into the queue.
5. **Taxonomy re-derived for local-first unattended use** (`automation plan` §5). Severity is assigned by
   the consequence of the incident going unnoticed overnight.
6. **Deterministic summaries.** Incident text is rendered from the evidence by template, so the system is
   offline and free, and the summary cannot assert anything the evidence does not contain.
7. **Ground truth from a real system.** Only the operator's action space is simulated; ingest, validation,
   catalog, the metric sink, and the feature builder are the real implementation, with faults injected into
   the running process. A clean twin is not a valid control on this platform (Windows filesystem and Defender
   jitter, EXP-0002), so the null band is the across-replicate distribution of N clean runs.
8. **No new dependencies.** Phases 0–3 use only the stdlib and what the project already depends on. This is
   partly the point: the notifier cannot acquire a dependency, a model artifact, or a credential it does not
   need.

## Consequences

- Every incident is reproducible from its evidence, which is what makes the chaos-harness numbers auditable
  and the failure modes debuggable.
- Adding a dependency later requires its own ADR and a technology-matrix entry, as usual.
- The taxonomy is now the load-bearing artefact. Coverage of the real fault space is the honest unknown, and
  the unlabelled-events channel exists to shrink it from evidence rather than from imagination.
- Recalibration of control limits is an operational concern (baseline age is surfaced in
  `/api/v1/monitoring/health`), not a training-pipeline concern.
- An LLM enrichment stage that *rewrites* the deterministic summary remains open as a later phase, off by
  default and unable to affect detection. The evidence bundle is structured for it now, so the option is
  cheap to take up and impossible to confuse with a detector.

## Alternatives considered

- **Isolation Forest over the feature vector:** rejected — no labels, insufficient volume, and a
  non-reproducible output for a system whose value is being trusted. Revisit only with ≥ 30 days of real
  telemetry and ≥ 200 labelled incidents from the unlabelled channel, and only if it demonstrably beats
  rules-plus-statistics on Tier-3 faults without exceeding the false-positive budget.
- **LLM in the detection decision:** rejected outright. It would make the notifier non-deterministic,
  network-dependent, quota-bound, and unable to explain a decision it made.
- **LLM for summary only, at MVP:** rejected as unnecessary. A template rendered from the same evidence is
  deterministic, offline, instant, and cannot hallucinate a cause the evidence does not contain. Deferred.
- **Full-pipeline simulation for ground truth:** rejected — it puts the simulator–reality gap on the entire
  feature space and never exercises the real measurement layer.
- **Paired clean twin as the null:** rejected — not available on Windows, where the same seed does not
  reproduce the same run. Replicate variance replaces it.
