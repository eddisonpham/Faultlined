"""Benchmarks and Experiments pages: the readers, the rendering, the routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from data_engine.web import records


def _client() -> TestClient:
    from data_engine.api.app import create_app

    return TestClient(create_app(initialize_database=False), raise_server_exceptions=False)


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout-shaped directory the readers can be pointed at."""
    (tmp_path / "benchmarks" / "baselines").mkdir(parents=True)
    (tmp_path / "benchmarks" / "results").mkdir(parents=True)
    (tmp_path / "agents" / "experiments").mkdir(parents=True)
    monkeypatch.setattr(records, "repo_root", lambda: tmp_path)
    return tmp_path


def _write(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _baseline(name: str, *, p50: float, throughput: float | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "benchmark": {"name": name, "version": "1.0.0"},
        "config": {"trials": 5, "source_sha256": "a" * 64},
        "summary": {"p50_seconds": p50, "mean_seconds": p50, "n": 5, "failure_count": 0},
    }
    if throughput is not None:
        document["config"]["throughput_mib_per_second_p50"] = throughput
    return document


def _result(name: str, *, run_id: str, when: str, p50: float) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_id": run_id,
        "benchmark": {"name": name, "version": "1.0.0"},
        "status": "ok",
        "started_at": when,
        "config": {"trials": 5},
        "provenance": {"git_commit": "c" * 40, "git_dirty": False},
        "summary": {"p50_seconds": p50, "n": 5, "failure_count": 0},
    }


@pytest.mark.unit
def test_the_real_checkout_is_found() -> None:
    """The readers walk up from the module, not from the process CWD."""
    root = records.repo_root()
    assert root is not None
    assert (root / "benchmarks").is_dir() and (root / "agents" / "experiments").is_dir()


@pytest.mark.unit
def test_the_repositorys_own_baselines_and_experiments_are_readable() -> None:
    """The real data, not a fixture: a committed claim must be on the page."""
    model = records.benchmarks_model()

    assert model["error"] == ""
    names = {run.name for run in model["baselines"]}
    assert {"synthetic-episode-ingest", "mcap-ingest"} <= names, names
    assert model["result_total"] > 0
    mcap = next(run for run in model["baselines"] if run.name == "mcap-ingest")
    assert mcap.p50 and mcap.p50 > 0
    assert mcap.source_sha256

    experiments = records.experiments_model()
    ids = [record.exp_id for record in experiments["experiments"]]
    assert ids == sorted(ids, reverse=True)
    import re

    root = records.repo_root()
    assert root is not None
    on_disk = {
        f"EXP-{match.group(1)}"
        for path in (root / "agents" / "experiments").glob("*.md")
        if (match := re.match(r"^(\d{4}[a-z]?)-", path.name))
    }
    assert set(ids) == on_disk
    for record in experiments["experiments"]:
        assert record.status, f"{record.exp_id} has no status line"
        assert record.date, f"{record.exp_id} has no date line"
        assert record.title


@pytest.mark.unit
def test_a_truncated_record_is_skipped_and_reported(fake_repo: Path) -> None:
    """A half-written file from an interrupted run must not take the page down, and it must not"""

    _write(fake_repo / "benchmarks" / "baselines" / "good.json", _baseline("good", p50=0.5))
    (fake_repo / "benchmarks" / "results" / "cut-off.json").write_text('{"run_id": "x"', "utf-8")

    model = records.benchmarks_model()

    assert [run.name for run in model["baselines"]] == ["good"]
    assert model["unreadable"] == ["results/cut-off.json"]
    assert "cut-off.json" in records.benchmarks_page(model, "vt220")


@pytest.mark.unit
def test_a_record_missing_its_summary_renders_as_unknown_not_zero(fake_repo: Path) -> None:
    """A fabricated 0.000 s would be a claim nobody measured."""
    _write(
        fake_repo / "benchmarks" / "baselines" / "sparse.json",
        {"benchmark": {"name": "sparse"}, "summary": {}},
    )

    model = records.benchmarks_model()

    assert model["baselines"][0].p50 is None
    html = records.benchmarks_page(model, "vt220")
    assert "—" in html
    assert "0.000 s" not in html


@pytest.mark.unit
def test_a_run_with_no_committed_baseline_is_named(fake_repo: Path) -> None:
    """A number with nothing to compare against is the thing to surface."""
    _write(fake_repo / "benchmarks" / "baselines" / "known.json", _baseline("known", p50=0.5))
    _write(
        fake_repo / "benchmarks" / "results" / "r.json",
        _result("brand-new", run_id="r1", when="2026-10-01T00:00:00Z", p50=0.4),
    )

    html = records.benchmarks_page(records.benchmarks_model(), "vt220")

    assert model_unmatched(fake_repo) == ["brand-new"]
    assert "<code>brand-new</code>" in html


def model_unmatched(_repo: Path) -> list[str]:
    return records.benchmarks_model()["unmatched"]


@pytest.mark.unit
def test_runs_are_newest_first_and_capped(fake_repo: Path) -> None:
    for index in range(records.RESULT_LIMIT + 5):
        _write(
            fake_repo / "benchmarks" / "results" / f"{index:03d}.json",
            _result(
                "w",
                run_id=f"{index:03d}",
                when=f"2026-10-01T00:{index:02d}:00Z",
                p50=1.0 + index,
            ),
        )

    model = records.benchmarks_model()

    assert model["result_total"] == records.RESULT_LIMIT + 5
    assert len(model["results"]) == records.RESULT_LIMIT
    assert model["results"][0].recorded > model["results"][-1].recorded


@pytest.mark.unit
def test_non_record_markdown_is_not_an_experiment(fake_repo: Path) -> None:
    """registry.md and TEMPLATE.md sit in the same directory and are not records."""
    _write_record(
        fake_repo / "agents" / "experiments" / "0001-real.md",
        "# EXP-0001: A real one\n\n- **Status:** accepted\n- **Date (UTC):** measured 2026-10-01\n",
    )
    (fake_repo / "agents" / "experiments" / "registry.md").write_text("# Registry\n", "utf-8")
    (fake_repo / "agents" / "experiments" / "TEMPLATE.md").write_text("# T\n", "utf-8")

    model = records.experiments_model()

    assert [record.exp_id for record in model["experiments"]] == ["EXP-0001"]


@pytest.mark.unit
def test_the_result_preview_is_the_records_own_prose(fake_repo: Path) -> None:
    """The page must not paraphrase a record into a different claim."""
    _write_record(
        fake_repo / "agents" / "experiments" / "0002-x.md",
        "# EXP-0002: X\n\n"
        "- **Status:** accepted\n"
        "## Result\n\n"
        "P50 fell from 6.21 to 1.10 MiB/s and output was byte-identical.\n\n"
        "## Notes\n\nNot the result section.\n",
    )

    model = records.experiments_model()

    preview = model["experiments"][0].preview
    assert "6.21 to 1.10" in preview
    assert "Not the result section" not in preview


@pytest.mark.unit
def test_a_record_with_no_result_section_previews_as_nothing(fake_repo: Path) -> None:
    _write_record(
        fake_repo / "agents" / "experiments" / "0003-y.md",
        "# EXP-0003: Y\n\n- **Status:** planned\n## Configuration\n\nnot a result\n",
    )

    assert records.experiments_model()["experiments"][0].preview == ""


def _write_record(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


@pytest.mark.unit
def test_a_workload_name_from_a_record_cannot_inject_markup(fake_repo: Path) -> None:
    """Names come off disk and are rendered unescaped nowhere."""
    _write(
        fake_repo / "benchmarks" / "baselines" / "x.json",
        _baseline("<script>alert(1)</script>", p50=0.1),
    )
    (fake_repo / "agents" / "experiments" / "0004-z.md").write_text(
        "# EXP-0004: <img src=x onerror=alert(1)>\n\n- **Status:** accepted\n", encoding="utf-8"
    )

    for html in (
        records.benchmarks_page(records.benchmarks_model(), "vt220"),
        records.experiments_page(records.experiments_model(), "vt220"),
    ):
        assert "<script>alert(1)</script>" not in html
        assert "<img src=x" not in html
        assert "&lt;script&gt;" in html or "&lt;img" in html


@pytest.mark.unit
def test_both_pages_survive_an_empty_checkout(fake_repo: Path) -> None:
    """A fresh clone with no `just bench` run yet must still render."""
    for html in (
        records.benchmarks_page(records.benchmarks_model(), "vt220"),
        records.experiments_page(records.experiments_model(), "vt220"),
    ):
        assert html.startswith("<!doctype html>")
        assert 'class="de-empty"' in html


@pytest.mark.unit
def test_a_missing_repository_says_so_instead_of_rendering_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(records, "repo_root", lambda: None)

    for html in (
        records.benchmarks_page(records.benchmarks_model(), "vt220"),
        records.experiments_page(records.experiments_model(), "vt220"),
    ):
        assert "repository root not found" in html


@pytest.mark.unit
def test_both_pages_are_reachable_and_offers_a_fragment() -> None:
    client = _client()

    for path, heading in (("/ui/benchmarks", "Benchmarks"), ("/ui/experiments", "Experiments")):
        page = client.get(path)
        assert page.status_code == 200, path
        assert f'<h1 class="fine-use-h1">{heading}</h1>' in page.text
        fragment = client.get(path, headers={"X-Fragment": "1"})
        assert fragment.status_code == 200
        assert "<!doctype html>" not in fragment.text
        assert "<main" not in fragment.text


@pytest.mark.unit
def test_both_pages_are_in_the_nav_with_unique_shortcuts() -> None:
    client = _client()

    page = client.get("/ui/benchmarks").text

    assert 'href="/ui/benchmarks"' in page
    assert 'href="/ui/experiments"' in page
    assert 'aria-current="page"' in page
