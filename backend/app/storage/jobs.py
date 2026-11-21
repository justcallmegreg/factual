"""A job queue on PostgreSQL: SELECT … FOR UPDATE SKIP LOCKED, retries with a growing delay."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.clock import Clock, system_clock
from app.core.logging import ctx
from app.storage.db import Database
from app.storage.models import job as job_table

log = logging.getLogger("factual.jobs")


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    max_attempts: int


class JobHandler(Protocol):
    async def run(self, job: Job) -> None: ...

    async def give_up(self, job: Job, error: str) -> None: ...


async def enqueue(
    conn: AsyncConnection,
    kind: str,
    payload: dict,
    *,
    now: datetime,
    run_at: datetime | None = None,
    max_attempts: int = 5,
) -> int:
    """Queue a job inside the caller's transaction, so it commits with the entry it belongs to."""
    result = await conn.execute(
        insert(job_table)
        .values(
            kind=kind,
            payload=payload,
            status="queued",
            run_at=run_at or now,
            max_attempts=max_attempts,
            created_at=now,
        )
        .returning(job_table.c.id)
    )
    return int(result.scalar_one())


class JobWorker:
    """Claims due jobs one at a time and runs their handler."""

    def __init__(
        self,
        db: Database,
        handlers: dict[str, JobHandler],
        *,
        clock: Clock | None = None,
        poll_interval: float = 1.0,
        base_delay: float = 30.0,
    ) -> None:
        self.db = db
        self.handlers = handlers
        self.clock = clock or system_clock
        self.poll_interval = poll_interval
        self.base_delay = base_delay
        self._stopping = False

    async def run_once(self) -> bool:
        """Claim and run one due job. False when nothing is due."""
        now = self.clock()
        async with self.db.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(job_table)
                    .where(job_table.c.status == "queued", job_table.c.run_at <= now)
                    .order_by(job_table.c.run_at, job_table.c.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).first()
            if row is None:
                return False
            attempts = int(row.attempts) + 1
            await conn.execute(
                update(job_table)
                .where(job_table.c.id == row.id)
                .values(status="running", attempts=attempts, locked_at=now)
            )
            job = Job(int(row.id), row.kind, dict(row.payload), attempts, int(row.max_attempts))
        handler = self.handlers.get(job.kind)
        if handler is None:
            await self._finish(job, "failed", f"no handler for kind {job.kind!r}")
            return True
        try:
            await handler.run(job)
        except Exception as exc:  # noqa: BLE001 — every failure is retried or given up
            error = f"{type(exc).__name__}: {exc}"
            if job.attempts >= job.max_attempts:
                await self._finish(job, "failed", error)
                log.error("job given up", extra=ctx(action="job.failed", job=job.id, kind=job.kind))
                try:
                    await handler.give_up(job, error)
                except Exception as give_up_exc:  # noqa: BLE001
                    log.error(
                        "give_up handler failed: %s",
                        type(give_up_exc).__name__,
                        extra=ctx(job=job.id),
                    )
            else:
                delay = self.base_delay * (2 ** (job.attempts - 1))
                await self._reschedule(job, error, self.clock() + timedelta(seconds=delay))
                log.warning(
                    "job failed, retrying",
                    extra=ctx(
                        action="job.retry", job=job.id, attempt=job.attempts, retry_in_s=delay
                    ),
                )
        else:
            await self._finish(job, "done", None)
        return True

    async def _finish(self, job: Job, status: str, error: str | None) -> None:
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(job_table)
                .where(job_table.c.id == job.id)
                .values(status=status, last_error=error, finished_at=self.clock())
            )

    async def _reschedule(self, job: Job, error: str, run_at: datetime) -> None:
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(job_table)
                .where(job_table.c.id == job.id)
                .values(status="queued", last_error=error, run_at=run_at)
            )

    async def start(self) -> None:
        self._stopping = False
        while not self._stopping:
            try:
                ran = await self.run_once()
            except Exception as exc:  # noqa: BLE001 — the queue itself failed (database?)
                log.warning("job worker error: %s", type(exc).__name__)
                ran = False
            if not ran:
                await asyncio.sleep(self.poll_interval)

    async def stop(self) -> None:
        self._stopping = True

    async def pending(self, conn: AsyncConnection) -> int:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM job WHERE status IN ('queued','running')")
                )
            ).scalar_one()
        )
