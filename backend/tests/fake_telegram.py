"""A scripted stand-in for the Telegram Bot API, recording every call."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field


class StopPolling(BaseException):
    """Raised by the fake when its script is exhausted, so tests end deterministically.

    A BaseException: the connector retries every Exception, and this must pass through.
    """


@dataclass
class FakeTelegramApi:
    batches: list[list[dict]] = field(default_factory=list)
    calls: list[tuple[str, dict]] = field(default_factory=list)
    offsets: list[int | None] = field(default_factory=list)
    next_message_id: int = 9000
    fail_reactions: int = 0  # how many set_message_reaction calls fail first
    retry_after_seconds: float = 0.02
    stop_when_empty: bool = True
    failures: dict[str, list[Exception]] = field(default_factory=dict)  # method → errors to raise

    def _fail(self, method: str) -> None:
        queue = self.failures.get(method)
        if queue:
            raise queue.pop(0)

    async def get_updates(
        self, offset: int | None, poll_timeout: int, allowed_updates: list[str]
    ) -> list[dict]:
        self.offsets.append(offset)
        while self.batches:
            batch = self.batches.pop(0)
            # like Telegram, only deliver updates at or past the offset
            batch = [u for u in batch if offset is None or u["update_id"] >= offset]
            if batch:
                return copy.deepcopy(batch)
        if self.stop_when_empty:
            raise StopPolling
        return []

    async def get_me(self) -> dict:
        self.calls.append(("get_me", {}))
        return {"id": 7000000001, "username": "FactualThothBot"}

    async def send_message(self, chat_id, text, reply_to_message_id=None, buttons=None) -> dict:
        self._fail("send_message")
        self.calls.append(
            (
                "send_message",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": reply_to_message_id,
                    "buttons": buttons,
                },
            )
        )
        self.next_message_id += 1
        return {"message_id": self.next_message_id, "chat": {"id": chat_id}}

    async def edit_message_text(self, chat_id, message_id, text, buttons=None) -> dict:
        self._fail("edit_message_text")
        self.calls.append(
            (
                "edit_message_text",
                {"chat_id": chat_id, "message_id": message_id, "text": text, "buttons": buttons},
            )
        )
        return {"message_id": message_id, "chat": {"id": chat_id}}

    async def answer_callback_query(self, callback_query_id, text=None) -> None:
        self._fail("answer_callback_query")
        self.calls.append(
            ("answer_callback_query", {"callback_query_id": callback_query_id, "text": text})
        )

    async def set_message_reaction(self, chat_id, message_id, emoji) -> None:
        self.calls.append(
            ("set_message_reaction", {"chat_id": chat_id, "message_id": message_id, "emoji": emoji})
        )
        if self.fail_reactions:
            self.fail_reactions -= 1
            raise RetryAfterError(retry_after=self.retry_after_seconds)

    async def leave_chat(self, chat_id) -> None:
        self._fail("leave_chat")
        self.calls.append(("leave_chat", {"chat_id": chat_id}))

    async def set_my_commands(self, commands) -> None:
        self.calls.append(("set_my_commands", {"commands": commands}))

    async def delete_message(self, chat_id, message_id) -> None:
        self.calls.append(("delete_message", {"chat_id": chat_id, "message_id": message_id}))

    async def send_document(self, chat_id, filename, data, caption, reply_to_message_id) -> dict:
        self._fail("send_document")
        self.calls.append(
            (
                "send_document",
                {
                    "chat_id": chat_id,
                    "filename": filename,
                    "size": len(data),
                    "caption": caption,
                    "reply_to_message_id": reply_to_message_id,
                },
            )
        )
        self.next_message_id += 1
        return {"message_id": self.next_message_id, "chat": {"id": chat_id}}

    async def get_file(self, file_id) -> dict:
        self.calls.append(("get_file", {"file_id": file_id}))
        return {"file_id": file_id, "file_path": f"documents/{file_id}.bin", "file_size": 3}

    async def download(self, file_path: str):
        self.calls.append(("download", {"file_path": file_path}))
        yield b"abc"

    def named(self, name: str) -> list[dict]:
        return [args for called, args in self.calls if called == name]


class RetryAfterError(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"Flood control: retry after {retry_after}")
        self.retry_after = retry_after
