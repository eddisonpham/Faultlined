# Plan: A Deterministic Notifier for Unattended Curation

**Status: approved for implementation. This document describes a deterministic system: no trained model,
no LLM call, and no network dependency in the detection path.**
**Author/agent:** monitoring proposal, 2026-09-29
**Related:** [run-intelligence slice](run-intelligence-slice.md) (the metrics this consumes),
[telldown plan](telldown-plan.md) (curation surfaces this monitors), ADR 0020 (deterministic notifier),
ADR 0017 (metrics aggregation), ADR 0014 (server-rendered UI), ADR 0005 (catalog), and the platform thesis
in [spec/problem.md](../spec/problem.md): *the platform is the product; models are workloads*.

## 1. Problem, and what "done" means

Faultlined now emits the raw material for machine monitoring (ADR 0017): queue depth, queue/run time, stage
durations, failures by reason code, retries, cancellations, timeouts, ingest counters, API request latency,
catalog query latency, worker heartbeats — plus the curation state this release added (quarantine rate,
reason-code distribution, per-profile failure counts, slice membership drift).

Today a human must open `/ui/metrics` and *notice* something. This plan builds a notifier that decides
whether something is actually wrong and puts a short, deduplicated, explainable queue of candidate
incidents in front of the operator.

**The hard part is not detecting odd numbers. It is not crying wolf.** Production monitoring's dominant
failure mode is alert fatigue — hundreds of alerts per week, the overwhelming majority noise, and engineers
learn to ignore the channel entirely (Dynatrace, *Best practices for avoiding overalerting*; Bigeye,
*Anomaly detection part 1*; Netdata, *What is Alert Fatigue*). A detector that is right 90% of the time but
pages fifty times a day is worse than useless. **The objective is precision under a bounded budget, not
recall.** Recall improves with more rules; trust is won or lost on precision.

**The system is deterministic.** Every incident is produced by a rule or a control limit over a feature
vector, carries the evidence that produced it, and is reproducible from that evidence. There is no trained
model, no inference step, and no remote call anywhere in the detection path. §2 explains why this is a
deliberate engineering decision and not a deferral.

Success, for the MVP:

1. Every afk failure class in §5 has a detector, and every injected fault in §6 is caught within a bounded
   median detection latency.
2. False positives per chaos run are below a fixed budget, measured outside the clean-replicate band (§6.4).
3. Repeat firings of one underlying fault collapse into a single incident.
4. Every entry cites its evidence: the signal, the threshold, the scope, and the observation window. No
   entry exists that cannot cite its evidence.
5. A declared completion contract that is breached produces an incident (§8).
6. The monitor runs out of band. A failed monitor must never fail a build (§13).
7. Zero outbound communication. Email is a hard TODO behind a disabled flag (§10.3).

## 2. Why no machine learning

The obvious design for "detect anomalies" is a trained model. It is the wrong tool here, for four
independent reasons, any one of which would be sufficient.

**Almost every real failure has a known mechanism.** A suspended laptop, a dead worker, a full disk, a
run that stopped making progress, a quarantine rate far above its baseline, a dataset that came back with
too few episodes — each of these is a rule with a threshold. A model would rediscover the rule less reliably
and less interpretably.

**There is no data to train on.** Real incidents are rare and unlabelled. The only labelled data available
is the injected catalogue from the chaos harness (§6), and that catalogue is *my* idea of what goes wrong. A
model trained on it learns my assumptions and reports them back as accuracy. This is a labelling problem, and
no modelling choice fixes it.

**Volume is far below what any of this needs.** Idle periods produce no ticks, 60-second ticks are strongly
autocorrelated, and the honest tick supply is a fraction of what a naive count suggests. Effective sample
size, not tick count, is the binding constraint.

**Latency and operability are free at zero.** A rule over thirty features costs microseconds and is
inspectable. Any learned component introduces an artifact to version, a schema to keep in sync with it, a
non-deterministic output to explain, and a failure mode that cannot be reproduced from its inputs. For a
notifier whose entire value is being trusted, determinism is the feature.

The one thing that *is* fitted rather than hardcoded is the **control limit** per scope (§4.2): each metric
learns its own centre and spread from observed history. That is robust statistics, it is interpretable, it
handles "this source is normally slower" without a flag, and it is deterministic given its state. It is
fitted parameters, not a learned model, and the line is drawn there on purpose.

**What production practice actually does.** Netflix monitors at the source with generic metadata checks
before business metrics — partition loaded, row count, min/max, cardinality, *percentage of data discarded*.
Dynatrace treats over-alerting as the primary failure mode and uses severity routing with event-based
suppression. Metric-spike detection is a solved, cheap problem handled with median/MAD and EWMA control
limits; deep models are not the default even in mature stacks. The industry default for this class of
problem is layered, interpretable rules over statistics — which is what this plan builds.

**One consequence of dropping the model.** Sourcing the taxonomy from cloud SRE would import the wrong
failure modes: `QUEUE_BACKLOG` and `WORKLOAD_ETA_SHIFT` are fleet-scale concerns about many workers and many
users, and on a single-user single-worker machine a backlog usually just means *I submitted too much*. §5
re-derives the taxonomy for local-first unattended use rather than copying one.

## 3. Architecture

```text
runtime metrics (JSONL sink, ADR 0017) ─┐
catalog state (jobs/episodes/validation) ─┼─▶ features ─▶ baselines ─▶ detectors ─▶ triage ─▶ notify
completion contracts ────────────────────┘   (pure)     (EWMA/MAD)  (rules)    (dedup,   (3 classes)
                                                                │          budget)   (TODO)
                                                                └────────────┬─────────────┘
                                                                          ▼
                                                              incident store (Postgres)
```

Five stages, each independently testable, each with a fallback:

1. **Features** (`monitoring/features.py`): pure functions from (metric records, catalog snapshot,
   contracts) to a fixed-width, fixed-order, versioned feature vector per evaluation tick. No I/O, no clock,
   no randomness — the same inputs always produce the same vector.
2. **Baselines** (`monitoring/baselines.py`): per-scope EWMA centre and median/MAD spread, updated from
   observed values and persisted. Supplies each statistical detector with its own limits.
3. **Detectors** (`monitoring/detectors.py`): each returns a `Signal` carrying label, severity, scope, and
   the evidence that justifies it. Two classes: **contract checks** and **threshold rules** (§5, §8).
4. **Triage** (`monitoring/queue.py`): fingerprinting, dedup, cooldown, severity gate, and a hard budget.
   This is the component that enforces the alert budget (§7) and it is where most of the precision comes
   from — precision is a property of the whole pipeline, not of any one detector.
5. **Notification** (`monitoring/notify.py`): renders the notify class (§10.1). Email is behind a disabled
   flag and is not implemented.

## 4. Features and baselines

### 4.1 The feature vector

One fixed-width vector per evaluation tick (default 60 s), ~30 numeric features, grouped as follows. All are
derived from what Faultlined already emits or can count cheaply.

| Group | Features |
|---|---|
| Queue | depth by state, oldest queued age, claim latency p50/p95, depth trend |
| Runs | run time p50/p95 by job type, attempts, retry rate, cancel rate, timeout rate, failure rate |
| Progress | episodes ingested per tick, bytes written, episodes per second, episodes per run |
| Curation | quarantine rate, verdict mix, reason-code mix, episodes vs contract expectation |
| Quality | movement / jerk / stall means and their dispersion, frame-count dispersion |
| Health | worker heartbeat age, catalog query p95 by operation, API p95 by route template |
| Resources | CPU, memory, disk free, GPU presence |
| Meta | evaluation tick index, sensor availability, data freshness (sink lag) |

Invariants, each of which exists because violating it produces a detector that fails in production:

- **No unbounded labels.** Route templates and job types only — never job ids, episode ids, or URLs. The
  same low-cardinality rule ADR 0017 already applies to metric labels.
- **Missing is not zero.** A missing heartbeat is a *feature* (`heartbeat_age = sentinel`), not an absent
  row. Silent missingness is the most common way monitors fail: the signal the operator most needs is the
  one that disappears.
- **Pure functions, no I/O, no clock.** Every feature is a function of its inputs, so the chaos harness
  replays recorded windows and detector tests need no database.
- **Versioned schema.** `FEATURE_SCHEMA_VERSION` is part of every incident record. A schema change
  invalidates stored baselines explicitly rather than silently mis-scoring history.

### 4.2 Baselines

Each (feature, scope) pair carries a centre and a spread, updated online:

- **EWMA centre**, α = 0.2, for the level.
- **Robust spread** via median and MAD over a bounded recent window (default 50 observations), converted to
  a σ-equivalent via `1.4826 × MAD`. Median/MAD rather than mean/std because a single spike must not widen
  the limits that are supposed to catch it — this is the standard failure of a mean/std baseline.
- **Cold start**: until a scope has `MIN_OBSERVATIONS` (default 20) observations, only absolute-threshold
  rules apply and statistical rules abstain. A monitor that is confidently wrong on its first ten ticks is
  worse than one that is silent.
- **Freeze during a breach**: while a scope's control limit is exceeded, its baseline does not absorb the
  value. Otherwise a sustained fault becomes the new normal within a few ticks and the incident silently
  resolves itself.

## 5. Anomaly taxonomy, re-derived for unattended local-first curation

These are the labels the fault injectors produce (§6.2) and the metrics report ground truth against.

| Label | Detected by | Severity | Rationale |
|---|---|---|---|
| `CONTRACT_BREACH` | contract check (§8) | critical | The work the user asked for did not happen. Highest value per incident |
| `RUN_STALLED` | rule: running past deadline with no progress | critical | The canonical afk failure; the night is being wasted |
| `WORKER_LOST` | rule: heartbeat age > threshold | critical | Laptop suspend, OOM, closed terminal — the most likely afk failure on this hardware |
| `DISK_PRESSURE` | rule: free space below floor | critical | Fails at publish, after all the compute; the artifact is lost |
| `DATABASE_UNREACHABLE` | rule: catalog probe fails | critical | Everything else is downstream of this |
| `PARTIAL_SUCCESS` | rule: run finished, valid fraction below contract | high | Job is green, the output is short. Silent without a contract |
| `QUARANTINE_RATE_HIGH` | rule: quarantine rate above per-source baseline | high | Netflix's "percentage discarded"; the dataset is smaller than it looks |
| `QUALITY_SHIFT` | statistical: verdict mix / jerk mean | high | The dataset will poison a training run days later |
| `FRAME_COUNT_COLLAPSE` | statistical: frame-count median below baseline | high | A camera dropped rate; episodes ingest fine and are worthless |
| `CLOCK_DRIFT` | rule: timestamp gaps in episode metadata | high | Breaks camera↔joint alignment; totally silent, totally destructive |
| `REPEATED_READ_FAILURE` | rule: read/parse failures on one source | high | One flaky source, not a fleet problem; names the source |
| `TIME_MISSED` | contract check (§8) | medium | A stated deadline passed without the work finishing |
| `METRIC_SHIFT` | statistical: robust z on one signal | medium | The residual; cheap detector, most of it legitimate |
| `RESOURCE_DEGRADED` | rule: sustained CPU / memory pressure | medium | No error raised; the tool just feels broken |
| `SILENT` | no detector fires | — | The negative class, and the most important one to keep quiet |

**Severity is the cost of the night, not the size of the deviation.** A 3σ wobble in a metric nobody is
waiting on is not more urgent than a run that will not finish. This is a deliberate departure from
fleet-oriented severity models, and it is what makes the afk budget tractable.

## 6. Ground truth: a real system with a simulated operator

Ground truth comes from faults we cause on purpose. The decision that shapes everything below: **only the
operator's action space is simulated.** Ingest, validation, quality analysis, indexing, catalog, the metrics
sink and the feature builder are all the real implementation, against the real database and the real
artifacts. Nothing about the feature space is invented.

| Layer | Real or simulated |
|---|---|
| Ingest / validation / quality / index / catalog | **Real** — the actual `de` binary, real Postgres, real artifacts |
| Metric emission, aggregation, sink, labels, cardinality | **Real** — the ADR 0017 path, unmodified |
| Feature builder and baselines | **Real** — identical in evaluation and production |
| Operator action space | **Simulated** — the only stochastic component |
| Faults | **Injected** — simulated by definition; a real stall cannot be waited for |

An earlier draft of this plan modelled the whole pipeline and generated feature vectors directly, and an
earlier draft of this plan proposed an Isolation Forest. Both were discarded: the first put the
simulator–reality gap on the entire feature space and never exercised the measurement layer; the second had
no labels to learn from. The costs of the current design are wall-clock time and the two bounded gaps in §6.8.

### 6.1 The action space

A small auditable scenario file (`scenarios/*.yaml`) rather than code. States mirror the real UI, so every
action is one a user can actually perform:

- **States**: `idle`, `submit_ingest`, `watch_queue`, `inspect_episodes`, `curate`, `save_slice`, `export`.
- **Dwell times**: lognormal per state. Think-time is heavy-tailed; a uniform makes the queue look
  implausibly regular.
- **Burst mixture**: mostly one job, occasionally 5–40. With a single worker this is the only thing that
  produces genuine queueing.
- **Legitimate-but-unusual behaviour is mandatory**: an oversized valid batch, a long quiet stretch, a
  back-to-back export. Without it, a detector is rewarded for flagging anything unusual, which is the
  cheapest way to inflate recall.
- **Two modes**: *scripted playback* (deterministic, the default for evaluation) and *stochastic sampling*
  (training data only). If the operator is live during an evaluation run, the operator has become an
  uncontrolled variable in the experiment.

### 6.2 Fault injection

Each injector perturbs the real system; the label follows from the mechanism, never the reverse.

| Label | Injection against the running system |
|---|---|
| `WORKER_LOST` | `taskkill` the worker mid-run; heartbeats stop for real |
| `RUN_STALLED` | submit a job that makes no deadline progress on the slow path |
| `MALFORMED_OUTPUT` | truncate or corrupt a parquet before the reader opens it |
| `REPEATED_READ_FAILURE` | point the reader at a source that repeatedly fails to read |
| `PARTIAL_SUCCESS` | tighten a validation profile so real episodes quarantine |
| `QUEUE_BACKLOG` | submit a burst larger than the single worker |
| `QUALITY_SHIFT` | ingest the other real dataset |
| `DISK_PRESSURE` | fill a scratch directory near the artifact root |
| `CLOCK_DRIFT` | shift episode timestamps in a staging copy |

**Observability delay.** A fault cannot be detected before the metric window containing it has closed. Every
injected fault records `t_detectable = t_onset + aggregation_window + write_delay`, and detection latency is
measured against `t_detectable`, never against `t_onset`. Measuring against onset reports a latency the
system physically cannot achieve.

**Difficulty tiers.** Tier 1: single signal, large magnitude. Tier 2: subtle magnitude, overlapping signals.
Tier 3: compound faults, delayed onset, and a fault whose signal is masked by benign drift.

### 6.3 Reset protocol

The most important operational rule in this section. The system is real, Postgres is shared, and
content-addressed state accumulates across runs — episode counts, quarantine rates and quality distributions
drift upward run over run. A monitor measured against a moving null produces numbers that are not comparable
between runs, and that drift is easy to misread as a genuine regression.

Every run therefore starts from a **baseline snapshot**:

1. A dedicated run database or schema (`de_chaos_<run_id>`), created from one seed snapshot taken with no
   prior run history. Never reuse a database that has hosted another run.
2. Real data directories, artifacts, and benchmark output are wiped and re-seeded from that snapshot.
3. Baseline state (EWMA centres, MAD windows) is reset alongside the database, so no state leaks across runs.
4. The run records snapshot id, scenario id, seed, and git sha. A number without all four is not reproducible
   and is not published.

The reset is verified by a pre-flight assertion that the seeded episode count equals the snapshot's.

### 6.4 The null band is replicate variance, not a twin

A paired "clean twin" is not an available control: the platform runs on Windows, where filesystem pressure
and antivirus scanning move ingest latency by tens of milliseconds (EXP-0002). Two clean runs from one seed
are not the same run, so a twin is not a valid control.

Instead: **N clean replicates per scenario**, same scenario, no injection. The across-replicate distribution
of every feature and of the detector's own scores *is* the null. A false positive is counted only when the
score falls outside that band — not merely when it exceeds a threshold calibrated on some other run.

The side effect is useful: the system's natural jitter becomes visible, and no detector can be credited with a
stability the platform does not have.

### 6.5 Evaluation rules

Each of these exists because violating it produces a plausible-looking wrong number.

1. **Score at the incident level, never the tick level.** A true positive is the *first* qualifying incident
   whose fault onset falls inside the injection window. One fault spanning forty ticks counts once.
2. **Split by seed and scenario, never by tick.** Adjacent ticks are autocorrelated and one fault spans many
   of them, so a random tick split places the same fault in both train and test.
3. **Bootstrap over runs, never over ticks.** Intervals resample whole runs; tick-level intervals on
   correlated data are far too narrow and present noise as a result.
4. **Fix the tuning thresholds in advance**, in a config file, and report only on held-out scenarios.
   Threshold-shopping on the evaluation set is as easy as seed-shopping and produces the same false results.

Report the **detectable floor** alongside recall: for each injected fault, the distance from its feature
vector to the clean-replicate band. Where the two overlap, no threshold can separate them, and publishing
"this label is undetectable below magnitude X" is more useful than a recall number.

### 6.6 Two harness modes, and what each may not stand in for

| Mode | What it is | Use |
|---|---|---|
| **Fast** | In-process detector tests over recorded feature windows; no system, no database | Detector logic, triage, dedup, contract checks — CI, every commit |
| **Chaos** | The real system, real wall clock, real metrics, real injections | Every published number |

The fast mode must never be quoted where a chaos number is meant. A chaos run costs wall clock — ingest here
is I/O-bound, with artifact publish alone around 20 ms of a 22 ms operation — so the budget is roughly **one
simulated hour per acceptance run, not 30 simulated days**. Latency benchmarks reuse the existing
`benchmarks/harness.py` registry under ADR 0019 (one workload per process; no committed baselines without the
owner's authorization).

### 6.7 Unlabelled events

Not every fault is one we chose to cause. Thermal throttling, antivirus interference, connection exhaustion
and plain operator improvisation produce real incidents with no label attached. Every run keeps an
**unlabelled-events channel**: any window not covered by a scripted action and not explained by an injection
is recorded for human review with its feature window attached.

This channel is the only mechanism by which the §5 taxonomy improves from evidence, and it is the direct
counterpart of the coverage gap in §6.8. Emergent failure modes are enumerated as an explicit watchlist
before the first chaos run, not discovered afterwards.

### 6.8 What is measured, and what is not

| Reported | Status |
|---|---|
| Feature-space fidelity | Real |
| Detector cost per tick | Real measurement |
| Sink lag, aggregation windows, label-cardinality effects | Real, and previously untested |
| Detection latency (median, p95) | Real, against `t_detectable` |
| False positives per run | Real, against the clean-replicate band (§6.4) |
| Recall per label | Real *for injectable faults only* |
| Coverage of the real fault space | **Unknown** — the one number that cannot be obtained, and the one that matters most |

The last row is the honest price of this design. Every report carries that distinction in its header.

### 6.9 Harness first increment (2026-09-30)

`monitoring/chaos.py` implements the replay half of this section: injection
seams, labeled windows, per-window scoring, and sequence replay with the real
baselines book ([EXP-0006](../experiments/0006-chaos-harness-first-increment.md)).
What remains from §6 before any accuracy figure: the scripted operator action
space (YAML), the fault-injection table run against `just run`, and
clean-replicate null bands.

### 6.10 Reported metrics

| Metric | Why it matters |
|---|---|
| Precision @ alert budget | the number that determines whether anyone keeps the channel open |
| Recall per label | which failure classes are actually covered |
| False positives per run | the fatigue driver; counted outside the clean-replicate band |
| Detection latency (median, p95) | from `t_detectable` to incident creation |
| **Alert compression ratio** | raw firings → deduplicated incidents; the suppression layer's value |
| Contract breach rate | how often stated expectations are not met |
| Unlabelled-event count | fault modes the catalogue does not yet cover |

## 7. Alert budget, dedup, and suppression

The part most toy implementations skip, and the part that decides whether this is used:

- **Fingerprint** = hash(detector, scope, signature) where signature is a coarse bucket of the evidence
  (e.g. the reason code and profile, never a count). One fault → one incident regardless of how many ticks
  or metrics notice it.
- **Severity gate** before the queue: only `critical` and `high` enter on their own. `medium` requires two
  independent detectors to agree — a cheap, large precision lever.
- **Cooldown** per fingerprint (default 30 min) with an occurrence counter: a fault still open after
  cooldown does not create a new entry, it increments a counter on the existing one.
- **Hard budget** (≤ 10 new incidents per chaos run, configurable): when exceeded the system degrades to
  log-only for the rest of the window and *says so*, rather than silently dropping or silently flooding.
- **Maintenance windows and a global mute**, so a deliberate experiment does not train the operator to ignore
  the channel.

## 8. Completion contracts

The single highest-value component, and the cheapest. During unattended curation the only question that
actually matters is: **did the thing I asked for happen?**

A contract is declared alongside a submission — expected episode count, expected valid fraction, expected
duration, and a deadline — and is evaluated when the run settles. Because it compares a stated expectation
against an observed outcome, it is exact, deterministic, interpretable, and free of threshold tuning.

- `expected_episodes`: fewer valid episodes than declared is `PARTIAL_SUCCESS`.
- `expected_valid_fraction`: a ratio below the declared floor is `PARTIAL_SUCCESS`.
- `max_duration_seconds` / `deadline_at`: exceeded is `TIME_MISSED`.
- A contract on a run that never settles and has no deadline is how `RUN_STALLED` gets its deadline.

This does not replace the other detectors; it *anchors* them. Anomaly-shaped signals with no contract
behind them are exactly the ambiguous ones, and they are routed to the queue at a higher bar.

## 9. Storage and interfaces

- **Incidents** table: fingerprint (unique while open), label, severity, scope, confidence, first_seen,
  last_seen, occurrence_count, status (`open`/`acknowledged`/`resolved`), evidence JSONB, summary text,
  feature schema version. An incident is an audit trail a build can cite, consistent with ADR 0007.
- **Contracts** table: job id, expectations, declared and observed values, outcome, timestamps.
- **Baselines** table: per (feature, scope) centre, median, MAD, observation count, updated_at.
- **Read APIs**, mirroring existing conventions, read-only except ack/resolve:
  `GET /api/v1/incidents` (filters: severity, status, label), `GET /api/v1/incidents/{id}`,
  `POST /api/v1/incidents/{id}/ack`, `GET /api/v1/incidents/summary`,
  `GET /api/v1/contracts` / `GET /api/v1/contracts/{job_id}`,
  `GET /api/v1/monitoring/health` (detector availability, feature freshness, baseline coverage, budget
  consumption). A monitor that silently stops is the worst outcome of all, so its own health is observable.
- **UI**: `/ui/incidents` in the existing instrument style — severity badges, occurrence counters, evidence
  table, ack action. No chart library (ADR 0014).
- **Metrics** for the monitor itself, per ADR 0017: `monitor_tick_seconds`, `monitor_signals_total{label}`,
  `incidents_opened_total{label,severity}`, `incidents_suppressed_total`, `baseline_observations`,
  `monitor_sink_lag_seconds`.

## 10. Notification policy, and the email TODO

### 10.1 Notify versus queue

The filter that decides everything: **would the user have found out anyway?**

| | Meaning | Where it goes |
|---|---|---|
| **Would never have looked** | Silent failure, no reason to check | Notify |
| **Would find it tomorrow** | Visible in `/ui/insights` if they went looking | Queue only |
| **Expected consequence of the work requested** | They asked for it | Not an incident |

Because the user is absent, the correct action for most incidents is *nothing until morning*. Waking someone
is reserved for exactly three classes:

1. **The night is being wasted** — `RUN_STALLED`, `WORKER_LOST`, `DISK_PRESSURE`, `DATABASE_UNREACHABLE`.
2. **Data is being lost as we speak** — `DISK_PRESSURE`, `CLOCK_DRIFT`.
3. **A commitment was missed** — `CONTRACT_BREACH`, `TIME_MISSED`.

Everything else batches. This is a much tighter budget than a daily one, and "severity" therefore means
*cost of the night*, not magnitude of deviation.

A notify must be actionable without opening the laptop, or it must not claim to be. "Quarantine rate 4× the
`svla` baseline; 61 of 200 episodes quarantined by `staged-strict`" is actionable. "Unusual metric activity
detected" is not, and would destroy the channel faster than any false positive.

### 10.2 Deterministic summaries

Incident summaries are rendered from the evidence by template, not generated. This keeps the system
deterministic and offline, costs nothing, and — because the summary is assembled from the same fields the
evidence contains — cannot assert something the evidence does not support. A future LLM enrichment stage
would consume exactly this evidence bundle as its input; that is a later phase (§11, Phase 4), and the
bundle is structured for it now so the option stays open.

### 10.3 Email: explicitly a TODO, deliberately disabled

**Do not implement outbound email under this green flag.** Requirements when it is built, recorded now so
the eventual implementation is not casual:

- Hard-disabled behind `DE_MONITOR_NOTIFY_EMAIL_ENABLED=false`; requires explicit owner authorization.
- Notify class only, and only after a configurable grace period for batched delivery.
- Provider selection goes through the platform's service-discovery step; no provider is hardcoded.
- Delivery idempotent per incident id; retries bounded and dead-lettered.
- Dry-run mode logging exactly what would be sent, so the blast radius is inspectable before it is live.

## 11. Phasing and acceptance criteria

| Phase | Content | Acceptance |
|---|---|---|
| 0. Features and baselines | `features.py`, `baselines.py`, schema versioning, cold-start and freeze semantics | Pure and deterministic; no I/O; property tests for the invariants in §4.1 |
| 1. Rules and contracts | §5 threshold rules, §8 contracts, triage, incident store, read APIs, `/ui/incidents` | 100% recall on Tier-1; zero FPs outside the clean-replicate band |
| 2. Statistics and chaos | Statistical detectors on fitted baselines, chaos harness, reset protocol, unlabelled channel | Recall ≥ 0.9 Tier-1, ≥ 0.7 Tier-2; FP budget met; tick cost p95 well under budget |
| 3. Notification | Notify/queue split, digest batching, dry-run renderer | Notify class only; every message actionable; no outbound call |
| 4. LLM enrichment (deferred) | Optional summary rewriting over the existing evidence bundle | Only if the deterministic summary proves insufficient; off by default; no detection change |

**Dependencies.** Phases 0–3 add nothing beyond the stdlib and what the project already uses. This is a
direct consequence of §2: the design was chosen partly so the notifier cannot acquire a dependency, a model
artifact, or a credential it does not need.

**Hardware fit.** A tick is thirty arithmetic operations and a dictionary comparison. It is not a constraint.

## 12. Risks and open questions

1. **Fault-catalogue coverage is the real risk.** The system under test is real, so the residual gap is not
   realism — it is that the taxonomy only contains faults we thought of (§6.8). Mitigation: Tier-3 masking,
   the unlabelled-events channel, a pre-declared watchlist of emergent modes, and headers that separate
   measured from unknown.
2. **A skipped reset invalidates a whole campaign.** §6.3 is load-bearing and easy to forget; state carried
   between runs moves the null. Mitigation: the pre-flight assertion fails the run rather than warning.
3. **n = 1 operator, and a live one.** The action model is calibrated on a single operator on a single
   machine, and that operator is present during chaos runs. Mitigation: scripted playback by default, one
   deliberately live stress mode, an action-replay path seeded from real session timestamps.
4. **Threshold drift.** Per-scope baselines drift with the platform — new job types, a faster disk, a
   different dataset. Mitigation: freeze-during-breach (§4.2) and a visible baseline age in
   `/api/v1/monitoring/health`.
5. **A frozen-on-fault bug inverts the freeze.** If freeze never releases, a resolved fault stays open
   forever. Mitigation: freeze has a maximum duration and the incident's own resolution is independent of it.
6. **Open question for the owner:** which alert budget is actually acceptable, and whether `medium` should
   ever notify. Default here: notify is limited to the three classes in §10.1 and `medium` is queue-only.
7. **Open question:** should incidents block dataset builds (a build cites incident state) or merely annotate
   them? Default: annotate, with a documented `blocked` state deferred to the builds stage.

## 13. What this plan deliberately does not do

- **No trained model, no LLM call, no network in the detection path.** See §2. This is a decision, not a
  deferral; a later phase may add summary *enrichment* and may not change detection.
- **No model in the pipeline path.** The monitor runs out of band; a failed monitor must never fail a build.
- **No email.** Disabled by design until authorized (§10.3).
- **No claims about production accuracy across the real fault space.** The action space and the fault
  catalogue are synthetic; what is measured on top of them is a real measurement of a real system. §6.8
  states exactly which is which.
- **No new dependencies until a phase needs one**, and on the current design no phase does.
