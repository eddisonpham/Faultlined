"""FastAPI application factory for the vertical slice."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from data_engine.api.errors import install_error_handling
from data_engine.api.schemas import (
    AnyJobRequest,
    ArtifactListResponse,
    EpisodeExportResponse,
    EpisodeListResponse,
    EpisodeQualityResponse,
    EpisodeResponse,
    EpisodeValidationResponse,
    FailingEpisodesResponse,
    FailureSummaryResponse,
    JobListResponse,
    JobReportResponse,
    JobResponse,
    MetricsResponse,
    QualitySummaryResponse,
    SliceCreateRequest,
    SliceDetailResponse,
    SliceListResponse,
    SliceManifestResponse,
    SliceUpdateRequest,
    StatusResponse,
    SubmitSourceJobRequest,
)
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import InvalidTransition, PostgresCatalog
from data_engine.config import Settings, load_settings
from data_engine.curation import REASON_CODES
from data_engine.jobs.state import DEFAULT_MAX_ATTEMPTS, JobState
from data_engine.observability.aggregate import (
    heartbeat_age_seconds,
    read_metric_records,
    summarize,
    window_records,
)
from data_engine.observability.aggregate import (
    series as metric_series,
)
from data_engine.observability.metrics import JsonlMetricSink, RuntimeMetrics
from data_engine.observability.telemetry import sample_resources
from data_engine.web import (
    DEFAULT_THEME,
    THEMES,
    artifacts_fragment,
    artifacts_page,
    episode_detail_page,
    episodes_fragment,
    episodes_page,
    failures_fragment,
    failures_page,
    font_bytes,
    insights_fragment,
    insights_page,
    job_detail_page,
    jobs_fragment,
    jobs_page,
    layout_css,
    metrics_fragment,
    metrics_page,
    slices_fragment,
    slices_page,
    status_fragment,
    status_page,
    vendor_css,
)
from data_engine.web.pages import EPISODE_FLAGS, EPISODE_STATES

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


def _metrics_model(
    configured: Settings,
    catalog: PostgresCatalog,
    window_seconds: float | None,
    bucket_seconds: float = 60.0,
) -> dict[str, Any]:
    """Aggregate the metrics sink; shared by the API endpoint and the Metrics UI."""
    records = read_metric_records(configured.metrics_path, max_records=200_000)
    since = (
        datetime.now(UTC) - timedelta(seconds=window_seconds)
        if window_seconds is not None
        else None
    )
    windowed = window_records(records, since=since)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_seconds": window_seconds,
        "record_count": len(windowed),
        "summaries": summarize(windowed),
        "series": metric_series(windowed, bucket_seconds=bucket_seconds),
        "jobs_queue_depth": {state.value: catalog.count_jobs(state) for state in JobState},
        "worker_heartbeat_age_seconds": heartbeat_age_seconds(records),
    }


def _route_template(app: FastAPI, request: Request) -> str:
    """Route template for the matched endpoint, or `unmatched` for 404s.

    Raw paths are high-cardinality and banned as metric labels; the template
    (`/api/v1/jobs/{job_id}`) is the label.
    """
    endpoint = request.scope.get("endpoint")
    if endpoint is not None:
        for route in app.routes:
            if getattr(route, "endpoint", None) is endpoint:
                return str(getattr(route, "path", "unmatched"))
    return "unmatched"


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
    metrics: RuntimeMetrics | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    app_metrics = metrics or RuntimeMetrics(JsonlMetricSink(configured.metrics_path))
    app_catalog = catalog or PostgresCatalog(configured, metrics=app_metrics)

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

    @app.middleware("http")
    async def request_latency_middleware(request: Request, call_next: Any) -> Any:
        """Record `api_request_duration_seconds` per route template (ADR 0017)."""
        started = time.perf_counter()
        response = await call_next(request)
        app_metrics.api_request(
            time.perf_counter() - started,
            route=_route_template(app, request),
            method=request.method,
            status_class=f"{response.status_code // 100}xx",
        )
        return response

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
        body: AnyJobRequest,
        response: Response,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        """Queue an ingest job.

        Two request shapes, one endpoint: `ingest` carries an episode in the request
        body (the synthetic contract), `ingest_source` names a dataset on disk and
        lets a reader interpret it. They are separate types rather than one optional
        payload because "either a body or a path" is exactly the kind of either/or that
        a typo turns into a confusing 422.
        """
        correlation_id = request.state.correlation_id or str(uuid4())
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        if isinstance(body, SubmitSourceJobRequest):
            payload = body.payload.model_dump(mode="json")
        else:
            payload = {"episode": body.payload.episode.model_dump(mode="json")}
        row, created = catalog_for_request.submit_job(
            body.type,
            payload,
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

    @app.get("/api/v1/jobs/{job_id}/episodes", response_model=EpisodeListResponse)
    def list_job_episodes(job_id: str, request: Request) -> dict[str, Any]:
        """Episodes this job produced, following lineage (run inspection)."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        if catalog_for_request.get_job(job_id) is None:
            raise KeyError(job_id)
        return {"items": catalog_for_request.episodes_produced_by(job_id)}

    @app.get("/api/v1/jobs/{job_id}/report", response_model=JobReportResponse)
    def get_job_report(job_id: str, request: Request) -> dict[str, Any]:
        """Run triage card: what the run produced, how validation judged it, motion quality."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        report = catalog_for_request.job_report(job_id)
        if report is None:
            raise KeyError(job_id)
        report["job"] = _job_summary(report["job"])
        return report

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

    @app.get("/api/v1/metrics", response_model=MetricsResponse)
    def runtime_metrics(
        request: Request,
        window_seconds: float | None = None,
        bucket_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """Aggregated runtime telemetry from the JSONL metrics sink.

        Summaries describe every (metric, labels) sample set; series are bucketed
        means per metric name for sparklines. Worker heartbeat age is derived from
        the newest heartbeat record's timestamp, over all records rather than the
        window (a dead worker must not vanish from a narrow window).
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return _metrics_model(configured, catalog_for_request, window_seconds, bucket_seconds)

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
        produced = catalog_for_request.episodes_produced_by(job_id)
        report = catalog_for_request.job_report(job_id)
        return HTMLResponse(job_detail_page(job, _theme(theme), produced, report))

    @app.post("/ui/jobs/{job_id}/cancel")
    def ui_cancel_job(job_id: str, request: Request, theme: str | None = None) -> RedirectResponse:
        """Cancel from the UI and bounce back to the job.

        A plain form post, so the control works without JavaScript. It calls the same
        repository method as the API route rather than issuing an HTTP request to
        ourselves; the redirect is 303 so a refresh does not re-submit the cancel.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        # The detail page renders the current state, including why a cancel is no
        # longer possible; a 409 here would only replace that with browser chrome for
        # a race the user did not cause.
        with suppress(KeyError, InvalidTransition):
            catalog_for_request.request_cancel(job_id)
        suffix = f"?theme={theme}" if theme in THEMES else ""
        return RedirectResponse(f"/ui/jobs/{job_id}{suffix}", status_code=303)

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

    @app.get("/ui/vendor/departure-mono/DepartureMono-Regular.woff2")
    def ui_font() -> Response:
        """Serve the vendored display font. See web/vendor/departure-mono/NOTICE.md."""
        return Response(font_bytes(), media_type="font/woff2")

    @app.get("/ui/failures", response_class=HTMLResponse)
    def ui_failures(
        request: Request,
        theme: str | None = None,
        limit: int = 50,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = {
            "summary": catalog_for_request.failure_summary(),
            "items": catalog_for_request.failing_episodes(limit=min(limit, 200)),
        }
        if x_fragment:
            return HTMLResponse(failures_fragment(model))
        return HTMLResponse(failures_page(model, _theme(theme)))

    @app.get("/ui/slices", response_class=HTMLResponse)
    def ui_slices(
        request: Request,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = {"items": catalog_for_request.list_slices(limit=50)}
        if x_fragment:
            return HTMLResponse(slices_fragment(model))
        return HTMLResponse(slices_page(model, _theme(theme)))

    @app.get("/ui/metrics", response_class=HTMLResponse)
    def ui_metrics(
        request: Request,
        window_seconds: float | None = None,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Live telemetry dashboard over the same model as /api/v1/metrics."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = _metrics_model(configured, catalog_for_request, window_seconds)
        if x_fragment:
            return HTMLResponse(metrics_fragment(model))
        return HTMLResponse(metrics_page(model, _theme(theme)))

    @app.get("/ui/episodes", response_class=HTMLResponse)
    def ui_episodes(
        request: Request,
        state: str | None = None,
        flag: str | None = None,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Episode index with curation flags (jerky / stalled / short / long)."""
        if state is not None and state not in EPISODE_STATES:
            raise HTTPException(status_code=422, detail=f"unknown episode state: {state}")
        if flag is not None and flag not in EPISODE_FLAGS:
            raise HTTPException(status_code=422, detail=f"unknown flag: {flag}")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = {
            "items": catalog_for_request.list_episodes(
                limit=50, state=state or None, flag=flag or None
            )
        }
        if x_fragment:
            return HTMLResponse(episodes_fragment(model))
        return HTMLResponse(episodes_page(model, state, flag, _theme(theme)))

    @app.get("/ui/episodes/{episode_id}", response_class=HTMLResponse)
    def ui_episode_detail(
        episode_id: str, request: Request, theme: str | None = None
    ) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        episode = catalog_for_request.get_episode(episode_id)
        if episode is None:
            return HTMLResponse(
                '<!doctype html><p class="de-empty">// no such episode. '
                '<a href="/ui/episodes">back to episodes</a></p>',
                status_code=404,
            )
        quality = catalog_for_request.get_episode_quality(episode_id)
        validations = catalog_for_request.get_validation_results(episode_id)
        return HTMLResponse(episode_detail_page(episode, quality, _theme(theme), validations))

    @app.get("/ui/insights", response_class=HTMLResponse)
    def ui_insights(
        request: Request,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Dataset curation view: distributions, variance heat, outlier lists."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = catalog_for_request.quality_summary()
        if x_fragment:
            return HTMLResponse(insights_fragment(model))
        return HTMLResponse(insights_page(model, _theme(theme)))

    @app.get("/api/v1/episodes", response_model=EpisodeListResponse)
    def list_episodes_api(
        request: Request,
        state: str | None = None,
        flag: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Scriptable episode catalog with the same curation views as the UI."""
        if state is not None and state not in EPISODE_STATES:
            raise HTTPException(status_code=422, detail=f"unknown episode state: {state}")
        if flag is not None and flag not in EPISODE_FLAGS:
            raise HTTPException(status_code=422, detail=f"unknown flag: {flag}")
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return {
            "items": catalog_for_request.list_episodes(
                limit=limit, state=state or None, flag=flag or None
            )
        }

    @app.get("/api/v1/failures", response_model=FailureSummaryResponse)
    def failures_summary(request: Request) -> dict[str, Any]:
        """What is failing, how often, and under which profiles (read-only)."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        summary = catalog_for_request.failure_summary()
        if summary is None:
            return {
                "reason_codes": {},
                "by_profile": [],
                "by_format": [],
                "quarantined_count": 0,
                "episodes_evaluated": 0,
            }
        return summary

    @app.get("/api/v1/failures/episodes", response_model=FailingEpisodesResponse)
    def failures_episodes(
        request: Request,
        limit: int = 50,
        before: str | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        """Quarantined episodes with the reason codes that put them there."""
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        if reason_code is not None and reason_code not in REASON_CODES:
            raise HTTPException(status_code=422, detail=f"unknown reason_code: {reason_code}")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        rows = catalog_for_request.failing_episodes(
            limit=limit, before=_parse_cursor(before), reason_code=reason_code or None
        )
        return {"items": rows}

    @app.post("/api/v1/slices", response_model=SliceDetailResponse, status_code=201)
    def create_slice(
        body: SliceCreateRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        """Save a named curation filter.

        Membership is recomputed on the next manifest read, so creating a slice is
        cheap and the slice never goes stale as episodes change.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.register_slice(
            name=body.name, notes=body.notes, filter_config=body.filter_config
        )
        response.headers["X-Correlation-Id"] = request.state.correlation_id or str(uuid4())
        detail = catalog_for_request.get_slice(str(row["id"]))
        if detail is None:
            raise KeyError(str(row["id"]))
        return detail

    @app.get("/api/v1/slices", response_model=SliceListResponse)
    def list_slices(request: Request, limit: int = 50, before: str | None = None) -> dict[str, Any]:
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        rows = catalog_for_request.list_slices(limit=limit, before=_parse_cursor(before))
        return {"items": rows}

    @app.get("/api/v1/slices/{slice_id}", response_model=SliceDetailResponse)
    def get_slice(slice_id: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_slice(slice_id)
        if row is None:
            raise KeyError(slice_id)
        return row

    @app.get("/api/v1/slices/{slice_id}/manifest", response_model=SliceManifestResponse)
    def slice_manifest(slice_id: str, request: Request) -> dict[str, Any]:
        """The curated manifest for a saved slice: identity, state, quality."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        manifest = catalog_for_request.slice_manifest(slice_id)
        if manifest is None:
            raise KeyError(slice_id)
        return manifest

    @app.patch("/api/v1/slices/{slice_id}", response_model=SliceDetailResponse)
    def update_slice(slice_id: str, body: SliceUpdateRequest, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.update_slice(
            slice_id, name=body.name, notes=body.notes, filter_config=body.filter_config
        )
        if row is None:
            raise KeyError(slice_id)
        return row

    @app.delete("/api/v1/slices/{slice_id}", status_code=204)
    def delete_slice(slice_id: str, request: Request) -> Response:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        if not catalog_for_request.delete_slice(slice_id):
            raise KeyError(slice_id)
        return Response(status_code=204)

    @app.get("/api/v1/episodes/export", response_model=EpisodeExportResponse)
    def export_episodes_api(
        request: Request,
        state: str | None = None,
        flag: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Curated manifest for curation and dataset builds.

        The same curation views as the catalog listing plus content identity
        (source/artifact hashes). Registered before `/api/v1/episodes/{episode_id}`
        so `export` is never read as an episode id.
        """
        if state is not None and state not in EPISODE_STATES:
            raise HTTPException(status_code=422, detail=f"unknown episode state: {state}")
        if flag is not None and flag not in EPISODE_FLAGS:
            raise HTTPException(status_code=422, detail=f"unknown flag: {flag}")
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        items = catalog_for_request.list_episodes(
            limit=limit, state=state or None, flag=flag or None
        )
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "filters": {"state": state, "flag": flag, "limit": limit},
            "count": len(items),
            "items": items,
        }

    @app.get("/api/v1/episodes/{episode_id}", response_model=EpisodeResponse)
    def get_episode(episode_id: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_episode(episode_id)
        if row is None:
            raise KeyError(episode_id)
        return row

    @app.get("/api/v1/episodes/{episode_id}/quality", response_model=EpisodeQualityResponse)
    def get_episode_quality(episode_id: str, request: Request) -> dict[str, Any]:
        """Motion-quality signals computed at ingest (ADR 0018)."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_episode_quality(episode_id)
        if row is None:
            raise KeyError(episode_id)
        return row

    @app.get("/api/v1/episodes/{episode_id}/validation", response_model=EpisodeValidationResponse)
    def get_episode_validation(episode_id: str, request: Request) -> dict[str, Any]:
        """Validation verdicts per profile: why an episode passed or was quarantined."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        if catalog_for_request.get_episode(episode_id) is None:
            raise KeyError(episode_id)
        return {
            "episode_id": episode_id,
            "results": catalog_for_request.get_validation_results(episode_id),
        }

    @app.get("/api/v1/quality/summary", response_model=QualitySummaryResponse)
    def get_quality_summary(request: Request) -> dict[str, Any]:
        """Length/speed distributions, cross-episode variance matrix, outlier lists."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return catalog_for_request.quality_summary()

    return app
