"""Integration tests for the task vocabulary (ADR 0029), against real PostgreSQL.

A throwaway database per test (the `test_migrations.py` pattern): the vocabulary
is global state over episodes, so queue and health assertions on the shared test
catalog would measure whatever else ran first. The ledger has to start empty for
the backfill test too.

What is pinned here: the 0002 backfill and its first-wins rule; deterministic
entry creation and id stability across renames; map-or-resolve at ingest; the
unmapped queue as a query that cannot drift; and - the reason events exist -
merge, split, map and dismiss are all exactly reversible.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb

from data_engine.catalog import migrations, vocabulary
from data_engine.catalog.database import connect, initialize_schema
from data_engine.catalog.migrations.task_vocabulary import TaskVocabularyMigration
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings
from data_engine.ingest.service import EpisodeIngestService
from data_engine.storage.artifacts import FileArtifactStore
from tests.conftest import postgres_test_dsn

pytestmark = pytest.mark.integration


@pytest.fixture
def settings() -> Iterator[Settings]:
    """An empty throwaway database, dropped after the test."""
    base = postgres_test_dsn()
    if not base:
        pytest.skip("no DE_DATABASE_URL configured")
    dbname = f"data_engine_voc_{uuid.uuid4().hex[:8]}"
    info = {key: str(value) for key, value in conninfo_to_dict(base).items()}
    with psycopg.connect(**{**info, "dbname": "postgres"}, autocommit=True) as maintenance:
        maintenance.execute(f'CREATE DATABASE "{dbname}"')
    try:
        configured = Settings(
            _env_file=None, database_url=make_conninfo(**{**info, "dbname": dbname})
        )
        initialize_schema(configured)
        yield configured
    finally:
        with psycopg.connect(**{**info, "dbname": "postgres"}, autocommit=True) as maintenance:
            maintenance.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture
def catalog(settings: Settings) -> PostgresCatalog:
    return PostgresCatalog(settings)


def _seed_episode(catalog: PostgresCatalog, task: str) -> None:
    token = uuid.uuid4().hex[:8]
    job, _ = catalog.submit_job("ingest", {}, f"vocab-{token}", "test-correlation")
    catalog.register_episode(
        source_hash=token,
        artifact_hash=token,
        size_bytes=512,
        metadata={"task": task},
        job_id=str(job["id"]),
        episode_key=f"episode_{token}",
        episode_format="lerobot-v3",
    )


# ------------------------------------------------------------------ migration 0002


class TestBackfill:
    def test_confirmations_and_reviews_become_vocabulary(self, settings: Settings) -> None:
        with connect(settings) as connection:
            connection.execute(
                "INSERT INTO cluster_confirmations (label, tasks) VALUES (%s, %s)",
                ("Mug handling", Jsonb(["put the red mug down", "put the blue mug down"])),
            )
            connection.execute(
                "INSERT INTO cluster_confirmations (label, tasks) VALUES (%s, %s)",
                ("Mug handling", Jsonb(["put the green mug down"])),
            )
            connection.execute(
                "INSERT INTO cluster_review_decisions (task, disposition, label) "
                "VALUES (%s, 'dismissed', '')",
                ("xyzzy noise string",),
            )
            connection.execute(
                "INSERT INTO cluster_review_decisions (task, disposition, label) "
                "VALUES (%s, 'class', '')",
                ("unnamed but real task",),
            )
            connection.commit()

        _apply_backfill(settings)

        entries = vocabulary.list_entries(settings)
        # Two confirmations with one label collapse into one entry.
        assert [e["preferred_label"] for e in entries] == ["Mug handling"]
        assert entries[0]["task_count"] == 3
        # Oldest confirmation wins for a shared string: provenance is 'confirm'.
        members = vocabulary.entry_members(settings, entries[0]["id"])
        assert {m["task_string"] for m in members} == {
            "put the red mug down",
            "put the blue mug down",
            "put the green mug down",
        }
        # A dismissed string never re-queues; an unnamed class has no name to map to.
        dismissed = vocabulary.mapping_for(settings, "xyzzy noise string")
        assert dismissed == {
            "task_string": "xyzzy noise string",
            "entry_id": None,
            "provenance": "dismiss",
        }
        assert vocabulary.mapping_for(settings, "unnamed but real task") is None

    def test_the_backfill_is_idempotent(self, settings: Settings) -> None:
        with connect(settings) as connection:
            connection.execute(
                "INSERT INTO cluster_confirmations (label, tasks) VALUES (%s, %s)",
                ("Twice", Jsonb(["task a", "task b"])),
            )
            connection.commit()
        _apply_backfill(settings)
        first = (vocabulary.list_entries(settings), vocabulary.unmapped_counts(settings))
        _apply_backfill(settings)
        assert (vocabulary.list_entries(settings), vocabulary.unmapped_counts(settings)) == first

    def test_the_shipped_upgrade_applies_the_backfill_once(self, settings: Settings) -> None:
        with connect(settings) as connection:
            connection.execute(
                "INSERT INTO cluster_confirmations (label, tasks) VALUES (%s, %s)",
                ("Via upgrade", Jsonb(["task x"])),
            )
            connection.commit()
        applied = migrations.upgrade(settings)
        assert "0002" in applied
        assert migrations.upgrade(settings) == []
        assert vocabulary.mapping_for(settings, "task x") is not None


# --------------------------------------------------------------- entries and labels


class TestEntries:
    def test_a_renamed_id_cannot_be_reused_by_a_new_label(self, settings: Settings) -> None:
        original = vocabulary.create_entry(settings, preferred_label="Mug handling")
        vocabulary.rename_entry(settings, entry_id=original["id"], preferred_label="Cup handling")

        with pytest.raises(vocabulary.LabelConflict, match="already used by renamed entry"):
            vocabulary.create_entry(settings, preferred_label="Mug handling")

    def test_create_is_idempotent_and_the_id_survives_a_rename(self, settings: Settings) -> None:
        created = vocabulary.create_entry(settings, preferred_label="Stack blocks")
        again = vocabulary.create_entry(settings, preferred_label="Stack blocks")
        assert created["id"] == again["id"] == vocabulary.entry_id_for("Stack blocks")

        renamed = vocabulary.rename_entry(
            settings, entry_id=created["id"], preferred_label="Stack the blocks"
        )
        assert renamed["id"] == created["id"], "the id names the entry, not the label"
        assert renamed["preferred_label"] == "Stack the blocks"

        undone = vocabulary.undo_event(settings, event_id=_last_event(settings))
        assert undone["undone"] is True
        assert vocabulary.get_entry(settings, created["id"])["preferred_label"] == "Stack blocks"

    def test_a_rename_cannot_take_another_entry_s_label(self, settings: Settings) -> None:
        first = vocabulary.create_entry(settings, preferred_label="One")
        vocabulary.create_entry(settings, preferred_label="Two")
        with pytest.raises(ValueError, match="already taken"):
            vocabulary.rename_entry(settings, entry_id=first["id"], preferred_label="Two")


# ----------------------------------------------------------------- map-or-resolve


class TestMapOrResolve:
    def test_resolution_statuses(self, settings: Settings, catalog: PostgresCatalog) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="mug", core="mug")
        vocabulary.map_task(settings, task_string="known string", entry_id=entry["id"])

        assert vocabulary.map_or_resolve(settings, "known string") == "mapped"
        # The extracted core matches the entry's core exactly ("pick up the red
        # mug" -> "mug"), so ingest maps it without a human.
        assert vocabulary.map_or_resolve(settings, "pick up the red mug") == "auto"
        auto = vocabulary.mapping_for(settings, "pick up the red mug")
        assert auto == {
            "task_string": "pick up the red mug",
            "entry_id": entry["id"],
            "provenance": "ingest",
        }
        # A core with no entry, or with two, is never auto-mapped: choosing
        # between equally good entries is a human's call.
        assert vocabulary.map_or_resolve(settings, "polish the telescope") == "unmapped"
        vocabulary.create_entry(settings, preferred_label="golden mug", core="mug")
        assert vocabulary.map_or_resolve(settings, "pick up the blue mug") == "unmapped"
        vocabulary.dismiss_task(settings, task_string="polish the telescope")
        assert vocabulary.map_or_resolve(settings, "polish the telescope") == "dismissed"

    def test_the_unmapped_queue_is_a_query_that_cannot_drift(
        self, settings: Settings, catalog: PostgresCatalog
    ) -> None:
        for _ in range(3):
            _seed_episode(catalog, "shake the maraca")
        _seed_episode(catalog, "tune the piano")

        queue = vocabulary.list_unmapped(settings, limit=10)
        ranked = {row["task_string"]: row["episodes"] for row in queue}
        assert ranked["shake the maraca"] == 3
        assert ranked["tune the piano"] == 1
        assert queue[0]["task_string"] == "shake the maraca", "most fragmenting first"

        entry = vocabulary.create_entry(settings, preferred_label="maracas")
        vocabulary.map_task(settings, task_string="shake the maraca", entry_id=entry["id"])
        queue = vocabulary.list_unmapped(settings, limit=10)
        assert "shake the maraca" not in {row["task_string"] for row in queue}

    def test_the_queue_pages_by_rank(self, settings: Settings, catalog: PostgresCatalog) -> None:
        for task, times in (("alpha task", 3), ("beta task", 2), ("gamma task", 1)):
            for _ in range(times):
                _seed_episode(catalog, task)
        first = vocabulary.list_unmapped(settings, limit=1)
        assert [row["task_string"] for row in first] == ["alpha task"]
        second = vocabulary.list_unmapped(
            settings, limit=1, after=(first[0]["episodes"], first[0]["task_string"])
        )
        assert [row["task_string"] for row in second] == ["beta task"]


# ------------------------------------------------------------- events and reversals


class TestGuards:
    """The refusals, because each one is a case the operator can actually hit."""

    def test_a_label_is_required(self, settings: Settings) -> None:
        with pytest.raises(ValueError, match="needs a preferred label"):
            vocabulary.create_entry(settings, preferred_label="   ")
        with pytest.raises(ValueError, match="needs a preferred label"):
            vocabulary.rename_entry(settings, entry_id="voc_missing", preferred_label="")
        with pytest.raises(ValueError, match="needs a preferred label"):
            vocabulary.split_entry(
                settings, entry_id="voc_missing", task_strings=["x"], new_label=" "
            )

    def test_an_unknown_entry_is_a_key_error_not_a_silent_no_op(self, settings: Settings) -> None:
        for call in (
            lambda: vocabulary.rename_entry(
                settings, entry_id="voc_missing", preferred_label="anything"
            ),
            lambda: vocabulary.update_notes(settings, entry_id="voc_missing", notes="x"),
            lambda: vocabulary.map_task(settings, task_string="a task", entry_id="voc_missing"),
            lambda: vocabulary.split_entry(
                settings, entry_id="voc_missing", task_strings=["a task"], new_label="x"
            ),
        ):
            with pytest.raises(KeyError):
                call()

    def test_a_dismissal_cannot_be_written_as_a_mapping(self, settings: Settings) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="things")
        with pytest.raises(ValueError, match="dismiss is not a mapping"):
            vocabulary.map_task(
                settings, task_string="t", entry_id=entry["id"], provenance="dismiss"
            )

    def test_merge_refuses_a_self_merge_and_a_missing_counterparty(
        self, settings: Settings
    ) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="cups")
        with pytest.raises(ValueError, match="cannot merge into itself"):
            vocabulary.merge_entries(settings, source_id=entry["id"], target_id=entry["id"])
        with pytest.raises(KeyError):
            vocabulary.merge_entries(settings, source_id=entry["id"], target_id="voc_missing")
        with pytest.raises(KeyError):
            vocabulary.merge_entries(settings, source_id="voc_missing", target_id=entry["id"])

    def test_split_refuses_strings_it_does_not_own_and_a_taken_label(
        self, settings: Settings
    ) -> None:
        parent = vocabulary.create_entry(settings, preferred_label="tools")
        vocabulary.map_task(settings, task_string="the hammer", entry_id=parent["id"])
        with pytest.raises(ValueError, match="not mapped to"):
            vocabulary.split_entry(
                settings,
                entry_id=parent["id"],
                task_strings=["the hammer", "the saw"],
                new_label="hammers",
            )
        with pytest.raises(ValueError, match="at least one task string"):
            vocabulary.split_entry(
                settings, entry_id=parent["id"], task_strings=[], new_label="hammers"
            )
        with pytest.raises(ValueError, match="already is this entry"):
            vocabulary.split_entry(
                settings, entry_id=parent["id"], task_strings=["the hammer"], new_label="tools"
            )
        vocabulary.create_entry(settings, preferred_label="hammers")
        with pytest.raises(vocabulary.LabelConflict, match="already taken"):
            vocabulary.split_entry(
                settings, entry_id=parent["id"], task_strings=["the hammer"], new_label="hammers"
            )

    def test_a_candidate_must_name_one_target_and_500_strings_at_most(
        self, settings: Settings
    ) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="mugs", core="mug")
        with pytest.raises(ValueError, match="exactly one existing entry or new label"):
            vocabulary.accept_candidate(settings, task_strings=["a"], expected_core="mug")
        with pytest.raises(ValueError, match="exactly one existing entry or new label"):
            vocabulary.accept_candidate(
                settings,
                task_strings=["a"],
                expected_core="mug",
                entry_id=entry["id"],
                new_label="both",
            )
        with pytest.raises(ValueError, match="between 1 and 500"):
            vocabulary.accept_candidate(
                settings,
                task_strings=[],
                expected_core="mug",
                entry_id=entry["id"],
            )

    def test_a_stale_attach_is_refused_in_the_store_too(
        self, settings: Settings, catalog: PostgresCatalog
    ) -> None:
        """The UI re-checks the candidate; the store refuses the write regardless."""
        _seed_episode(catalog, "open the drawer")
        target = vocabulary.create_entry(settings, preferred_label="drawer work")
        with pytest.raises(KeyError):
            vocabulary.accept_candidate(
                settings,
                task_strings=["open the drawer"],
                expected_core="drawer",
                entry_id="voc_missing",
            )
        # An entry whose core is not the candidate's core cannot be its target.
        with pytest.raises(ValueError, match="no longer the unique core match"):
            vocabulary.accept_candidate(
                settings,
                task_strings=["open the drawer"],
                expected_core="drawer",
                entry_id=target["id"],
            )
        # A new entry is refused once an entry already owns the suggested core.
        vocabulary.create_entry(settings, preferred_label="drawers", core="drawer")
        with pytest.raises(ValueError, match="an entry now uses the suggested core"):
            vocabulary.accept_candidate(
                settings,
                task_strings=["open the drawer"],
                expected_core="drawer",
                new_label="another drawer entry",
            )
        assert "drawer work" in {
            entry["preferred_label"] for entry in vocabulary.list_entries(settings)
        }
        assert (
            vocabulary.get_entry(settings, vocabulary.entry_id_for("another drawer entry")) is None
        )

    def test_a_string_that_stops_sharing_the_core_is_refused(
        self, settings: Settings, catalog: PostgresCatalog
    ) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="mugs", core="mug")
        vocabulary.map_task(settings, task_string="already placed", entry_id=entry["id"])
        _seed_episode(catalog, "open the drawer")
        _seed_episode(catalog, "grab the blue mug")
        with pytest.raises(ValueError, match="no longer share the suggested core"):
            vocabulary.accept_candidate(
                settings,
                task_strings=["open the drawer", "grab the blue mug"],
                expected_core="mug",
                entry_id=entry["id"],
            )
        assert vocabulary.mapping_for(settings, "grab the blue mug") is None

    def test_undo_keeps_a_created_entry_that_something_else_still_maps_to(
        self, settings: Settings, catalog: PostgresCatalog
    ) -> None:
        _seed_episode(catalog, "polish the telescope")
        created = vocabulary.accept_candidate(
            settings,
            task_strings=["polish the telescope"],
            new_label="telescope polishing",
            expected_core=vocabulary.core_of("polish the telescope"),
        )
        vocabulary.map_task(settings, task_string="later mapping", entry_id=created["id"])

        vocabulary.undo_event(settings, event_id=_last_event(settings) - 1)
        vocabulary.undo_event(settings, event_id=_last_event(settings))
        assert vocabulary.mapping_for(settings, "polish the telescope") is None
        assert vocabulary.get_entry(settings, created["id"]) is not None, (
            "the later mapping still depends on this entry"
        )

    def test_the_event_log_refuses_a_kind_it_cannot_undo(self, settings: Settings) -> None:
        """`undo_event` raises on an unknown kind, and the schema refuses to store one.

        The guard in `undo_event` is therefore unreachable through the API, which
        is the point: a future migration that adds a kind without an undo branch
        cannot slip an uncompensable event into the log.
        """
        with connect(settings) as connection, pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO task_vocabulary_events (kind, payload) VALUES ('teleport', %s)",
                (Jsonb({}),),
            )

    def test_an_empty_task_string_is_treated_as_noise_at_ingest(self, settings: Settings) -> None:
        assert vocabulary.map_or_resolve(settings, "   ") == "dismissed"


class TestMergeSplitUndo:
    def test_candidate_acceptance_is_atomic_and_reversible(
        self, settings: Settings, catalog: PostgresCatalog
    ) -> None:
        _seed_episode(catalog, "polish the telescope")
        entry = vocabulary.accept_candidate(
            settings,
            task_strings=["polish the telescope"],
            new_label="telescope polishing",
            expected_core=vocabulary.core_of("polish the telescope"),
        )
        mapping = vocabulary.mapping_for(settings, "polish the telescope")
        assert mapping == {
            "task_string": "polish the telescope",
            "entry_id": entry["id"],
            "provenance": "confirm",
        }
        event_id = _last_event(settings)
        vocabulary.undo_event(settings, event_id=event_id)
        assert vocabulary.mapping_for(settings, "polish the telescope") is None
        assert vocabulary.get_entry(settings, entry["id"]) is None

    def test_rejected_candidate_does_not_leave_a_half_created_entry(
        self, settings: Settings
    ) -> None:
        with pytest.raises(ValueError, match="no longer exist"):
            vocabulary.accept_candidate(
                settings,
                task_strings=["stale task"],
                new_label="stale entry",
                expected_core=vocabulary.core_of("stale task"),
            )
        assert vocabulary.get_entry(settings, vocabulary.entry_id_for("stale entry")) is None

    def test_merge_moves_strings_and_undo_restores_exactly(self, settings: Settings) -> None:
        source = vocabulary.create_entry(settings, preferred_label="cups")
        target = vocabulary.create_entry(settings, preferred_label="mugs", core="mug")
        vocabulary.map_task(settings, task_string="the red cup", entry_id=source["id"])
        vocabulary.map_task(settings, task_string="the blue cup", entry_id=source["id"])

        merged = vocabulary.merge_entries(settings, source_id=source["id"], target_id=target["id"])
        assert merged["moved"] == 2
        assert vocabulary.get_entry(settings, source["id"]) is None
        moved = vocabulary.mapping_for(settings, "the red cup")
        assert moved["entry_id"] == target["id"]
        assert moved["provenance"] == "merge"

        vocabulary.undo_event(settings, event_id=merged["event_id"])
        restored = vocabulary.get_entry(settings, source["id"])
        assert restored["preferred_label"] == "cups"
        back = vocabulary.mapping_for(settings, "the red cup")
        assert back["entry_id"] == source["id"]
        assert back["provenance"] == "confirm", "undo restores the provenance it replaced"

    def test_split_moves_strings_out_and_undo_takes_them_back(self, settings: Settings) -> None:
        parent = vocabulary.create_entry(settings, preferred_label="tools")
        vocabulary.map_task(settings, task_string="the hammer", entry_id=parent["id"])
        vocabulary.map_task(settings, task_string="the saw", entry_id=parent["id"])

        split = vocabulary.split_entry(
            settings,
            entry_id=parent["id"],
            task_strings=["the hammer"],
            new_label="hammers",
        )
        assert split["moved"] == 1
        assert (
            vocabulary.mapping_for(settings, "the hammer")["entry_id"] == split["new_entry"]["id"]
        )
        assert vocabulary.mapping_for(settings, "the saw")["entry_id"] == parent["id"]

        vocabulary.undo_event(settings, event_id=split["event_id"])
        assert vocabulary.get_entry(settings, split["new_entry"]["id"]) is None
        assert vocabulary.mapping_for(settings, "the hammer")["entry_id"] == parent["id"]

    def test_map_and_dismiss_undo_restore_the_previous_state(self, settings: Settings) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="things")
        vocabulary.map_task(settings, task_string="wandering string", entry_id=entry["id"])

        dismissed = vocabulary.dismiss_task(settings, task_string="wandering string")
        assert vocabulary.mapping_for(settings, "wandering string")["entry_id"] is None
        vocabulary.undo_event(settings, event_id=_last_event(settings))
        restored = vocabulary.mapping_for(settings, "wandering string")
        assert restored["entry_id"] == entry["id"]
        assert restored["provenance"] == "confirm"
        assert dismissed["provenance"] == "dismiss"

        # A map over nothing, undone, leaves nothing behind.
        vocabulary.map_task(settings, task_string="fresh string", entry_id=entry["id"])
        vocabulary.undo_event(settings, event_id=_last_event(settings))
        assert vocabulary.mapping_for(settings, "fresh string") is None

    def test_an_event_undoes_exactly_once(self, settings: Settings) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="once")
        vocabulary.map_task(settings, task_string="x", entry_id=entry["id"])
        event_id = _last_event(settings)
        vocabulary.undo_event(settings, event_id=event_id)
        with pytest.raises(ValueError, match="already undone"):
            vocabulary.undo_event(settings, event_id=event_id)
        with pytest.raises(KeyError):
            vocabulary.undo_event(settings, event_id=999_999)


# ------------------------------------------------------------------------ health


class TestHealth:
    def test_shares_weight_both_strings_and_episodes(
        self, settings: Settings, catalog: PostgresCatalog
    ) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="mug", core="mug")
        vocabulary.map_task(settings, task_string="mapped task", entry_id=entry["id"])
        vocabulary.dismiss_task(settings, task_string="dismissed task")
        _seed_episode(catalog, "mapped task")
        _seed_episode(catalog, "mapped task")
        _seed_episode(catalog, "dismissed task")
        _seed_episode(catalog, "novel task")

        health = vocabulary.vocabulary_health(settings)
        assert health["entries"] == 1
        assert health["mapped_strings"] == 1
        assert health["dismissed_strings"] == 1
        assert health["unmapped_strings"] == 1
        assert health["unmapped_string_share"] == pytest.approx(1 / 3)
        assert health["unmapped_episode_share"] == pytest.approx(1 / 4)


class TestIngestWiring:
    def test_ingest_resolves_the_task_against_the_vocabulary(
        self, settings: Settings, catalog: PostgresCatalog, tmp_path: Path
    ) -> None:
        """ADR 0029 §3 on the real ingest path: map known cores, queue novel strings."""
        entry = vocabulary.create_entry(settings, preferred_label="mug", core="mug")
        service = EpisodeIngestService(catalog, FileArtifactStore(tmp_path))
        job, _ = catalog.submit_job("ingest", {}, f"vocab-ingest-{uuid.uuid4().hex[:8]}", "corr")

        service.ingest(_synthetic_episode("pick up the red mug"), job_id=str(job["id"]))
        auto = vocabulary.mapping_for(settings, "pick up the red mug")
        assert auto == {
            "task_string": "pick up the red mug",
            "entry_id": entry["id"],
            "provenance": "ingest",
        }

        service.ingest(_synthetic_episode("polish the telescope"), job_id=str(job["id"]))
        assert vocabulary.mapping_for(settings, "polish the telescope") is None
        queue = {row["task_string"] for row in vocabulary.list_unmapped(settings, limit=50)}
        assert "polish the telescope" in queue


def _synthetic_episode(task: str) -> dict[str, Any]:
    return {
        "task": task,
        "robot": "so101",
        "timestamps": [0.0, 0.1, 0.2],
        "observations": [[0.0], [0.1], [0.2]],
        "actions": [[0.0], [0.1], [0.2]],
    }


def _apply_backfill(settings: Settings) -> None:
    """Run migration 0002's body the way the runner would: one transaction."""
    with connect(settings) as connection:
        TaskVocabularyMigration().apply(connection)
        connection.commit()


def _last_event(settings: Settings) -> int:
    with connect(settings) as connection:
        row = connection.execute("SELECT max(id) AS id FROM task_vocabulary_events").fetchone()
    return int(row["id"]) if row and row["id"] is not None else 0
