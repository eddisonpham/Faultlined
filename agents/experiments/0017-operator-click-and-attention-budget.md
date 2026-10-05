# EXP-0017 — The operator's click and attention budget, measured off the served HTML

- **Date:** 2026-10-04
- **Commit:** `bab9ab5` (clean tree)
- **Hardware:** Windows 11, x86_64, PostgreSQL 17.11 on 127.0.0.1:55432
- **Tooling:** `scripts/operator_walkthrough.py`
- **Method:** [methodology](../benchmarking/methodology.md). Every number is read off HTML served by a real process
  against a real catalog. **No number in this record is estimated.**

## What this measures and why

"The more clicks the user does, the more exhausted they are" is usually argued from memory, and memory is exactly
what is wrong when a team has spent months inside its own product. This record removes the argument.

`scripts/operator_walkthrough.py` seeds a real catalog (40 episodes, 40 vocabulary entries with 160 mappings, three
jobs driven to terminal state), starts the real app, **crawls 104 reachable pages breadth-first from 17 seeds**,
builds the link graph from the rendered `<a href>`s, and reports the shortest click path between the pages that make
up the workflows the product exists for. It also reports, per page, the count of interactive controls, forms and
fields — because a page with one button and a page with twenty-eight are not the same amount of *attention* even at
the same click count.

## Navigation: the information architecture is fine

Every journey between pages costs **one click**:

| Workflow | Clicks |
|---|---:|
| read why a job failed | 1 |
| inspect an episode's motion quality | 1 |
| see what a vocabulary entry covers | 1 |
| find quarantined episodes | 1 |
| build a dataset | 1 |
| see the build that contains an episode | 1 |
| check the system is healthy | 1 |
| see what broke | 1 |
| submit a bag and watch it land | 2 |
| triage one raw task string | 0 (start page) |

**Navigation is not the problem, and it is worth saying so before critiquing it.** ADR 0027's grouped nav works: the
brand link and the nav are on every page, entries link to their detail pages, an episode links to the builds that
contain it. Finding things is one click away everywhere. Anyone claiming the IA is confusing is describing a
different product.

One cost is real but small: the 14 nav links sit in three collapsed `<details>` groups, so a nav item costs two
interactions (expand the group, click the item) unless its group is already open. It is not counted above because
the group is open on the page you are already on.

## Attention: this is where the cost is

Per-page interactive density, from the served HTML:

| Page | buttons | forms | fields | table rows | bytes | p95 (EXP-0015) |
|---|---:|---:|---:|---:|---:|---:|
| **`/ui/vocabulary`** | **28** | **27** | **12** | 35 | 14.9 KiB | **595 ms** |
| **`/ui/vocabulary/entries/{id}`** | **6** | **5** | **13** | 17 | 8.4 KiB | - |
| `/ui/episodes` | 3 | 2 | 3 | 42 | 27.9 KiB | 92 ms |
| `/ui/jobs` | 3 | 2 | 2 | 4 | 6.7 KiB | 77 ms |
| every other page | 2 | 1 | 1 | - | 3.7–48 KiB | 32–538 ms |

The `2 buttons / 1 form / 1 field` baseline on every other page is the theme picker. Measured against that baseline,
the vocabulary page is **fourteen times denser**, and it is the page the newest feature exists for.

**Twenty-seven forms means twenty-seven POST round trips, each a full page reload.** This is a deliberate and
defensible choice — ADR 0014 takes server-rendered pages that work with JavaScript disabled, and every control here is
a real form submission — but the cost of that choice lands entirely on the one page where an operator has to do the
most work.

### The arithmetic an operator actually experiences

`/ui/vocabulary` renders, per screen: a candidate row with *accept* actions, an unmapped row with a *map* select and
a *dismiss* button, and an entry list. Triaging one screen of 10 unmapped strings is 10 map submissions. Each one
costs a full page load at the measured **595 ms p95** (EXP-0015), and each one re-runs the five catalog queries the
page performs — including the duplicated pair from EXP-0015 that costs 182 ms on its own.

**10 strings ≈ 10 clicks ≈ 6 seconds of waiting, plus 10 full re-renders that re-do 182 ms of redundant work each
time.** At 20 strings it is 12 seconds. And this is a page that polls — `app.js` refreshes `.de-live` every few
seconds — so the operator is paying the 595 ms again on every poll whether or not they acted.

## What is missing rather than slow

- **The entry detail page is reachable in one click** (confirmed — an earlier reading of an empty-vocabulary render
  suggested otherwise, and was wrong). But it is reachable only by clicking the entry's label; there is no entry
  *context* on the main page beyond label, core, string count and episode count.
- **`/ui/incidents` renders zero rows, permanently.** Not because nothing has gone wrong, but because nothing can:
  the monitor is never ticked in a running deployment (EXP-0016). A page that exists to show you a problem, and is
  structurally incapable of showing you one, is worse than no page — it reads as "all clear".
- **`/ui/slices`, `/ui/builds` and `/ui/failures` render zero rows** on a freshly seeded catalog. Empty is honest.
  But they are empty because **nothing in the browser can make them**.

## The browser can ingest and can curate. It cannot do anything else.

The complete set of `POST` routes the UI exposes is:

```
POST /ui/jobs                      POST /ui/jobs/{id}/cancel
POST /ui/vocabulary/accept         POST /ui/vocabulary/map
POST /ui/vocabulary/dismiss        POST /ui/vocabulary/entries
POST /ui/vocabulary/entries/{id}/rename | /notes | /merge | /split
POST /ui/vocabulary/events/{id}/undo
POST /ui/clusters/*                (all 303/410 — the frozen archive)
```

`POST /ui/jobs` builds exactly two request shapes: `ingest_source` (a path) and `ingest` (a synthetic ramp).
`api/app.py:660-694` is the whole handler. It cannot emit `validate`, `build` or `export`.

So the entire downstream half of the product is **unreachable from a browser**:

| Capability | API | UI |
|---|---|---|
| Ingest from a path | `POST /api/v1/jobs` | `POST /ui/jobs` |
| Validate against a profile | `POST /api/v1/jobs` (`type: validate`) | **nothing** |
| Build a content-addressed dataset | `POST /api/v1/jobs` (`type: build`) | **nothing** |
| Export a build as a LeRobot v3 dataset | `POST /api/v1/jobs` (`type: export`) | **nothing** |
| Save a curation slice | `POST /api/v1/slices` | **nothing** |
| Revalidate an episode | — | **nothing** |

An operator can get a bag into the catalog and can name the task strings inside it. They cannot then validate it,
build from it, or export it without leaving the browser and writing `curl`. Everything the README describes as the
product's output — reproducible, content-addressed, lineage-tracked LeRobot v3 datasets — is reachable only through
the JSON API.

This is the sharpest gap between the product's story and its surface anywhere in the program, and it is invisible to
every gate currently in place: the routes do not exist, so no route test fails; the API works, so the contract check
passes; and the UI audit visits the pages and finds them clean, because a page with zero rows is a valid page.

## Caveats

- Crawled against a seeded catalog, so page *contents* are representative but not exhaustive: a catalog with real
  MCAP metadata would have wider tables and more entry rows.
- Click counts measure navigation only. They deliberately exclude typing into fields, which is real work and is not
  counted anywhere in this record.
- The crawl follows rendered links; it cannot find a control that has no link (a `<button>` with no destination),
  so it is a lower bound on reachable surface, not a complete model of it.
- One operator was emulated, by reading the served HTML. No human was observed. This measures the *designed* cost of
  a workflow, not the variance in how long a real person takes.

## Reproduction

```bash
uv run python scripts/operator_walkthrough.py --json var/walkthrough.json
```

Roughly 90 seconds; crawls 104 pages.