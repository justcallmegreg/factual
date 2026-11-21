"""`/verify`: one entry's hash check, seal and filing history, from the chat."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.connectors.base import SourceEvent
from app.core.clock import format_local
from app.ledger.verify import fast_verify
from app.storage.models import filing, record

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher

USAGE = "Usage: /verify as a reply to a record, or /verify E41233 with the entry number."
NUMBER = re.compile(r"^E?(\d+)$", re.IGNORECASE)


async def resolve_seq(dispatcher: CommandDispatcher, event: SourceEvent) -> int | str:
    """The entry number from the argument, or from the replied message."""
    args = (event.command.args if event.command else "").strip()
    if args:
        match = NUMBER.match(args)
        return int(match.group(1)) if match else USAGE
    reply_to = event.normalized.reply_to if event.normalized else None
    if reply_to is None:
        return USAGE
    async with dispatcher.db.engine.connect() as conn:
        row = (
            await conn.execute(
                select(record.c.seq).where(
                    record.c.chat_id == event.chat.chat_id,
                    record.c.message_id == reply_to.message_id,
                )
            )
        ).first()
    return int(row.seq) if row else "That message is not a recorded message."


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    seq = await resolve_seq(dispatcher, event)
    if isinstance(seq, str):
        await dispatcher.reply(event, seq)
        return
    async with dispatcher.db.engine.connect() as conn:
        verification = await fast_verify(conn, seq, dispatcher.ledger.genesis)
        if verification.entry is None and not verification.checks:
            await dispatcher.reply(event, f"No entry E{seq}.")
            return
        rec = (await conn.execute(select(record).where(record.c.seq == seq))).first()
        history = (
            await conn.execute(
                select(filing).where(filing.c.record_seq == seq).order_by(filing.c.seq)
            )
        ).all()
    zone = dispatcher.zone
    entry = verification.entry or {}
    lines: list[str] = []
    if rec is not None:
        project = rec.project or "no project"
        if rec.origin == "forwarded" and rec.forward_sender_name:
            sent = (
                format_local(rec.forward_sent_at, zone) if rec.forward_sent_at else "unknown time"
            )
            who = f"Forwarded from {rec.forward_sender_name}, sent {sent}"
        else:
            who = f"Note by {rec.author_name}" if rec.author_name else "Note"
        lines.append(f"E{seq} · {project} · {who}")
        lines.append(f"Recorded {format_local(rec.observed_at, zone)} · {verification.status}")
    else:
        lines.append(f"E{seq} · {entry.get('type', 'entry')}")
        observed = entry.get("observed_at")
        lines.append(f"Recorded {observed} · {verification.status}")
    lines.append(f"Checks: {verification.checks_line()}")
    if rec is not None and int(rec.version) > 1 and rec.edited_at is not None:
        lines.append(f"Versions: {rec.version} (edited {format_local(rec.edited_at, zone)})")
    if rec is not None:
        if history:
            lines.append("Filing history:")
            for row in history:
                change = f"{row.previous} → {row.project}" if row.previous else row.project
                by = f" · by {row.by_name}" if row.by_name else ""
                lines.append(f"• {format_local(row.filed_at, zone)} · {change}{by}")
        else:
            lines.append("Filing history: no project yet")
    await dispatcher.reply(event, "\n".join(lines))
