"""FastAPI application factory. Endpoints are implemented in the vertical slice (phase 05)."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Construct the API application; phase 05 adds the public routes."""
    return FastAPI(title="Robot Episode Data Engine", version="0.1.0")
