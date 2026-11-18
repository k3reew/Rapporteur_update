"""Versioned API composed from domain routers."""

from fastapi import APIRouter

from rapporteur.api import (
    conferences,
    departments,
    files,
    reports,
    summaries,
    system,
    talks,
    workflow,
)

api = APIRouter(prefix="/api/v1")
for module in (conferences, files, talks, workflow, summaries, departments, reports, system):
    api.include_router(module.api)
