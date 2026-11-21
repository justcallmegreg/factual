"""Raw Telegram updates → platform-neutral events. Pure functions, no I/O.

The raw update dict is kept exactly as received and travels with the event; the
normalised view is derived from it and never replaces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.connectors.base import (
    BlobRef,
    ChatRef,
    Choice,
    Command,
    ForwardOrigin,
    MessageRef,
    NormalizedMessage,
    SourceEvent,
    UserRef,
)

CONNECTOR = "telegram"

SERVICE_FIELDS = (
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "group_chat_created",
    "supergroup_chat_created",
    "channel_chat_created",
    "message_auto_delete_timer_changed",
    "migrate_to_chat_id",
    "migrate_from_chat_id",
    "pinned_message",
    "successful_payment",
    "forum_topic_created",
    "forum_topic_edited",
    "forum_topic_closed",
    "forum_topic_reopened",
    "video_chat_scheduled",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    "boost_added",
    "chat_background_set",
    "write_access_allowed",
    "proximity_alert_triggered",
    "giveaway_created",
    "giveaway",
    "giveaway_winners",
    "giveaway_completed",
    "web_app_data",
)

# media fields that carry a downloadable file, in the order we list them
FILE_FIELDS = ("photo", "document", "voice", "video", "audio", "video_note", "sticker", "animation")
# message content that is a record without a file
INLINE_FIELDS = ("location", "contact", "venue", "poll", "dice")


@dataclass(frozen=True)
class Ignored:
    """An update that is not a record of the capture chat, and why."""

    reason: str  # private | other_chat | unconfigured | from_bot | service | unsupported | ...
    chat: ChatRef | None
    update_id: int


def chat_ref(chat: dict) -> ChatRef:
    kind = {"private": "private", "channel": "channel"}.get(chat.get("type"), "group")
    return ChatRef(CONNECTOR, chat["id"], kind=kind, title=chat.get("title"))


def user_ref(user: dict) -> UserRef:
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return UserRef(
        user_id=user["id"],
        display_name=name or user.get("username") or str(user["id"]),
        username=user.get("username"),
        is_bot=bool(user.get("is_bot")),
    )


def _when(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _forward_origin(msg: dict) -> ForwardOrigin | None:
    origin = msg.get("forward_origin")
    if not origin:
        return None
    kind = origin.get("type", "user")
    sent_at = _when(origin["date"])
    if kind == "user":
        user = origin["sender_user"]
        return ForwardOrigin(kind, user_ref(user).display_name, sent_at, sender_id=user["id"])
    if kind == "hidden_user":
        return ForwardOrigin(kind, origin.get("sender_user_name", "hidden"), sent_at)
    chat = origin.get("sender_chat") or origin.get("chat") or {}
    return ForwardOrigin(kind, chat.get("title") or str(chat.get("id")), sent_at, chat.get("id"))


def _blobs(msg: dict) -> list[BlobRef]:
    blobs: list[BlobRef] = []
    for field_name in FILE_FIELDS:
        media = msg.get(field_name)
        if not media:
            continue
        if field_name == "photo":
            media = max(media, key=lambda size: size.get("file_size") or 0)
        blobs.append(
            BlobRef(
                file_id=media["file_id"],
                unique_id=media["file_unique_id"],
                kind=field_name,
                size=media.get("file_size"),
                mime=media.get("mime_type") or ("image/jpeg" if field_name == "photo" else None),
                file_name=media.get("file_name"),
            )
        )
    return blobs


def _media_kinds(msg: dict) -> tuple[str, ...]:
    return tuple(name for name in FILE_FIELDS + INLINE_FIELDS if msg.get(name))


def _command(msg: dict, bot_username: str | None) -> Command | None | Ignored:
    text = msg.get("text") or ""
    entities = msg.get("entities") or []
    if not text.startswith("/") or not entities:
        return None
    first = entities[0]
    if first.get("type") != "bot_command" or first.get("offset") != 0:
        return None
    token = text[1 : first["length"]]
    name, _, mention = token.partition("@")
    if mention and bot_username and mention.lower() != bot_username.lower():
        return Ignored("other_bot_command", chat_ref(msg["chat"]), -1)
    return Command(name=name.lower(), args=text[first["length"] :].strip())


def normalize(msg: dict, chat: ChatRef, edited: bool) -> NormalizedMessage:
    reply = msg.get("reply_to_message")
    return NormalizedMessage(
        text=msg.get("text"),
        caption=msg.get("caption"),
        author=user_ref(msg["from"]) if msg.get("from") else None,
        forwarded_from=_forward_origin(msg),
        reply_to=MessageRef(chat, reply["message_id"]) if reply else None,
        album_id=msg.get("media_group_id"),
        edited_at=_when(msg["edit_date"]) if edited and msg.get("edit_date") else None,
        media_kinds=_media_kinds(msg),
    )


def map_update(
    raw: dict, *, capture_chat_id: int | None, bot_username: str | None = None
) -> SourceEvent | Ignored:
    """Classify one raw update. Only the capture chat yields events."""
    update_id = raw["update_id"]
    event_id = f"{CONNECTOR}:update:{update_id}"

    if "my_chat_member" in raw:
        chat = chat_ref(raw["my_chat_member"]["chat"])
        if chat.kind == "private":
            return Ignored("private", chat, update_id)
        if capture_chat_id is None or chat.chat_id != capture_chat_id:
            return Ignored("other_chat", chat, update_id)
        return Ignored("service", chat, update_id)

    callback = raw.get("callback_query")
    edited = "edited_message" in raw
    msg = raw.get("message") or raw.get("edited_message") or (callback or {}).get("message")
    if msg is None:
        return Ignored("unsupported", None, update_id)

    chat = chat_ref(msg["chat"])
    if chat.kind == "private":
        return Ignored("private", chat, update_id)
    if capture_chat_id is None:
        return Ignored("unconfigured", chat, update_id)
    if chat.chat_id != capture_chat_id:
        return Ignored("other_chat", chat, update_id)

    if callback is not None:
        actor = user_ref(callback["from"])
        return SourceEvent(
            kind="choice",
            event_id=event_id,
            chat=chat,
            occurred_at=datetime.now(UTC),  # Telegram sends no time for a tap
            raw=raw,
            message=MessageRef(chat, msg["message_id"]),
            actor=actor,
            choice=Choice(
                data=callback.get("data", ""),
                query_id=callback["id"],
                message=MessageRef(chat, msg["message_id"]),
            ),
        )

    sender = msg.get("from")
    if sender and sender.get("is_bot"):
        return Ignored("from_bot", chat, update_id)
    if any(msg.get(name) for name in SERVICE_FIELDS):
        return Ignored("service", chat, update_id)

    message = MessageRef(chat, msg["message_id"])
    actor = user_ref(sender) if sender else None
    command = _command(msg, bot_username)
    if isinstance(command, Ignored):
        return Ignored(command.reason, chat, update_id)
    if command is not None:
        if edited:
            return Ignored("command_edit", chat, update_id)
        return SourceEvent(
            kind="command",
            event_id=event_id,
            chat=chat,
            occurred_at=_when(msg["date"]),
            raw=raw,
            message=message,
            actor=actor,
            normalized=normalize(msg, chat, edited=False),
            command=command,
        )

    normalized = normalize(msg, chat, edited)
    return SourceEvent(
        kind="record.edited" if edited else "record.created",
        event_id=event_id,
        chat=chat,
        occurred_at=normalized.edited_at or _when(msg["date"]),
        raw=raw,
        message=message,
        actor=actor,
        normalized=normalized,
        blobs=_blobs(msg),
    )
