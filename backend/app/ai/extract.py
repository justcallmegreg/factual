"""Derived text: Claude reads the text in images and PDFs so that search finds them.

The file and its hash stay the evidence; the derived text is a projection with
the model's name and the time, never a ledger entry. Each file is sent once,
within a size limit and a page limit.
"""

from __future__ import annotations

import base64
import io
import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import insert, select, update

from app.core.clock import Clock, system_clock
from app.core.logging import ctx
from app.storage.blobs import ObjectStore, decrypt, unwrap_key
from app.storage.db import Database
from app.storage.jobs import Job
from app.storage.models import blob, derived_text, search_chunk

log = logging.getLogger("factual.extract")

EXTRACT_JOB = "attachment.extract"
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # the API's image limit
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 100
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_TYPE = "application/pdf"
INSTRUCTION = (
    "Transcribe all the text in this file verbatim, in reading order, keeping line breaks. "
    "Output only the transcribed text. If the file holds no readable text, output nothing."
)


def extractable(mime: str | None) -> bool:
    return mime in IMAGE_TYPES or mime == PDF_TYPE


def count_pdf_pages(data: bytes) -> int | None:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001 — an unreadable PDF has no page count
        return None


@dataclass(frozen=True)
class ExtractedText:
    text: str
    model: str
    truncated: bool = False  # the model stopped at max_tokens: the text is incomplete


class Extractor(Protocol):
    model: str

    async def extract(
        self, *, media_type: str, data: bytes, file_name: str | None
    ) -> ExtractedText: ...


class AnthropicExtractor:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = 120.0,
        max_tokens: int = 16000,
        effort: str = "low",
    ):
        import anthropic

        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)

    @staticmethod
    def build_content(media_type: str, data: bytes, file_name: str | None) -> list[dict]:
        encoded = base64.b64encode(data).decode()
        if media_type == PDF_TYPE:
            block = {
                "type": "document",
                "source": {"type": "base64", "media_type": media_type, "data": encoded},
            }
            if file_name:
                block["title"] = file_name
        else:
            block = {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": encoded},
            }
        return [block, {"type": "text", "text": INSTRUCTION}]

    async def extract(
        self, *, media_type: str, data: bytes, file_name: str | None
    ) -> ExtractedText:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": self.build_content(media_type, data, file_name)}],
            output_config={"effort": self.effort},
        )
        if response.stop_reason == "refusal":
            return ExtractedText(text="", model=self.model)
        text = "".join(block.text for block in response.content if block.type == "text")
        return ExtractedText(
            text=text,
            model=response.model or self.model,
            truncated=response.stop_reason == "max_tokens",
        )


class ExtractionJob:
    """The job handler: decrypt the stored file, ask Claude for its text, store it as derived."""

    KIND = EXTRACT_JOB

    def __init__(
        self,
        db: Database,
        store: ObjectStore,
        master_key: bytes,
        extractor: Extractor,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.db = db
        self.store = store
        self.master_key = master_key
        self.extractor = extractor
        self.clock = clock or system_clock

    async def run(self, job: Job) -> None:
        record_seq = int(job.payload["record_seq"])
        blob_seq = int(job.payload["blob_seq"])
        file_name = job.payload.get("file_name")
        async with self.db.engine.connect() as conn:
            row = (await conn.execute(select(blob).where(blob.c.seq == blob_seq))).first()
            existing = (
                await conn.execute(
                    select(derived_text.c.id).where(
                        derived_text.c.blob_seq == blob_seq, derived_text.c.status == "done"
                    )
                )
            ).first()
        if row is None:
            raise LookupError(f"no blob {blob_seq}")
        if existing is not None:
            return
        mime = row.mime
        if not extractable(mime):
            await self._store(
                record_seq,
                blob_seq,
                file_name,
                mime,
                None,
                None,
                "skipped: not an image or PDF",
                None,
            )
            return
        limit = MAX_PDF_BYTES if mime == PDF_TYPE else MAX_IMAGE_BYTES
        if int(row.size_bytes) > limit:
            await self._store(
                record_seq,
                blob_seq,
                file_name,
                mime,
                None,
                None,
                f"skipped: file larger than {limit} bytes",
                None,
            )
            return
        stored = await self.store.get(row.object_key, row.version_id)
        aad = row.aad.encode()
        data = decrypt(stored, unwrap_key(self.master_key, bytes(row.wrapped_key), aad), aad)
        pages = None
        if mime == PDF_TYPE:
            pages = count_pdf_pages(data)
            if pages is None:
                await self._store(
                    record_seq,
                    blob_seq,
                    file_name,
                    mime,
                    None,
                    None,
                    "skipped: unreadable PDF",
                    None,
                )
                return
            if pages > MAX_PDF_PAGES:
                await self._store(
                    record_seq,
                    blob_seq,
                    file_name,
                    mime,
                    None,
                    None,
                    f"skipped: {pages} pages, more than {MAX_PDF_PAGES}",
                    pages,
                )
                return
        result = await self.extractor.extract(media_type=mime, data=data, file_name=file_name)
        text = result.text.strip()
        if not text:
            await self._store(
                record_seq, blob_seq, file_name, mime, result.model, None, "no_text", pages
            )
            return
        status = "truncated" if result.truncated else "done"
        await self._store(record_seq, blob_seq, file_name, mime, result.model, text, status, pages)
        log.info(
            "derived text stored",
            extra=ctx(
                action="attachment.extracted", entry=blob_seq, model=result.model, count=len(text)
            ),
        )

    async def give_up(self, job: Job, error: str) -> None:
        kind = error.split(":", 1)[0]
        await self._store(
            int(job.payload["record_seq"]),
            int(job.payload["blob_seq"]),
            job.payload.get("file_name"),
            job.payload.get("mime"),
            None,
            None,
            f"failed: {kind}",
            None,
        )

    async def _store(
        self, record_seq, blob_seq, file_name, mime, model, text, status, pages
    ) -> None:
        now = self.clock()
        async with self.db.engine.begin() as conn:
            previous = (
                await conn.execute(
                    select(derived_text.c.id).where(derived_text.c.blob_seq == blob_seq)
                )
            ).first()
            values = {
                "record_seq": record_seq,
                "blob_seq": blob_seq,
                "file_name": file_name,
                "mime": mime,
                "model": model,
                "extracted_at": now,
                "text": text,
                "status": status,
                "pages": pages,
                "chars": len(text) if text else None,
            }
            if previous is None:
                await conn.execute(insert(derived_text).values(**values))
            else:
                await conn.execute(
                    update(derived_text).where(derived_text.c.id == previous.id).values(**values)
                )
            if text:
                await conn.execute(
                    insert(search_chunk).values(
                        record_seq=record_seq,
                        kind="derived",
                        source=file_name,
                        content=text,
                        created_at=now,
                    )
                )
