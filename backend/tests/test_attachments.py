"""FAC-16: files are stored with the record; albums form one record; the 20 MB rule."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from functools import partial

import pytest

from app.connectors.telegram.mapping import map_update
from app.storage.blobs import InMemoryObjectStore, decrypt, unwrap_key
from tests.chat_harness import Chat
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

FILE_BYTES = b"%PDF-1.7\n" + b"contract bytes " * 2000


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url, attachments=True)
    chat.connector.files["BQACDOC1"] = FILE_BYTES
    chat.connector.files["AgACP1y"] = b"\xff\xd8\xff\xe0" + b"photo one " * 100
    chat.connector.files["AgACP2y"] = b"\xff\xd8\xff\xe0" + b"photo two " * 100
    yield chat
    await chat.close()


async def deliver(chat: Chat, raw: dict) -> None:
    event = map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    if event.message is not None:  # as the connector does: the ✍ callback is acknowledge()
        event = replace(event, ack=partial(chat.connector.acknowledge, event.message))
    await chat.ingestion.handle(event)


async def test_a_file_is_queued_at_ingestion_and_stored_by_the_worker(chat):
    await deliver(chat, load("message_document"))
    assert chat.rows("SELECT kind, status FROM job") == [("attachment.fetch", "queued")]
    assert await chat.worker.run_once() is True
    entries = chat.entries()
    assert [e["type"] for e in entries] == ["record.created", "attachment.stored"]
    stored = entries[-1]
    record_seq = entries[0]["seq"]
    assert stored["refs"] == [record_seq]
    payload = chat.payload(stored["seq"])
    assert payload["record_seq"] == record_seq
    assert payload["sha256_plain"] == hashlib.sha256(FILE_BYTES).hexdigest()
    assert payload["size_bytes"] == len(FILE_BYTES) and payload["mime"] == "application/pdf"
    ((stored_hash,),) = chat.rows("SELECT sha256_stored FROM blob")
    assert payload["object_key"] == f"blobs/{bytes(stored_hash).hex()}"
    assert payload["object_key"] != f"blobs/{hashlib.sha256(FILE_BYTES).hexdigest()}", (
        "the key must not tell the store which document this is"
    )
    assert payload["version_id"] and payload["sha256_stored"] and payload["retain_until"]
    assert payload["attachment"]["file_name"] == "contract-v2.pdf"
    assert "wrapped_key" not in json.dumps(payload)
    ((blob_seq, sha_plain, sha_stored, size, mime, key, version, wrapped, retain, aad),) = (
        chat.rows(
            "SELECT seq, sha256_plain, sha256_stored, size_bytes, mime, object_key, version_id,"
            " wrapped_key, retain_until, aad FROM blob"
        )
    )
    assert blob_seq == stored["seq"] and bytes(sha_plain).hex() == payload["sha256_plain"]
    assert size == len(FILE_BYTES) and mime == "application/pdf" and retain is not None
    # the object in the store is ciphertext that decrypts with the unwrapped key
    store: InMemoryObjectStore = chat.store
    stored_bytes = await store.get(key, version)
    assert hashlib.sha256(stored_bytes).digest() == bytes(sha_stored)
    assert FILE_BYTES not in stored_bytes
    assert aad == f"attachment:{record_seq}:DOC1" == payload["encryption"]["aad"]
    file_key = unwrap_key(chat.master_key, bytes(wrapped), aad=aad.encode())
    assert decrypt(stored_bytes, file_key, aad=aad.encode()) == FILE_BYTES
    assert chat.rows("SELECT record_seq, status, file_name, mime FROM attachment") == [
        (record_seq, "stored", "contract-v2.pdf", "application/pdf")
    ]
    assert chat.rows("SELECT status FROM job") == [("done",)]


async def test_a_file_over_20_mb_gets_attachment_unavailable_and_the_sender_is_told_at_once(chat):
    raw = load("message_document")
    raw["message"]["document"]["file_size"] = 25 * 1024 * 1024
    await deliver(chat, raw)
    types = [e["type"] for e in chat.entries()]
    assert types == ["record.created", "attachment.unavailable"]
    payload = chat.payload(2)
    assert payload["reason"] == "too_large" and payload["attachment"]["size"] == 25 * 1024 * 1024
    assert chat.rows("SELECT count(*) FROM job") == [(0,)]
    notice = next(m for _, m in chat.connector.sent if "20 MB" in m.text)
    assert notice.reply_to.message_id == raw["message"]["message_id"]
    assert "25 MB" in notice.text and "smaller" in notice.text
    assert chat.rows("SELECT status FROM attachment") == [("unavailable",)]


async def test_a_download_that_keeps_failing_ends_in_attachment_unavailable_with_a_notice(chat):
    del chat.connector.files["BQACDOC1"]  # the platform cannot deliver the file
    await deliver(chat, load("message_document"))
    for _ in range(10):
        if not await chat.worker.run_once():
            chat.clock.advance(3600)
    types = [e["type"] for e in chat.entries()]
    assert types == ["record.created", "attachment.unavailable"]
    assert chat.payload(2)["reason"] == "download_failed"
    assert any("could not be stored" in m.text for _, m in chat.connector.sent)


async def test_photos_sent_as_one_album_form_one_record_with_one_mark(chat):
    await deliver(chat, load("message_photo_1"))
    await deliver(chat, load("message_photo_2"))
    assert chat.rows("SELECT count(*) FROM record") == [(1,)]
    entries = chat.entries()
    assert [e["type"] for e in entries] == ["record.created", "record.created"]
    assert entries[1]["refs"] == [entries[0]["seq"]], "the second photo references the album head"
    assert chat.payload(2)["normalized"]["album_of"] == entries[0]["seq"]
    assert [ref.message_id for ref in chat.connector.acknowledged] == [8816]
    pickers = [m for _, m in chat.connector.sent if "Which project?" in m.text]
    assert len(pickers) == 1
    assert chat.rows("SELECT count(*) FROM job") == [(2,)]
    while await chat.worker.run_once():
        pass
    assert chat.rows("SELECT count(*) FROM attachment WHERE status = 'stored'") == [(2,)]
    assert chat.rows("SELECT count(DISTINCT record_seq) FROM attachment") == [(1,)]
    (caption,) = chat.rows("SELECT caption FROM record")[0]
    assert caption == "The letter, page 1 and 2"


async def test_a_redelivered_album_item_changes_nothing(chat):
    await deliver(chat, load("message_photo_1"))
    await deliver(chat, load("message_photo_2"))
    await deliver(chat, load("message_photo_2"))
    assert len(chat.entries()) == 2 and chat.rows("SELECT count(*) FROM job") == [(2,)]


async def test_the_bucket_retention_follows_the_setting(chat):
    await deliver(chat, load("message_document"))
    await chat.worker.run_once()
    ((retain,),) = chat.rows("SELECT retain_until FROM blob")
    assert (retain - chat.clock()).days in (3649, 3650)


async def test_a_record_with_a_file_is_searchable_by_its_caption_while_the_file_is_stored(chat):
    await deliver(chat, load("message_document"))
    chat.clock.advance(5)
    await chat.say("/ask signed contract")
    assert "[document contract-v2.pdf]" in chat.replies[-1]


async def test_the_second_delivery_of_a_file_message_does_not_queue_twice(chat):
    raw = load("message_document")
    await deliver(chat, raw)
    await deliver(chat, copy.deepcopy(raw))
    assert chat.rows("SELECT count(*) FROM job") == [(1,)]
