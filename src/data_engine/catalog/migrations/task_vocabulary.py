"""Migration 0002: the task vocabulary, backfilled from the cluster archive (ADR 0029).

Schema-only changes are idempotent DDL and belong in the baseline; this
migration exists for what idempotent DDL cannot express - the **one-time
backfill** that turns the frozen cluster surface's confirmations and review
dispositions into vocabulary entries and mappings.

Backfill rules, stated because "first wins" is a decision:

- One entry per distinct confirmation label (id derived from the label, so two
  confirmations with the same label collapse into one entry).
- One mapping per task string, provenance `confirm`. When several confirmations
  cover the same string, the **oldest** wins (rows are read in id order); a
  later confirmation cannot silently move a string an operator already placed.
- Review dispositions: `dismissed` becomes a dismissal mapping; `class` with a
  label becomes an entry + confirm mapping; `class` without a label is left
  unmapped on purpose - it has no name to map to, and inventing one would put a
  label nobody wrote into the vocabulary.

The backfill writes no events: it is a reconstruction of history, not an
operation, and its provenance is already recorded on every mapping row.
Running it twice changes nothing (`ON CONFLICT DO NOTHING` throughout).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from psycopg import Connection

from data_engine.catalog.vocabulary import core_of, entry_id_for

# Self-contained: the same idempotent DDL as the baseline, so `de migrate` can
# run before any process has started against the database.
_DDL = """
CREATE TABLE IF NOT EXISTS task_vocabulary_entries (
    id text PRIMARY KEY,
    preferred_label text NOT NULL UNIQUE,
    core text NOT NULL DEFAULT '',
    notes text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT 'operator'
);

CREATE TABLE IF NOT EXISTS task_vocabulary_mappings (
    task_string text PRIMARY KEY,
    entry_id text REFERENCES task_vocabulary_entries(id) ON DELETE CASCADE,
    provenance text NOT NULL
        CHECK (provenance IN ('ingest', 'confirm', 'merge', 'split', 'dismiss')),
    mapped_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT task_vocabulary_mappings_target
        CHECK ((provenance = 'dismiss') = (entry_id IS NULL))
);

CREATE INDEX IF NOT EXISTS task_vocabulary_mappings_entry_idx
    ON task_vocabulary_mappings (entry_id);

CREATE TABLE IF NOT EXISTS task_vocabulary_events (
    id bigserial PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('merge', 'split', 'label', 'map', 'dismiss')),
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL DEFAULT 'operator',
    undone_at timestamptz
);
"""

_ENTRY_SQL = """
INSERT INTO task_vocabulary_entries (id, preferred_label, core, created_by)
VALUES (%s, %s, %s, 'migration')
ON CONFLICT (id) DO NOTHING
"""

_MAPPING_SQL = """
INSERT INTO task_vocabulary_mappings (task_string, entry_id, provenance)
VALUES (%s, %s, %s)
ON CONFLICT (task_string) DO NOTHING
"""

_DISMISS_SQL = """
INSERT INTO task_vocabulary_mappings (task_string, entry_id, provenance)
VALUES (%s, NULL, 'dismiss')
ON CONFLICT (task_string) DO NOTHING
"""


@dataclass(frozen=True, slots=True)
class TaskVocabularyMigration:
    """Version 0002: vocabulary tables plus the one-time archive backfill."""

    version: str = "0002"
    name: str = "task_vocabulary"

    def apply(self, connection: Connection[Any]) -> None:
        for statement in _DDL.split(";"):
            if statement.strip():
                connection.execute(statement)
        self._backfill_confirmations(connection)
        self._backfill_reviews(connection)

    @staticmethod
    def _backfill_confirmations(connection: Connection[Any]) -> None:
        rows = connection.execute(
            "SELECT label, tasks FROM cluster_confirmations ORDER BY id"
        ).fetchall()
        for row in rows:
            label = str(row["label"]).strip()
            if not label:
                continue
            connection.execute(_ENTRY_SQL, (entry_id_for(label), label, core_of(label)))
            for task in cast(list[object], row["tasks"] or []):
                task_string = str(task)
                if task_string:
                    connection.execute(_MAPPING_SQL, (task_string, entry_id_for(label), "confirm"))

    @staticmethod
    def _backfill_reviews(connection: Connection[Any]) -> None:
        rows = connection.execute(
            "SELECT task, disposition, label FROM cluster_review_decisions"
        ).fetchall()
        for row in rows:
            task_string = str(row["task"])
            if not task_string:
                continue
            if str(row["disposition"]) == "dismissed":
                connection.execute(_DISMISS_SQL, (task_string,))
                continue
            label = str(row["label"]).strip()
            if not label:
                continue
            connection.execute(_ENTRY_SQL, (entry_id_for(label), label, core_of(label)))
            connection.execute(_MAPPING_SQL, (task_string, entry_id_for(label), "confirm"))
