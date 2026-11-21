"""Open routes: liveness and version."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str


class Version(BaseModel):
    version: str


@router.get("/healthz", response_model=Health, summary="The process is alive")
async def healthz() -> Health:
    return Health(status="ok")


@router.get("/version", response_model=Version, summary="The version from VERSION.txt")
async def version(request: Request) -> Version:
    return Version(version=request.app.version)
