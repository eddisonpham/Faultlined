"""Public API problem responses and request correlation middleware."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from data_engine.catalog.repository import IdempotencyConflict
from data_engine.observability.logging import correlation_id_var

logger = logging.getLogger(__name__)


def install_error_handling(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next: Any) -> Any:
        correlation_id = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
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
