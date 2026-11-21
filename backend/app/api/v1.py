"""The read-only HTTP API for the future frontend and tools such as Claude Code.

Every route needs `Authorization: Bearer <API_TOKEN>`; when API_TOKEN is not set the
routes answer 404 and are, for all purposes, switched off.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import func, select

from app.ledger.audit import last_audit_info
from app.ledger.keys import key_fingerprint_of_pem
from app.ledger.verify import Verifier
from app.records.projects import list_projects
from app.records.search import search
from app.storage.models import (
    attachment,
    filing,
    ledger_entry,
    record,
    record_version,
    seal,
)

router = APIRouter(prefix="/api/v1", tags=["api"])
bearer = HTTPBearer(auto_error=False)
SWITCHED_OFF = "The API is switched off: API_TOKEN is not set."


async def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    settings = getattr(request.app.state, "settings", None)
    token = settings.api_token if settings is not None else None
    if not token:
        raise HTTPException(status_code=404, detail=SWITCHED_OFF)
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not secrets.compare_digest(credentials.credentials, token)
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")


Auth = Depends(require_token)


class StatusOut(BaseModel):
    label: str
    seal_no: int | None = None


class Author(BaseModel):
    id: int | str | None = None
    name: str | None = None
    username: str | None = None


class Forward(BaseModel):
    sender_name: str | None = None
    sender_id: int | None = None
    sent_at: datetime | None = None


class AttachmentOut(BaseModel):
    file_name: str | None = None
    kind: str
    mime: str | None = None
    size_bytes: int | None = None
    status: str
    sha256_plain: str | None = None
    entry_seq: int | None = None


class RecordOut(BaseModel):
    seq: int
    project: str | None
    origin: str
    author: Author
    text: str | None
    caption: str | None
    observed_at: datetime
    received_at: datetime
    stated_at: datetime | None
    tags: list[str]
    media: list[str]
    forward: Forward | None
    reply_to_seq: int | None
    versions: int
    status: StatusOut
    attachments: list[AttachmentOut]


class VersionOut(BaseModel):
    version: int
    entry_seq: int
    text: str | None
    caption: str | None
    observed_at: datetime


class FilingOut(BaseModel):
    entry_seq: int
    project: str
    previous: str | None
    by: str | None
    via: str
    filed_at: datetime


class RecordDetail(RecordOut):
    entry: dict
    version_list: list[VersionOut]
    filing_history: list[FilingOut]


class RecordsOut(BaseModel):
    records: list[RecordOut]
    limit: int
    offset: int


class ProjectOut(BaseModel):
    name: str
    records: int
    archived_at: datetime | None


class ProjectsOut(BaseModel):
    projects: list[ProjectOut]


class SealOut(BaseModel):
    no: int
    up_to_seq: int
    created_at: datetime


class LedgerOut(BaseModel):
    head_seq: int
    entries: int


class ApiStatus(BaseModel):
    ledger: LedgerOut
    last_seal: SealOut | None
    last_audit: dict | None
    records_without_project: int
    signing_key: str
    polling: str


class HitOut(BaseModel):
    seq: int
    project: str | None
    kind: str
    text: str
    origin: str
    author_name: str | None
    observed_at: datetime
    status: StatusOut


class SearchOut(BaseModel):
    words: list[str]
    project: str | None
    hits: list[HitOut]


def _verifier(request: Request) -> Verifier | None:
    return getattr(request.app.state, "verifier", None)


async def _status(conn, verifier: Verifier | None, seq: int) -> StatusOut:
    if verifier is None:
        return StatusOut(label="Pending seal")
    status = (await verifier.verify(conn, seq)).status
    return StatusOut(label=status.label, seal_no=status.seal_no)


async def _record_out(conn, verifier, row) -> RecordOut:
    attachments = (
        await conn.execute(select(attachment).where(attachment.c.record_seq == row.seq))
    ).all()
    return RecordOut(
        seq=int(row.seq),
        project=row.project,
        origin=row.origin,
        author=Author(id=row.author_id, name=row.author_name, username=row.author_username),
        text=row.text,
        caption=row.caption,
        observed_at=row.observed_at,
        received_at=row.received_at,
        stated_at=row.stated_at,
        tags=list(row.tags or []),
        media=list(row.media_kinds or []),
        forward=(
            Forward(
                sender_name=row.forward_sender_name,
                sender_id=row.forward_sender_id,
                sent_at=row.forward_sent_at,
            )
            if row.origin == "forwarded"
            else None
        ),
        reply_to_seq=row.reply_to_seq,
        versions=int(row.version),
        status=await _status(conn, verifier, int(row.seq)),
        attachments=[
            AttachmentOut(
                file_name=a.file_name,
                kind=a.kind,
                mime=a.mime,
                size_bytes=a.size_bytes,
                status=a.status,
                sha256_plain=bytes(a.sha256_plain).hex() if a.sha256_plain else None,
                entry_seq=a.entry_seq,
            )
            for a in attachments
        ],
    )


@router.get(
    "/status",
    response_model=ApiStatus,
    dependencies=[Auth],
    summary="Last seal, last audit, records without a project",
)
async def status(request: Request) -> ApiStatus:
    db = request.app.state.db
    async with db.engine.connect() as conn:
        head = (await conn.execute(select(func.max(ledger_entry.c.seq)))).scalar_one() or 0
        count = (await conn.execute(select(func.count()).select_from(ledger_entry))).scalar_one()
        last = (await conn.execute(select(seal).order_by(seal.c.no.desc()).limit(1))).first()
        unfiled = (
            await conn.execute(select(func.count()).where(record.c.project.is_(None)))
        ).scalar_one()
        audit = await last_audit_info(conn)
    connector = getattr(request.app.state, "connector", None)
    status_line = getattr(connector, "status_line", None)
    pack_builder = getattr(request.app.state, "pack_builder", None)
    fingerprint = key_fingerprint_of_pem(pack_builder.public_key_pem) if pack_builder else ""
    return ApiStatus(
        ledger=LedgerOut(head_seq=int(head), entries=int(count)),
        last_seal=(
            SealOut(no=int(last.no), up_to_seq=int(last.up_to_seq), created_at=last.created_at)
            if last
            else None
        ),
        last_audit=audit,
        records_without_project=int(unfiled),
        signing_key=fingerprint,
        polling=status_line() if callable(status_line) else "unknown",
    )


@router.get(
    "/projects",
    response_model=ProjectsOut,
    dependencies=[Auth],
    summary="Projects with their record counts",
)
async def projects(request: Request) -> ProjectsOut:
    async with request.app.state.db.engine.connect() as conn:
        infos = await list_projects(conn)
    return ProjectsOut(
        projects=[
            ProjectOut(name=i.name, records=i.records, archived_at=i.archived_at) for i in infos
        ]
    )


@router.get(
    "/records",
    response_model=RecordsOut,
    dependencies=[Auth],
    summary="Records, filtered by project, date and text",
)
async def records(
    request: Request,
    project: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    text: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecordsOut:
    query = select(record)
    if project:
        query = query.where(record.c.project == project)
    if from_ is not None:
        query = query.where(record.c.observed_at >= from_)
    if to is not None:
        query = query.where(record.c.observed_at <= to)
    if text:
        pattern = f"%{text}%"
        query = query.where(record.c.text.ilike(pattern) | record.c.caption.ilike(pattern))
    query = (
        query.order_by(record.c.observed_at.desc(), record.c.seq.desc()).limit(limit).offset(offset)
    )
    verifier = _verifier(request)
    async with request.app.state.db.engine.connect() as conn:
        rows = (await conn.execute(query)).all()
        items = [await _record_out(conn, verifier, row) for row in rows]
    return RecordsOut(records=items, limit=limit, offset=offset)


@router.get(
    "/records/{seq}",
    response_model=RecordDetail,
    dependencies=[Auth],
    summary="One record with its versions and filing history",
)
async def record_detail(request: Request, seq: int) -> RecordDetail:
    verifier = _verifier(request)
    async with request.app.state.db.engine.connect() as conn:
        row = (await conn.execute(select(record).where(record.c.seq == seq))).first()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No record E{seq}.")
        base = await _record_out(conn, verifier, row)
        entry_row = (
            await conn.execute(select(ledger_entry.c.entry).where(ledger_entry.c.seq == seq))
        ).first()
        versions = (
            await conn.execute(
                select(record_version)
                .where(record_version.c.record_seq == seq)
                .order_by(record_version.c.version)
            )
        ).all()
        filings = (
            await conn.execute(
                select(filing).where(filing.c.record_seq == seq).order_by(filing.c.seq)
            )
        ).all()
    import json

    return RecordDetail(
        **base.model_dump(),
        entry=json.loads(bytes(entry_row.entry)) if entry_row else {},
        version_list=[
            VersionOut(
                version=v.version,
                entry_seq=v.seq,
                text=v.text,
                caption=v.caption,
                observed_at=v.observed_at,
            )
            for v in versions
        ],
        filing_history=[
            FilingOut(
                entry_seq=f.seq,
                project=f.project,
                previous=f.previous,
                by=f.by_name,
                via=f.via,
                filed_at=f.filed_at,
            )
            for f in filings
        ],
    )


@router.get(
    "/search",
    response_model=SearchOut,
    dependencies=[Auth],
    summary="Full-text search, the same hits as /ask",
)
async def search_route(
    request: Request,
    q: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> SearchOut:
    verifier = _verifier(request)
    async with request.app.state.db.engine.connect() as conn:
        result = await search(conn, q, limit=limit, verifier=verifier)
    return SearchOut(
        words=result.words,
        project=result.project,
        hits=[
            HitOut(
                seq=h.seq,
                project=h.project,
                kind=h.kind,
                text=h.content,
                origin=h.origin,
                author_name=h.author_name,
                observed_at=h.observed_at,
                status=StatusOut(label=h.status.label, seal_no=h.status.seal_no),
            )
            for h in result.hits
        ],
    )


@router.get(
    "/records/{seq}/proof",
    dependencies=[Auth],
    summary="The evidence pack as a zip",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}, "description": "proof-E<seq>.zip"}},
)
async def proof(request: Request, seq: int) -> Response:
    builder = getattr(request.app.state, "pack_builder", None)
    if builder is None:
        raise HTTPException(status_code=503, detail="Evidence packs are not available.")
    try:
        pack = await builder.build([seq])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"No entry E{exc.args[0]}.") from exc
    return Response(
        content=pack.data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{pack.filename}"'},
    )
