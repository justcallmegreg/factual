"""FAC-16: the job queue on PostgreSQL (SKIP LOCKED) with retries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.storage.db import Database
from app.storage.jobs import Job, JobWorker, enqueue
from tests.chat_harness import FakeClock


@pytest.fixture
async def db(database_url):
    database = Database(database_url)
    yield database
    await database.dispose()


@dataclass
class Handler:
    fail_times: int = 0
    runs: list[dict] = field(default_factory=list)
    given_up: list[tuple[dict, str]] = field(default_factory=list)

    async def run(self, job: Job) -> None:
        self.runs.append(job.payload)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("download failed")

    async def give_up(self, job: Job, error: str) -> None:
        self.given_up.append((job.payload, error))


def worker(db, handler, clock, **kw) -> JobWorker:
    return JobWorker(db, {"test": handler}, clock=clock, poll_interval=0.01, base_delay=30.0, **kw)


async def test_a_queued_job_runs_once_and_is_marked_done(db):
    clock = FakeClock(datetime(2026, 11, 21, 6, 0, tzinfo=UTC))
    handler = Handler()
    async with db.engine.begin() as conn:
        job_id = await enqueue(conn, "test", {"n": 1}, now=clock())
    w = worker(db, handler, clock)
    assert await w.run_once() is True
    assert await w.run_once() is False
    assert handler.runs == [{"n": 1}]
    async with db.engine.connect() as conn:
        from sqlalchemy import text

        status, attempts = (
            await conn.execute(
                text("SELECT status, attempts FROM job WHERE id = :id"), {"id": job_id}
            )
        ).one()
    assert (status, attempts) == ("done", 1)


async def test_a_failing_job_is_retried_with_a_growing_delay_then_given_up(db):
    clock = FakeClock(datetime(2026, 11, 21, 6, 0, tzinfo=UTC))
    handler = Handler(fail_times=10)
    async with db.engine.begin() as conn:
        await enqueue(conn, "test", {"n": 2}, now=clock(), max_attempts=3)
    w = worker(db, handler, clock)
    assert await w.run_once() is True  # attempt 1 fails
    assert await w.run_once() is False, "not due yet"
    clock.advance(29)
    assert await w.run_once() is False
    clock.advance(2)  # 30 s after the first failure
    assert await w.run_once() is True  # attempt 2
    clock.advance(59)
    assert await w.run_once() is False
    clock.advance(2)  # 60 s after the second failure
    assert await w.run_once() is True  # attempt 3: the last
    assert len(handler.runs) == 3
    assert handler.given_up == [({"n": 2}, "RuntimeError: download failed")]
    clock.advance(600)
    assert await w.run_once() is False
    async with db.engine.connect() as conn:
        from sqlalchemy import text

        (status,) = (await conn.execute(text("SELECT status FROM job"))).one()
    assert status == "failed"


async def test_two_workers_never_run_the_same_job(db):
    clock = FakeClock(datetime(2026, 11, 21, 6, 0, tzinfo=UTC))

    class Slow(Handler):
        async def run(self, job: Job) -> None:
            await asyncio.sleep(0.05)
            await super().run(job)

    handler = Slow()
    async with db.engine.begin() as conn:
        for n in range(6):
            await enqueue(conn, "test", {"n": n}, now=clock())
    w1, w2 = worker(db, handler, clock), worker(db, handler, clock)
    await asyncio.gather(*(w1.run_once() for _ in range(3)), *(w2.run_once() for _ in range(3)))
    assert sorted(run["n"] for run in handler.runs) == list(range(6))


async def test_a_job_scheduled_for_later_waits(db):
    clock = FakeClock(datetime(2026, 11, 21, 6, 0, tzinfo=UTC))
    handler = Handler()
    async with db.engine.begin() as conn:
        await enqueue(
            conn, "test", {"later": True}, now=clock(), run_at=clock() + timedelta(minutes=5)
        )
    w = worker(db, handler, clock)
    assert await w.run_once() is False
    clock.advance(301)
    assert await w.run_once() is True


async def test_an_unknown_kind_is_failed_not_retried_forever(db):
    clock = FakeClock(datetime(2026, 11, 21, 6, 0, tzinfo=UTC))
    async with db.engine.begin() as conn:
        await enqueue(conn, "mystery", {}, now=clock())
    w = worker(db, Handler(), clock)
    assert await w.run_once() is True
    async with db.engine.connect() as conn:
        from sqlalchemy import text

        status, error = (await conn.execute(text("SELECT status, last_error FROM job"))).one()
    assert status == "failed" and "no handler" in error


async def test_start_and_stop_run_the_loop(db):
    clock = FakeClock(datetime(2026, 11, 21, 6, 0, tzinfo=UTC))
    handler = Handler()
    async with db.engine.begin() as conn:
        await enqueue(conn, "test", {"loop": 1}, now=clock())
    w = worker(db, handler, clock)
    task = asyncio.create_task(w.start())
    for _ in range(100):
        if handler.runs:
            break
        await asyncio.sleep(0.01)
    await w.stop()
    await asyncio.wait_for(task, timeout=2)
    assert handler.runs == [{"loop": 1}]
