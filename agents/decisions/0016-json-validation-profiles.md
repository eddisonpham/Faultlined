# 0016. Declarative Validation Profiles as Hash-Addressed JSON

- **Status:** accepted
- **Date (UTC):** 2026-09-29
- **Deciders:** implementer (agent) / owner

## Context

FR-002 requires each episode to be checked against a *declarative validation profile* — required
channels, schema, monotonic time, frequency bounds, NaN checks — with pass/fail and stable reason
codes. FR-003 adds quarantine and re-validation after remediation without re-ingesting bytes.
[components.md](../architecture/components.md) §6 specifies "declarative validation profiles (YAML,
versioned, hashed)" and a rule registry.

Two decisions were open. What the profile is *written in* — the architecture said YAML, which needs a
dependency the technology matrix never justified. And whether rules read the artifact bytes or the
metadata the reader already computed.

## Options considered

| Option | Pros | Cons |
|---|---|---|
| YAML profiles + PyYAML | Human-friendly comments in profiles; what the architecture said | A new runtime dependency for a file format nothing else in the stack uses |
| JSON profiles, stdlib only | Zero dependencies; matches LeRobot's own `meta/*.json`; canonical serialization is trivially stable for hashing | No comments in a profile |
| Hard-coded Python rule functions | No profile at all | Violates "declarative"; every consumer change becomes a code change; profile hash means nothing |
| Rules read artifact bytes | Validates the actual data | Re-reads and re-parses the file the reader just walked; duplicates the format logic in a second place |

## Decision

- **Profiles are JSON, not YAML.** LeRobot datasets already ship `meta/info.json` and the engine
  already hashes canonical JSON for idempotency and manifests, so JSON keeps one serialization
  discipline across the whole platform and adds no dependency. The documented deviation from
  components.md §6 is recorded here. A profile that needs a comment gets one at the level of the
  profile *name* in the catalog.
- **A profile is immutable and hash-addressed.** `ValidationProfile.hash` is the SHA-256 of its
  canonical JSON. Two profiles with the same hash are the same profile, which is what makes
  `validation_results` and build manifests reproducible.
- **A profile is validated when it is constructed**, not when it is used. A malformed profile is
  rejected at registration with `VALIDATION_PROFILE_INVALID`; there is no way to persist one.
- **Rules read the extraction, not the bytes.** A rule receives the reader's `EpisodeExtraction`
  (task, robot, frame count, duration, fps, per-channel stats) and the optional per-channel sample
  the caller provides. This keeps validation O(metadata) rather than O(file), and — more
  importantly — keeps a second implementation of the LeRobot layout from drifting out of sync with
  the reader. The trade-off is explicit below.
- **A rule that raises is a failure, not a pass.** Rule exceptions become `VALIDATION_RULE_ERROR` and
  the episode is quarantined (fail closed, per `failure-handling.md`).
- **Quarantine is a state on the episode row, not a delete.** `episodes.state` is
  `ingested | valid | quarantined`, and the reason codes live in an immutable
  `validation_results` row keyed by `(episode_id, profile_hash)`, so re-validating after a profile
  fix adds a result rather than overwriting history (FR-003).

## Consequences

- (+) No new dependency; one canonical-JSON discipline for profiles, manifests, and idempotency.
- (+) Validation is fast enough to run on every episode, which is the point of NFR-002.
- (−) Rules can only check what the reader surfaces. A per-frame check that needs the full column
  (for example "no two consecutive frames are identical") has to be a *reader* capability or an
  explicit read, not something a rule can do for free. The rule signature takes an optional
  per-channel sample so a caller that does want the column can pass it, but the default path
  does not read the file.
- (−) Stats-based checks inherit the format's published statistics when present. A dataset with
  wrong published stats would validate against wrong numbers. Recomputing is available by passing a
  sample; making it the default is a measured decision, not a guess.
- Profiles are engine-wide policy, so changing one changes the meaning of every result that cites
  it. Hash-addressing makes that visible rather than silent.

## Docs updated

- [../architecture/components.md](../architecture/components.md) (§6 — JSON deviation, implemented surface)
- [../architecture/storage.md](../architecture/storage.md) (§1 — `validation_profiles` / `validation_results` are now real)
- [../spec/requirements.md](../spec/requirements.md) (§4 — profile format is JSON)
- [../testing/failure-modes.md](../testing/failure-modes.md) (F2, F3)
- [../implementation/status.md](../implementation/status.md)
