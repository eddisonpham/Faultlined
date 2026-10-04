"""FastAPI application factory for the vertical slice."""

from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from data_engine.api.cluster_schemas import (
    ClusterConfirmRequest,
    ClusterDetailResponse,
    ClusterListResponse,
    ClusterRebuildRequest,
    ClusterReviewCandidate,
    ClusterReviewDecision,
    ClusterReviewListResponse,
    ClusterReviewRequest,
)
from data_engine.api.download import (
    CURSOR_START,
    columns_of,
    download_response,
    paged_rows,
    parse_download_format,
)
from data_engine.api.errors import install_error_handling
from data_engine.api.schemas import (
    AnyJobRequest,
    ArtifactListResponse,
    ArtifactSummary,
    BuildDetailResponse,
    BuildListResponse,
    ContractListResponse,
    ContractPayload,
    ContractRequest,
    EpisodeExportResponse,
    EpisodeListResponse,
    EpisodeQualityResponse,
    EpisodeResponse,
    EpisodeSummary,
    EpisodeValidationResponse,
    FailingEpisodeRow,
    FailingEpisodesResponse,
    FailureSummaryResponse,
    IncidentListResponse,
    IncidentPayload,
    IncidentSummaryResponse,
    IngestPayload,
    JobListResponse,
    JobReportResponse,
    JobResponse,
    JobSummary,
    MetricsResponse,
    MonitoringHealthResponse,
    QualitySummaryResponse,
    SliceCreateRequest,
    SliceDetailResponse,
    SliceImpactResponse,
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
from data_engine.api.vocabulary_schemas import (
    VocabularyCandidateListResponse,
    VocabularyDismissRequest,
    VocabularyEntry,
    VocabularyEntryCreateRequest,
    VocabularyEntryDetailResponse,
    VocabularyEntryUpdateRequest,
    VocabularyEventListResponse,
    VocabularyEventUndoResponse,
    VocabularyListResponse,
    VocabularyMapping,
    VocabularyMapRequest,
    VocabularyMergeRequest,
    VocabularyMergeResponse,
    VocabularySplitRequest,
    VocabularySplitResponse,
    VocabularyUnmappedResponse,
)
from data_engine.catalog import vocabulary as vocabulary_store
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
    benchmarks_fragment,
    benchmarks_page,
    builds_fragment,
    builds_page,
    episode_detail_page,
    episodes_fragment,
    episodes_page,
    experiments_fragment,
    experiments_page,
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
    slice_impact_page,
    slices_fragment,
    slices_page,
    status_fragment,
    status_page,
    theme_or_default,
    vendor_css,
    vocabulary_detail_page,
    vocabulary_page,
)
from data_engine.web.pages import EPISODE_FLAGS, EPISODE_STATES
from data_engine.web.records import benchmarks_model, experiments_model

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


#: Page bound shared by every list endpoint. Declared in the signature rather than
#: checked in each body so FastAPI rejects an out-of-range value itself and OpenAPI
#: documents the range. The routes previously disagreed - some hand-rolled a 1..500
#: check, others silently clamped to 200 - so the same parameter meant two different
#: things depending on which endpoint a client called.
Limit = Annotated[int, Query(ge=1, le=500)]

#: ``?format=csv|jsonl`` on a list route streams the same read as a file
#: download (ADR 0030). Named ``download`` in Python because ``format`` shadows
#: the builtin; the wire name is what matters. Absent = the JSON view.
Download = Annotated[str | None, Query(alias="format")]


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
        "queue_depth": catalog.count_jobs_by_state(),
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
        "jobs_queue_depth": catalog.count_jobs_by_state(),
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


def _download_redirect(request: Request, api_path: str) -> RedirectResponse:
    """Resolve a UI download link to the same read's serializer (ADR 0030).

    The link is the page's own URL with ``format`` added; the browser follows
    this redirect silently, so the download is literally the view on screen and
    the transport never appears in the operator's page.
    """
    query = str(request.url.query)
    return RedirectResponse(f"{api_path}?{query}" if query else api_path, status_code=302)


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
        limit: Limit = 50,
        before: str | None = None,
        download: Download = None,
    ) -> Response | dict[str, Any]:
        """Newest-first page of jobs for the Jobs UI.

        Cursor-based: pass the last item's ``created_at`` back as ``before``. The UI
        never asks for every row. ``?format=csv|jsonl`` streams the whole filtered
        set as a file download instead; ``limit``/``before`` page the JSON view only.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        parsed_state = _parse_state(state)
        fmt = parse_download_format(download)
        if fmt:

            def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
                return catalog_for_request.list_jobs(
                    state=parsed_state, job_type=type, limit=limit, before=before
                )

            return download_response(
                stem="jobs",
                fmt=fmt,
                columns=columns_of(JobSummary),
                rows=paged_rows(fetch),
                item_model=JobSummary,
            )
        parsed_before = _parse_cursor(before)
        rows = catalog_for_request.list_jobs(
            state=parsed_state, job_type=type, limit=limit, before=parsed_before
        )
        items = [_job_summary(row) for row in rows]
        next_before = rows[-1]["created_at"] if len(rows) == limit else None
        return {"items": items, "next_before": next_before}

    @app.get("/api/v1/artifacts", response_model=ArtifactListResponse)
    def list_artifacts(
        request: Request,
        limit: Limit = 50,
        before: str | None = None,
        download: Download = None,
    ) -> Response | dict[str, Any]:
        """Newest-first page of content-addressed artifacts with their referencing episodes.

        ``?format=csv|jsonl`` streams every artifact as a file download.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        fmt = parse_download_format(download)
        if fmt:

            def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
                return catalog_for_request.list_artifacts(limit=limit, before=before)

            return download_response(
                stem="artifacts",
                fmt=fmt,
                columns=columns_of(ArtifactSummary),
                rows=paged_rows(fetch),
                item_model=ArtifactSummary,
            )
        rows = catalog_for_request.list_artifacts(limit=limit, before=_parse_cursor(before))
        next_before = rows[-1]["created_at"] if len(rows) == limit else None
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
        download: Download = None,
    ) -> Response | dict[str, Any]:
        """Aggregated runtime telemetry from the JSONL metrics sink.

        Summaries describe every (metric, labels) sample set; series are bucketed
        means per metric name for sparklines. Worker heartbeat age is derived from
        the newest heartbeat record's timestamp, over all records rather than the
        window (a dead worker must not vanish from a narrow window).

        ``?format=csv|jsonl`` downloads the sparkline series (metric, t, v,
        count) for the same window.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = _metrics_model(configured, catalog_for_request, window_seconds, bucket_seconds)
        fmt = parse_download_format(download)
        if fmt:
            rows = [
                {"metric": name, **point}
                for name, points in model["series"].items()
                for point in points
            ]
            return download_response(
                stem="metrics-series",
                fmt=fmt,
                columns=("metric", "t", "v", "count"),
                rows=iter(rows),
            )
        return model

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
        download: Download = None,
        x_fragment: str | None = Header(default=None),
    ) -> Response:
        if parse_download_format(download):
            return _download_redirect(request, "/api/v1/jobs")
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
    def list_builds(request: Request, limit: Limit = 50) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        items = catalog_for_request.list_builds(limit=limit)
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
        download: Download = None,
        x_fragment: str | None = Header(default=None),
    ) -> Response:
        if parse_download_format(download):
            return _download_redirect(request, "/api/v1/artifacts")
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
        limit: Limit = 50,
        download: Download = None,
        x_fragment: str | None = Header(default=None),
    ) -> Response:
        if parse_download_format(download):
            return _download_redirect(request, "/api/v1/failures/episodes")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        model = {
            "summary": catalog_for_request.failure_summary(),
            "items": catalog_for_request.failing_episodes(limit=limit),
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

    @app.get("/ui/slices/{slice_id}", response_class=HTMLResponse)
    def ui_slice_detail(slice_id: str, request: Request, theme: str | None = None) -> HTMLResponse:
        """What this slice drops versus the whole dataset, and why."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        impact = catalog_for_request.slice_impact(slice_id)
        if impact is None:
            raise HTTPException(status_code=404, detail=f"No slice {slice_id}.")
        return HTMLResponse(slice_impact_page(impact, theme_or_default(theme)))

    @app.get("/ui/metrics", response_class=HTMLResponse)
    def ui_metrics(
        request: Request,
        window_seconds: float | None = None,
        theme: str | None = None,
        download: Download = None,
        x_fragment: str | None = Header(default=None),
    ) -> Response:
        """Live telemetry dashboard over the same model as the metrics read."""
        if parse_download_format(download):
            return _download_redirect(request, "/api/v1/metrics")
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
        download: Download = None,
        x_fragment: str | None = Header(default=None),
    ) -> Response:
        """Episode index with curation flags (jerky / stalled / short / long)."""
        if parse_download_format(download):
            return _download_redirect(request, "/api/v1/episodes")
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

    @app.get("/ui/benchmarks", response_class=HTMLResponse)
    def ui_benchmarks(
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Committed benchmark baselines and the runs recorded on this machine.

        Reads `benchmarks/` off disk, so it needs no database and no catalog
        table. A benchmark claim is a repository artifact, not a row.
        """
        model = benchmarks_model()
        if x_fragment:
            return HTMLResponse(benchmarks_fragment(model))
        return HTMLResponse(benchmarks_page(model, theme_or_default(theme)))

    @app.get("/ui/experiments", response_class=HTMLResponse)
    def ui_experiments(
        theme: str | None = None,
        x_fragment: str | None = Header(default=None),
    ) -> HTMLResponse:
        """Every experiment record, indexed from its own metadata block."""
        model = experiments_model()
        if x_fragment:
            return HTMLResponse(experiments_fragment(model))
        return HTMLResponse(experiments_page(model, theme_or_default(theme)))

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
        limit: Limit = 50,
        download: Download = None,
    ) -> Response | dict[str, Any]:
        """Scriptable episode catalog with the same curation views as the UI.

        ``?format=csv|jsonl`` streams every matching episode as a file download,
        newest first.
        """
        if state is not None and state not in EPISODE_STATES:
            raise HTTPException(status_code=422, detail=f"unknown episode state: {state}")
        if flag is not None and flag not in EPISODE_FLAGS:
            raise HTTPException(status_code=422, detail=f"unknown flag: {flag}")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        fmt = parse_download_format(download)
        if fmt:

            def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
                # Always through the cursor path (a far-future first cursor): it
                # orders newest-first, which is what pages consistently. The
                # flag views' signal ranking is a display affordance and would
                # duplicate rows across a page boundary.
                return catalog_for_request.list_episodes(
                    limit=limit,
                    state=state or None,
                    flag=flag or None,
                    before=before or CURSOR_START,
                )

            return download_response(
                stem="episodes",
                fmt=fmt,
                columns=columns_of(EpisodeSummary),
                rows=paged_rows(fetch),
                item_model=EpisodeSummary,
            )
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
        limit: Limit = 50,
        before: str | None = None,
        reason_code: str | None = None,
        download: Download = None,
    ) -> Response | dict[str, Any]:
        """Quarantined episodes with the reason codes that put them there.

        ``?format=csv|jsonl`` streams the whole quarantined set as a download.
        """
        if reason_code is not None and reason_code not in REASON_CODES:
            raise HTTPException(status_code=422, detail=f"unknown reason_code: {reason_code}")
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        fmt = parse_download_format(download)
        if fmt:

            def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
                return catalog_for_request.failing_episodes(
                    limit=limit, before=before, reason_code=reason_code or None
                )

            return download_response(
                stem="failures",
                fmt=fmt,
                columns=columns_of(FailingEpisodeRow),
                rows=paged_rows(fetch),
                item_model=FailingEpisodeRow,
            )
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

    # ------------------------------------------------------------- clusters (ADR 0026)

    def _run_payload(run: dict[str, Any] | None) -> dict[str, Any] | None:
        """A stored run as the API returns it, with a JSON-safe timestamp."""
        if run is None:
            return None
        created = run.get("created_at")
        stamp = created.isoformat() if isinstance(created, datetime) else None
        return {
            "id": int(run.get("id") or 0),
            "source": str(run.get("source") or ""),
            "ignored": str(run.get("ignored") or ""),
            "radius": float(run.get("radius") or 0.0),
            "rule": str(run.get("rule") or ""),
            "created_at": stamp,
            "health": dict(run.get("health") or {}),
        }

    def _cluster_model() -> dict[str, Any]:
        """Rehydrate the exact stored partition; page reads never recluster it."""
        from data_engine.catalog import clusters as cluster_store
        from data_engine.clustering import Ignored, Member, Proposal, ProposalSet, coverage, extract

        store = cluster_store.model(configured)
        ignored = store["ignored"] or Ignored()
        built: list[Proposal] = []
        all_members: list[Member] = []
        for row in store["rows"]:
            members = tuple(
                Member(
                    task=str(member["task"]),
                    core=str(member["core"]),
                    episodes=int(member["episodes"]),
                    verb=str(member.get("verb") or ""),
                    colours=tuple(str(value) for value in (member.get("colours") or [])),
                )
                for member in store["members"].get(row["key"], [])
            )
            all_members.extend(members)
            built.append(
                Proposal(
                    key=str(row["key"]),
                    core=str(row["core"]),
                    label=str(row["label"] or ""),
                    frozen=bool(row["label"]),
                    members=members,
                    centroid=tuple(float(value) for value in (row.get("centroid") or [])),
                    merged_cores=tuple(str(value) for value in (row.get("merged_cores") or [])),
                )
            )
        tasks = {member.task: member.episodes for member in all_members}
        measured = coverage([extract(task, ignored=ignored) for task in tasks])
        health = store.get("health", {}) or {}
        proposals = ProposalSet(
            proposals=tuple(built),
            coverage=measured,
            ignored=ignored,
            radius=float(store["radius"]),
            rule=str(store["rule"]),
            source=str(store["source"]),
            tasks=int(health.get("tasks", len(tasks))),
            episodes=int(health.get("episodes", sum(tasks.values()))),
            confirmed=int(health.get("frozen", sum(1 for item in built if item.frozen))),
        )
        model = dict(store)
        model["proposals"] = proposals
        model["health"] = health
        return model

    @app.get("/api/v1/clusters", response_model=ClusterListResponse)
    def list_clusters() -> dict[str, Any]:
        """Stored proposals with the health numbers of the run that produced them."""

        model = _cluster_model()
        return {
            "proposals": [
                {
                    "key": proposal.key,
                    "core": proposal.core,
                    "label": proposal.label,
                    "frozen": proposal.frozen,
                    "episodes": proposal.size,
                    "task_count": proposal.task_count,
                    "merged_cores": list(proposal.merged_cores),
                    "verbs": dict(proposal.verbs()),
                    "colours": dict(proposal.colours()),
                }
                for proposal in model["proposals"].proposals
            ],
            "run": _run_payload(model["run"]),
            "review_candidates": [
                ClusterReviewCandidate(**item) for item in model.get("review_candidates", [])
            ],
            "review_decisions": [
                ClusterReviewDecision.model_validate(item)
                for item in model.get("review_decisions", [])
            ],
            # The live numbers come from the set just rebuilt for the figures; the
            # orphaned count comes from the stored run, because it is a fact about the
            # last rebuild and cannot be recomputed from the proposals alone.
            "health": {
                **model["proposals"].health(),
                "orphaned": int((model["health"] or {}).get("orphaned", 0)),
                "truncated": bool((model["health"] or {}).get("truncated", False)),
            },
        }

    @app.post("/api/v1/clusters/rebuild", deprecated=True, include_in_schema=False)
    def rebuild_clusters(
        body: ClusterRebuildRequest,
    ) -> None:
        raise HTTPException(
            status_code=410,
            detail=f"Cluster rebuild for {body.source} is archived; use the vocabulary.",
        )

    @app.get("/api/v1/clusters/review", response_model=ClusterReviewListResponse)
    def list_cluster_review() -> dict[str, Any]:
        """Current bounded triage queue and the latest human decisions."""
        from data_engine.catalog import clusters as cluster_store

        return {
            "candidates": [
                ClusterReviewCandidate(**item)
                for item in cluster_store.review_candidates(configured)
            ],
            "decisions": [
                ClusterReviewDecision.model_validate(item)
                for item in cluster_store.review_decisions(configured)
            ],
        }

    @app.post("/api/v1/clusters/review", deprecated=True, include_in_schema=False)
    def decide_cluster_review(body: ClusterReviewRequest) -> None:
        raise HTTPException(
            status_code=410,
            detail=f"Cluster review ({body.disposition}) is archived; use the vocabulary.",
        )

    @app.delete("/api/v1/clusters/review", deprecated=True, include_in_schema=False)
    def reopen_cluster_review(
        tasks: Annotated[list[str], Query(min_length=1, max_length=25)],
    ) -> None:
        raise HTTPException(
            status_code=410,
            detail=f"Cluster review for {len(tasks)} task strings is archived; use the vocabulary.",
        )

    @app.get("/api/v1/clusters/{key}", response_model=ClusterDetailResponse)
    def cluster_detail(key: str) -> dict[str, Any]:
        """One proposal and the task strings behind it."""
        from data_engine.catalog import clusters as cluster_store

        row = cluster_store.get_proposal(configured, key)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No cluster {key}.")
        members = cluster_store.proposal_members(configured, key)
        return {
            "proposal": {
                "key": str(row["key"]),
                "core": str(row["core"]),
                "label": str(row["label"]),
                "frozen": bool(row["label"]),
                "episodes": int(row["episodes"]),
                "task_count": int(row["task_count"]),
                "merged_cores": list(row["merged_cores"] or []),
            },
            "members": [
                {
                    "task": str(member["task"]),
                    "core": str(member["core"]),
                    "episodes": int(member["episodes"]),
                    "verb": str(member["verb"]),
                    "colours": list(member["colours"] or []),
                }
                for member in members
            ],
        }

    @app.post("/api/v1/clusters/{key}/confirm", deprecated=True, include_in_schema=False)
    def confirm_cluster(key: str, body: ClusterConfirmRequest) -> None:
        raise HTTPException(
            status_code=410,
            detail=f"Cluster proposal {key} ({body.label}) is archived; use the vocabulary.",
        )

    @app.delete("/api/v1/clusters/{key}/confirm", deprecated=True, include_in_schema=False)
    def release_cluster(key: str) -> None:
        raise HTTPException(
            status_code=410,
            detail=f"Cluster proposal {key} is archived; use the vocabulary.",
        )

    # The frozen cluster API remains available as a read-only archive; the vocabulary
    # interface below is now the only active curation workflow (ADR 0029).

    @app.get("/ui/clusters", include_in_schema=False)
    def ui_clusters_redirect() -> RedirectResponse:
        return RedirectResponse("/ui/vocabulary", status_code=307)

    @app.get("/ui/clusters/{key}", include_in_schema=False)
    def ui_cluster_detail_redirect(key: str) -> RedirectResponse:
        return RedirectResponse(f"/ui/vocabulary?from_cluster={quote(key)}", status_code=307)

    @app.post("/ui/clusters/rebuild", include_in_schema=False)
    @app.post("/ui/clusters/review", include_in_schema=False)
    @app.post("/ui/clusters/review/undo", include_in_schema=False)
    def ui_cluster_action_redirect() -> RedirectResponse:
        return RedirectResponse("/ui/vocabulary", status_code=303)

    @app.post("/ui/clusters/{key}/confirm", include_in_schema=False)
    @app.post("/ui/clusters/{key}/release", include_in_schema=False)
    def ui_cluster_detail_action_redirect(key: str) -> RedirectResponse:
        return RedirectResponse(f"/ui/vocabulary?from_cluster={quote(key)}", status_code=303)

    # ---------------------------------------------------------- vocabulary (ADR 0029)

    @app.get("/api/v1/vocabulary", response_model=VocabularyListResponse)
    def list_vocabulary() -> dict[str, Any]:
        entries = vocabulary_store.list_entries(configured)
        return {"entries": entries, "health": vocabulary_store.vocabulary_health(configured)}

    @app.post("/api/v1/vocabulary", response_model=VocabularyEntryDetailResponse, status_code=201)
    def create_vocabulary_entry(body: VocabularyEntryCreateRequest) -> dict[str, Any]:
        entry = vocabulary_store.create_entry(
            configured, preferred_label=body.preferred_label, notes=body.notes
        )
        return {"entry": entry, "members": []}

    @app.get("/api/v1/vocabulary/unmapped", response_model=VocabularyUnmappedResponse)
    def list_vocabulary_unmapped(
        limit: Limit = 50,
        after_episodes: int | None = Query(default=None, ge=0),
        after_task: str | None = None,
    ) -> dict[str, Any]:
        if (after_episodes is None) != (after_task is None):
            raise HTTPException(
                status_code=422, detail="after_episodes and after_task must be paired"
            )
        after = (
            (after_episodes, after_task)
            if after_episodes is not None and after_task is not None
            else None
        )
        items = vocabulary_store.list_unmapped(configured, limit=limit, after=after)
        next_after = None
        if len(items) == limit:
            next_after = {
                "episodes": items[-1]["episodes"],
                "task_string": items[-1]["task_string"],
            }
        return {"items": items, "next_after": next_after}

    @app.get("/api/v1/vocabulary/candidates", response_model=VocabularyCandidateListResponse)
    def list_vocabulary_candidates(limit: Limit = 50) -> dict[str, Any]:
        from data_engine.clustering.ranker import candidates

        entries = vocabulary_store.list_entries(configured)
        queue = vocabulary_store.list_unmapped(configured, limit=limit)
        return {"items": [candidate.as_dict() for candidate in candidates(queue, entries)]}

    @app.post("/api/v1/vocabulary/mappings", response_model=VocabularyMapping)
    def map_vocabulary_task(body: VocabularyMapRequest) -> dict[str, Any]:
        if vocabulary_store.get_entry(configured, body.entry_id) is None:
            raise HTTPException(status_code=404, detail=f"No vocabulary entry {body.entry_id}.")
        return vocabulary_store.map_task(
            configured, task_string=body.task_string, entry_id=body.entry_id
        )

    @app.post("/api/v1/vocabulary/dismissals", response_model=VocabularyMapping)
    def dismiss_vocabulary_task(body: VocabularyDismissRequest) -> dict[str, Any]:
        return vocabulary_store.dismiss_task(configured, task_string=body.task_string)

    @app.get("/api/v1/vocabulary/events", response_model=VocabularyEventListResponse)
    def list_vocabulary_events(limit: Limit = 50) -> dict[str, Any]:
        return {"items": vocabulary_store.list_events(configured, limit=limit)}

    @app.post(
        "/api/v1/vocabulary/events/{event_id}/undo", response_model=VocabularyEventUndoResponse
    )
    def undo_vocabulary_event(event_id: int) -> dict[str, Any]:
        try:
            return vocabulary_store.undo_event(configured, event_id=event_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/v1/vocabulary/{entry_id}", response_model=VocabularyEntryDetailResponse)
    def get_vocabulary_entry(entry_id: str) -> dict[str, Any]:
        entry = vocabulary_store.get_entry(configured, entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"No vocabulary entry {entry_id}.")
        return {"entry": entry, "members": vocabulary_store.entry_members(configured, entry_id)}

    @app.patch("/api/v1/vocabulary/{entry_id}", response_model=VocabularyEntry)
    def update_vocabulary_entry(
        entry_id: str, body: VocabularyEntryUpdateRequest
    ) -> dict[str, Any]:
        try:
            entry = vocabulary_store.get_entry(configured, entry_id)
            if entry is None:
                raise KeyError(entry_id)
            if body.preferred_label is not None:
                entry = vocabulary_store.rename_entry(
                    configured, entry_id=entry_id, preferred_label=body.preferred_label
                )
            if body.notes is not None:
                entry = vocabulary_store.update_notes(
                    configured, entry_id=entry_id, notes=body.notes
                )
            return entry
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail=f"No vocabulary entry {entry_id}."
            ) from error

    @app.post("/api/v1/vocabulary/{entry_id}/merge", response_model=VocabularyMergeResponse)
    def merge_vocabulary_entry(entry_id: str, body: VocabularyMergeRequest) -> dict[str, Any]:
        try:
            return vocabulary_store.merge_entries(
                configured, source_id=entry_id, target_id=body.target_entry_id
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail="The source or target entry does not exist."
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.post("/api/v1/vocabulary/{entry_id}/split", response_model=VocabularySplitResponse)
    def split_vocabulary_entry(entry_id: str, body: VocabularySplitRequest) -> dict[str, Any]:
        try:
            return vocabulary_store.split_entry(
                configured,
                entry_id=entry_id,
                task_strings=body.task_strings,
                new_label=body.new_label,
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail=f"No vocabulary entry {entry_id}."
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    # The UI redirects legacy cluster bookmarks; the API remains a frozen archive.
    @app.get("/ui/vocabulary", response_class=HTMLResponse)
    def ui_vocabulary(
        theme: str | None = None,
        error: str | None = None,
        from_cluster: str | None = None,
    ) -> HTMLResponse:
        from data_engine.clustering.ranker import candidates

        entries = vocabulary_store.list_entries(configured)
        unmapped = vocabulary_store.list_unmapped(configured)
        model = {
            "entries": entries,
            "unmapped": unmapped,
            "candidates": [item.as_dict() for item in candidates(unmapped, entries)],
            "health": vocabulary_store.vocabulary_health(configured),
            "events": vocabulary_store.list_events(configured),
            "error": error or "",
            "legacy_cluster": from_cluster or "",
        }
        return HTMLResponse(vocabulary_page(model, theme_or_default(theme)))

    @app.get("/ui/vocabulary/entries/{entry_id}", response_class=HTMLResponse)
    def ui_vocabulary_entry(
        entry_id: str, theme: str | None = None, error: str | None = None
    ) -> HTMLResponse:
        entry = vocabulary_store.get_entry(configured, entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"No vocabulary entry {entry_id}.")
        return HTMLResponse(
            vocabulary_detail_page(
                entry,
                vocabulary_store.entry_members(configured, entry_id),
                vocabulary_store.list_entries(configured),
                theme_or_default(theme),
                error=error or "",
            )
        )

    async def _vocabulary_form(request: Request) -> dict[str, list[str]]:
        from urllib.parse import parse_qs

        return parse_qs((await request.body()).decode("utf-8", "replace"))

    def _vocabulary_redirect(
        error: Exception | None = None, entry_id: str | None = None
    ) -> RedirectResponse:
        destination = f"/ui/vocabulary/entries/{quote(entry_id)}" if entry_id else "/ui/vocabulary"
        if error is not None:
            destination += f"?error={quote(str(error))}"
        return RedirectResponse(destination, status_code=303)

    @app.post("/ui/vocabulary/entries")
    async def ui_create_vocabulary(request: Request) -> RedirectResponse:
        form = await _vocabulary_form(request)
        try:
            vocabulary_store.create_entry(
                configured,
                preferred_label=(form.get("preferred_label") or [""])[0],
                notes=(form.get("notes") or [""])[0],
            )
        except (KeyError, ValueError) as error:
            return _vocabulary_redirect(error)
        return _vocabulary_redirect()

    @app.post("/ui/vocabulary/accept")
    async def ui_accept_vocabulary_candidate(request: Request) -> RedirectResponse:
        from data_engine.clustering.ranker import candidates

        form = await _vocabulary_form(request)
        tasks = form.get("task", [])
        kind = (form.get("kind") or [""])[0]
        entry_id = (form.get("entry_id") or [""])[0]
        current_entries = vocabulary_store.list_entries(configured)
        current_queue = vocabulary_store.list_unmapped(configured)
        suggestions = candidates(current_queue, current_entries)
        submitted = tuple(sorted(set(tasks)))
        accepted = next(
            (
                item
                for item in suggestions
                if item.task_strings == submitted
                and item.kind == kind
                and (kind != "attach" or item.entry_id == entry_id)
            ),
            None,
        )
        if accepted is None:
            return _vocabulary_redirect(ValueError("candidate changed; reload and review it again"))
        try:
            if kind == "attach":
                vocabulary_store.accept_candidate(
                    configured,
                    task_strings=accepted.task_strings,
                    entry_id=entry_id,
                    expected_core=accepted.core,
                )
            else:
                label = (form.get("new_label") or [""])[0]
                vocabulary_store.accept_candidate(
                    configured,
                    task_strings=accepted.task_strings,
                    new_label=label,
                    expected_core=accepted.core,
                )
        except (KeyError, ValueError) as error:
            return _vocabulary_redirect(error)
        return _vocabulary_redirect()

    @app.post("/ui/vocabulary/map")
    async def ui_map_vocabulary_task(request: Request) -> RedirectResponse:
        form = await _vocabulary_form(request)
        try:
            task = (form.get("task_string") or [""])[0]
            entry_id = (form.get("entry_id") or [""])[0]
            if vocabulary_store.get_entry(configured, entry_id) is None:
                raise ValueError("choose an existing vocabulary entry")
            vocabulary_store.map_task(configured, task_string=task, entry_id=entry_id)
        except (KeyError, ValueError) as error:
            return _vocabulary_redirect(error)
        return _vocabulary_redirect()

    @app.post("/ui/vocabulary/dismiss")
    async def ui_dismiss_vocabulary_task(request: Request) -> RedirectResponse:
        form = await _vocabulary_form(request)
        task = (form.get("task_string") or [""])[0]
        if not task.strip():
            return _vocabulary_redirect(ValueError("choose a task string to dismiss"))
        vocabulary_store.dismiss_task(configured, task_string=task)
        return _vocabulary_redirect()

    @app.post("/ui/vocabulary/entries/{entry_id}/rename")
    async def ui_rename_vocabulary_entry(entry_id: str, request: Request) -> RedirectResponse:
        form = await _vocabulary_form(request)
        try:
            vocabulary_store.rename_entry(
                configured,
                entry_id=entry_id,
                preferred_label=(form.get("preferred_label") or [""])[0],
            )
        except (KeyError, ValueError) as error:
            return _vocabulary_redirect(error, entry_id)
        return _vocabulary_redirect(entry_id=entry_id)

    @app.post("/ui/vocabulary/entries/{entry_id}/notes")
    async def ui_notes_vocabulary_entry(entry_id: str, request: Request) -> RedirectResponse:
        form = await _vocabulary_form(request)
        try:
            vocabulary_store.update_notes(
                configured, entry_id=entry_id, notes=(form.get("notes") or [""])[0]
            )
        except KeyError as error:
            return _vocabulary_redirect(error)
        return _vocabulary_redirect(entry_id=entry_id)

    @app.post("/ui/vocabulary/entries/{entry_id}/merge")
    async def ui_merge_vocabulary_entry(entry_id: str, request: Request) -> RedirectResponse:
        form = await _vocabulary_form(request)
        try:
            vocabulary_store.merge_entries(
                configured, source_id=entry_id, target_id=(form.get("target_entry_id") or [""])[0]
            )
        except (KeyError, ValueError) as error:
            return _vocabulary_redirect(error, entry_id)
        return _vocabulary_redirect()

    @app.post("/ui/vocabulary/entries/{entry_id}/split")
    async def ui_split_vocabulary_entry(entry_id: str, request: Request) -> RedirectResponse:
        form = await _vocabulary_form(request)
        try:
            vocabulary_store.split_entry(
                configured,
                entry_id=entry_id,
                task_strings=form.get("task", []),
                new_label=(form.get("new_label") or [""])[0],
            )
        except (KeyError, ValueError) as error:
            return _vocabulary_redirect(error, entry_id)
        return _vocabulary_redirect(entry_id=entry_id)

    @app.post("/ui/vocabulary/events/{event_id}/undo")
    def ui_undo_vocabulary_event(event_id: int) -> RedirectResponse:
        try:
            vocabulary_store.undo_event(configured, event_id=event_id)
        except (KeyError, ValueError) as error:
            return _vocabulary_redirect(error)
        return _vocabulary_redirect()

    @app.get("/api/v1/slices", response_model=SliceListResponse)
    def list_slices(
        request: Request, limit: Limit = 50, before: str | None = None
    ) -> dict[str, Any]:
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

    @app.get("/api/v1/slices/{slice_id}/impact", response_model=SliceImpactResponse)
    def slice_impact(slice_id: str, request: Request) -> dict[str, Any]:
        """What the slice drops versus the whole dataset, and why (read-only)."""
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        impact = catalog_for_request.slice_impact(slice_id)
        if impact is None:
            raise KeyError(slice_id)
        return impact

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
        limit: Limit = 100,
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
        limit: Limit = 50,
        before: str | None = None,
        download: Download = None,
    ) -> Response | dict[str, Any]:
        """The incident queue, newest first. Read-only.

        ``?format=csv|jsonl`` streams the filtered queue as a download.
        """
        if severity is not None and severity not in {item.value for item in Severity}:
            raise HTTPException(status_code=422, detail=f"unknown severity: {severity}")
        if label is not None:
            try:
                Label(label)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"unknown label: {label}") from exc
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        fmt = parse_download_format(download)
        if fmt:

            def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
                return catalog_for_request.list_incidents(
                    severity=severity,
                    status=status,
                    label=label,
                    limit=limit,
                    before=before,
                )

            return download_response(
                stem="incidents",
                fmt=fmt,
                columns=columns_of(IncidentPayload),
                rows=paged_rows(fetch),
                item_model=IncidentPayload,
            )
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
        request: Request,
        outcome: str | None = None,
        limit: Limit = 50,
        before: str | None = None,
        download: Download = None,
    ) -> Response | dict[str, Any]:
        """Completion contracts, newest first.

        ``?format=csv|jsonl`` streams the filtered set as a download.
        """
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        fmt = parse_download_format(download)
        if fmt:

            def fetch(*, limit: int, before: datetime | None) -> list[dict[str, Any]]:
                return catalog_for_request.list_contracts(
                    outcome=outcome, limit=limit, before=before
                )

            return download_response(
                stem="contracts",
                fmt=fmt,
                columns=columns_of(ContractPayload),
                rows=paged_rows(fetch),
                item_model=ContractPayload,
            )
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
        download: Download = None,
        x_fragment: str | None = Header(default=None),
    ) -> Response:
        if parse_download_format(download):
            return _download_redirect(request, "/api/v1/incidents")
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
