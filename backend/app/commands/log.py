"""`/log` (pick the project first) and `/log pending`. `/log <text>` is recorded by ingestion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.connectors.base import SourceEvent

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    filing = dispatcher.options.get("filing")
    if filing is None:
        await dispatcher.reply(event, "Filing is not available.")
        return
    args = (event.command.args if event.command else "").strip().lower()
    if args == "pending":
        await filing.list_pending(event)
    elif not args:
        await filing.start_session(event)
    else:  # `/log <text>` never reaches here; ingestion records it
        await dispatcher.reply(event, "Recorded.")


async def tap(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    filing = dispatcher.options.get("filing")
    if filing is not None:
        await filing.tap(event)
