# Plan: Automated Monitoring and Anomaly Detection for Faultlined

**Status: proposal for review. Nothing in this document is implemented.**
**Author/agent:** ML/DL monitoring proposal, 2026-09-29
**Related:** [run-intelligence slice](run-intelligence-slice.md) (the metrics this consumes),
[telldown plan](telldown-plan.md) (curation surfaces this monitors), ADR 0017 (metrics aggregation),
ADR 0008 (observability direction), ADR 0005 (catalog), the platform thesis in
[spec/problem.md](../spec/problem.md): *the platform is the product; models are workloads*.

## 1. Problem, and what "done" means

Faultlined now emits the raw material for machine monitoring (ADR 0017): queue depth, queue/run time, stage
durations, failures by reason code, retries, cancellations, timeouts, ingest counters, API request latency,
catalog query latency, worker heartbeats — plus the curation state this release added (quarantine rate,
reason-code distribution, per-profile failure counts, slice membership drift).

Today, a human must open `/ui/metrics` and *notice* something. The proposal is a service that watches those
signals, decides whether something is actually wrong, and puts a short, deduplicated, human-readable queue
of candidate incidents in front of the operator.

The hard part is not detecting odd numbers. It is **not crying wolf**. The literature and production
practice are unambiguous on the failure mode: teams receive hundreds of alerts per week per host, the
overwhelming majority noise, and engineers learn to ignore the channel entirely (Dynatrace, *Best practices
for avoiding overalerting*; Bigeye, *Anomaly detection part 1*; Netdata, *What is Alert Fatigue*). A
detector that is right 90% of the time but pages 50 times a day is worse than useless — it destroys the
signal for the one alert that mattered. **The primary objective of this system is precision and a bounded
alert budget, not recall.** Recall improves with better features; trust is won or lost on precision.

Success, for the MVP:

1. Every incident in a labelled synthetic workload is detected with a bounded median detection latency.
2. False positives per simulated day are below a fixed budget (a stated number, below).
3. Repeat firings of the same underlying fault collapse into one incident (dedup + suppression).
4. The queue is *explainable*: each entry names the signal, the threshold, the evidence window, and a
   confidence. No entry exists that cannot cite its evidence.
5. Zero outbound communication. The emailer is a hard TODO behind a disabled feature flag (§10).

## 2. What production systems actually flag (research)

Sources read 2026-09-29; recorded because the design borrows their taxonomy rather than inventing one.

- **Netflix** (Bigeye, *Anomaly detection for data quality at Netflix*): monitor **at the source**, before
  ETL; generic metadata checks first (partition loaded, row count, min/max, cardinality, **percentage of
  data discarded in processing**); then business metrics. High label cardinality (countries × ISPs) is
  called out as the hard part of scaling checks.
- **Dynatrace**: over-alerting is the primary failure mode; severity-based routing and event-based
  (state-transition) suppression are the standard remedies. Anomaly detection is configured to reduce noise,
  not to maximize detection.
- **Metric-spike detection is a solved, cheap problem** (VictoriaMetrics anomaly-detection handbook;
  Tinybird). Robust statistics (median/MAD, EWMA control limits) are the standard first line. Deep models
  are not the default even in mature stacks.
- **Model choice for small tabular streams**: Isolation Forest is repeatedly identified as the best
  accuracy/latency tradeoff at small feature counts and small sample sizes, with neural autoencoders
  competitive only at larger scale and costing far more at inference (SSRN 6732514; *Isolation Forest vs
  Autoencoders*; the lightweight AE-IF literature reports AE inference orders of magnitude slower than IF
  for comparable accuracy).

**Conclusion that drives the architecture:** the layered, interpretable design (rules → robust statistics →
Isolation Forest) is the industry default *and* the right engineering choice for the stated hardware. A
deep sequence model is not justified at MVP data volume; it is documented as a later, gated phase (§11).

## 3. Architecture

```text
runtime metrics (JSONL sink, ADR 0017) ─┐
catalog state (jobs/episodes/validation) ─┼─▶ feature builder ─▶ detectors ─▶ triage/queue ─▶ LLM ─▶ TODO: email
                                          │   (windowed, low-dim)   (rules,   (priority,     (summary
                                          │                        stats,   dedup,        only,
                                          │                        IF)      cooldown)     budgeted)
                                          └──────────────────────────────┐
                                                 incident store (Postgres, audit trail)
```

Four stages, each independently testable, each with a fallback:

1. **Feature builder** (`monitoring/features.py`): pure functions from (metric records, catalog snapshots)
   to a fixed-width, fixed-order feature vector per evaluation window. Pure, unit-tested, no I/O.
2. **Detectors** (`monitoring/detectors.py`): each returns `Signal[]` with evidence. Three classes:
   - **Deterministic rules** — the hard failures with known thresholds (§5). No model, no false positives
     by construction; these are the highest-severity entries.
   - **Robust statistical detectors** — per-metric EWMA control limits and median/MAD robust z-scores over
     recent history. Microsecond cost, fully explainable ("3.2σ above the last 30-minute median").
   - **Multivariate detector** — Isolation Forest (scikit-learn) over the feature vector, for combinations
     no single threshold catches (e.g. run time creeping up *while* frame counts drop).
3. **Triage queue** (`monitoring/queue.py`): priority queue keyed by `severity × confidence`, with
   incident fingerprinting (hash of detector + scope + signature), cooldown/suppression windows, and a
   hard cap on entries per window. This is the component that enforces the alert budget (§7).
4. **LLM interpreter** (`monitoring/llm.py`): optional, budgeted, off by default (§8). Turns a queue entry
   into a human-readable incident summary. It never decides *whether* to alert — the detectors do — and it
   never sends anything.

## 4. State space and features

The user's framing ("a state space involving the user's actions and how the data processing happens") is
right about the *inputs* and worth being precise about the *representation*.

MVP uses a **stateless windowed observation**, not a learned state space. At each evaluation tick (default
60 s) the feature vector describes the state of the pipeline; the history of past vectors is the sequence
that a sequence model (Phase 3) would consume. This is deliberate: with a single local worker, days of
history, and no labelled incidents, a stateful sequence model would be fitting noise. The simulator (§6)
generates the labelled sequences that would justify one.

Feature vector (per tick; ~30 numeric features, no raw IDs, no high-cardinality labels — Netflix's
cardinality lesson). All derived from what Faultlined already emits or can count cheaply:

| Group | Features |
|---|---|
| Queue | depth by state, oldest queued age, claim latency p50/p95, depth trend (Δ, slope) |
| Runs | run time p50/p95 by job type, attempts/retries, cancel rate, timeout rate, failure rate by reason code |
| Progress | episodes ingested per tick, bytes written, episodes per second, per-run episode count |
| Curation | quarantine rate, verdict mix (smooth/moderate/jerky), reason-code mix entropy, slice membership drift |
| Quality | movement/jerk/stall means and their dispersion, length distribution shift |
| Health | worker heartbeat age, catalog query p95 by operation, API request p95 by route template, host CPU/RAM/disk |
| Meta | evaluation window index, detector availability (missing sensors), data freshness (sink lag) |

Invariants the feature builder must preserve:

- **No unbounded labels.** Route templates and job types only — never job ids, episode ids, or URLs
  (the same low-cardinality rule ADR 0017 already applies to metric labels).
- **Missing is not zero.** A missing heartbeat is a *feature* (health = 0), not an absent row. Silent
  missingness is the most common way anomaly detectors fail in production.
- **Pure functions, no I/O.** Every feature is a function of its inputs, so the simulator can generate
  them and the tests need no database.
- **Deterministic order and units.** Features are a fixed-length vector with a versioned schema; a schema
  change retires the model, explicitly, rather than silently mis-scoring.

## 5. Anomaly taxonomy (deterministic class labels)

These are the labels the simulator injects and the metrics report ground truth against. Rule-based ones are
exactly the classes the user named, plus the ones production systems converge on:

| Label | Detected by | Severity | Rationale / source |
|---|---|---|---|
| `JOB_STUCK` | rule: running past deadline with no heartbeat, or queue age > threshold | critical | ADR 0015 deadlines exist; silent stalls are the top user complaint |
| `WORKLOAD_ETA_SHIFT` | stats: run-time p50 trend, queue depth trend | high | "heavy workload changes ETA" (user's example) |
| `QUEUE_BACKLOG` | rule: depth > N or oldest-queued age > S | high | Dynatrace event suppression pattern |
| `FAILURE_RATE_SPIKE` | rule/EWMA: failure rate over baseline | critical | failures by reason code already emitted |
| `RETRY_STORM` | rule: retries/tick above baseline | high | bounded retry budget makes this a real failure mode |
| `METRIC_SPIKE` | robust z on any single signal | medium | classic; the cheap detector catches most of it |
| `MULTIVARIATE_DRIFT` | Isolation Forest | medium | catches combinations no single threshold sees |
| `MALFORMED_OUTPUT` | rule: read/parse failure rate > 0 (the reader raises instead of guessing) | critical | Netflix "malformed records" |
| `DATA_DISCARDED` | rule: quarantine rate up, or episodes/expected ratio down | high | Netflix "% discarded in ETL" |
| `QUALITY_SHIFT` | stats: verdict mix / jerk mean shift | medium | curation-specific; a dataset silently going bad |
| `WORKER_LOST` | rule: heartbeat age > threshold | critical | ADR 0015 deferred lease heartbeats; this is the observable stand-in |
| `SILENT` | no detector fires | — | the negative class; the most important one to keep quiet |

Severity is an input to the queue, not an output of the LLM.

## 6. Simulator, ground truth, and the metrics that matter

Real incidents are rare and unlabelled, so ground truth comes from a **synthetic process simulator** that
models the user's workflow (the user's core idea, kept):

- **Generative model of normal use.** A Markov chain over operator actions (submit ingest → watch queue →
  inspect episodes → curate flag → save slice → export) driving job submission at realistic rates, with
  diurnal variation, occasional bursts, and correlated stages. Normal faults (retry, cancel, deadline miss)
  are emitted at a base rate.
- **Fault injection with labels.** The generator injects the §5 conditions at known times, with known
  magnitude and duration, and records `(t_start, t_end, label, magnitude, scope)`. This is the ground truth.
- **Counterfactual control.** Every run has a paired "clean" twin (same seed, no injection) so false
  positives are measured against exactly the distribution the detector claims to model, not a generic one.
- **Difficulty tiers.** Tier 1: single-signal, large magnitude (easy). Tier 2: subtle magnitude, overlapping
  signals. Tier 3: compound faults, delayed onset, and a background of near-miss values engineered to
  punish a threshold that is merely aggressive.

**Reported metrics** (all per tier, with confidence intervals, and the alert budget alongside):

| Metric | Why it matters |
|---|---|
| Precision @ alert budget | the number that determines whether anyone keeps the channel open |
| Recall per label | which failure classes are actually covered |
| False positives per simulated day | the fatigue driver; must be a stated, enforced budget |
| Detection latency (median, p95) | how long from fault onset to queue entry |
| **Alert compression ratio** | raw firings → deduplicated incidents; the suppression layer's value |
| Time-to-acknowledge proxy | duration before a same-fault recurrence opens a *new* incident |

Scaffolding note: the simulator and detector are pure functions over vectors, so the whole evaluation
harness runs in-process with no database and no network, and the latency benchmarks reuse the existing
`benchmarks/harness.py` registry under ADR 0019 (one workload per process; no committed baselines without
the owner's authorization).

## 7. Alert budget, dedup, and suppression

The part most toy implementations skip, and the part that decides whether this is used:

- **Fingerprint** = hash(detector, scope, signature) where signature is a coarse bucket of the evidence
  (e.g. `TOO_FEW_FRAMES` in profile `staged-strict`, not a count). One fault → one incident regardless of
  how many ticks or metrics notice it.
- **Severity gate** before the queue: only `critical` and `high` enter; `medium` requires two independent
  detectors to agree, which is a cheap precision lever.
- **Cooldown** per fingerprint (default 30 min) with an escalation counter: a fault still open after
  cooldown does not create a new entry, it increments a counter on the existing one.
- **Hard budget** (e.g. ≤ 10 new incidents per simulated day; configurable): when exceeded, the system
  degrades to "log-only" for the rest of the window and *says so* rather than silently dropping or
  silently flooding.
- **Maintenance windows** and a global mute, so a deliberate experiment does not train the operator to
  ignore the channel.

## 8. LLM role: interpretation only, and rationed

The LLM is deliberately the *last* stage and never a gate. Detectors decide; the LLM explains.

- **Model routing** (latency and cost first, per the owner's constraint that Gemini quota is limited):
  - Default: a small fast HF model for classification/summarization of the structured evidence bundle.
  - Escalation: Gemini (fast tier) only for incidents above a severity threshold, only when the budget
    allows, and only in batches.
  - Batch size 1–5 incidents, hard per-hour and per-day token ceilings, full request/response caching by
    incident fingerprint so a repeated incident is never re-summarized.
  - Offline/degraded: if the budget is exhausted or the provider fails, the incident still exists with its
    detector evidence; only the prose is missing. The system degrades to "no LLM", never to "no alert".
- **Structured input, structured output.** The model receives a fixed JSON evidence bundle (signals,
  thresholds, windows, affected scope) and must return a schema-constrained object: `summary`,
  `likely_cause`, `evidence[]`, `recommended_next_steps[]`, `confidence`. Invalid output is rejected and
  retried once, then dropped — the detector evidence stands on its own.
- **Injection safety.** All model input is our own JSON; episode metadata and notes are treated as
  untrusted data and never as instructions. No tool calls, no outbound requests from the model.
- **Framework.** No agentic framework at MVP: a single structured call is sufficient, and LangChain/ADK
  would add dependencies and nondeterminism for no benefit. Revisit only if multi-step investigation
  (e.g. correlating several incidents into a root-cause narrative) proves necessary.

## 9. Storage and interfaces

- **Incidents** table (Postgres): fingerprint, detector, label, severity, confidence, first_seen,
  last_seen, occurrence_count, status (`open`/`acknowledged`/`resolved`), evidence JSONB, llm_summary,
  escalation level. Incidents are an audit trail a build can cite, consistent with ADR 0007.
- **Read APIs** (mirroring existing conventions, all read-only except ack/resolve):
  `GET /api/v1/incidents` (filters: severity, status, label), `GET /api/v1/incidents/{id}`,
  `POST /api/v1/incidents/{id}/ack`. `GET /api/v1/monitoring/health` (detector availability, feature
  freshness, budget consumption) so the monitor's own health is observable — a monitor that silently stops
  is the worst outcome of all.
- **UI**: an `/ui/incidents` page in the existing instrument style: severity badges, dedup counters,
  evidence sparklines, ack action. No chart library (ADR 0014).
- **Metrics** for the monitor itself, following ADR 0017: `detector_evaluations_total`,
  `detector_signal_seconds`, `incidents_opened_total{label,severity}`, `incidents_suppressed_total`,
  `llm_tokens_total{model}`, `llm_budget_exhausted`, `feature_build_seconds`, `sink_lag_seconds`.

## 10. Email: explicitly a TODO, deliberately disabled

**Do not implement outbound email as part of this plan's first green flag.** Requirements when it is
built, recorded now so the eventual implementation is not casual:

- Hard-disabled by default behind `DE_MONITOR_EMAIL_ENABLED=false`; requires explicit owner
  authorization, per this conversation.
- Only `critical` incidents, and only after a configurable acknowledgment grace period.
- Provider selection goes through the platform's service-discovery step; no provider is hardcoded.
- Delivery is idempotent per incident id; retries are bounded and dead-lettered.
- Every outbound message embeds the incident id, the evidence, and a one-click acknowledge link.
- A dry-run mode that logs exactly what would be sent, so the blast radius is inspectable before it is live.

## 11. Phasing and acceptance criteria

| Phase | Content | Acceptance |
|---|---|---|
| 0. Ground truth | Simulator v0 (normal-use Markov chain + Tier-1 injections) | Reproducible seeds; labelled incidents; harness runs offline |
| 1. Rules | §5 deterministic detectors, no model, no LLM | 100% recall on Tier-1; zero FPs on clean twins |
| 2. Statistics | EWMA/MAD detectors, features, dedup/suppression, incident store, read APIs, UI | Recall ≥ 0.9 Tier-1, ≥ 0.7 Tier-2; FP budget met; latency p95 < 50 ms/evaluation |
| 3. Multivariate | Isolation Forest over the feature vector, trained on simulator output | Beats statistics on Tier-3 without exceeding the FP budget; inference < 5 ms/tick on CPU |
| 4. LLM | Budgeted, schema-constrained interpretation; HF default, Gemini escalation | Valid schema ≥ 99%; budget respected; system fully functional with LLM disabled |
| 5. Sequence model (gated) | Only if volume + labels justify it (e.g. ≥ 30 days of real telemetry and ≥ 200 labelled incidents) | Demonstrably beats Phase-3 on a held-out simulator set, or the phase is cancelled |

**Dependencies (not added by this plan; proposed when each phase starts).** Phase 2 needs nothing beyond
the stdlib. Phase 3 adds `scikit-learn` — justified in a technology-matrix entry, CPU-only, model artifact
in the hundreds of KB, no GPU required. `torch` is **not** proposed for any phase: it would pull ~2 GB for
no measured benefit at this scale and data volume. PyTorch becomes defensible only under the Phase-5 gate,
and even then CPU-only is sufficient for the feature counts involved.

**Hardware fit.** The design targets the stated laptop (7 GB VRAM, RTX 5060). Isolation Forest inference is
CPU-bound and a few hundred microseconds to low milliseconds for ~30 features; no GPU path is required, and
the LLM calls are remote. VRAM is not a constraint for this architecture — which is itself an argument for
choosing it.

## 12. Risks and open questions

1. **Precision on Tier-3 is the real risk.** Compound and delayed faults are where statistics and IF both
   degrade. Mitigation: report per-tier metrics honestly, and let the budget gate *suppress* rather than
   guess.
2. **Simulator–reality gap.** A simulator that is too easy inflates every number. Mitigation: Tier-3
   difficulty, clean-twin counterfactuals, and an explicit statement in every report that these are
   synthetic numbers, not production claims (the same honesty rule EXP-0001 follows).
3. **Metric sink growth.** Aggregation is already the slowest read path (p50 ~44 ms, B-015); the feature
   builder must read bounded tails, and compaction (B-004) is a prerequisite for sustained operation.
4. **Cardinality.** Per-route, per-operation, and per-reason-code features are the tempting ones and the
   ones that will not generalize. Cap the label space at the feature schema, not at query time.
5. **Open question for the owner:** which alert budget (incidents/day) is actually acceptable, and whether
   `medium` should ever page or only appear in the UI. The default here is UI-only, on the assumption that
   paging should be rare.
6. **Open question:** should incidents block dataset builds (a build cites incident state) or merely
   annotate them? Default: annotate, with a documented `blocked` state deferred to the builds stage.

## 13. What this plan deliberately does not do

- No model in the pipeline path. The detector runs out of band; a failed monitor must never fail a build.
- No LLM in the detection decision. The model's job is explanation, on a budget.
- No email. Disabled by design until authorized (§10).
- No claims about production accuracy before real labels exist. Every number is synthetic and labelled as
  such.
- No new dependencies until the phase that needs them, and no GPU dependency at all unless Phase-5 gates
  are met.
