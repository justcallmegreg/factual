"""A connector that does nothing: for tests and for running the service without Telegram."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar

from app.connectors.base import (
    BlobRef,
    Capabilities,
    ChatRef,
    EventSink,
    MessageRef,
    OutboundMessage,
    SentRef,
)


class NullConnector:
    type: ClassVar[str] = "null"
    capabilities: ClassVar[Capabilities] = Capabilities(False, False, "none", 1, False)

    def __init__(self) -> None:
        self.sent: list[tuple[ChatRef, OutboundMessage]] = []
        self.acknowledged: list[MessageRef] = []
        self._stopped = asyncio.Event()

    async def start(self, sink: EventSink) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()

    async def fetch_blob(self, ref: BlobRef) -> AsyncIterator[bytes]:
        if False:  # pragma: no cover — an empty async generator
            yield b""

    async def send(self, chat: ChatRef, msg: OutboundMessage) -> SentRef:
        self.sent.append((chat, msg))
        return SentRef(MessageRef(chat, len(self.sent)))

    async def acknowledge(self, ref: MessageRef) -> None:
        self.acknowledged.append(ref)
