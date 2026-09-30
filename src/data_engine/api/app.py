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
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from data_engine.api.errors import install_error_handling
from data_engine.api.schemas import (
    AnyJobRequest,
    ArtifactListResponse,
    BuildDetailResponse,
    BuildListResponse,
    ContractListResponse,
    ContractPayload,
    ContractRequest,
    EpisodeExportResponse,
    EpisodeListResponse,
    EpisodeQualityResponse,
    EpisodeResponse,
    EpisodeValidationResponse,
    FailingEpisodesResponse,
    FailureSummaryResponse,
    IncidentListResponse,
    IncidentPayload,
    IncidentSummaryResponse,
    IngestPayload,
    JobListResponse,
    JobReportResponse,
    JobResponse,
    MetricsResponse,
    MonitoringHealthResponse,
    QualitySummaryResponse,
    SliceCreateRequest,
    SliceDetailResponse,
    SliceListResponse,
    SliceManifestResponse,
    SliceUpdateRequest,
    SourceIngestPayload,
    StatusResponse,
    SubmitJobRequest,
    SubmitSourceJobRequest,
    SyntheticEpisode,
    TickResponse,
)
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.introspect import describe
from data_engine.catalog.repository import InvalidTransition, PostgresCatalog
from data_engine.config import Settings, load_settings
from data_engine.curation import REASON_CODES
from data_engine.jobs.state import DEFAULT_MAX_ATTEMPTS, JobState
from data_engine.monitoring.contracts import Expectation, InvalidExpectation
from data_engine.monitoring.service import MonitorService
from data_engine.monitoring.signals import Label, Severity
from data_engine.monitoring.summary import render_digest
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
    THEMES,
    app_script,
    artifacts_fragment,
    artifacts_page,
    builds_fragment,
    builds_page,
    episode_detail_page,
    episodes_fragment,
    episodes_page,
    failures_fragment,
    failures_page,
    font_bytes,
    incidents_fragment,
    incidents_page,
    insights_fragment,
    insights_page,
    job_detail_page,
    jobs_fragment,
    jobs_page,
    layout_css,
    lineage_page,
    metrics_fragment,
    metrics_page,
    schema_fragment,
    schema_page,
    slices_fragment,
    slices_page,
    status_fragment,
    status_page,
    theme_or_default,
    vendor_css,
)
from data_engine.web.pages import EPISODE_FLAGS, EPISODE_STATES

# Only these two filename shapes are servable; anything else is a 404.
_SAFE_STYLESHEET = re.compile(r"(core|theme-[a-z0-9-]+)")


def _first_message(exc: ValidationError) -> str:
    """The first validation failure, as a sentence a person can act on.

    A raw Pydantic error is a JSON object with a type code, a field path and a
    context dict. It is the right thing for a machine and the wrong thing for a
    form: the field path has to be recovered, and the message dropped, before it
    becomes anything a reader can use.
    """
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "payload")
        message = str(error.get("msg", "invalid value"))
        # Pydantic prefixes constraint failures with the constraint name, which
        # means nothing on its own.
        for prefix in ("Value error, ", "String should match pattern "):
            if message.startswith(prefix):
                message = message[len(prefix) :]
        return f"{location}: {message}" if location else message
    return "That submission was not valid."


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
        # The browser surface is the operator UI, not a developer contract viewer.
        # /openapi.json stays: the committed contract check still needs it, and it
        # is a machine endpoint rather than a page. See agents/decisions/0021.
        docs_url=None,
        redoc_url=None,
    )
    app.state.catalog = app_catalog
    app.state.monitor = MonitorService(
        app_catalog, metrics=app_metrics, metrics_path=configured.metrics_path
    )
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

        `/` is JSON rather than a redirect because a bare 404 in the browser was
        the original complaint; the UI is one field away and the API callers get
        the machine-readable map they expect.
        """
        return {
            "service": "faultlined",
            "description": "Local-first robot episode data engine.",
            "ui": "/ui",
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

        Four request shapes, one endpoint: `ingest` carries an episode in the
        request body (the synthetic contract), `ingest_source` names a dataset on
        disk and lets a reader interpret it, `validate` and `build` name a
        selection of episodes already in the catalog. They are separate types
        rather than one optional payload because "either a body or a path" is
        exactly the kind of either/or that a typo turns into a confusing 422.
        """
        correlation_id = request.state.correlation_id or str(uuid4())
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        if isinstance(body, SubmitJobRequest):
            payload = {"episode": body.payload.episode.model_dump(mode="json")}
        else:
            # Source, validate, and build payloads are already flat and
            # self-describing; there is no episode to unwrap.
            payload = body.payload.model_dump(mode="json")
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

    @app.get("/ui/app.js")
    def ui_app_js() -> Response:
        """The client runtime. Served as a route rather than a static mount so the
        UI stays one Python-owned directory with no extra file-serving surface."""
        return Response(app_script(), media_type="text/javascript; charset=utf-8")

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
        return HTMLResponse(status_page(model, theme_or_default(theme)))

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
        return HTMLResponse(jobs_page(summaries, state, theme_or_default(theme)))

    @app.get("/ui/jobs/{job_id}", response_class=HTMLResponse)
    def ui_job_detail(job_id: str, request: Request, theme: str | None = None) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        job = catalog_for_request.get_job(job_id)
        if job is None:
            # Raised, not returned: the shared handler renders the full error page
            # in the requested theme, which a hand-rolled one-line body cannot.
            raise HTTPException(status_code=404, detail=f"No job {job_id}.")
        produced = catalog_for_request.episodes_produced_by(job_id)
        report = catalog_for_request.job_report(job_id)
        return HTMLResponse(job_detail_page(job, theme_or_default(theme), produced, report))

    @app.post("/ui/jobs")
    async def ui_submit_job(request: Request) -> Response:
        """Queue an ingest job from the browser form.

        The form is parsed by hand rather than with ``Form(...)`` because that
        would pull in ``python-multipart`` for a single urlencoded body - a new
        dependency, on the dependency-free path ADR 0014 is built around, to save
        three lines of ``urllib.parse``.

        The payload is built and then validated through the *same* Pydantic
        models the JSON endpoint uses. A form is a second front door to the same
        door, and a second front door with its own weaker validation is how a
        UI ends up able to create jobs the API would refuse.

        On a bad submission this re-renders the page with the error inline and
        the typed values preserved, rather than redirecting: losing what someone
        typed because of one bad field is the fastest way to make a form feel
        hostile. The redirect on success is 303, so a refresh does not queue the
        same job twice.
        """
        from urllib.parse import parse_qs

        raw = parse_qs((await request.body()).decode("utf-8", "replace"))
        kind = (raw.get("kind") or ["episode"])[0].strip() or "episode"
        source = (raw.get("source") or [""])[0].strip()
        values = {
            "kind": kind,
            "task": (raw.get("task") or [""])[0].strip(),
            "robot": (raw.get("robot") or [""])[0].strip(),
            "frames": (raw.get("frames") or [""])[0].strip(),
            "source": source,
            "episode_key": (raw.get("episode_key") or [""])[0].strip(),
        }
        theme = theme_or_default((raw.get("theme") or [""])[0] or None)

        def fail(message: str) -> HTMLResponse:
            model = _status_model(cast(PostgresCatalog, request.app.state.catalog))
            model["ingest_error"] = message
            model["ingest_values"] = values
            return HTMLResponse(status_page(model, theme))

        try:
            body: AnyJobRequest
            if kind == "path" or (not values["task"] and source):
                if not source:
                    return fail("A dataset path is required when loading from disk.")
                body = SubmitSourceJobRequest(
                    type="ingest_source",
                    payload=SourceIngestPayload(
                        source=source, episode_key=values["episode_key"] or None
                    ),
                )
            else:
                if not values["task"] or not values["robot"]:
                    return fail("A task and a robot are both required.")
                try:
                    frames = int(values["frames"] or "3")
                except ValueError:
                    return fail("Frames must be a whole number.")
                if not 1 <= frames <= 10_000:
                    return fail("Frames must be between 1 and 10000.")
                # A deterministic ramp: the form is a way to get data *in*, not a
                # sensor. Real data arrives by path or by the JSON endpoint.
                step = 0.1
                stamps = [round(i * step, 6) for i in range(frames)]
                body = SubmitJobRequest(
                    type="ingest",
                    payload=IngestPayload(
                        episode=SyntheticEpisode(
                            task=values["task"],
                            robot=values["robot"],
                            timestamps=stamps,
                            observations=[[round(i * 0.01, 6)] for i in range(frames)],
                            actions=[[round(i * 0.02, 6)] for i in range(frames)],
                        )
                    ),
                )
        except ValidationError as exc:
            return fail(_first_message(exc))

        payload = (
            body.payload.model_dump(mode="json")
            if isinstance(body, SubmitSourceJobRequest)
            else {"episode": body.payload.episode.model_dump(mode="json")}
        )
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row, _created = catalog_for_request.submit_job(
            body.type,
            payload,
            None,
            str(getattr(request.state, "correlation_id", "") or uuid4()),
        )
        suffix = f"?theme={theme}" if theme in THEMES else ""
        return RedirectResponse(f"/ui/jobs/{row['id']}{suffix}", status_code=303)

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

    @app.get("/api/v1/builds", response_model=BuildListResponse)
    def list_builds(request: Request, limit: int = 50) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        items = catalog_for_request.list_builds(limit=max(1, min(limit, 200)))
        return {"items": items}

    @app.get("/api/v1/builds/{build_hash}", response_model=BuildDetailResponse)
    def get_build(build_hash: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_build(build_hash)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No build {build_hash}.")
        # The manifest and the membership are returned together: a build whose
        # manifest cannot be read back is not reproducible, which is the one
        # property it exists to have.
        return {**row, "episodes": catalog_for_request.build_episodes(build_hash)}

    @app.get("/api/v1/episodes/{episode_id}/builds", response_model=BuildListResponse)
    def builds_for_episode(episode_id: str, request: Request) -> dict[str, Any]:
        """The reverse lineage direction (FR-008): what contains this episode."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return {"items": catalog_for_request.builds_for_episode(episode_id)}

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
        return HTMLResponse(artifacts_page(items, theme_or_default(theme)))

    @app.get("/ui/builds", response_class=HTMLResponse)
    def ui_builds(
        request: Request,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Content-addressed builds. The address is the content."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = {"items": catalog_for_request.list_builds(limit=50)}
        if x_fragment:
            return HTMLResponse(builds_fragment(model))
        return HTMLResponse(builds_page(model, theme_or_default(theme)))

    @app.get("/ui/builds/{build_hash}", response_class=HTMLResponse)
    def ui_build_detail(
        request: Request,
        build_hash: str,
        theme: str | None = None,
    ) -> HTMLResponse:
        """One build, drawn as the graph lineage actually is."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_build(build_hash)
        if row is None:
            return HTMLResponse(lineage_page({}, theme_or_default(theme)))
        # The same two reads the API endpoint makes, in the same order: a build
        # whose manifest cannot be read back is not reproducible, and the page
        # has to say so rather than draw an empty graph that looks complete.
        return HTMLResponse(
            lineage_page(
                {**row, "episodes": catalog_for_request.build_episodes(build_hash)},
                theme_or_default(theme),
            )
        )

    @app.get("/ui/schema")
    def ui_schema(
        request: Request,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
        table: str | None = None,
    ) -> HTMLResponse:
        """The catalog, read live. A drawing of it would eventually be a lie."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = describe(catalog_for_request.settings)
        if x_fragment:
            return HTMLResponse(schema_fragment(model, table or ""))
        return HTMLResponse(schema_page(model, theme_or_default(theme), table or ""))

    @app.get("/ui/schema/{table}")
    def ui_schema_table(
        request: Request,
        table: str,
        theme: str | None = None,
    ) -> HTMLResponse:
        """One table's columns, with the same page around it."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = describe(catalog_for_request.settings)
        return HTMLResponse(schema_page(model, theme_or_default(theme), table))

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
        return HTMLResponse(failures_page(model, theme_or_default(theme)))

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
        return HTMLResponse(slices_page(model, theme_or_default(theme)))

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
        return HTMLResponse(metrics_page(model, theme_or_default(theme)))

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
        return HTMLResponse(episodes_page(model, state, flag, theme_or_default(theme)))

    @app.get("/ui/episodes/{episode_id}", response_class=HTMLResponse)
    def ui_episode_detail(
        episode_id: str, request: Request, theme: str | None = None
    ) -> HTMLResponse:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        episode = catalog_for_request.get_episode(episode_id)
        if episode is None:
            raise HTTPException(status_code=404, detail=f"No episode {episode_id}.")
        quality = catalog_for_request.get_episode_quality(episode_id)
        validations = catalog_for_request.get_validation_results(episode_id)
        return HTMLResponse(
            episode_detail_page(episode, quality, theme_or_default(theme), validations)
        )

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
        return HTMLResponse(insights_page(model, theme_or_default(theme)))

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

    # ---- monitoring notifier (ADR 0020) ----

    def _monitor_for(request: Request) -> MonitorService:
        return cast(MonitorService, request.app.state.monitor)

    @app.get("/api/v1/incidents", response_model=IncidentListResponse)
    def list_incidents(
        request: Request,
        severity: str | None = None,
        status: str | None = None,
        label: str | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> dict[str, Any]:
        """The incident queue, newest first. Read-only."""
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        if severity is not None and severity not in {item.value for item in Severity}:
            raise HTTPException(status_code=422, detail=f"unknown severity: {severity}")
        if label is not None:
            try:
                Label(label)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"unknown label: {label}") from exc
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return {
            "items": catalog_for_request.list_incidents(
                severity=severity,
                status=status,
                label=label,
                limit=limit,
                before=_parse_cursor(before),
            )
        }

    @app.get("/api/v1/incidents/summary", response_model=IncidentSummaryResponse)
    def incidents_summary(request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return catalog_for_request.incident_summary()

    @app.get("/api/v1/incidents/{incident_id}", response_model=IncidentPayload)
    def get_incident(incident_id: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_incident(incident_id)
        if row is None:
            raise KeyError(incident_id)
        return row

    @app.post("/api/v1/incidents/{incident_id}/ack", response_model=IncidentPayload)
    def acknowledge_incident(incident_id: str, request: Request) -> dict[str, Any]:
        """Acknowledge: an append-only fact, so a recurrence opens a new incident."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.set_incident_status(incident_id, "acknowledged")
        if row is None:
            raise KeyError(incident_id)
        return row

    @app.post("/api/v1/incidents/{incident_id}/resolve", response_model=IncidentPayload)
    def resolve_incident(incident_id: str, request: Request) -> dict[str, Any]:
        """Resolve. A same-fault recurrence then opens a new incident, subject to cooldown."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.set_incident_status(incident_id, "resolved")
        if row is None:
            raise KeyError(incident_id)
        return row

    @app.get("/api/v1/monitoring/health", response_model=MonitoringHealthResponse)
    def monitoring_health(request: Request) -> dict[str, Any]:
        """The monitor's own state. A monitor that silently stops is the worst outcome."""
        return _monitor_for(request).health()

    @app.post("/api/v1/monitoring/tick", response_model=TickResponse)
    def run_monitor_tick(request: Request) -> dict[str, Any]:
        """Evaluate one window on demand.

        The loop is a scheduler's job, not a webhook's; this exists so the notifier
        can be driven deterministically from a test, a benchmark, or an operator who
        wants to see what it would say right now.
        """
        return (
            _monitor_for(request).tick(cast(PostgresCatalog, request.app.state.catalog)).to_dict()
        )

    @app.get("/api/v1/monitoring/notify-preview", response_class=PlainTextResponse)
    def notify_preview(request: Request) -> str:
        """What a notifier would send right now. Rendering only; nothing is sent.

        Email delivery is deliberately not implemented (ADR 0020 §10.3): it needs
        explicit owner authorization, and a dry run that is a real code path is the
        only honest way to leave it switched off.
        """
        return _monitor_for(request).notify_preview(
            cast(PostgresCatalog, request.app.state.catalog)
        )

    # ---- completion contracts ----

    @app.put("/api/v1/contracts/{job_id}", response_model=ContractPayload)
    def declare_contract(
        job_id: str, body: ContractRequest, request: Request, response: Response
    ) -> dict[str, Any]:
        """Declare what a run is expected to produce; idempotent per job.

        Re-declaring resets the outcome to pending, because a revised expectation
        has not been evaluated yet and claiming otherwise would hide a breach.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        if catalog_for_request.get_job(job_id) is None:
            raise KeyError(job_id)
        expectation = Expectation(
            expected_episodes=body.expected_episodes,
            expected_valid_fraction=body.expected_valid_fraction,
            max_duration_seconds=body.max_duration_seconds,
            deadline_at=body.deadline_at,
        )
        try:
            expectation.validate()
        except InvalidExpectation as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = catalog_for_request.register_contract(job_id, expectation.to_dict())
        response.headers["X-Correlation-Id"] = request.state.correlation_id or str(uuid4())
        return row

    @app.get("/api/v1/contracts", response_model=ContractListResponse)
    def list_contracts(
        request: Request, outcome: str | None = None, limit: int = 50, before: str | None = None
    ) -> dict[str, Any]:
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        return {
            "items": catalog_for_request.list_contracts(
                outcome=outcome, limit=limit, before=_parse_cursor(before)
            )
        }

    @app.get("/api/v1/contracts/{job_id}", response_model=ContractPayload)
    def get_contract(job_id: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_contract(job_id)
        if row is None:
            raise KeyError(job_id)
        return row

    # ---- incidents UI ----

    def _incidents_model(request: Request) -> dict[str, Any]:
        """One read of the queue, reused by the page, its fragment, and the digest."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        rows = catalog_for_request.list_incidents(limit=100)
        return {
            "items": rows,
            "summary": catalog_for_request.incident_summary(),
            "health": _monitor_for(request).health(),
            "digest": render_digest(rows),
        }

    @app.get("/ui/incidents", response_class=HTMLResponse)
    def incidents_page_route(
        request: Request,
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Honours `X-Fragment`, like every other polled page.

        This route used to ignore the header and always return a whole document,
        while the page's own `data-poll` pointed at this URL. The poller then
        injected the entire page - nav bar and all - inside the panel it was
        meant to replace, and the operator got a page inside a page. The
        separate `/ui/incidents/fragment` route that made the fragment reachable
        existed and was tested, but nothing ever polled it, which is why the bug
        survived a test that asserted the fragment was bare.
        """
        model = _incidents_model(request)
        if x_fragment:
            return HTMLResponse(incidents_fragment(model))
        return HTMLResponse(incidents_page(model, theme_or_default(theme)))

    return app
