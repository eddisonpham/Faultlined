"""FastAPI application factory for the vertical slice."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from data_engine.api.errors import install_error_handling
from data_engine.api.schemas import (
    ArtifactListResponse,
    EpisodeResponse,
    JobListResponse,
    JobResponse,
    StatusResponse,
    SubmitJobRequest,
)
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings, load_settings
from data_engine.jobs.state import DEFAULT_MAX_ATTEMPTS, JobState
from data_engine.observability.telemetry import sample_resources
from data_engine.web import (
    DEFAULT_THEME,
    THEMES,
    artifacts_fragment,
    artifacts_page,
    job_detail_page,
    jobs_fragment,
    jobs_page,
    layout_css,
    status_fragment,
    status_page,
    vendor_css,
)

# Only these two filename shapes are servable; anything else is a 404.
_SAFE_STYLESHEET = re.compile(r"(core|theme-[a-z0-9-]+)")


def _theme(name: str | None) -> str:
    """Only vendored themes are selectable; anything else falls back to the default."""
    return name if name in THEMES else DEFAULT_THEME


def _parse_state(state: str | None) -> JobState | None:
    if state is None:
        return None
    try:
        return JobState(state)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"unknown job state: {state}") from exc


def _status_model(catalog: PostgresCatalog) -> dict[str, Any]:
    sample = sample_resources()
    return {
        "status": "ok",
        "queue_depth": {state.value: catalog.count_jobs(state) for state in JobState},
        "artifact_count": catalog.count_artifacts(),
        "episode_count": catalog.count_episodes(),
        "resources": {
            "cpu_percent": sample.cpu_percent,
            "memory_used_bytes": sample.memory_used_bytes,
            "memory_available_bytes": sample.memory_available_bytes,
            "disk_free_bytes": sample.disk_free_bytes,
            "gpu_present": sample.gpu_present,
            "gpu_memory_total_bytes": sample.gpu_memory_total_bytes,
        },
    }


def _parse_cursor(before: str | None) -> datetime | None:
    if before is None:
        return None
    try:
        return datetime.fromisoformat(before)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid cursor: {before}") from exc


def _job_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "state": row["state"],
        "correlation_id": row["correlation_id"],
        "error": row.get("error"),
        "attempts": row.get("attempts", 0),
        "max_attempts": row.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
        "created_at": row["created_at"],
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
    }


def create_app(
    settings: Settings | None = None,
    *,
    initialize_database: bool = True,
    catalog: PostgresCatalog | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    app_catalog = catalog or PostgresCatalog(configured)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if initialize_database:
            initialize_schema(configured)
        yield

    app = FastAPI(
        title="Faultlined",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.catalog = app_catalog
    install_error_handling(app)

    @app.get("/")
    def root() -> dict[str, object]:
        """Point a first-time visitor somewhere useful.

        The platform has no web UI yet (frontend is a later stage), so a bare `/`
        would otherwise be a blank 404 in the browser.
        """
        return {
            "service": "faultlined",
            "description": "Local-first robot episode data engine. API only; no web UI yet.",
            "docs": "/docs",
            "openapi": "/openapi.json",
            "health": "/api/v1/health",
            "endpoints": {
                "submit_job": "POST /api/v1/jobs",
                "get_job": "GET /api/v1/jobs/{job_id}",
                "get_episode": "GET /api/v1/episodes/{episode_id}",
            },
        }

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        if initialize_database:
            initialize_schema(configured)
        return {"status": "ok"}

    @app.post("/api/v1/jobs", response_model=JobResponse, status_code=202)
    def submit_job(
        body: SubmitJobRequest,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        correlation_id = request.state.correlation_id or str(uuid4())
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row, created = catalog_for_request.submit_job(
            body.type,
            {"episode": body.payload.episode.model_dump(mode="json")},
            idempotency_key,
            correlation_id,
            max_attempts=body.max_attempts,
            deadline_seconds=body.deadline_seconds,
        )
        response.headers["X-Correlation-Id"] = correlation_id
        response.headers["X-Idempotent-Replay"] = "false" if created else "true"
        return row

    @app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
    def get_job(job_id: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_job(job_id)
        if row is None:
            raise KeyError(job_id)
        return row

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=JobResponse)
    def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        """Request cancellation (F7).

        A queued job is canceled immediately; a running job moves to
        `cancel_requested` and the worker stops it at its next checkpoint. A job that
        already reached a terminal state cannot be cancelled (409), and an unknown id
        is a 404.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return catalog_for_request.request_cancel(job_id)

    @app.get("/api/v1/jobs", response_model=JobListResponse)
    def list_jobs(
        request: Request,
        state: str | None = None,
        type: str | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> dict[str, Any]:
        """Newest-first page of jobs for the Jobs UI.

        Cursor-based: pass the last item's ``created_at`` back as ``before``. The UI
        never asks for every row.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        parsed_state = _parse_state(state)
        parsed_before = _parse_cursor(before)
        rows = catalog_for_request.list_jobs(
            state=parsed_state, job_type=type, limit=min(limit, 200), before=parsed_before
        )
        items = [_job_summary(row) for row in rows]
        next_before = rows[-1]["created_at"] if len(rows) == min(limit, 200) else None
        return {"items": items, "next_before": next_before}

    @app.get("/api/v1/artifacts", response_model=ArtifactListResponse)
    def list_artifacts(
        request: Request, limit: int = 50, before: str | None = None
    ) -> dict[str, Any]:
        """Newest-first page of content-addressed artifacts with their referencing episodes."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        rows = catalog_for_request.list_artifacts(
            limit=min(limit, 200), before=_parse_cursor(before)
        )
        next_before = rows[-1]["created_at"] if len(rows) == min(limit, 200) else None
        return {"items": rows, "next_before": next_before}

    @app.get("/api/v1/status", response_model=StatusResponse)
    def status(request: Request) -> dict[str, Any]:
        """Health, queue depth by state, entity counts, and host telemetry."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return _status_model(catalog_for_request)

    @app.get("/ui/faultlined.css")
    def ui_layout_css() -> Response:
        return Response(layout_css(), media_type="text/css; charset=utf-8")

    @app.get("/ui/vendor/terminal-ui/{name}.css")
    def ui_vendor_css(name: str) -> Response:
        """Serve the vendored terminal stylesheet. See web/vendor/terminal-ui/NOTICE.md."""
        if not _SAFE_STYLESHEET.fullmatch(name):
            raise HTTPException(status_code=404, detail="unknown stylesheet")
        try:
            body = vendor_css(f"{name}.css")
        except OSError as exc:
            raise HTTPException(status_code=404, detail="unknown stylesheet") from exc
        return Response(body, media_type="text/css; charset=utf-8")

    @app.get("/ui", response_class=HTMLResponse)
    def ui_status(
        request: Request,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Status/Overview. The X-Fragment header returns only the polling body."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = _status_model(catalog_for_request)
        if x_fragment:
            return HTMLResponse(status_fragment(model))
        return HTMLResponse(status_page(model, _theme(theme)))

    @app.get("/ui/jobs", response_class=HTMLResponse)
    def ui_jobs(
        request: Request,
        state: str | None = None,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        rows = catalog_for_request.list_jobs(state=_parse_state(state), limit=50)
        summaries = {"items": [_job_summary(row) for row in rows]}
        if x_fragment:
            return HTMLResponse(jobs_fragment(summaries))
        return HTMLResponse(jobs_page(summaries, state, _theme(theme)))

    @app.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
    def ui_job_detail(job_id: str, request: Request, theme: str | None = None) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        job = catalog_for_request.get_job(job_id)
        if job is None:
            return HTMLResponse(
                '<!doctype html><p class="de-empty">// no such job. '
                '<a href="/ui/jobs">back to jobs</a></p>',
                status_code=404,
            )
        return HTMLResponse(job_detail_page(job, _theme(theme)))

    @app.get("/ui/artifacts", response_class=HTMLResponse)
    def ui_artifacts(
        request: Request,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        items = {"items": catalog_for_request.list_artifacts(limit=50)}
        if x_fragment:
            return HTMLResponse(artifacts_fragment(items))
        return HTMLResponse(artifacts_page(items, _theme(theme)))

    @app.get("/api/v1/episodes/{episode_id}", response_model=EpisodeResponse)
    def get_episode(episode_id: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_episode(episode_id)
        if row is None:
            raise KeyError(episode_id)
        return row

    return app
