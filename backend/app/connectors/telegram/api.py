"""The slice of the Telegram Bot API that Thoth uses.

`getUpdates` and file downloads go through httpx so that the raw JSON reaches
the ledger exactly as Telegram sent it; every other call goes through aiogram.
Tests replace the whole class with a scripted fake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    ReplyParameters,
)

API_BASE = "https://api.telegram.org"


class TelegramApiError(RuntimeError):
    pass


class TelegramApi(Protocol):
    async def get_updates(
        self, offset: int | None, poll_timeout: int, allowed_updates: list[str]
    ) -> list[dict]: ...

    async def get_me(self) -> dict: ...

    async def send_message(
        self, chat_id, text: str, reply_to_message_id: int | None = None, buttons=None
    ) -> dict: ...

    async def edit_message_text(
        self, chat_id, message_id: int, text: str, buttons=None
    ) -> dict: ...

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> None: ...

    async def set_message_reaction(self, chat_id, message_id: int, emoji: str) -> None: ...

    async def leave_chat(self, chat_id) -> None: ...

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None: ...

    async def delete_message(self, chat_id, message_id: int) -> None: ...

    async def get_file(self, file_id: str) -> dict: ...

    def download(self, file_path: str) -> AsyncIterator[bytes]: ...


def _markup(buttons) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    rows = [
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class AiogramTelegramApi:
    def __init__(
        self, token: str, *, bot: Bot | None = None, http: httpx.AsyncClient | None = None
    ):
        self._token = token
        self.bot = bot or Bot(token=token)
        self.http = http or httpx.AsyncClient(base_url=API_BASE)

    async def close(self) -> None:
        await self.bot.session.close()
        await self.http.aclose()

    async def get_updates(
        self, offset: int | None, poll_timeout: int, allowed_updates: list[str]
    ) -> list[dict]:
        params: dict[str, object] = {
            "timeout": poll_timeout,
            "allowed_updates": json.dumps(allowed_updates),
        }
        if offset is not None:
            params["offset"] = offset
        response = await self.http.get(
            f"/bot{self._token}/getUpdates", params=params, timeout=poll_timeout + 15
        )
        data = response.json()
        if not data.get("ok"):
            raise TelegramApiError(
                f"getUpdates failed: {data.get('description', response.status_code)}"
            )
        return data["result"]

    async def get_me(self) -> dict:
        me = await self.bot.get_me()
        return {"id": me.id, "username": me.username}

    async def send_message(self, chat_id, text, reply_to_message_id=None, buttons=None) -> dict:
        sent = await self.bot.send_message(
            chat_id,
            text,
            reply_parameters=(
                ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
                if reply_to_message_id
                else None
            ),
            reply_markup=_markup(buttons),
        )
        return {"message_id": sent.message_id, "chat": {"id": sent.chat.id}}

    async def edit_message_text(self, chat_id, message_id, text, buttons=None) -> dict:
        await self.bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, reply_markup=_markup(buttons)
        )
        return {"message_id": message_id, "chat": {"id": chat_id}}

    async def answer_callback_query(self, callback_query_id, text=None) -> None:
        await self.bot.answer_callback_query(callback_query_id, text=text)

    async def set_message_reaction(self, chat_id, message_id, emoji) -> None:
        await self.bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji(emoji=emoji)])

    async def leave_chat(self, chat_id) -> None:
        await self.bot.leave_chat(chat_id)

    async def set_my_commands(self, commands) -> None:
        await self.bot.set_my_commands(
            [BotCommand(command=name, description=description) for name, description in commands]
        )

    async def delete_message(self, chat_id, message_id) -> None:
        await self.bot.delete_message(chat_id, message_id)

    async def get_file(self, file_id) -> dict:
        info = await self.bot.get_file(file_id)
        return {"file_id": info.file_id, "file_path": info.file_path, "file_size": info.file_size}

    async def download(self, file_path: str) -> AsyncIterator[bytes]:
        async with self.http.stream("GET", f"/file/bot{self._token}/{file_path}", timeout=120) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes(64 * 1024):
                yield chunk
