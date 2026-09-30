"""Wipe the development catalog, its artifacts, and its metrics log.

`just run` has no "start over", and the first-run panel on `/ui` is gated on a
genuinely empty system - so re-running the demo needs a way back to empty that is
not a pile of ad-hoc `DELETE FROM` statements typed against whatever table names
happened to exist that week.

Two things this script refuses to do, because the failure mode is not recoverable
locally:

- **Touch a `*_test` database.** The test suite owns those. Resetting the
  development catalog must never be a way to lose a test run.
- **Run without `--yes`.** It deletes data. A script that can wipe a catalog on
  invocation is a footgun in a repository where `just` recipes are muscle memory.

It truncates rather than drops the schema, so the catalog keeps its shape and
`initialize_schema` is not what rebuilds it - the tables are the same ones the
running system created, which is the point of a reset.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from data_engine.config import load_settings
from data_engine.storage.artifacts import FileArtifactStore

TEST_DB_SUFFIX = "_test"


def catalog_tables(cursor: psycopg.Cursor[tuple[object, ...]]) -> list[str]:
    cursor.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename"
    )
    return [str(row[0]) for row in cursor.fetchall()]


def wipe_catalog(dsn: str) -> list[str]:
    with psycopg.connect(dsn, autocommit=True) as connection, connection.cursor() as cursor:
        names = catalog_tables(cursor)
        if not names:
            return []
        quoted = ", ".join(f'"{name}"' for name in names)
        cursor.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
    return names


def wipe_path(path: Path) -> int:
    """Remove a file or directory tree; return how many entries went."""
    if not path.exists():
        return 0
    if path.is_dir():
        count = sum(1 for _ in path.rglob("*"))
        shutil.rmtree(path)
        return count
    path.unlink()
    return 1


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="reset-data")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required: this deletes every job, episode, artifact and metric on record",
    )
    parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="empty the catalog but leave the content-addressed blobs on disk",
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    dsn = settings.database_url.get_secret_value()
    database = conninfo_to_dict(dsn).get("dbname", "")
    if database.endswith(TEST_DB_SUFFIX):
        raise SystemExit(
            f"refusing to reset {database!r}: the test suite owns {TEST_DB_SUFFIX} databases. "
            "Point DE_DATABASE_URL at the development database."
        )
    if not args.yes:
        raise SystemExit(
            f"this empties the catalog in {database!r} and deletes its artifacts and metrics. "
            "Re-run with --yes if that is what you want."
        )

    tables = wipe_catalog(dsn)
    store = FileArtifactStore(settings.artifact_root)
    removed = 0 if args.keep_artifacts else wipe_path(store.root)
    metrics = wipe_path(settings.metrics_path)

    print(f"catalog  {database}: truncated {len(tables)} table(s)")
    if tables:
        print(f"          {', '.join(tables)}")
    if not args.keep_artifacts:
        print(f"artifacts {store.root}: removed {removed} entr(ies)")
    print(f"metrics   {settings.metrics_path}: removed {metrics} entr(ies)")
    print("done. `just run` will now show the first-run ingest panel on /ui.")


if __name__ == "__main__":
    sys.exit(main())
