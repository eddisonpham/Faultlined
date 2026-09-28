"""Server-rendered UI package for the MVP slice (ADR 0014)."""

from data_engine.web.pages import (
    artifacts_fragment,
    artifacts_page,
    job_detail_page,
    jobs_fragment,
    jobs_page,
    status_fragment,
    status_page,
    stylesheet,
)

__all__ = [
    "artifacts_fragment",
    "artifacts_page",
    "job_detail_page",
    "jobs_fragment",
    "jobs_page",
    "status_fragment",
    "status_page",
    "stylesheet",
]
