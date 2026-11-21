"""`/help` and `/start`, plus the commands that arrive with a later phase."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.commands.menu import help_text
from app.connectors.base import SourceEvent

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    await dispatcher.reply(event, help_text())
