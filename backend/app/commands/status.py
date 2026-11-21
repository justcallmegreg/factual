"""`/status`: polling, the ledger head, unfiled records; later the seals and audits."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.connectors.base import SourceEvent
from app.storage.models import ledger_entry, record

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher


async def status_lines(dispatcher: CommandDispatcher) -> list[str]:
    status_line = getattr(dispatcher.connector, "status_line", None)
    polling = status_line() if callable(status_line) else "unknown"
    async with dispatcher.db.engine.connect() as conn:
        unfiled = (
            await conn.execute(select(func.count()).where(record.c.project.is_(None)))
        ).scalar_one()
        head = (await conn.execute(select(func.max(ledger_entry.c.seq)))).scalar_one() or 0
    lines = [
        f"Polling: {polling}",
        f"Records without a project: {unfiled}",
        f"Ledger: {head} entries, head E{head}" if head else "Ledger: empty",
    ]
    extra = dispatcher.options.get("status_extra")
    if extra is not None:
        lines.extend(await extra())
    else:
        lines.append("Last seal: none yet")
    return lines


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    await dispatcher.reply(event, "\n".join(await status_lines(dispatcher)))
