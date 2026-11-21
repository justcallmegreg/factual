"""The connector interface: everything the core knows about a chat platform.

Telegram sits behind this small typed interface and the core never imports a
platform SDK. A connector turns messages, edits, slash commands and button taps
into `SourceEvent`s and pushes them into an `EventSink`; it sends replies and
sets the acknowledgement mark when the core asks. Adding another source means
writing one package and passing the shared contract tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar, Literal, Protocol, runtime_checkable

EventKind = Literal["record.created", "record.edited", "choice", "command"]


@dataclass(frozen=True)
class Capabilities:
    """What a platform can promise; the core reads these to decide what it offers."""

    edits_reported: bool
    deletions_reported: bool
    acknowledgement: str  # "reaction", or "none"
    max_file_bytes: int
    public_endpoint_needed: bool


@dataclass(frozen=True)
class ChatRef:
    connector: str
    chat_id: int | str
    kind: str = "group"  # group | private | channel
    title: str | None = None


@dataclass(frozen=True)
class MessageRef:
    chat: ChatRef
    message_id: int | str


@dataclass(frozen=True)
class UserRef:
    user_id: int | str
    display_name: str
    username: str | None = None
    is_bot: bool = False


@dataclass(frozen=True)
class ForwardOrigin:
    """Where a forwarded message came from, as the platform reports it."""

    kind: str  # user | hidden_user | chat | channel
    sender_name: str
    sent_at: datetime
    sender_id: int | str | None = None


@dataclass(frozen=True)
class BlobRef:
    """A file the platform holds; `fetch_blob` streams it."""

    file_id: str
    unique_id: str
    kind: str  # photo | document | voice | video | audio | video_note | sticker | animation
    size: int | None = None
    mime: str | None = None
    file_name: str | None = None


@dataclass(frozen=True)
class NormalizedMessage:
    """The platform-neutral view of a message. The raw payload stays the evidence."""

    text: str | None = None
    caption: str | None = None
    author: UserRef | None = None
    forwarded_from: ForwardOrigin | None = None
    reply_to: MessageRef | None = None
    album_id: str | None = None
    edited_at: datetime | None = None
    media_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class Command:
    """A slash command: `/log pending` is name "log", args "pending"."""

    name: str
    args: str = ""


@dataclass(frozen=True)
class Choice:
    """A button tap: the opaque data the core put on the button, and the tap's own id."""

    data: str
    query_id: str
    message: MessageRef | None = None


async def _no_ack() -> None:
    return None


@dataclass(frozen=True)
class SourceEvent:
    kind: EventKind
    event_id: str  # connector + platform event id, unique per delivery: "telegram:update:123"
    chat: ChatRef
    occurred_at: datetime  # platform timestamp
    raw: dict  # full platform payload exactly as received; stored as JCS bytes
    message: MessageRef | None = None
    actor: UserRef | None = None
    normalized: NormalizedMessage | None = None
    blobs: list[BlobRef] = field(default_factory=list)
    command: Command | None = None
    choice: Choice | None = None
    ack: Callable[[], Awaitable[None]] = _no_ack  # called by the core only after the commit


@dataclass(frozen=True)
class Button:
    label: str
    data: str  # opaque callback data; comes back in a Choice


@dataclass(frozen=True)
class OutboundMessage:
    text: str
    reply_to: MessageRef | None = None
    buttons: tuple[tuple[Button, ...], ...] = ()
    edit_of: MessageRef | None = None  # edit this message instead of sending a new one
    answer_choice: str | None = None  # the query_id of a tap to answer, if any
    answer_text: str | None = None  # a short notice shown to the person who tapped


@dataclass(frozen=True)
class OutboundFile:
    filename: str
    data: bytes
    caption: str = ""
    reply_to: MessageRef | None = None


@dataclass(frozen=True)
class SentRef:
    message: MessageRef


class EventSink(Protocol):
    async def handle(self, event: SourceEvent) -> None:
        """Record the event. Returning means the ledger commit is done."""


@runtime_checkable
class Connector(Protocol):
    type: ClassVar[str]
    capabilities: ClassVar[Capabilities]

    async def start(self, sink: EventSink) -> None:
        """Push SourceEvents into the sink until stopped."""

    async def stop(self) -> None: ...

    def fetch_blob(self, ref: BlobRef) -> AsyncIterator[bytes]: ...

    async def send(self, chat: ChatRef, msg: OutboundMessage) -> SentRef: ...

    async def send_file(self, chat: ChatRef, file: OutboundFile) -> SentRef: ...

    async def acknowledge(self, ref: MessageRef) -> None:
        """The ✍ mark, or the platform's equivalent."""
