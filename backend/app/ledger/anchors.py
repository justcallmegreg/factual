"""Anchoring: every seal gets RFC 3161 tokens from the configured authorities, via jobs."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from sqlalchemy import insert, select

from app.connectors.base import ChatRef, Connector, OutboundMessage
from app.core.clock import Clock, system_clock
from app.core.logging import ctx
from app.ledger.seals import Seal, stamped_bytes
from app.ledger.tsa import TimestampClient
from app.storage.db import Database
from app.storage.jobs import Job, enqueue
from app.storage.models import seal as seal_table
from app.storage.models import seal_anchor

log = logging.getLogger("factual.anchors")

ANCHOR_JOB = "seal.anchor"
REPORT_AFTER_ATTEMPTS = 3
MAX_ATTEMPTS = 10


def host_of(url: str) -> str:
    return urlparse(url).hostname or url


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
    ) -> None:
        self.db = db
        self.connector = connector
        self.client = client
        self.urls = urls
        self.capture_chat = capture_chat
        self.clock = clock or system_clock

    async def after_seal(self, sealed: Seal) -> None:
        """The sealer's hook: one job per authority."""
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

    async def run(self, job: Job) -> None:
        seal_no = int(job.payload["seal_no"])
        url = job.payload["url"]
        kind = job.payload.get("kind", "tsa")
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(select(seal_table).where(seal_table.c.no == seal_no))).first()
            if row is None:
                raise LookupError(f"no seal {seal_no}")
            existing = (
                await conn.execute(
                    select(seal_anchor.c.id).where(
                        seal_anchor.c.seal_no == seal_no,
                        seal_anchor.c.issuer == url,
                        seal_anchor.c.kind == kind,
                    )
                )
            ).first()
        if existing is not None:
            return
        data = stamped_bytes(bytes(row.body), bytes(row.signature))
        try:
            token = await self.client.request(url, data)
        except Exception as exc:
            if job.attempts == REPORT_AFTER_ATTEMPTS:
                await self._notify(
                    f"The time-stamping authority {url} has not answered for seal {seal_no} after "
                    f"{job.attempts} attempts ({type(exc).__name__}). Thoth keeps retrying."
                )
            raise
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(seal_anchor).values(
                    seal_no=seal_no,
                    kind=kind,
                    issuer=url,
                    proof=token.response_bytes,
                    chain_pem=token.chain_pem or None,
                    gen_time=token.gen_time,
                    policy=token.policy,
                    added_at=self.clock(),
                )
            )
        log.info(
            "seal timestamped", extra=ctx(action="seal.anchored", seal=seal_no, tsa=host_of(url))
        )

    async def give_up(self, job: Job, error: str) -> None:
        await self._notify(
            f"Seal {job.payload['seal_no']} has no timestamp from {job.payload['url']} after "
            f"{job.attempts} attempts ({error[:80]}). The nightly audit will try again."
        )

    async def _notify(self, text: str) -> None:
        if self.connector is None or self.capture_chat is None:
            log.warning(text)
            return
        await self.connector.send(self.capture_chat, OutboundMessage(text))
