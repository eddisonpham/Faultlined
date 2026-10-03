# Runbook: Backup and Restore

Scope: the state that makes a Faultlined install worth keeping. Everything here runs
under Git Bash on the host described in [agents/spec/environment.md](../../agents/spec/environment.md)
— no Docker, no remote services. The tier this supports is **Production-like local**
([agents/architecture/deployment.md](../../agents/architecture/deployment.md)).

## What actually holds state

| State | Where | Backup method | Why that method is enough |
|---|---|---|---|
| Catalog (jobs, episodes, builds, lineage, manifests) | PostgreSQL, `DE_DATABASE_URL` | `pg_dump --format=custom` | The schema is code (`initialize_schema`); only rows are irreplaceable |
| Artifacts (content-addressed blobs) | `DE_ARTIFACT_ROOT` (default `./var/artifacts`) | file-level copy | Blobs are immutable and named by their own hash ([ADR 0006](../../agents/decisions/0006-storage-and-formats.md)), so a copy is always consistent and a re-copy never rewrites bytes |
| Build exports (LeRobot v3 datasets) | `DE_EXPORT_ROOT` (default `./var/exports`) | file-level copy | Same: `DE_EXPORT_ROOT/{build_hash}` is written atomically and then read-only |
| Metrics sink | `DE_METRICS_PATH` (default `./var/metrics/runtime.jsonl`) | **not backed up** | Telemetry is disposable. Losing it loses dashboards, not data |
| `var/pgdata` (the isolated `just pg-up` cluster) | gitignored | **not copied** | Copying a live `pgdata` is not a backup. `pg_dump` is the backup; `just pg-down` / `just pg-up` rebuilds the cluster |

There is no other durable state. Sessions, locks, and caches are derived.

## Finding the PostgreSQL binaries

`pg_dump` is **not on the Git Bash `PATH`** on a stock Windows PostgreSQL install, so a
bare `pg_dump` fails with "command not found" and that is expected, not a broken setup.
Resolve the bin directory the same way `scripts/dev_postgres.py` does — it looks at
`$PGBIN`, then `PATH`, then `C:/Program Files/PostgreSQL/*/bin`:

```bash
export PGBIN="${PGBIN:-$(ls -d "/c/Program Files/PostgreSQL"/*/bin 2>/dev/null | sort -V | tail -1)}"
"$PGBIN/pg_dump" --version   # sanity check before relying on it
```

The DSN comes from the environment only (ADR
[0002](../../agents/decisions/0002-secrets-handling.md)); `just` loads it from `.env`, or
export it yourself. Never paste it into a file or a command that gets logged.

## Backup

```bash
set -euo pipefail
STAMP=$(date +%Y%m%dT%H%M%S)
mkdir -p var/backups

# 1. Catalog. Custom format: compressed, and restorable table-by-table.
"$PGBIN/pg_dump" --format=custom --file "var/backups/catalog-$STAMP.dump" "$DE_DATABASE_URL"

# 2. Artifacts and exports. Immutable trees, so a plain recursive copy is correct.
cp -a "$DE_ARTIFACT_ROOT" "var/backups/artifacts-$STAMP"
[ -d "${DE_EXPORT_ROOT:-./var/exports}" ] && cp -a "$DE_EXPORT_ROOT" "var/backups/exports-$STAMP" || true

# 3. Prove the dump is loadable, not merely present. A zero-byte dump is a
#    successful pg_dump of an empty database and looks identical on disk.
"$PGBIN/pg_restore" --list "var/backups/catalog-$STAMP.dump" | head
```

Point 3 is the verification step. `--list` parses the archive's table of contents; if it
lists your tables, the dump is structurally sound. A nightly cron entry is a one-liner
around the block above — see "Scheduling" at the end.

## Restore

Restoring is the operation that matters, so it is written out in full. Target an empty
database: `pg_restore` into a populated one conflicts on primary keys.

```bash
set -euo pipefail
STAMP=20261001T120000        # the backup you are restoring

# 1. Stop the writers. A worker that claims a job while the catalog is being
#    swapped will write into whichever schema it opened first.
just stop

# 2. Recreate the database. The schema comes from the dump, not from
#    initialize_schema, so the columns match the code that wrote them.
"$PGBIN/dropdb"   --if-exists "$DE_DATABASE_URL"
"$PGBIN/createdb" "$DE_DATABASE_URL"
"$PGBIN/pg_restore" --dbname "$DE_DATABASE_URL" --no-owner "var/backups/catalog-$STAMP.dump"

# 3. Put the artifact tree back. Content addressing means this is a copy; if a
#    blob is already present and hashes correctly, skip it.
rm -rf "$DE_ARTIFACT_ROOT"
cp -a "var/backups/artifacts-$STAMP" "$DE_ARTIFACT_ROOT"

# 4. Start up and check the catalog actually answers.
just run &
just doctor
curl -s localhost:8000/api/v1/status
```

`just doctor` connects and initializes the schema, so it is the cheapest proof that the
restored dump is usable. `/api/v1/status` then proves the API can count jobs and
episodes from it.

### Verifying artifact integrity after a restore

Every artifact row records a manifest with a content hash, so a restore that lost or
truncated a blob is detectable rather than silent:

```bash
# One episode's manifest, and the blob path it names.
curl -s localhost:8000/api/v1/episodes/<episode_id> | python -m json.tool | head -40
ls -l "$DE_ARTIFACT_ROOT"
```

Walk every referenced blob and re-hash it to check the whole tree at once:

```bash
find "$DE_ARTIFACT_ROOT" -type f -name '*.bin' -print0 \
  | xargs -0 sha256sum > var/backups/artifact-hashes.txt
```

A file whose name already is its hash prefix but whose `sha256sum` disagrees means a
truncated copy. Re-copy that file from the backup; nothing else needs to move.

## Recovery decision table

| Symptom | Most likely cause | Action |
|---|---|---|
| `just run` starts but every job stays `queued` | no worker (worker is a child of `de dev`, so a dead parent takes it with it — F5) | see [worker-operations.md](worker-operations.md) |
| API returns 503 with `Retry-After: 5` | Postgres unreachable; handler emits `event: database_unavailable` | `just pg-status`; restart the cluster with `just pg-down && just pg-up` |
| `pg_restore` fails on "already exists" | restoring into a populated database | drop and recreate first (step 2 above) |
| Episodes present, artifacts 404 on fetch | catalog restored, artifact tree not | re-run step 3; re-hash as above |
| Artifacts present, `/ui` empty | artifact tree restored, catalog not | re-run steps 1–2 |
| `just stop` refuses to kill a process | something unrelated holds the port | `netstat -ano \| findstr :8000`, identify it, leave it alone |

## Scheduling

Nothing in this project schedules itself; that is a deliberate stage-3 boundary recorded
in [deployment.md](../../agents/architecture/deployment.md). Two honest options:

- **Windows Task Scheduler** (the documented production-like path): a daily task running
  the backup block through `bash.exe -lc`. No service wrapper is required, and the
  scheduler's own retry/log is the audit trail.
- **Git Bash + a `while true; do ...; sleep 86400; done` loop** for a laptop that is
  simply left on. Fine for a single machine, and it dies with the shell — which is
  visible, unlike a cron entry that silently stops running.

Whichever is used, the nightly run must also copy `var/backups/` off the machine. A
backup on the same disk as the data is not a backup.

## Related

- [worker-operations.md](worker-operations.md) — restarting writers, reaping stranded jobs
- [../../agents/architecture/deployment.md](../../agents/architecture/deployment.md) — tiers and configuration
- [../../agents/architecture/storage.md](../../agents/architecture/storage.md) — content addressing and atomic publish
- [../../agents/testing/failure-modes.md](../../agents/testing/failure-modes.md) — F9 (disk full), F10 (database loss)
