"""Integration tests for the task vocabulary (ADR 0029), against real PostgreSQL."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
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
        assert [e["preferred_label"] for e in entries] == ["Mug handling"]
        assert entries[0]["task_count"] == 3
        members = vocabulary.entry_members(settings, entries[0]["id"])
        assert {m["task_string"] for m in members} == {
            "put the red mug down",
            "put the blue mug down",
            "put the green mug down",
        }
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


class TestMapOrResolve:
    def test_resolution_statuses(self, settings: Settings, catalog: PostgresCatalog) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="mug", core="mug")
        vocabulary.map_task(settings, task_string="known string", entry_id=entry["id"])

        assert vocabulary.map_or_resolve(settings, "known string") == "mapped"
        assert vocabulary.map_or_resolve(settings, "pick up the red mug") == "auto"
        auto = vocabulary.mapping_for(settings, "pick up the red mug")
        assert auto == {
            "task_string": "pick up the red mug",
            "entry_id": entry["id"],
            "provenance": "ingest",
        }
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
        with pytest.raises(ValueError, match="no longer the unique core match"):
            vocabulary.accept_candidate(
                settings,
                task_strings=["open the drawer"],
                expected_core="drawer",
                entry_id=target["id"],
            )
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
        """`undo_event` raises on an unknown kind, and the schema refuses to store one."""
        with connect(settings) as connection, pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO task_vocabulary_events (kind, payload) VALUES ('teleport', %s)",
                (Jsonb({}),),
            )

    def test_an_empty_task_string_is_treated_as_noise_at_ingest(self, settings: Settings) -> None:
        assert vocabulary.map_or_resolve(settings, "   ") == "dismissed"


class TestConcurrentWriters:
    """Two writers at once, over real connections on real PostgreSQL."""

    @staticmethod
    def _race(*calls: Callable[[], Any]) -> list[Any]:
        """Run the calls at once and return each one's outcome."""
        start = threading.Barrier(len(calls))
        results: list[Any] = [None] * len(calls)

        def run(index: int, call: Callable[[], Any]) -> None:
            start.wait(timeout=10)
            try:
                results[index] = ("ok", call())
            except Exception as error:
                results[index] = ("raised", error)

        threads = [
            threading.Thread(target=run, args=(index, call), daemon=True)
            for index, call in enumerate(calls)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads), "a writer did not finish"
        return results

    def test_two_acceptances_of_one_candidate_produce_one_commit(
        self, settings: Settings, catalog: PostgresCatalog
    ) -> None:
        """The loser of the race must refuse, not write half a second copy."""
        for task in ("pick up the red telescope", "grab the blue telescope"):
            _seed_episode(catalog, task)
        tasks = ["pick up the red telescope", "grab the blue telescope"]
        core = vocabulary.core_of("pick up the red telescope")
        assert all(vocabulary.core_of(task) == core for task in tasks)

        outcomes = self._race(
            lambda: vocabulary.accept_candidate(
                settings, task_strings=tasks, new_label="telescope care", expected_core=core
            ),
            lambda: vocabulary.accept_candidate(
                settings, task_strings=tasks, new_label="telescope care", expected_core=core
            ),
        )
        assert [kind for kind, _ in outcomes].count("ok") == 1, outcomes
        refused = next(value for kind, value in outcomes if kind == "raised")
        assert isinstance(refused, ValueError)
        assert "no longer unmapped" in str(refused), refused

        labels = [entry["preferred_label"] for entry in vocabulary.list_entries(settings)]
        assert labels == ["telescope care"], labels
        entry_id = vocabulary.get_entry(settings, vocabulary.entry_id_for("telescope care"))["id"]
        for task in tasks:
            assert vocabulary.mapping_for(settings, task) == {
                "task_string": task,
                "entry_id": entry_id,
                "provenance": "confirm",
            }
        assert (
            len([e for e in vocabulary.list_events(settings, limit=50) if e["kind"] == "map"]) == 2
        )

    def test_concurrent_creates_of_one_label_agree_on_one_entry(self, settings: Settings) -> None:
        outcomes = self._race(
            lambda: vocabulary.create_entry(settings, preferred_label="mug handling"),
            lambda: vocabulary.create_entry(settings, preferred_label="mug handling"),
        )
        assert [kind for kind, _ in outcomes] == ["ok", "ok"], outcomes
        ids = {value["id"] for _kind, value in outcomes}
        assert len(ids) == 1, ids
        assert len(vocabulary.list_entries(settings)) == 1

    def test_two_renames_onto_one_label_produce_one_winner_and_one_conflict(
        self, settings: Settings
    ) -> None:
        first = vocabulary.create_entry(settings, preferred_label="first")
        second = vocabulary.create_entry(settings, preferred_label="second")

        outcomes = self._race(
            lambda: vocabulary.rename_entry(
                settings, entry_id=first["id"], preferred_label="shared name"
            ),
            lambda: vocabulary.rename_entry(
                settings, entry_id=second["id"], preferred_label="shared name"
            ),
        )
        kinds = [kind for kind, _ in outcomes]
        assert kinds.count("ok") == 1, outcomes
        refused = next(value for kind, value in outcomes if kind == "raised")
        assert isinstance(refused, vocabulary.LabelConflict), refused
        labels = sorted(entry["preferred_label"] for entry in vocabulary.list_entries(settings))
        assert labels.count("shared name") == 1, labels

    def test_two_writers_moving_one_string_leave_a_reversible_history(
        self, settings: Settings
    ) -> None:
        """Last writer wins by design, and the winner's undo restores the loser."""
        first = vocabulary.create_entry(settings, preferred_label="mugs")
        second = vocabulary.create_entry(settings, preferred_label="goblets")
        task = "the red cup"

        outcomes = self._race(
            lambda: vocabulary.map_task(settings, task_string=task, entry_id=first["id"]),
            lambda: vocabulary.map_task(settings, task_string=task, entry_id=second["id"]),
        )
        assert [kind for kind, _ in outcomes] == ["ok", "ok"], outcomes
        final = vocabulary.mapping_for(settings, task)
        assert final["entry_id"] in {first["id"], second["id"]}

        winner = second if final["entry_id"] == second["id"] else first
        events = [e for e in vocabulary.list_events(settings, limit=10) if e["kind"] == "map"]
        assert len(events) == 2
        latest = max(events, key=lambda event: event["id"])
        assert latest["payload"]["entry_id"] == winner["id"]
        assert latest["payload"]["previous"]["entry_id"] in {first["id"], second["id"]}

        vocabulary.undo_event(settings, event_id=latest["id"])
        restored = vocabulary.mapping_for(settings, task)
        assert restored["entry_id"] != winner["id"]
        assert restored["entry_id"] in {first["id"], second["id"]}


class TestStaleUndo:
    """An undo that would replay over a newer decision is refused, not forced."""

    def test_a_map_that_was_mapped_again_cannot_be_undone(self, settings: Settings) -> None:
        first = vocabulary.create_entry(settings, preferred_label="mugs")
        second = vocabulary.create_entry(settings, preferred_label="goblets")
        vocabulary.map_task(settings, task_string="the red cup", entry_id=first["id"])
        stale_event = _last_event(settings)
        vocabulary.map_task(settings, task_string="the red cup", entry_id=second["id"])

        with pytest.raises(ValueError, match="no longer applies"):
            vocabulary.undo_event(settings, event_id=stale_event)
        assert vocabulary.mapping_for(settings, "the red cup") == {
            "task_string": "the red cup",
            "entry_id": second["id"],
            "provenance": "confirm",
        }

    def test_a_merge_whose_strings_moved_on_cannot_be_undone(self, settings: Settings) -> None:
        source = vocabulary.create_entry(settings, preferred_label="cups")
        target = vocabulary.create_entry(settings, preferred_label="mugs")
        elsewhere = vocabulary.create_entry(settings, preferred_label="goblets")
        vocabulary.map_task(settings, task_string="the red cup", entry_id=source["id"])
        merge = vocabulary.merge_entries(settings, source_id=source["id"], target_id=target["id"])
        vocabulary.map_task(settings, task_string="the red cup", entry_id=elsewhere["id"])

        with pytest.raises(ValueError, match="no longer applies"):
            vocabulary.undo_event(settings, event_id=merge["event_id"])
        assert vocabulary.get_entry(settings, source["id"]) is None
        assert vocabulary.mapping_for(settings, "the red cup")["entry_id"] == elsewhere["id"]

    def test_a_merge_undo_keeps_edits_made_after_the_merge(self, settings: Settings) -> None:
        """The legitimate case: undoing the merge must not disturb later work."""
        source = vocabulary.create_entry(settings, preferred_label="cups")
        target = vocabulary.create_entry(settings, preferred_label="mugs")
        vocabulary.map_task(settings, task_string="the red cup", entry_id=source["id"])
        merge = vocabulary.merge_entries(settings, source_id=source["id"], target_id=target["id"])
        vocabulary.map_task(settings, task_string="a blue one", entry_id=target["id"])

        vocabulary.undo_event(settings, event_id=merge["event_id"])
        assert vocabulary.mapping_for(settings, "the red cup")["entry_id"] == source["id"]
        assert vocabulary.mapping_for(settings, "a blue one")["entry_id"] == target["id"]

    def test_a_split_whose_strings_moved_on_cannot_be_undone(self, settings: Settings) -> None:
        parent = vocabulary.create_entry(settings, preferred_label="tools")
        elsewhere = vocabulary.create_entry(settings, preferred_label="hammers, rehomed")
        vocabulary.map_task(settings, task_string="the hammer", entry_id=parent["id"])
        split = vocabulary.split_entry(
            settings, entry_id=parent["id"], task_strings=["the hammer"], new_label="hammers"
        )
        vocabulary.map_task(settings, task_string="the hammer", entry_id=elsewhere["id"])

        with pytest.raises(ValueError, match="no longer applies"):
            vocabulary.undo_event(settings, event_id=split["event_id"])
        assert vocabulary.get_entry(settings, split["new_entry"]["id"]) is not None
        assert vocabulary.mapping_for(settings, "the hammer")["entry_id"] == elsewhere["id"]

    def test_a_rename_that_was_renamed_again_cannot_be_undone(self, settings: Settings) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="mug handling")
        vocabulary.rename_entry(
            settings, entry_id=entry["id"], preferred_label="mug and cup handling"
        )
        stale_event = _last_event(settings)
        vocabulary.rename_entry(
            settings, entry_id=entry["id"], preferred_label="drinkware handling"
        )

        with pytest.raises(ValueError, match="no longer applies"):
            vocabulary.undo_event(settings, event_id=stale_event)
        assert vocabulary.get_entry(settings, entry["id"])["preferred_label"] == (
            "drinkware handling"
        )

    def test_a_dismiss_that_was_mapped_again_cannot_be_undone(self, settings: Settings) -> None:
        entry = vocabulary.create_entry(settings, preferred_label="mugs")
        vocabulary.dismiss_task(settings, task_string="noise")
        stale_event = _last_event(settings)
        vocabulary.map_task(settings, task_string="noise", entry_id=entry["id"])

        with pytest.raises(ValueError, match="no longer applies"):
            vocabulary.undo_event(settings, event_id=stale_event)
        assert vocabulary.mapping_for(settings, "noise")["entry_id"] == entry["id"]


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
