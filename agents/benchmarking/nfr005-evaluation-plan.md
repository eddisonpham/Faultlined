# NFR-005 Evaluation Plan — the queue-latency wording decision

- **Date (UTC):** 2026-10-03
- **Status:** plan — evidence packet for the owner sign-off on NFR-005's revision
- **Question:** should NFR-005 read the original raw bound ("enqueue→start p95 ≤ 1 s at 100 queued
  jobs"), the dispatch decomposition currently proposed in [requirements.md](../spec/requirements.md)
  (NFR-005), or a capacity restatement? The revision is provisional until this packet closes.
- **Current evidence:** [EXP-0010d](../experiments/0010d-queue-latency-and-cancel.md)

## Claim under test

Enqueue→start latency decomposes into two independent terms:

| Term | Definition | Who can change it |
|---|---|---|
| Queue-position wait | job's position in line × service time ÷ workers | capacity only (NFR-008); no scheduler can shorten it |
| Dispatch overhead | job becomes claimable → actually running (claim poll + start) | the scheduler; this is the only thing "≤ 1 s" can honestly bound |

If the decomposition holds, the original wording is unsatisfiable as a raw bound and the dispatch
reading is its only meaningful form. If it does not hold (e.g. overhead that grows with depth), then
the original bound is a real requirement the scheduler is failing — fix the code, not the wording.

## Evaluations

| # | Evaluation | Method | Decision rule | Status |
|---|---|---|---|---|
| E1 | Service-time baseline | drain 100 ingest jobs with 1 worker (`scale_campaign.py --leg enqueue-start`); t_service = drain wall ÷ jobs | feeds E2's floor; 3 repeats, report CV | measured once: 0.84 s/job (EXP-0010d) |
| E2 | Feasibility floor | compute wait_floor(d, w) = (d ÷ w) × t_service at the requirement's stated shape (d = 100, w = 1) | floor ≫ 1 s ⇒ no implementation of any kind satisfies the raw bound; the revision is **necessary**, not a preference | computed: ~85 s vs a 1 s target |
| E3 | Depth-scaling curve | submit one job at depths 0 / 10 / 25 / 50 / 100, 1 worker, n ≥ 5 per point; fit wait = a + b·depth | linear (R² ≥ 0.99) with b ≈ t_service ⇒ queue physics confirmed; superlinear ⇒ scheduler overhead grows with depth and the original intent is violated (defect, not wording); sublinear ⇒ batching/preemption exists, re-examine the model | not run |
| E4 | Dispatch-overhead isolation | submit→running with ≥ 1 idle worker, at depth 0 and at depth 100; record the claim-poll cadence | p95 ≤ 1 s at both depths ⇒ dispatch clause validated. Hidden constraint surfaced: the clause requires claim poll ≪ 1 s (currently `poll_seconds = 0.25`, `cli.py::_worker_loop`) — the cadence is part of the requirement | depth-100 measured (p95 0.78 s, EXP-0010d); depth-0 not run |
| E5 | Capacity-scaling confirmation | repeat E3's depth-100 point with 2 workers | wait ratio 1w : 2w ∈ [1.7, 2.3] ⇒ wait is capacity-bound; the remedy knob is workers (NFR-008), not the scheduler | not run |
| E6 | Cancel-clause scope | cancel (a) a queued job and (b) a mid-ingest running job (cooperative path), n ≥ 10 each, plus one run with a busy worker at depth ≥ 50 | max ≤ 2 s in all conditions ⇒ the cancel clause stands unqualified; a running-job miss ⇒ scope the clause to "queued jobs" or add a cooperative-checkpoint bound | (a) measured: max 0.175 s (EXP-0010d); (b) not run |
| E7 | Run-to-run variance | 3 independent repeats of E3/E4 on fresh throwaway DBs | p95 CV ≤ 20% ⇒ single-run numbers are stable enough to cite in a requirement; else publish bands | not run |

## Decision matrix

1. **E2 floor > 1 s and E3 linear with b ≈ t_service** ⇒ the raw wording is void as a bound. Adopt
   the dispatch decomposition.
2. **E4 p95 ≤ 1 s at both depths** ⇒ the decomposition's target is met and honest. NFR-005 reads
   "dispatch p95 ≤ 1 s (submit→running with an available worker) + submit-at-depth-100 p95 ≤ 200 ms,
   queue-position wait reported as service-rate-bounded".
3. **E3 superlinear or E4 > 1 s** ⇒ the original bound's intent stands and the scheduler is the work
   item; do not adopt the revision to paper over a defect.
4. **E6 running-cancel > 2 s** ⇒ narrow the clause to queued jobs or define a checkpoint bound.
5. If a capacity promise is wanted alongside the dispatch reading, add it in NFR-008's shape
   ("depth-100 backlog drains within ⌈100 ÷ w⌉ × t_service × 1.2 with w workers") instead of a
   per-job latency bound.

Owner sign-off is the final step (same pattern as NFR-001's revision); this packet is what the owner
signs off on.

## Measurement protocol

- One throwaway Postgres DB per run (the `scale_campaign.py` convention), 120 s MCAP fixture,
  one worker unless the evaluation says otherwise.
- n ≥ 5 samples per point; report p50 / p95 / max. E7 covers across-run variance.
- Results land in an experiment record (`agents/experiments/0012-nfr005-decomposition.md`), which
  supersedes this plan's "not run" cells; update backlog B-004 at the same time.

## Implementation notes

- `scale_campaign.py --leg enqueue-start` needs `--depth` and `--workers` parameters (E3/E5); it
  currently fixes depth 100 and 1 worker.
- `--leg cancel` needs a running-job cancel mode (E6b): the current leg only cancels queued jobs.

## Traces

- Backlog: B-004 (queue time), B-006 (worker scaling)
- Requirement: [NFR-005](../spec/requirements.md)
- Method: [methodology.md](methodology.md) — results become experiment records with full provenance
