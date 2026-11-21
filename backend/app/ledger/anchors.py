"""Anchoring: every seal gets RFC 3161 tokens from the configured authorities, via jobs.

Two optional anchors sit on top. With QUALIFIED_TSA_URL set, the first seal of each
local day also gets a qualified timestamp (eIDAS Article 41(2)); because the chain
links every entry, that stamp covers all earlier entries. And when the deep audit
sees that an authority's certificate expires soon, it requests a renewal: a new
token over the old one, so the old time stays provable after the certificate has
expired (the Evidence Record Syntax approach, RFC 4998).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlparse, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.connectors.base import ChatRef, Connector, OutboundMessage
from app.core.clock import Clock, system_clock
from app.core.logging import ctx
from app.ledger.seals import Seal, stamped_bytes
from app.ledger.tsa import TimestampClient
from app.storage.db import Database
from app.storage.jobs import Job, enqueue
from app.storage.models import schedule_run, seal_anchor
from app.storage.models import seal as seal_table

log = logging.getLogger("factual.anchors")

ANCHOR_JOB = "seal.anchor"
QUALIFIED_KIND = "qualified_tsa"
QUALIFIED_DAILY = "qualified_tsa"  # the schedule_run job name: one row per local day
REPORT_AFTER_ATTEMPTS = 3
MAX_ATTEMPTS = 10


def host_of(url: str) -> str:
    return urlparse(url).hostname or url


def public_url(url: str) -> str:
    """The address without credentials: what is stored, logged and shown."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class SealAnchoring:
    def __init__(
        self,
        db: Database,
        connector: Connector | None,
        client: TimestampClient,
        *,
        urls: list[str],
        capture_chat: ChatRef | None = None,
        clock: Clock | None = None,
        qualified_url: str | None = None,
        timezone: str = "UTC",
    ) -> None:
        self.db = db
        self.connector = connector
        self.client = client
        self.urls = urls
        self.capture_chat = capture_chat
        self.clock = clock or system_clock
        self.qualified_url = qualified_url
        self.zone = ZoneInfo(timezone)

    async def after_seal(self, sealed: Seal) -> None:
        """The sealer's hook: one job per authority, plus the day's qualified stamp."""
        now = self.clock()
        async with self.db.engine.begin() as conn:
            for url in self.urls:
                await enqueue(
                    conn,
                    ANCHOR_JOB,
                    {"seal_no": sealed.no, "url": url, "kind": "tsa"},
                    now=now,
                    max_attempts=MAX_ATTEMPTS,
                )
            if self.qualified_url and await self._first_seal_of_the_day(conn, sealed, now):
                await enqueue(
                    conn,
                    ANCHOR_JOB,
                    {"seal_no": sealed.no, "kind": QUALIFIED_KIND},  # the address stays in settings
                    now=now,
                    max_attempts=MAX_ATTEMPTS,
                )

    async def _first_seal_of_the_day(self, conn: AsyncConnection, sealed: Seal, now) -> bool:
        local = now.astimezone(self.zone)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        inserted = (
            await conn.execute(
                pg_insert(schedule_run)
                .values(
                    job=QUALIFIED_DAILY,
                    scheduled_for=day,
                    ran_at=now,
                    status="queued",
                    detail=f"seal {sealed.no}",
                )
                .on_conflict_do_nothing(index_elements=["job", "scheduled_for"])
                .returning(schedule_run.c.id)
            )
        ).first()
        return inserted is not None

    async def renew(self, conn: AsyncConnection, anchor, url: str, now: datetime) -> None:
        """Ask `url` for a token over an existing token (the deep audit's renewal)."""
        await enqueue(
            conn,
            ANCHOR_JOB,
            {"seal_no": int(anchor.seal_no), "url": url, "kind": "tsa", "renews": int(anchor.id)},
            now=now,
            max_attempts=MAX_ATTEMPTS,
        )

    def _address(self, payload: dict) -> str | None:
        if payload.get("kind", "tsa") == QUALIFIED_KIND:
            return self.qualified_url
        return payload.get("url")

    async def run(self, job: Job) -> None:
        seal_no = int(job.payload["seal_no"])
        kind = job.payload.get("kind", "tsa")
        renews = job.payload.get("renews")
        url = self._address(job.payload)
        if url is None:
            log.warning(
                "qualified timestamp for seal %d skipped: QUALIFIED_TSA_URL is unset", seal_no
            )
            return
        issuer = public_url(url)
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(select(seal_table).where(seal_table.c.no == seal_no))).first()
            if row is None:
                raise LookupError(f"no seal {seal_no}")
            old = None
            if renews is not None:
                old = (
                    await conn.execute(select(seal_anchor).where(seal_anchor.c.id == int(renews)))
                ).first()
                if old is None:
                    raise LookupError(f"no anchor {renews} to renew")
            same = select(seal_anchor.c.id).where(
                seal_anchor.c.seal_no == seal_no,
                seal_anchor.c.issuer == issuer,
                seal_anchor.c.kind == kind,
                seal_anchor.c.renews == int(renews)
                if renews is not None
                else seal_anchor.c.renews.is_(None),
            )
            existing = (await conn.execute(same)).first()
        if existing is not None:
            return
        data = (
            bytes(old.proof)
            if old is not None
            else stamped_bytes(bytes(row.body), bytes(row.signature))
        )
        try:
            token = await self.client.request(url, data)
        except Exception as exc:
            if job.attempts == REPORT_AFTER_ATTEMPTS:
                await self._notify(
                    f"The time-stamping authority {issuer} has not answered for seal {seal_no} "
                    f"after {job.attempts} attempts ({type(exc).__name__}). Thoth keeps retrying."
                )
            raise
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(seal_anchor).values(
                    seal_no=seal_no,
                    kind=kind,
                    issuer=issuer,
                    proof=token.response_bytes,
                    chain_pem=token.chain_pem or None,
                    gen_time=token.gen_time,
                    policy=token.policy,
                    added_at=self.clock(),
                    renews=int(renews) if renews is not None else None,
                )
            )
        log.info(
            "seal timestamped",
            extra=ctx(
                action="seal.renewed" if renews is not None else "seal.anchored",
                seal=seal_no,
                tsa=host_of(url),
            ),
        )

    async def give_up(self, job: Job, error: str) -> None:
        address = self._address(job.payload)
        await self._notify(
            f"Seal {job.payload['seal_no']} has no timestamp from "
            f"{public_url(address) if address else 'the qualified authority'} after "
            f"{job.attempts} attempts ({error[:80]}). The nightly audit will try again."
        )

    async def _notify(self, text: str) -> None:
        if self.connector is None or self.capture_chat is None:
            log.warning(text)
            return
        await self.connector.send(self.capture_chat, OutboundMessage(text))
