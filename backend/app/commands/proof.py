"""`/proof`: the evidence pack for one or more records, posted in the capture chat."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.connectors.base import OutboundFile, SourceEvent
from app.storage.models import record

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher

USAGE = "Usage: /proof as a reply to a record, or /proof E41233 E41502 with entry numbers."
SEND_LIMIT_BYTES = 50 * 1024 * 1024  # a bot sends files up to 50 MB
NUMBER = re.compile(r"^E?(\d+)$", re.IGNORECASE)


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    builder = dispatcher.options.get("pack_builder")
    if builder is None:
        await dispatcher.reply(event, "Evidence packs are not available.")
        return
    args = (event.command.args if event.command else "").split()
    seqs: list[int] = []
    for arg in args:
        match = NUMBER.match(arg)
        if not match:
            await dispatcher.reply(event, USAGE)
            return
        if len(match.group(1)) > 18:
            await dispatcher.reply(event, f"E{match.group(1)} is not a recorded entry.")
            return
        seqs.append(int(match.group(1)))
    if not seqs:
        reply_to = event.normalized.reply_to if event.normalized else None
        if reply_to is None:
            await dispatcher.reply(event, USAGE)
            return
        async with dispatcher.db.engine.connect() as conn:
            row = (
                await conn.execute(
                    select(record.c.seq).where(
                        record.c.chat_id == event.chat.chat_id,
                        record.c.message_id == reply_to.message_id,
                    )
                )
            ).first()
        if row is None:
            await dispatcher.reply(event, "That message is not a recorded message.")
            return
        seqs = [int(row.seq)]
    try:
        pack = await builder.build(seqs)
    except LookupError as exc:
        await dispatcher.reply(event, f"No entry E{exc.args[0]}.")
        return
    names = ", ".join(f"E{seq}" for seq in pack.records)
    if pack.size > SEND_LIMIT_BYTES:
        size_mb = pack.size / (1024 * 1024)
        await dispatcher.reply(
            event,
            f"The evidence pack for {names} is {size_mb:.0f} MB, over Telegram's 50 MB limit for "
            "files. Fetch it from the HTTP API instead: "
            f"GET /api/v1/records/{pack.records[0]}/proof (with the API token).",
        )
        return
    caption = f"Evidence pack for {names}. Unpack it and run: python3 verify.py"
    if pack.unsealed:
        caption += " (not yet sealed: " + ", ".join(f"E{s}" for s in pack.unsealed) + ")"
    await dispatcher.connector.send_file(
        event.chat,
        OutboundFile(
            filename=pack.filename, data=pack.data, caption=caption, reply_to=event.message
        ),
    )
