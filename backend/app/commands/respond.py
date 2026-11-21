"""`/respond`: as a reply to the record that holds the claim, or with the claim pasted."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.connectors.base import SourceEvent
from app.storage.models import record

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher

USAGE = "Usage: /respond as a reply to the record that holds the claim, or /respond <the claim>."


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    engine = dispatcher.options.get("respond_engine")
    if engine is None:
        await dispatcher.reply(event, "/respond is not available: Claude is not configured.")
        return
    claim = (event.command.args if event.command else "").strip()
    claim_seq = None
    if not claim:
        reply_to = event.normalized.reply_to if event.normalized else None
        if reply_to is None:
            await dispatcher.reply(event, USAGE)
            return
        async with dispatcher.db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(record.c.seq, record.c.text, record.c.caption).where(
                        record.c.chat_id == event.chat.chat_id,
                        record.c.message_id == reply_to.message_id,
                    )
                )
            ).first()
        if row is None:
            await dispatcher.reply(event, "That message is not a recorded message.")
            return
        claim_seq = int(row.seq)
        claim = (row.text or row.caption or "").strip()
    await engine.answer(dispatcher, event, claim, claim_seq)
