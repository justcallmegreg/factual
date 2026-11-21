"""A small scheduler: every interval, give each job a tick. Jobs decide whether they are due."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from app.core.logging import ctx

log = logging.getLogger("factual.scheduler")


class ScheduledJob(Protocol):
    name: str

    async def tick(self) -> bool:
        """Run when due. True when something was done."""


class Scheduler:
    def __init__(self, jobs: list[ScheduledJob], *, interval: float = 60.0) -> None:
        self.jobs = jobs
        self.interval = interval
        self._stopping = False

    async def tick_all(self) -> None:
        for job in self.jobs:
            try:
                await job.tick()
            except Exception as exc:  # noqa: BLE001 — one job's failure must not stop the others
                log.error(
                    "scheduled job failed: %s",
                    type(exc).__name__,
                    extra=ctx(action="schedule.failed", kind=job.name),
                )

    async def start(self) -> None:
        self._stopping = False
        while not self._stopping:
            await self.tick_all()
            slept = 0.0
            while slept < self.interval and not self._stopping:
                step = min(0.5, self.interval - slept)
                await asyncio.sleep(step)
                slept += step

    async def stop(self) -> None:
        self._stopping = True
