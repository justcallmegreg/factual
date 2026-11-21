"""Ingestion: source events become ledger entries and projections, then the mark.

One transaction per event: the ledger lock, the entry, the payload and the
projections commit together. Only after the commit does the event's `ack` run,
which sets the ✍ mark. A second delivery of the same event changes nothing and
still re-sets the mark.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.connectors.base import BlobRef, NormalizedMessage, SourceEvent
from app.core.logging import ctx
from app.ledger.ledger import Ledger, iso_seconds
from app.records.normalize import hashtags, stated_time
from app.storage.db import Database
from app.storage.models import record, record_version

log = logging.getLogger("factual.ingest")


def _blob_dict(blob: BlobRef) -> dict:
    data = asdict(blob)
    return {
        "kind": data["kind"],
        "file_id": data["file_id"],
        "file_unique_id": data["unique_id"],
        "size": data["size"],
        "mime": data["mime"],
        "file_name": data["file_name"],
    }


class Ingestion:
    """The EventSink of the core."""

    def __init__(self, ledger: Ledger, db: Database, *, timezone: str) -> None:
        self.ledger = ledger
        self.db = db
        self.zone = ZoneInfo(timezone)

    async def handle(self, event: SourceEvent) -> None:
        if event.kind == "record.created":
            await self._record(event)
        elif event.kind == "record.edited":
            await self._edit(event)
        elif event.kind == "command" and self._is_log_with_text(event):
            await self._record(event, text=event.command.args)
        else:
            log.debug("event left to the command layer", extra=ctx(kind=event.kind))

    @staticmethod
    def _is_log_with_text(event: SourceEvent) -> bool:
        command = event.command
        return (
            command is not None
            and command.name == "log"
            and bool(command.args)
            and command.args.strip().lower() != "pending"
        )

    @staticmethod
    def _source(event: SourceEvent) -> dict:
        source: dict = {"connector": event.chat.connector, "chat_id": event.chat.chat_id}
        if event.message is not None:
            source["message_id"] = event.message.message_id
        if isinstance(event.raw, dict) and "update_id" in event.raw:
            source["update_id"] = event.raw["update_id"]
        return source

    @staticmethod
    async def _record_seq_for(conn: AsyncConnection, chat_id, message_id) -> int | None:
        row = (
            await conn.execute(
                select(record.c.seq).where(
                    record.c.chat_id == chat_id, record.c.message_id == message_id
                )
            )
        ).first()
        return int(row.seq) if row else None

    def _normalized_view(
        self, event: SourceEvent, n: NormalizedMessage, text: str | None, reply_seq: int | None
    ) -> dict:
        stated = stated_time(text, written_at=event.occurred_at, zone=self.zone)
        forward = None
        if n.forwarded_from is not None:
            forward = {
                "kind": n.forwarded_from.kind,
                "sender_name": n.forwarded_from.sender_name,
                "sender_id": n.forwarded_from.sender_id,
                "sent_at": iso_seconds(n.forwarded_from.sent_at),
            }
        author = event.actor or n.author
        return {
            "author": (
                {"id": author.user_id, "name": author.display_name, "username": author.username}
                if author
                else None
            ),
            "text": text,
            "caption": n.caption,
            "origin": "forwarded" if forward else "note",
            "forward": forward,
            "reply_to": (
                {"message_id": n.reply_to.message_id, "seq": reply_seq} if n.reply_to else None
            ),
            "album_id": n.album_id,
            "media": list(n.media_kinds),
            "attachments": [_blob_dict(blob) for blob in event.blobs],
            "tags": hashtags(text, n.caption),
            "stated_at": stated.isoformat() if stated else None,
        }

    async def _record(self, event: SourceEvent, text: str | None = None) -> None:
        n = event.normalized or NormalizedMessage()
        if text is None:
            text = n.text
        now = datetime.now(UTC)
        async with self.db.engine.begin() as conn:
            reply_seq = None
            if n.reply_to is not None:
                reply_seq = await self._record_seq_for(
                    conn, event.chat.chat_id, n.reply_to.message_id
                )
            view = self._normalized_view(event, n, text, reply_seq)
            appended = await self.ledger.append(
                conn,
                type="record.created",
                source=self._source(event),
                source_event=event.event_id,
                observed_at=event.occurred_at,
                payload={"raw": event.raw, "normalized": view},
                refs=[reply_seq] if reply_seq else [],
                received_at=now,
            )
            if not appended.duplicate:
                author = view["author"] or {}
                forward = view["forward"] or {}
                stated = stated_time(text, written_at=event.occurred_at, zone=self.zone)
                await conn.execute(
                    insert(record).values(
                        seq=appended.seq,
                        connector=event.chat.connector,
                        chat_id=event.chat.chat_id,
                        message_id=event.message.message_id if event.message else None,
                        author_id=author.get("id"),
                        author_name=author.get("name"),
                        author_username=author.get("username"),
                        text=text,
                        caption=n.caption,
                        origin=view["origin"],
                        forward_sender_name=forward.get("sender_name"),
                        forward_sender_id=forward.get("sender_id"),
                        forward_sent_at=n.forwarded_from.sent_at if n.forwarded_from else None,
                        reply_to_seq=reply_seq,
                        album_id=n.album_id,
                        media_kinds=list(n.media_kinds),
                        stated_at=stated,
                        tags=view["tags"],
                        observed_at=event.occurred_at,
                        received_at=now,
                        version=1,
                    )
                )
                await conn.execute(
                    insert(record_version).values(
                        seq=appended.seq,
                        record_seq=appended.seq,
                        version=1,
                        text=text,
                        caption=n.caption,
                        observed_at=event.occurred_at,
                    )
                )
        log.info(
            "record recorded" if not appended.duplicate else "record delivered again",
            extra=ctx(
                action="record.created",
                entry=appended.seq,
                chat_id=event.chat.chat_id,
                message_id=event.message.message_id if event.message else None,
            ),
        )
        await event.ack()

    async def _edit(self, event: SourceEvent) -> None:
        n = event.normalized or NormalizedMessage()
        async with self.db.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(record).where(
                        record.c.chat_id == event.chat.chat_id,
                        record.c.message_id == event.message.message_id,
                    )
                )
            ).first()
            if row is None:
                known = False
            else:
                known = True
                if (n.text, n.caption) == (row.text, row.caption):
                    log.debug(
                        "edit without a change ignored",
                        extra=ctx(entry=int(row.seq), message_id=event.message.message_id),
                    )
                    return
                stated = stated_time(n.text, written_at=event.occurred_at, zone=self.zone)
                tags = hashtags(n.text, n.caption)
                appended = await self.ledger.append(
                    conn,
                    type="record.edited",
                    source=self._source(event),
                    source_event=event.event_id,
                    observed_at=event.occurred_at,
                    payload={
                        "raw": event.raw,
                        "normalized": {
                            "record_seq": int(row.seq),
                            "text": n.text,
                            "caption": n.caption,
                            "tags": tags,
                            "stated_at": stated.isoformat() if stated else None,
                            "edited_at": iso_seconds(event.occurred_at),
                        },
                    },
                    refs=[int(row.seq)],
                )
                if not appended.duplicate:
                    await conn.execute(
                        insert(record_version).values(
                            seq=appended.seq,
                            record_seq=int(row.seq),
                            version=int(row.version) + 1,
                            text=n.text,
                            caption=n.caption,
                            observed_at=event.occurred_at,
                        )
                    )
                    await conn.execute(
                        update(record)
                        .where(record.c.seq == row.seq)
                        .values(
                            text=n.text,
                            caption=n.caption,
                            tags=tags,
                            stated_at=stated,
                            version=int(row.version) + 1,
                            edited_at=event.occurred_at,
                        )
                    )
        if not known:
            # the first version was never recorded (sent before Thoth joined, or lost)
            await self._record(event)
            return
        log.info(
            "record edited",
            extra=ctx(
                action="record.edited", entry=appended.seq, message_id=event.message.message_id
            ),
        )
        await event.ack()
