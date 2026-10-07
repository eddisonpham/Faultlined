"""Public API problem responses and request correlation middleware."""

from __future__ import annotations

import logging
import string
import uuid
from typing import Any

import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from data_engine.catalog.repository import (
    IdempotencyConflict,
    InvalidTransition,
    SliceNameConflict,
)
from data_engine.catalog.vocabulary import LabelConflict
from data_engine.observability.logging import correlation_id_var
from data_engine.web import error_page, theme_or_default

logger = logging.getLogger(__name__)

_CORRELATION_ID_CHARS = frozenset(string.ascii_letters + string.digits + "-_.:/+=@,; ")
CORRELATION_ID_MAX_LENGTH = 200


def safe_correlation_id(raw: str | None) -> str:
    """Honor the caller's correlation id when it is safe to echo; issue one otherwise."""
    if raw and len(raw) <= CORRELATION_ID_MAX_LENGTH and set(raw) <= _CORRELATION_ID_CHARS:
        return raw
    return str(uuid.uuid4())


def install_error_handling(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next: Any) -> Any:
        correlation_id = safe_correlation_id(request.headers.get("X-Correlation-Id"))
        request.state.correlation_id = correlation_id
        token = correlation_id_var.set(correlation_id)
        try:
            response = await call_next(request)
            response.headers["X-Correlation-Id"] = correlation_id
            return response
        finally:
            correlation_id_var.reset(token)

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict_handler(
        _request: Request, _exc: IdempotencyConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "type": "about:blank",
                "title": "Idempotency key conflict",
                "status": 409,
                "code": "IDEMPOTENCY_KEY_CONFLICT",
                "detail": "The idempotency key was already used for a different request.",
                "correlation_id": correlation_id_var.get(),
            },
        )

    @app.exception_handler(SliceNameConflict)
    async def slice_name_conflict_handler(
        _request: Request, exc: SliceNameConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "type": "about:blank",
                "title": "Slice name conflict",
                "status": 409,
                "code": "SLICE_NAME_CONFLICT",
                "detail": f"A curation slice already uses that name: {exc}",
                "correlation_id": correlation_id_var.get(),
            },
        )

    @app.exception_handler(LabelConflict)
    async def vocabulary_label_conflict_handler(
        _request: Request, exc: LabelConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "type": "about:blank",
                "title": "Vocabulary label conflict",
                "status": 409,
                "code": "VOCABULARY_LABEL_CONFLICT",
                "detail": f"The vocabulary already uses that preferred label: {exc}",
                "correlation_id": correlation_id_var.get(),
            },
        )

    @app.exception_handler(InvalidTransition)
    async def invalid_transition_handler(_request: Request, exc: InvalidTransition) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "type": "about:blank",
                "title": "Invalid state transition",
                "status": 409,
                "code": "INVALID_TRANSITION",
                "detail": f"The requested transition is not allowed: {exc}",
                "correlation_id": correlation_id_var.get(),
            },
        )

    @app.exception_handler(psycopg.OperationalError)
    async def database_unavailable_handler(
        request: Request, exc: psycopg.OperationalError
    ) -> Response:
        """A database outage answers 503, not 500 (failure-modes F10)."""
        correlation = correlation_id_var.get()
        logger.warning(
            "database unavailable correlation_id=%s path=%s error=%s",
            correlation,
            request.url.path,
            type(exc).__name__,
            extra={"event": "database_unavailable", "correlation_id": correlation},
        )
        headers = {"Retry-After": "5"}
        if _wants_html(request):
            return HTMLResponse(
                error_page(
                    503,
                    f"The database is temporarily unreachable; retry shortly. "
                    f"// correlation {correlation}",
                    theme_or_default(request.query_params.get("theme")),
                ),
                status_code=503,
                headers=headers,
            )
        return JSONResponse(
            status_code=503,
            headers=headers,
            content={
                "type": "about:blank",
                "title": "Service unavailable",
                "status": 503,
                "code": "SERVICE_UNAVAILABLE",
                "detail": "The database is temporarily unreachable. Retry with backoff.",
                "correlation_id": correlation,
            },
        )

    @app.exception_handler(KeyError)
    async def not_found_handler(_request: Request, _exc: KeyError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "type": "about:blank",
                "title": "Resource not found",
                "status": 404,
                "code": "RESOURCE_NOT_FOUND",
                "detail": "The requested resource does not exist.",
                "correlation_id": correlation_id_var.get(),
            },
        )

    @app.exception_handler(StarletteHTTPException)
    @app.exception_handler(FastAPIHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        """Render HTML for a browser navigation, JSON for an API call."""
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        if not _wants_html(request):
            return JSONResponse(
                status_code=exc.status_code,
                headers=getattr(exc, "headers", None),
                content={
                    "type": "about:blank",
                    "title": "Request failed",
                    "status": exc.status_code,
                    "code": "REQUEST_FAILED",
                    "detail": detail,
                    "correlation_id": correlation_id_var.get(),
                },
            )
        html = error_page(
            exc.status_code,
            f"{detail} // correlation {correlation_id_var.get()}",
            theme_or_default(request.query_params.get("theme")),
        )
        return HTMLResponse(html, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, _exc: Exception) -> Response:
        """Last resort."""
        correlation = correlation_id_var.get()
        logger.exception("unhandled error correlation_id=%s path=%s", correlation, request.url.path)
        if not _wants_html(request):
            return JSONResponse(
                status_code=500,
                content={
                    "type": "about:blank",
                    "title": "Internal error",
                    "status": 500,
                    "code": "INTERNAL_ERROR",
                    "detail": "An unexpected error occurred.",
                    "correlation_id": correlation,
                },
            )
        return HTMLResponse(
            error_page(
                500,
                f"The request failed unexpectedly. Quote correlation id {correlation} "
                "in a bug report.",
                theme_or_default(request.query_params.get("theme")),
            ),
            status_code=500,
        )


def _wants_html(request: Request) -> bool:
    """True for a browser navigation to a UI route."""
    return request.url.path.startswith("/ui") or "text/html" in request.headers.get("accept", "")
