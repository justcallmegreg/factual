"""Attachments: queued at ingestion, fetched and stored by a worker, recorded as entries."""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.connectors.base import (
    BlobRef,
    ChatRef,
    Connector,
    MessageRef,
    OutboundMessage,
    SourceEvent,
)
from app.core.clock import Clock, system_clock
from app.core.logging import ctx
from app.ledger.ledger import Ledger
from app.records.projects import event_source
from app.storage.blobs import ObjectStore, encrypt_stream, sniff_mime, wrap_key
from app.storage.db import Database
from app.storage.jobs import Job, enqueue
from app.storage.models import attachment, blob

log = logging.getLogger("factual.attachments")

FETCH_JOB = "attachment.fetch"
MB = 1024 * 1024


def _descriptor(ref: BlobRef) -> dict:
    return {
        "kind": ref.kind,
        "file_id": ref.file_id,
        "file_unique_id": ref.unique_id,
        "size": ref.size,
        "mime": ref.mime,
        "file_name": ref.file_name,
    }


def _source_event(record_seq: int, unique_id: str) -> str:
    return f"attachment:{record_seq}:{unique_id}"


def _aad(record_seq: int, unique_id: str) -> bytes:
    return f"attachment:{record_seq}:{unique_id}".encode()


@dataclass(frozen=True)
class Notice:
    chat: ChatRef
    reply_to: MessageRef | None
    text: str


class AttachmentIntake:
    """Inside the record transaction: queue the fetch, or record that the file is too large."""

    def __init__(self, *, max_bytes: int, clock: Clock | None = None) -> None:
        self.max_bytes = max_bytes
        self.clock = clock or system_clock

    async def register(
        self, conn: AsyncConnection, ledger: Ledger, event: SourceEvent, record_seq: int
    ) -> list[Notice]:
        notices: list[Notice] = []
        now = self.clock()
        for ref in event.blobs:
            existing = (
                await conn.execute(
                    select(attachment.c.id).where(
                        attachment.c.record_seq == record_seq,
                        attachment.c.file_unique_id == ref.unique_id,
                    )
                )
            ).first()
            if existing is not None:
                continue
            if ref.size is not None and ref.size > self.max_bytes:
                appended = await ledger.append(
                    conn,
                    type="attachment.unavailable",
                    source=event_source(event),
                    source_event=_source_event(record_seq, ref.unique_id),
                    observed_at=event.occurred_at,
                    payload={
                        "record_seq": record_seq,
                        "attachment": _descriptor(ref),
                        "reason": "too_large",
                        "limit_bytes": self.max_bytes,
                    },
                    refs=[record_seq],
                )
                await conn.execute(
                    insert(attachment).values(
                        record_seq=record_seq,
                        file_unique_id=ref.unique_id,
                        file_id=ref.file_id,
                        kind=ref.kind,
                        file_name=ref.file_name,
                        mime=ref.mime,
                        size_bytes=ref.size,
                        status="unavailable",
                        entry_seq=appended.seq,
                        created_at=now,
                    )
                )
                size_mb = ref.size / MB
                name = ref.file_name or ref.kind
                notices.append(
                    Notice(
                        event.chat,
                        event.message,
                        f"The file {name} is {size_mb:.0f} MB; Telegram lets Thoth download files "
                        f"up to {self.max_bytes // MB} MB. The message is recorded without the "
                        "file. Send it in a smaller form, for example as a compressed photo "
                        "or a PDF.",
                    )
                )
                continue
            await conn.execute(
                insert(attachment).values(
                    record_seq=record_seq,
                    file_unique_id=ref.unique_id,
                    file_id=ref.file_id,
                    kind=ref.kind,
                    file_name=ref.file_name,
                    mime=ref.mime,
                    size_bytes=ref.size,
                    status="pending",
                    created_at=now,
                )
            )
            await enqueue(
                conn,
                FETCH_JOB,
                {
                    "record_seq": record_seq,
                    "chat_id": event.chat.chat_id,
                    "connector": event.chat.connector,
                    "message_id": event.message.message_id if event.message else None,
                    "attachment": _descriptor(ref),
                },
                now=now,
            )
        return notices


class AttachmentFetcher:
    """The job handler: download at once, hash, encrypt, upload, append attachment.stored."""

    KIND = FETCH_JOB

    def __init__(
        self,
        ledger: Ledger,
        db: Database,
        connector: Connector,
        store: ObjectStore,
        master_key: bytes,
        *,
        lock_days: int,
        max_bytes: int = 20 * MB,
        clock: Clock | None = None,
        timezone: str = "UTC",
        extract: bool = False,
    ) -> None:
        self.ledger = ledger
        self.db = db
        self.connector = connector
        self.store = store
        self.master_key = master_key
        self.lock_days = lock_days
        self.max_bytes = max_bytes
        self.clock = clock or system_clock
        self.zone = ZoneInfo(timezone)
        self.extract = extract  # queue a text extraction for images and PDFs

    async def run(self, job: Job) -> None:
        p = job.payload
        a = p["attachment"]
        record_seq = int(p["record_seq"])
        ref = BlobRef(
            file_id=a["file_id"],
            unique_id=a["file_unique_id"],
            kind=a["kind"],
            size=a.get("size"),
            mime=a.get("mime"),
            file_name=a.get("file_name"),
        )
        aad = _aad(record_seq, ref.unique_id)
        file_key = os.urandom(32)
        result = await encrypt_stream(self.connector.fetch_blob(ref), file_key, aad)
        if result.size_bytes > self.max_bytes:
            raise ValueError(f"file is {result.size_bytes} bytes, over the {self.max_bytes} limit")
        mime = sniff_mime(result.head, ref.mime, ref.file_name)
        object_key = f"blobs/{result.sha256_stored.hex()}"  # tells the store nothing about the file
        now = self.clock()
        retain_until = now + timedelta(days=self.lock_days) if self.lock_days > 0 else None
        checksum = base64.b64encode(result.sha256_stored).decode()
        put = await self.store.put(object_key, result.stored_bytes(), checksum, retain_until)
        async with self.db.engine.begin() as conn:
            appended = await self.ledger.append(
                conn,
                type="attachment.stored",
                source={
                    "connector": p.get("connector", "telegram"),
                    "chat_id": p["chat_id"],
                    "message_id": p.get("message_id"),
                },
                source_event=_source_event(record_seq, ref.unique_id),
                observed_at=now,
                payload={
                    "record_seq": record_seq,
                    "attachment": _descriptor(ref),
                    "sha256_plain": result.sha256_plain.hex(),
                    "sha256_stored": result.sha256_stored.hex(),
                    "size_bytes": result.size_bytes,
                    "mime": mime,
                    "object_key": object_key,
                    "version_id": put.version_id,
                    "retain_until": retain_until.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if retain_until
                    else None,
                    "encryption": {"algorithm": "AES-256-GCM", "aad": aad.decode()},
                },
                refs=[record_seq],
            )
            if not appended.duplicate:
                await conn.execute(
                    insert(blob).values(
                        seq=appended.seq,
                        sha256_plain=result.sha256_plain,
                        sha256_stored=result.sha256_stored,
                        size_bytes=result.size_bytes,
                        mime=mime,
                        object_key=object_key,
                        version_id=put.version_id,
                        wrapped_key=wrap_key(self.master_key, file_key, aad),
                        aad=aad.decode(),
                        retain_until=retain_until,
                    )
                )
                await conn.execute(
                    update(attachment)
                    .where(
                        attachment.c.record_seq == record_seq,
                        attachment.c.file_unique_id == ref.unique_id,
                    )
                    .values(
                        status="stored",
                        entry_seq=appended.seq,
                        mime=mime,
                        size_bytes=result.size_bytes,
                        object_key=object_key,
                        version_id=put.version_id,
                        sha256_plain=result.sha256_plain,
                    )
                )
                if self.extract:
                    from app.ai.extract import EXTRACT_JOB, extractable

                    if extractable(mime):
                        await enqueue(
                            conn,
                            EXTRACT_JOB,
                            {
                                "record_seq": record_seq,
                                "blob_seq": appended.seq,
                                "file_name": ref.file_name,
                                "mime": mime,
                            },
                            now=now,
                        )
        log.info(
            "attachment stored",
            extra=ctx(
                action="attachment.stored",
                entry=appended.seq,
                size_bytes=result.size_bytes,
                object_key=object_key,
            ),
        )

    async def give_up(self, job: Job, error: str) -> None:
        p = job.payload
        a = p["attachment"]
        record_seq = int(p["record_seq"])
        async with self.db.engine.begin() as conn:
            appended = await self.ledger.append(
                conn,
                type="attachment.unavailable",
                source={
                    "connector": p.get("connector", "telegram"),
                    "chat_id": p["chat_id"],
                    "message_id": p.get("message_id"),
                },
                source_event=_source_event(record_seq, a["file_unique_id"]),
                observed_at=self.clock(),
                payload={
                    "record_seq": record_seq,
                    "attachment": a,
                    "reason": "download_failed",
                    "attempts": job.attempts,
                    "error": error[:200],
                },
                refs=[record_seq],
            )
            if not appended.duplicate:
                await conn.execute(
                    update(attachment)
                    .where(
                        attachment.c.record_seq == record_seq,
                        attachment.c.file_unique_id == a["file_unique_id"],
                    )
                    .values(status="unavailable", entry_seq=appended.seq)
                )
        chat = ChatRef(p.get("connector", "telegram"), p["chat_id"])
        name = a.get("file_name") or a.get("kind", "file")
        await self.connector.send(
            chat,
            OutboundMessage(
                f"The file {name} of E{record_seq} could not be stored after {job.attempts} "
                "attempts and is recorded as unavailable. Send it again if you still have it.",
                reply_to=MessageRef(chat, p["message_id"]) if p.get("message_id") else None,
            ),
        )
