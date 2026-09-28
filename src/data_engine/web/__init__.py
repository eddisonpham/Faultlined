"""Server-rendered UI package for the MVP slice (ADR 0014)."""

from data_engine.web.pages import (
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

__all__ = [
    "DEFAULT_THEME",
    "THEMES",
    "artifacts_fragment",
    "artifacts_page",
    "job_detail_page",
    "jobs_fragment",
    "jobs_page",
    "layout_css",
    "status_fragment",
    "status_page",
    "vendor_css",
]
