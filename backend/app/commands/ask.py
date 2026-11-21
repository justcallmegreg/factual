"""`/ask <question>`: the matching records as verbatim quotes. Claude's verdict follows later."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.connectors.base import SourceEvent
from app.records.render import render_hits
from app.records.search import search

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher

USAGE = "Usage: /ask <question>, for example /ask did Greg say the kickoff is on 5 February?"
HIT_LIMIT = 5


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    question = (event.command.args if event.command else "").strip()
    if not question:
        await dispatcher.reply(event, USAGE)
        return
    async with dispatcher.db.engine.connect() as conn:
        result = await search(
            conn, question, limit=HIT_LIMIT, verifier=dispatcher.options.get("verifier")
        )
    if not result.hits:
        words = ", ".join(word.lower() for word in result.words) or "nothing searchable"
        await dispatcher.reply(event, f"No records match. I searched for: {words}.")
        return
    await dispatcher.reply(event, render_hits(result.hits, dispatcher.zone))
