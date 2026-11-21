"""Thoth: the Telegram connector. Long polling, one capture chat, offset after commit."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from functools import partial
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
from app.connectors.telegram.api import TelegramApi
from app.connectors.telegram.mapping import Ignored, map_update
from app.core.logging import ctx

log = logging.getLogger("factual.telegram")

PRIVATE_REPLY = "Thoth works only in its capture chat."
MAX_BACKOFF = 60.0
BACKLOG_AGE_SECONDS = 60  # a first poll with older messages means Thoth was down


class AckQueue:
    """Sets the ✍ mark; a failed attempt is retried later instead of lost.

    A rate limit (an error with `retry_after`) delays the mark by what Telegram
    asks; any other failure retries after `retry_delay`. After `max_attempts`
    the mark is given up with an error line: a missing mark is the outage signal.
    """

    def __init__(self, api: TelegramApi, emoji: str, *, retry_delay: float, max_attempts: int = 6):
        self.api = api
        self.emoji = emoji
        self.retry_delay = retry_delay
        self.max_attempts = max_attempts
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def pending(self) -> int:
        return len(self._tasks)

    async def acknowledge(self, ref: MessageRef) -> None:
        await self._attempt(ref, attempt=1)

    async def _attempt(self, ref: MessageRef, attempt: int) -> None:
        try:
            await self.api.set_message_reaction(ref.chat.chat_id, ref.message_id, self.emoji)
        except Exception as exc:  # noqa: BLE001 — every failure is retried or reported
            delay = getattr(exc, "retry_after", None)
            delay = float(delay) if delay else self.retry_delay * attempt
            if attempt >= self.max_attempts:
                log.error(
                    "mark given up after %d attempts: %s",
                    attempt,
                    type(exc).__name__,
                    extra=ctx(action="ack.failed", message_id=ref.message_id, attempt=attempt),
                )
                return
            log.warning(
                "mark delayed: %s",
                type(exc).__name__,
                extra=ctx(
                    action="ack.delayed",
                    message_id=ref.message_id,
                    attempt=attempt,
                    retry_in_s=delay,
                ),
            )
            task = asyncio.create_task(self._later(ref, attempt + 1, delay))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _later(self, ref: MessageRef, attempt: int, delay: float) -> None:
        await asyncio.sleep(delay)
        await self._attempt(ref, attempt)

    async def drain(self, seconds: float) -> None:
        """Wait for queued marks (used at shutdown and in tests)."""
        deadline = time.monotonic() + seconds
        while self._tasks and time.monotonic() < deadline:
            await asyncio.wait(list(self._tasks), timeout=max(0.0, deadline - time.monotonic()))


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str
    capture_chat_id: int | None
    ack_reaction: str = "✍"
    bot_id: int | None = None
    bot_username: str | None = None


class TelegramConnector:
    type: ClassVar[str] = "telegram"
    capabilities: ClassVar[Capabilities] = Capabilities(
        edits_reported=True,
        deletions_reported=False,
        acknowledgement="reaction",
        max_file_bytes=20 * 1024 * 1024,  # the cloud Bot API downloads files up to 20 MB
        public_endpoint_needed=False,
    )
    allowed_updates: ClassVar[tuple[str, ...]] = (
        "message",
        "edited_message",
        "callback_query",
        "my_chat_member",
    )

    def __init__(
        self,
        settings: TelegramSettings,
        api: TelegramApi,
        *,
        retry_delay: float = 5.0,
        poll_timeout: int = 25,
    ) -> None:
        self.settings = settings
        self.api = api
        self.retry_delay = retry_delay
        self.poll_timeout = poll_timeout
        self._offset: int | None = None
        self._stopping = False
        self._failures = 0
        self._polls = 0
        self._told_unconfigured: set[int | str] = set()
        self._left: set[int | str] = set()
        self.acks = AckQueue(api, settings.ack_reaction, retry_delay=max(retry_delay, 0.01))
        self.commands_menu: list[tuple[str, str]] = []  # published with setMyCommands at start
        self.last_poll_at: float | None = None
        self.last_update_at: float | None = None

    # --- polling -----------------------------------------------------------------------

    async def start(self, sink: EventSink) -> None:
        self._stopping = False
        if self.settings.bot_username is None:
            await self._learn_identity()
        if self.commands_menu:
            try:
                await self.api.set_my_commands(self.commands_menu)
            except Exception as exc:  # noqa: BLE001 — the menu is a convenience
                log.warning("setMyCommands failed: %s", type(exc).__name__)
        while not self._stopping:
            handled = await self.poll_once(sink)
            if not handled:
                await asyncio.sleep(0)  # let stop() and others run between empty polls

    async def stop(self) -> None:
        self._stopping = True
        await self.acks.drain(5)

    async def _learn_identity(self) -> None:
        try:
            me = await self.api.get_me()
        except Exception as exc:  # noqa: BLE001 — identity is a convenience, polling must go on
            log.warning("getMe failed: %s", type(exc).__name__)
            return
        self.settings = replace(self.settings, bot_id=me.get("id"), bot_username=me.get("username"))

    async def poll_once(self, sink: EventSink) -> int:
        """One getUpdates call. The offset moves past an update only after the sink returns."""
        try:
            updates = await self.api.get_updates(
                self._offset, self.poll_timeout, list(self.allowed_updates)
            )
        except Exception as exc:  # noqa: BLE001 — network and API errors are retried
            await self._backoff("getUpdates failed", exc)
            return 0
        self._failures = 0
        self._polls += 1
        self.last_poll_at = time.time()
        if updates:
            self.last_update_at = time.time()
        if self._polls == 1:
            self._note_backlog(updates)
        handled = 0
        for raw in updates:
            try:
                await self._handle(raw, sink)
            except Exception as exc:  # noqa: BLE001 — the update stays undelivered and comes back
                await self._backoff(
                    "handling an update failed", exc, update_id=raw.get("update_id")
                )
                return handled
            self._offset = raw["update_id"] + 1
            handled += 1
        return handled

    @staticmethod
    def _note_backlog(updates: list[dict]) -> None:
        """Log when the first poll after a start returns messages that waited for Thoth."""
        dates = [
            (raw.get("message") or raw.get("edited_message") or {}).get("date") for raw in updates
        ]
        dates = [d for d in dates if isinstance(d, int)]
        if dates and time.time() - min(dates) > BACKLOG_AGE_SECONDS:
            log.info(
                "recording a backlog of %d updates that arrived while Thoth was down",
                len(updates),
                extra=ctx(action="backlog", count=len(updates)),
            )

    async def _backoff(self, what: str, exc: Exception, update_id: int | None = None) -> None:
        self._failures += 1
        delay = min(self.retry_delay * (2 ** (self._failures - 1)), MAX_BACKOFF)
        extra = ctx(action="telegram.retry", attempt=self._failures, retry_in_s=delay)
        if update_id is not None:
            extra.update(ctx(update_id=update_id))
        log.warning("%s: %s; retrying", what, type(exc).__name__, extra=extra)
        await asyncio.sleep(delay)

    async def _handle(self, raw: dict, sink: EventSink) -> None:
        outcome = map_update(
            raw,
            capture_chat_id=self.settings.capture_chat_id,
            bot_username=self.settings.bot_username,
        )
        if isinstance(outcome, Ignored):
            await self._react_to_ignored(outcome)
            return
        ack = partial(self.acknowledge, outcome.message) if outcome.kind != "choice" else _noop
        await sink.handle(replace(outcome, ack=ack))

    async def _react_to_ignored(self, ignored: Ignored) -> None:
        chat = ignored.chat
        if ignored.reason == "unconfigured" and chat is not None:
            if chat.chat_id not in self._told_unconfigured:
                self._told_unconfigured.add(chat.chat_id)
                await self.api.send_message(
                    chat.chat_id,
                    f"This group's chat ID is {chat.chat_id}. Set TELEGRAM_CAPTURE_CHAT_ID to it "
                    "and restart Thoth; until then nothing is recorded.",
                )
        elif ignored.reason == "other_chat" and chat is not None:
            if chat.chat_id not in self._left:
                self._left.add(chat.chat_id)
                log.info(
                    "leaving a chat that is not the capture chat", extra=ctx(chat_id=chat.chat_id)
                )
                await self.api.leave_chat(chat.chat_id)
        elif ignored.reason == "private" and chat is not None:
            await self.api.send_message(chat.chat_id, PRIVATE_REPLY)
        else:
            log.debug(
                "update ignored", extra=ctx(update_id=ignored.update_id, reason=ignored.reason)
            )

    def status_line(self) -> str:
        """For /status: whether polling runs and when Telegram last answered."""
        if self.last_poll_at is None:
            return "not started" if self._failures == 0 else f"failing ({self._failures} attempts)"
        ago = int(time.time() - self.last_poll_at)
        state = "ok" if self._failures == 0 else f"retrying ({self._failures} failures)"
        return f"{state}, last check {ago} s ago"

    # --- outbound ------------------------------------------------------------------------

    async def acknowledge(self, ref: MessageRef) -> None:
        """Set the mark; never raises. A failure is queued for a retry."""
        await self.acks.acknowledge(ref)

    async def send(self, chat: ChatRef, msg: OutboundMessage) -> SentRef:
        buttons = [[(b.label, b.data) for b in row] for row in msg.buttons] or None
        if msg.answer_choice:
            await self.api.answer_callback_query(msg.answer_choice, msg.answer_text)
        if not msg.text and msg.edit_of is None:
            return SentRef(MessageRef(chat, 0))  # only the tap was answered
        if msg.edit_of is not None:
            result = await self.api.edit_message_text(
                chat.chat_id, msg.edit_of.message_id, msg.text, buttons
            )
        else:
            result = await self.api.send_message(
                chat.chat_id,
                msg.text,
                reply_to_message_id=msg.reply_to.message_id if msg.reply_to else None,
                buttons=buttons,
            )
        return SentRef(MessageRef(chat, result["message_id"]))

    async def send_file(self, chat: ChatRef, file: OutboundFile) -> SentRef:
        result = await self.api.send_document(
            chat.chat_id,
            file.filename,
            file.data,
            file.caption,
            file.reply_to.message_id if file.reply_to else None,
        )
        return SentRef(MessageRef(chat, result["message_id"]))

    async def fetch_blob(self, ref: BlobRef) -> AsyncIterator[bytes]:
        info = await self.api.get_file(ref.file_id)
        async for chunk in self.api.download(info["file_path"]):
            yield chunk


async def _noop() -> None:
    return None
