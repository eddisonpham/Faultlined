"""FastAPI application factory for the vertical slice."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Header, Request, Response

from data_engine.api.errors import install_error_handling
from data_engine.api.schemas import EpisodeResponse, JobResponse, SubmitJobRequest
from data_engine.catalog.database import initialize_schema
from data_engine.catalog.repository import PostgresCatalog
from data_engine.config import Settings, load_settings


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

    @app.get("/api/v1/episodes/{episode_id}", response_model=EpisodeResponse)
    def get_episode(episode_id: str, request: Request) -> dict[str, Any]:
        catalog_for_request = cast(PostgresCatalog, request.app.state.catalog)
        row = catalog_for_request.get_episode(episode_id)
        if row is None:
            raise KeyError(episode_id)
        return row

    return app
