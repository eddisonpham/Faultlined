# The operator's experience of Faultlined — a design critique

- **Date:** 2026-10-04
- **Author:** Independent design review, written as an operator who has to get a
  dataset built by lunch
- **Evidence:** [EXP-0015](../experiments/0015-feature-latency-at-three-scales.md) for every latency number,
  [EXP-0017](../experiments/0017-operator-click-and-attention-budget.md) for every click and control count. Both are
  measured off the served HTML of a running server; nothing here is estimated. Companion:
  [the engineering verdict](../reviews/2026-10-04-engineer-assessment-verdict.md).

---

## 0. The rule I am applying

**A design is good when the number of decisions an operator has to make goes *down* as they get better at their job,
not up.** Every extra screen, every extra confirmation, every control that only exists because a database has a table,
is a decision the operator is being asked to make about the tool instead of about their data.

I am also going to be fair about the parts that are genuinely good, because a critique that only finds faults is a
list, not a review.

---

## 1. What is genuinely, unusually good

**The visual language is the best thing in the repository.** ADR 0021's "bench instrument, not dashboard" is a real
design position and it is executed with real discipline: instrument readouts with tabular figures and units, silkscreen
legends, machined bezels, and — the detail I would point at in a code review — **hazard striping reserved for exactly
one thing**, the failure banner, so that when the stripes appear the operator knows without reading that the numbers
on screen may be lying. Most teams ship a dashboard and then spend six months discovering that their warning colour is
the same yellow as their "moderate" badge. This one solved that on purpose.

**The failure-visibility policy is right and rare.** `web/app.js` distinguishes `live` / `retry` / `stale` /
`offline` / `failed` / `paused`, never discards the last good render, backs off exponentially, and writes both the
failure *and the recovery* to an `aria-live` region. "A dead backend and a quiet system must not look the same" is
the right sentence to build a product around. Most tools get this wrong and their users learn to distrust every
number on screen.

**The typography and the accessibility mechanics are correct**: a skip link that is genuinely first, a `<caption>` on
every table, `role="img"` with a real label on every meter, focus rings restyled and never removed, every animation
behind `prefers-reduced-motion`, and colour never the only signal. The vendored Okabe-Ito status ramp, pulled down in
chroma per theme so it sits *in* the palette rather than on top of it, is the kind of detail most teams skip.

**It works with JavaScript disabled.** Every control is a real form submission. On a data-engineering tool used on
locked-down lab machines over SSH tunnels, that is not nostalgia, it is the correct engineering constraint.

**The information architecture actually works.** Every journey I measured between two pages is **one click**. ADR
0027's grouped nav does its job. The clustering work found a real thing (extract the object core, do not embed the
sentence), measured it honestly, and then *replaced its own feature with a better one* rather than defending it. That
is the healthiest thing in the engineering record.

I want all of that on the record before the criticism, because the criticism is about a specific failure and it would
be easy to misread as "the design is bad". **The design language is excellent. The interaction model is not.**

---

## 2. The failure: this is a data-entry tool pretending to be a pipeline

Here is what an operator's afternoon actually looks like.

> I have 40 minutes of bag on a robot that will not be there tomorrow. I need a dataset my colleague can train on.
>
> 1. Open the app. The Status page. I fill in a path, press one button. Good — that part works.
> 2. Wait. Go look at the bag in another tool. Come back. It says **succeeded**.
> 3. Now I need a dataset. I look for a "Build" button. There isn't one. There is a nav item called Builds. I click
>    it. **Zero rows.** There is no button on this page. There is no button on any page.
> 4. I go to the API docs — which are not linked, because they were deliberately removed — and find
>    `POST /api/v1/jobs` with `type: build`. I open a terminal. I paste JSON.
> 5. It works. Then I need to export it as a real LeRobot v3 dataset for the training script, which is the entire
>    reason the build exists. **Also no button.** Also `curl`.
> 6. I have now left the tool twice and pasted JSON twice.

**The tool can ingest. It cannot finish the job.** That is not a missing feature in a list — it is a change in what
the product *is*. Everything downstream of ingest is API-only.

And the reason it is invisible to every process that should catch it is worth stating plainly: **the routes do not
exist, so no route test fails; the API works, so the contract check passes; the pages render, so the browser audit
finds them clean.** A page with zero rows is a valid page. This is the one place I would add a gate, and the gate is
not a test — it is a line in the README: *every capability in the API table must have a browser route.* Anything else
lets the product's story drift away from its surface silently, and it has.

---

## 3. The second failure: the page you live on is the slowest page in the product

`/ui/vocabulary` is the newest feature, the one the last two commits were about, and the one an operator is meant to
spend their day in.

**Measured: 595 ms p95 at 10,000 episodes — the slowest page in Faultlined. Fourteen times the interactive density of
any other page: 28 buttons, 27 forms, 12 fields, on 14.9 KiB of HTML.**

Here is what that number means to a person, not to a percentile.

Every map, dismiss and accept is a form POST. There is no optimistic update, no batch action, no keyboard path, no
undo-without-a-round-trip. So triaging one screen of 10 unmapped strings is:

- **10 clicks**, each a full page load;
- **10 × 595 ms ≈ 6 seconds of pure waiting**, during which I cannot see whether my click registered;
- **10 full re-renders, each re-running five catalog queries**, one of which (`vocabulary_health`) duplicates two
  others — 182 ms of the 595 ms is work the page did *twice* because the readout strip underneath needs a count and
  the table above already has the rows.

And `app.js` polls every few seconds, so **I pay that 595 ms again on every poll whether or not I acted.**

Six seconds per screen is not catastrophic. It is also completely unnecessary, and it is unnecessary for a reason that
has nothing to do with Postgres: **the page does the same expensive work twice, and the slowest part of that work is
a count that the page has already computed.**

There is a design lesson in there that is bigger than the fix. The readout strip was designed as a *separate thing*
from the table — "here are the totals" living next to "here are the rows" rather than being derived from them. That
separation is why two queries became four. **A readout that cannot be derived from what is already on screen will
eventually be computed twice**, and in a server-rendered page "twice" means "twice in the latency budget too".

---

## 4. The third failure: the tool cannot tell me what went wrong

A design review that only looks at the happy path misses the thing that decides whether people keep using software.
So: it is 3pm, I submitted a bag, the job says **failed**.

The job page says:

```
type: ReaderError
message: job handler failed
```

I do not know which bag, which format, what was wrong, or what to try instead. The real answer —
`no reader recognises /data/ros2/2026-10-04-teleop (tried: mcap, lerobot)` — was written to a JSON log file that I
have to go and find, on a machine, with a correlation id.

**For a data tool, the failure message is the product.** Everything else — the readouts, the charts, the theme — is
furniture around the one thing that matters: what do I do next. And on the single most common event in the tool's
life (my file was not in the two formats it knows), it says nothing at all.

This is a hard-coded string at `jobs/worker.py:482`. It is one line to fix and it has been shipping this way through
every stage of the project. That it survived a stage called "Production Hardening" is the more interesting fact: the
project measures itself extremely well on latency, coverage and provenance, and not at all on **whether the thing it
shows a person is the thing that person needs.**

---

## 5. Smaller things that cost more than they should

**The nav costs two clicks for everything.** Fourteen links live in three collapsed `<details>` groups, so every
navigation is *expand the group, then click*. On a machine where I use one page all day, the group is usually open,
so this is a minor tax — but it is a tax paid on every visit to a page whose group happens to be closed, for no
design gain. The grouping is right; the collapse is the wrong call for fourteen items.

**`/ui/incidents` is a page that always says everything is fine.** Not because nothing has gone wrong — because
nothing *can*. The monitor is never scheduled, so no incident has ever been raised, so the page renders zero rows,
and it renders zero rows *permanently*. **A page whose entire purpose is to warn you, which is structurally incapable
of warning you, is worse than no page**: it reads as "all clear". If the scheduler is not going to exist, the page
should say "monitoring is not running" in the hazard stripes it already has. This is precisely the kind of honest-empty
the project's own design language is equipped to express.

**`/ui/schema` costs 538 ms to show the database's own structure**, flat from 20 episodes to 10,000. It re-reads
`information_schema` on every load. It is the second-slowest page in the product and it is a debugging convenience.

**`/ui/benchmarks` and `/ui/experiments` render the repository into itself.** Both are the fastest and cleanest pages
in the product and neither is something anybody needs to build a dataset. They are developer conveniences wearing
product nav.

**Everything is a page.** Fifteen nav destinations for a product whose actual workflow has four steps. Each one is a
full server render, each one costs 30–600 ms, and the operator pays a page load to move between stages of a single
task. The job lifecycle already has the right model — submit, watch, inspect — and it is spread across three pages
when it wants to be one thing that happens while you do something else.

---

## 6. What I would change, in order

1. **Put build, validate, export and slice creation in the browser.** Not a redesign — four routes, built through
   the Pydantic models the JSON endpoints already use, exactly as `POST /ui/jobs` already does. This is the single
   change that makes the product a product, and the pattern for it is already in the codebase.
2. **Write the real error where the operator can read it.** One line. Every other design improvement in this document
   is worth less than this one.
3. **Let the operator batch the work they actually came to do.** Ten map actions on ten rows should be one
   submission, not ten page loads. This is the change that takes the vocabulary page from six seconds to under one,
   and it is the difference between a tool that scales with the queue and a tool that makes the operator's day longer
   as the queue grows.
4. **Derive the readouts from what is already rendered, so a count cannot cost a second query.** 595 ms → ~330 ms,
   and it stops getting worse as the unmapped queue grows, which is exactly when it matters most.
5. **Say so when a page cannot do its job.** If the monitor is not scheduled, `/ui/incidents` should say so in the
   failure banner rather than showing an empty queue. The design language can already do this. It is being used to
   say "the backend is stale" and not to say "this feature is switched off".
6. **Schedule the monitor, or delete the page.** Half of option 5 disappears if the scheduler exists.
7. **Cap the payloads.** 2.2 MiB of JSON for a summary and 5.2 MiB of HTML for a page are not interfaces a human
   should be served. Both are contract changes and both need an ADR.
8. **Un-collapse the nav, or cut it to six items.** Fourteen destinations for four steps is a menu, not a map.

---

## 7. The one-line critique

**The team has been designing a bench instrument and shipping a data-entry form.** The craft is real and it shows in
every pixel; what it has not yet been applied to is the question the operator actually has, which is never *"what does
this number say"* — it is *"what do I do next, and how many things do I have to touch to do it"*.

The fastest route to fixing that is not a redesign. It is four routes, one line, and one batch action. The hard part
was never the aesthetic, and it never will be.