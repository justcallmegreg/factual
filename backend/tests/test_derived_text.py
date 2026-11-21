"""FAC-23: text in photos and PDFs becomes searchable derived text; the file stays evidence."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import pytest

from app.ai.extract import ExtractedText
from app.connectors.telegram.mapping import map_update
from tests.chat_harness import Chat
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n165\n%%EOF\n"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 400


@dataclass
class ScriptedExtractor:
    text: str = "Kickoff agreed for 5 February, signed by both parties."
    model: str = "scripted-vision"
    calls: list[dict] = field(default_factory=list)
    fail: Exception | None = None

    async def extract(
        self, *, media_type: str, data: bytes, file_name: str | None
    ) -> ExtractedText:
        self.calls.append({"media_type": media_type, "size": len(data), "file_name": file_name})
        if self.fail is not None:
            raise self.fail
        return ExtractedText(text=self.text, model=self.model)


@pytest.fixture
async def chat(database_url):
    extractor = ScriptedExtractor()
    chat = await Chat.open(database_url, attachments=True, seals=True, extractor=extractor)
    chat.extractor = extractor
    chat.connector.files["BQACDOC1"] = MINIMAL_PDF
    chat.connector.files["AgACP1y"] = JPEG
    chat.connector.files["AgACP2y"] = JPEG
    chat.connector.files["AwACVOICE1"] = b"OggS" + b"\x00" * 100
    await chat.say("/project new client-acme")
    yield chat
    await chat.close()


async def deliver(chat: Chat, name: str, **changes) -> int:
    raw = load(name)
    raw["message"].update(changes)
    event = map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    await chat.ingestion.handle(event)
    return chat.rows("SELECT max(seq) FROM record")[0][0]


async def drain(chat: Chat) -> None:
    while await chat.worker.run_once():
        pass


async def test_a_stored_pdf_gets_its_text_extracted_with_model_and_time(chat):
    seq = await deliver(chat, "message_document")
    await drain(chat)
    rows = chat.rows(
        "SELECT record_seq, file_name, mime, model, text, status, extracted_at IS NOT NULL, pages"
        " FROM derived_text"
    )
    assert rows == [
        (
            seq,
            "contract-v2.pdf",
            "application/pdf",
            "scripted-vision",
            chat.extractor.text,
            "done",
            True,
            1,
        )
    ]
    (call,) = chat.extractor.calls
    assert call["media_type"] == "application/pdf" and call["size"] == len(MINIMAL_PDF)
    assert call["file_name"] == "contract-v2.pdf"
    assert chat.rows("SELECT kind, status FROM job ORDER BY id") == [
        ("attachment.fetch", "done"),
        ("attachment.extract", "done"),
    ]
    assert [e["type"] for e in chat.entries()] == [
        "project.created",
        "record.created",
        "attachment.stored",
    ]


async def test_derived_text_is_searchable_and_labelled_as_read_from_the_file(chat):
    await deliver(chat, "message_document")
    await drain(chat)
    chunks = chat.rows("SELECT kind, source, content FROM search_chunk ORDER BY id")
    assert ("derived", "contract-v2.pdf", chat.extractor.text) in chunks
    chat.clock.advance(5)
    await chat.say("/ask was the kickoff signed by both parties?")
    reply = chat.replies[-1]
    assert (
        '[text read from contract-v2.pdf] "Kickoff agreed for 5 February, signed by both parties."'
        in reply
    )
    assert "Note by Anna K." in reply


async def test_photos_are_extracted_too_and_the_tool_returns_the_derived_text(chat):
    seq = await deliver(chat, "message_photo_1")
    await drain(chat)
    ((mime, model),) = chat.rows("SELECT mime, model FROM derived_text")
    assert mime == "image/jpeg" and model == "scripted-vision"
    from app.ai.tools import ToolExecutor

    executor = ToolExecutor(chat.db, timezone="Europe/Amsterdam")
    result = json.loads(await executor.run("get_attachment_text", {"seq": seq}))
    (att,) = result["attachments"]
    assert att["derived_text"] == chat.extractor.text and att["derived_from"] == "scripted-vision"


async def test_a_voice_note_is_not_sent_to_claude(chat):
    await deliver(chat, "message_voice")
    await drain(chat)
    assert chat.extractor.calls == []
    assert chat.rows("SELECT count(*) FROM derived_text") == [(0,)]
    assert chat.rows("SELECT kind FROM job") == [("attachment.fetch",)]


async def test_files_over_the_size_or_page_limit_are_skipped_with_a_reason(chat, monkeypatch):
    from app.ai import extract

    monkeypatch.setattr(extract, "MAX_IMAGE_BYTES", 100)
    await deliver(chat, "message_photo_1")
    await drain(chat)
    assert chat.extractor.calls == []
    assert chat.rows("SELECT status, text FROM derived_text") == [
        ("skipped: file larger than 100 bytes", None)
    ]
    monkeypatch.setattr(extract, "MAX_PDF_PAGES", 0)
    await deliver(chat, "message_document", message_id=9999)
    await drain(chat)
    assert ("skipped: 1 pages, more than 0", None) in chat.rows(
        "SELECT status, text FROM derived_text"
    )
    assert chat.extractor.calls == []


async def test_the_file_and_its_hash_stay_the_evidence_derived_text_is_not_an_entry(chat):
    await deliver(chat, "message_document")
    await drain(chat)
    types = [e["type"] for e in chat.entries()]
    assert "attachment.stored" in types and all(t != "derived_text" for t in types)
    ((sha,),) = chat.rows("SELECT sha256_plain FROM blob")
    import hashlib

    assert bytes(sha) == hashlib.sha256(MINIMAL_PDF).digest()
    # the stored object is untouched by the extraction
    ((key, version),) = chat.rows("SELECT object_key, version_id FROM blob")
    assert await chat.store.get(key, version)


async def test_a_failing_extraction_is_retried_and_finally_recorded_as_failed(chat):
    chat.extractor.fail = RuntimeError("model overloaded")
    await deliver(chat, "message_document")
    for _ in range(30):
        if not await chat.worker.run_once():
            chat.clock.advance(3600)
        if chat.rows("SELECT status FROM job WHERE kind = 'attachment.extract'") == [("failed",)]:
            break
    assert chat.rows("SELECT status FROM job WHERE kind = 'attachment.extract'") == [("failed",)]
    assert chat.rows("SELECT status FROM derived_text") == [("failed: RuntimeError",)]


async def test_an_empty_extraction_is_stored_as_no_text(chat):
    chat.extractor.text = "   "
    await deliver(chat, "message_photo_1")
    await drain(chat)
    assert chat.rows("SELECT status, text FROM derived_text") == [("no_text", None)]
    assert chat.rows("SELECT count(*) FROM search_chunk WHERE kind = 'derived'") == [(0,)]


def test_the_anthropic_extractor_builds_a_document_or_image_request():
    from app.ai.extract import AnthropicExtractor

    extractor = AnthropicExtractor("sk-ant-test", "claude-sonnet-5-5")
    pdf = extractor.build_content("application/pdf", MINIMAL_PDF, "contract.pdf")
    assert pdf[0]["type"] == "document" and pdf[0]["source"]["media_type"] == "application/pdf"
    image = extractor.build_content("image/jpeg", JPEG, "photo.jpg")
    assert image[0]["type"] == "image" and image[0]["source"]["type"] == "base64"
    assert image[1]["type"] == "text" and "verbatim" in image[1]["text"].lower()


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
async def test_live_extraction(database_url):
    from app.ai.extract import AnthropicExtractor

    extractor = AnthropicExtractor(
        os.environ["ANTHROPIC_API_KEY"], os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
    )
    result = await extractor.extract(
        media_type="application/pdf", data=MINIMAL_PDF, file_name="blank.pdf"
    )
    assert result.model and isinstance(result.text, str)
