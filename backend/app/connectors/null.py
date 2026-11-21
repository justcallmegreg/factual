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
    OutboundFile,
    OutboundMessage,
    SentRef,
)


class NullConnector:
    type: ClassVar[str] = "null"
    capabilities: ClassVar[Capabilities] = Capabilities(False, False, "none", 1, False)

    def __init__(self) -> None:
        self.sent: list[tuple[ChatRef, OutboundMessage]] = []
        self.message_ids: list[int] = []  # the id each sent (or edited) message got
        self.acknowledged: list[MessageRef] = []
        self.files: dict[str, bytes] = {}  # file_id → bytes that fetch_blob streams
        self.files_sent: list[OutboundFile] = []
        self._next_id = 0
        self._stopped = asyncio.Event()

    async def start(self, sink: EventSink) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()

    async def fetch_blob(self, ref: BlobRef) -> AsyncIterator[bytes]:
        if ref.file_id not in self.files:
            raise LookupError(f"no such file {ref.file_id}")
        data = self.files[ref.file_id]
        for start in range(0, len(data), 1024):
            yield data[start : start + 1024]

    async def send(self, chat: ChatRef, msg: OutboundMessage) -> SentRef:
        self.sent.append((chat, msg))
        if msg.edit_of is not None:
            message_id = msg.edit_of.message_id
        else:
            self._next_id += 1
            message_id = self._next_id
        self.message_ids.append(message_id)
        return SentRef(MessageRef(chat, message_id))

    async def send_file(self, chat: ChatRef, file: OutboundFile) -> SentRef:
        self.files_sent.append(file)
        self._next_id += 1
        self.message_ids.append(self._next_id)
        return SentRef(MessageRef(chat, self._next_id))

    async def acknowledge(self, ref: MessageRef) -> None:
        self.acknowledged.append(ref)

    def status_line(self) -> str:
        return "ok (null connector)"
