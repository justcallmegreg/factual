"""Open routes: liveness and version."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from app.core.logging import ctx

router = APIRouter(tags=["health"])
log = logging.getLogger("factual.api")


class Health(BaseModel):
    status: str


class Version(BaseModel):
    version: str


class Readiness(BaseModel):
    status: str
    database: str


@router.get("/healthz", response_model=Health, summary="The process is alive")
async def healthz() -> Health:
    return Health(status="ok")


@router.get("/version", response_model=Version, summary="The version from VERSION.txt")
async def version(request: Request) -> Version:
    return Version(version=request.app.version)


@router.get(
    "/readyz",
    response_model=Readiness,
    summary="The database works",
    responses={503: {"model": Readiness, "description": "A dependency does not answer"}},
)
async def readyz(request: Request, response: Response) -> Readiness:
    db = getattr(request.app.state, "db", None)
    problem = await db.check() if db is not None else "database not started"
    if problem:
        log.warning("readiness check failed: %s", problem, extra=ctx(action="readyz.failed"))
        response.status_code = 503
        return Readiness(status="not ready", database="unavailable")
    return Readiness(status="ready", database="ok")
