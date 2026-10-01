"""Tests for the full-scale corpus and the real-string harvest.

The corpus is the instrument for EXP-2.5-08, so its properties are asserted rather than
assumed: a corpus that quietly became unbalanced, or that reused one string 500 times,
would still produce a plausible-looking report. These tests run without a model and
without a network.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from pathlib import Path

import pytest
from experiments.clustering import attributes, corpus, real_tasks

# ------------------------------------------------------------------ synthetic


def test_the_corpus_is_the_size_asked_for_and_holds_no_duplicates() -> None:
    items = corpus.synthetic_corpus(600)
    assert len(items) == 600
    assert len({item.text for item in items}) == 600


def test_every_object_gets_a_comparable_share_of_the_corpus() -> None:
    """A prefix of the enumeration would give the first few objects everything."""
    items = corpus.synthetic_corpus(1200)
    counts = Counter(item.label for item in items)
    assert len(counts) == 48
    assert min(counts.values()) >= 24
    assert max(counts.values()) <= 27


def test_colour_cells_are_balanced_enough_to_compare() -> None:
    counts = Counter(item.colour or "none" for item in corpus.synthetic_corpus(1200))
    assert set(counts) == {"none", "red", "blue", "green"}
    assert max(counts.values()) - min(counts.values()) <= 60


def test_the_corpus_contains_verbs_the_masking_lexicon_does_not_know() -> None:
    """Otherwise a coverage number would be measuring the generator again."""
    items = corpus.synthetic_corpus(1200)
    unknown = [item for item in items if item.unknown_verb]
    assert len(unknown) > 200
    recognised = [item for item in unknown if attributes.verb_of(item.text) not in ("", "-")]
    # Some are recognised anyway ("turn on" inside "turn on the shelf"), which is
    # exactly why coverage is measured rather than assumed.
    assert len(recognised) < len(unknown)


def test_asking_for_more_than_the_grammar_can_make_is_an_error() -> None:
    assert corpus.available() > 100_000
    with pytest.raises(ValueError, match="grammar makes"):
        corpus.synthetic_corpus(corpus.available() + 1)


def test_a_small_corpus_stays_balanced_too() -> None:
    items = corpus.synthetic_corpus(120)
    counts = Counter(item.label for item in items)
    assert len(counts) == 48
    assert min(counts.values()) >= 1


def test_the_balance_report_exposes_the_imbalances_a_reader_should_worry_about() -> None:
    report = corpus.balance(corpus.synthetic_corpus(300))
    assert set(report) == {"label", "colour", "verb", "modifier"}
    assert report["colour"]["none"] > 0
    assert sum(report["label"].values()) == 300


# --------------------------------------------------------------------- orders


def test_shuffling_is_seeded_and_therefore_reproducible() -> None:
    items = corpus.synthetic_corpus(300)
    first = [item.text for item in corpus.shuffled(items)]
    second = [item.text for item in corpus.shuffled(items)]
    assert first == second
    assert first != [item.text for item in items]


def test_the_adversarial_order_puts_every_class_together() -> None:
    ordered = corpus.grouped(corpus.synthetic_corpus(200))
    labels = [item.label for item in ordered]
    assert labels == sorted(labels)
    runs = [len(list(group)) for _label, group in itertools.groupby(ordered, key=_label_of)]
    assert len(runs) == len(set(labels))


def _label_of(item: corpus.Item) -> str:
    return item.label


# ----------------------------------------------------------------- the control


def test_the_control_corpus_varies_only_verb_and_colour() -> None:
    items = corpus.minimal_corpus(200)
    assert len({item.text for item in items}) == 200
    assert all(not item.modifier for item in items)
    assert all(not item.site for item in items)
    assert len({item.label for item in items}) == 48


def test_an_axis_corpus_varies_exactly_one_axis() -> None:
    items = corpus.axis_corpus("modifier", ("", "plastic", "wooden"))
    assert {item.modifier for item in items} == {"", "plastic", "wooden"}
    assert all(item.site == "" for item in items)
    assert all(item.verb == "pick up" for item in items)
    assert len(items) == 48 * 3


def test_an_axis_corpus_rejects_an_axis_it_does_not_khow() -> None:
    with pytest.raises(ValueError, match="unknown axis"):
        corpus.axis_corpus("mood", ("", "happy"))


# ------------------------------------------------------------------ the core


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pick up the red cube", "cube"),
        ("put down the green plastic mug into the bin", "mug"),
        ("turn on the blue stove on the counter", "stove"),
        ("wipe the counter", "counter"),
        ("place the bowl on the plate", "bowl"),
    ],
)
def test_the_core_is_the_object_the_gold_label_names(text: str, expected: str) -> None:
    assert corpus.core_text(text) == expected


def test_the_core_keeps_a_multiword_object_intact() -> None:
    assert corpus.core_text("pick up the bar of soap") == "bar of soap"


def test_the_core_never_empties_a_string() -> None:
    assert corpus.core_text("cleanup")
    assert corpus.core_text("pick up the mug on the table")


def test_the_core_corpus_keeps_the_gold_labels() -> None:
    """The headroom measurement is only meaningful if the labels survive extraction."""
    items = corpus.synthetic_corpus(300)
    cores = corpus.core_corpus(items)
    assert [core.label for core in cores] == [item.label for item in items]
    assert len({core.text for core in cores}) == 48


# ----------------------------------------------------------------- real text


def test_placeholder_task_strings_are_rejected() -> None:
    assert real_tasks._usable("put the bowl on the plate")
    assert not real_tasks._usable("0")
    assert not real_tasks._usable("  ")
    assert not real_tasks._usable("12")


def test_a_repository_failure_is_reported_once_rather_than_per_file() -> None:
    seen = ["meta/tasks.parquet: RepositoryNotFoundError", "meta/tasks.jsonl: EntryNotFound"]
    assert "RepositoryNotFound" in real_tasks._best_reason(seen)
    assert real_tasks._best_reason([]) == "no task file"


def test_a_gated_repository_is_distinguished_from_a_missing_one() -> None:
    assert real_tasks._is_repo_level("meta/tasks.csv: GatedRepo")
    assert real_tasks._is_repo_level("meta/tasks.csv: 401 Client Error")
    assert not real_tasks._is_repo_level("meta/tasks.csv: EntryNotFoundError")


def test_task_sentences_are_read_from_a_jsonl_file(tmp_path: Path) -> None:
    path = tmp_path / "tasks.jsonl"
    path.write_text(
        '{"task_index": 0, "task": "put the bowl on the plate"}\n'
        "\n"
        '{"task_index": 1, "task": "open the drawer"}\n',
        encoding="utf-8",
    )
    assert real_tasks._texts_from(path) == [
        "put the bowl on the plate",
        "open the drawer",
    ]


def test_the_harvest_reads_its_cache_instead_of_the_network(tmp_path: Path) -> None:
    cache = tmp_path / "real_tasks.json"
    cache.write_text(
        json.dumps(
            {
                "rows": [["lerobot/pusht", "Push the block onto the target."]],
                "resolved": ["lerobot/pusht"],
                "failed": [["lerobot/gone", "meta/tasks.parquet: RepositoryNotFoundError"]],
            }
        ),
        encoding="utf-8",
    )
    harvested = real_tasks.harvest(cache=cache)
    assert harvested.count == 1
    assert harvested.resolved == ("lerobot/pusht",)
    assert harvested.failed[0][0] == "lerobot/gone"


def test_the_harvest_writes_a_cache_that_records_what_failed(tmp_path: Path) -> None:
    cache = tmp_path / "nested" / "real_tasks.json"
    harvested = real_tasks.harvest(repos=(), cache=cache)
    assert harvested.count == 0
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["rows"] == []
    assert "regenerate" in written["note"]


def test_local_task_strings_are_de_duplicated_by_text(tmp_path: Path) -> None:
    """Two datasets serving the same sentence is one string, not two rows."""
    for dataset in ("first", "second"):
        meta = tmp_path / dataset / "meta"
        meta.mkdir(parents=True)
        (meta / "info.json").write_text(
            json.dumps({"tasks": ["put the bowl on the plate"]}), encoding="utf-8"
        )
    items = corpus.local_task_strings(tmp_path)
    assert [item.text for item in items] == ["put the bowl on the plate"]


def test_hub_items_carry_the_dataset_as_their_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """A harvested sentence is labelled by its dataset, so a report can trace it back."""
    harvested = real_tasks.Harvest(
        rows=(("lerobot/pusht", "Push the block onto the target."),),
        resolved=("lerobot/pusht",),
        failed=(),
    )
    monkeypatch.setattr(real_tasks, "harvest", lambda **_kwargs: harvested)
    items = corpus.hub_items()
    assert len(items) == 1
    assert items[0].origin == "lerobot/pusht"
    assert items[0].label == "lerobot/pusht"
