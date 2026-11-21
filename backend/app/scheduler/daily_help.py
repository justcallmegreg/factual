"""The daily help message: the command list and a status line at 06:00, once a day.

The run is recorded in `schedule_run`, so a restart never sends twice. After an
outage the message is still sent until 09:00; from 09:00 the day is skipped. The
previous day's message is deleted, so only the latest list stays. If no message
arrives by 09:00, Thoth is down or was down that morning.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, select

from app.commands.menu import help_text
from app.connectors.base import ChatRef, Connector, MessageRef, OutboundMessage
from app.core.clock import Clock, system_clock
from app.core.logging import ctx
from app.storage.db import Database
from app.storage.models import record, schedule_run, seal

log = logging.getLogger("factual.daily_help")

StatusExtra = Callable[[], Awaitable[str]]


def _parse_hhmm(value: str) -> tuple[int, int]:
    hours, minutes = value.split(":")
    return int(hours), int(minutes)


class DailyHelp:
    name = "daily_help"

    def __init__(
        self,
        db: Database,
        connector: Connector,
        chat: ChatRef,
        *,
        timezone: str,
        at: str = "06:00",
        until: str | None = None,  # three hours after `at` unless given
        clock: Clock | None = None,
        last_audit: StatusExtra | None = None,
    ) -> None:
        self.db = db
        self.connector = connector
        self.chat = chat
        self.zone = ZoneInfo(timezone)
        self.at = _parse_hhmm(at)
        self.until = _parse_hhmm(until) if until else None
        self.clock = clock or system_clock
        self.last_audit = last_audit

    def slot_for(self, now: datetime) -> datetime | None:
        """The day's 06:00 (as an instant) when `now` lies between 06:00 and 09:00 local time."""
        local = now.astimezone(self.zone)
        start = local.replace(hour=self.at[0], minute=self.at[1], second=0, microsecond=0)
        if self.until is not None:
            end = local.replace(hour=self.until[0], minute=self.until[1], second=0, microsecond=0)
        else:
            end = start + timedelta(hours=3)
        if start <= local < end:
            return start.astimezone(UTC)
        return None

    async def compose(self, now: datetime) -> str:
        local = now.astimezone(self.zone)
        async with self.db.engine.connect() as conn:
            unfiled = (
                await conn.execute(select(func.count()).where(record.c.project.is_(None)))
            ).scalar_one()
            last_seal = (
                await conn.execute(select(seal.c.created_at).order_by(seal.c.no.desc()).limit(1))
            ).first()
        seal_text = (
            f"{last_seal.created_at.astimezone(self.zone):%H:%M}" if last_seal else "none yet"
        )
        audit_text = await self.last_audit() if self.last_audit else "not yet run"
        noun = "record" if unfiled == 1 else "records"
        return (
            f"Good morning. Thoth's commands for {local:%a} {local.day} {local:%b %Y}:\n\n"
            f"{help_text()}\n\n"
            f"Status: {unfiled} {noun} without a project · last seal {seal_text} · "
            f"last audit {audit_text}"
        )

    async def tick(self) -> bool:
        now = self.clock()
        slot = self.slot_for(now)
        if slot is None:
            return False
        async with self.db.engine.connect() as conn:
            done = (
                await conn.execute(
                    select(schedule_run.c.id).where(
                        schedule_run.c.job == self.name,
                        schedule_run.c.scheduled_for == slot,
                        schedule_run.c.status == "sent",
                    )
                )
            ).first()
            previous = (
                await conn.execute(
                    select(schedule_run)
                    .where(
                        schedule_run.c.job == self.name,
                        schedule_run.c.status == "sent",
                        schedule_run.c.message_id.is_not(None),
                        schedule_run.c.scheduled_for < slot,
                    )
                    .order_by(schedule_run.c.scheduled_for.desc())
                    .limit(1)
                )
            ).first()
        if done is not None:
            return False
        text = await self.compose(now)
        try:
            sent = await self.connector.send(self.chat, OutboundMessage(text))
        except Exception as exc:  # noqa: BLE001 — try again at the next tick
            log.warning(
                "daily help not sent: %s", type(exc).__name__, extra=ctx(action="daily_help.retry")
            )
            return False
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(schedule_run).values(
                    job=self.name,
                    scheduled_for=slot,
                    ran_at=now,
                    chat_id=self.chat.chat_id,
                    message_id=sent.message.message_id,
                    status="sent",
                )
            )
        if previous is not None:
            try:
                await self.connector.delete_message(
                    MessageRef(
                        ChatRef(self.chat.connector, previous.chat_id), int(previous.message_id)
                    )
                )
            except Exception as exc:  # noqa: BLE001 — an undeleted old message is a nuisance, not a loss
                log.warning("previous daily help not deleted: %s", type(exc).__name__)
        log.info(
            "daily help sent",
            extra=ctx(action="daily_help.sent", message_id=sent.message.message_id),
        )
        return True
