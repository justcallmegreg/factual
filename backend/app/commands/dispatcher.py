"""Platform-neutral commands and button taps. Each command is implemented once here."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.connectors.base import Button, Connector, OutboundMessage, SentRef, SourceEvent
from app.core.logging import ctx
from app.ledger.ledger import Ledger
from app.storage.db import Database

log = logging.getLogger("factual.commands")

Handler = Callable[["CommandDispatcher", SourceEvent], Awaitable[None]]


class CommandDispatcher:
    """Routes `command` and `choice` events to their handlers and sends the replies."""

    def __init__(
        self, ledger: Ledger, db: Database, connector: Connector, *, timezone: str, **options
    ) -> None:
        self.ledger = ledger
        self.db = db
        self.connector = connector
        self.zone = ZoneInfo(timezone)
        self.options = options
        self.handlers: dict[str, Handler] = {}
        self.choice_handlers: dict[str, Handler] = {}  # keyed by the data prefix before ':'
        self._register_builtin()

    def _register_builtin(self) -> None:
        from app.commands import project

        self.handlers["project"] = project.handle

    async def handle(self, event: SourceEvent) -> None:
        if event.kind == "command" and event.command is not None:
            handler = self.handlers.get(event.command.name)
            if handler is None:
                log.debug("unknown command", extra=ctx(command=event.command.name))
                await self.reply(event, "Unknown command. /help lists the commands.")
                return
            log.info("command received", extra=ctx(action="command", command=event.command.name))
            await handler(self, event)
        elif event.kind == "choice" and event.choice is not None:
            prefix = event.choice.data.split(":", 1)[0]
            handler = self.choice_handlers.get(prefix)
            if handler is None:
                log.debug("tap without a handler", extra=ctx(kind=prefix))
                return
            await handler(self, event)

    async def reply(
        self,
        event: SourceEvent,
        text: str,
        *,
        buttons: tuple[tuple[Button, ...], ...] = (),
        reply: bool = True,
    ) -> SentRef:
        return await self.connector.send(
            event.chat,
            OutboundMessage(text=text, reply_to=event.message if reply else None, buttons=buttons),
        )
