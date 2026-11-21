"""FAC-10: messages become entries and projections; edits, replies, forwards, duplicates."""

from __future__ import annotations

import io
import json
import time
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest

from app.connectors.telegram.mapping import map_update
from app.ledger.ledger import Ledger
from app.records.ingest import Ingestion
from app.storage.db import Database
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load


@pytest.fixture
async def db(database_url):
    database = Database(database_url)
    yield database
    await database.dispose()


@pytest.fixture
async def ingestion(db):
    ledger = await Ledger.open(db, public_key=b"\x09" * 32)
    return Ingestion(ledger, db, timezone="Europe/Amsterdam")


class Ack:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.calls: list[int] = []

    async def __call__(self) -> None:
        # the mark is set only after the commit: the entry must already be visible
        with psycopg.connect(self.database_url) as conn:
            (count,) = conn.execute("SELECT count(*) FROM ledger_entry").fetchone()
        self.calls.append(count)


def event(name: str, ack=None):
    mapped = map_update(load(name), capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    return replace(mapped, ack=ack) if ack else mapped


def rows(database_url: str, sql: str, params=()) -> list[tuple]:
    with psycopg.connect(database_url) as conn:
        return conn.execute(sql, params).fetchall()


def entry(database_url: str, seq: int) -> dict:
    ((entry_bytes,),) = rows(database_url, "SELECT entry FROM ledger_entry WHERE seq = %s", (seq,))
    return json.loads(bytes(entry_bytes))


def payload(database_url: str, seq: int) -> dict:
    ((payload_bytes,),) = rows(
        database_url, "SELECT payload FROM evidence_payload WHERE seq = %s", (seq,)
    )
    return json.loads(bytes(payload_bytes))


async def test_a_message_becomes_a_record_created_entry_with_the_raw_update(
    ingestion, database_url
):
    ack = Ack(database_url)
    await ingestion.handle(event("message_text", ack))
    assert ack.calls == [1], "acknowledged once, after the commit"
    e = entry(database_url, 1)
    assert e["type"] == "record.created"
    assert e["source"] == {
        "connector": "telegram",
        "chat_id": CAPTURE_CHAT_ID,
        "message_id": 8812,
        "update_id": 553201177,
    }
    assert e["observed_at"] == "2026-10-29T13:03:12Z" and e["refs"] == []
    p = payload(database_url, 1)
    assert p["raw"] == load("message_text")
    n = p["normalized"]
    assert n["author"] == {"id": 51234567, "name": "Anna K.", "username": "annak"}
    assert n["text"].startswith("@09:30 Greg called.") and n["origin"] == "note"
    assert n["tags"] == ["call"] and n["stated_at"] == "2026-10-29T09:30:00+01:00"
    ((seq, text, origin, tags, stated_at, version, project),) = rows(
        database_url, "SELECT seq, text, origin, tags, stated_at, version, project FROM record"
    )
    assert seq == 1 and "Greg called." in text and origin == "note" and tags == ["call"]
    assert stated_at.astimezone(UTC) == datetime(2026, 10, 29, 8, 30, tzinfo=UTC)
    assert version == 1 and project is None
    assert rows(database_url, "SELECT record_seq, version, text FROM record_version") == [
        (1, 1, text)
    ]


async def test_log_with_text_records_the_text_after_the_command(ingestion, database_url):
    raw = load("command_log_pending")
    raw["message"]["text"] = "/log Greg agreed to the new date by phone #call"
    raw["message"]["entities"][0]["length"] = 4
    mapped = map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    assert mapped.kind == "command" and mapped.command.name == "log"
    await ingestion.handle(mapped)
    assert entry(database_url, 1)["type"] == "record.created"
    p = payload(database_url, 1)
    assert p["normalized"]["text"] == "Greg agreed to the new date by phone #call"
    assert p["raw"] == raw
    assert rows(database_url, "SELECT text FROM record") == [
        ("Greg agreed to the new date by phone #call",)
    ]


async def test_an_edit_that_changes_the_text_appends_record_edited_and_keeps_the_first_version(
    ingestion, database_url
):
    await ingestion.handle(event("message_text"))
    await ingestion.handle(event("message_edit"))
    e = entry(database_url, 2)
    assert e["type"] == "record.edited" and e["refs"] == [1]
    assert e["observed_at"] == "2026-10-29T13:04:47Z"
    assert payload(database_url, 2)["raw"] == load("message_edit")
    versions = rows(database_url, "SELECT seq, version, text FROM record_version ORDER BY version")
    assert [(s, v) for s, v, _ in versions] == [(1, 1), (2, 2)]
    assert "tomorrow" not in versions[0][2] and "tomorrow" in versions[1][2]
    ((text, version, edited_at),) = rows(
        database_url, "SELECT text, version, edited_at FROM record"
    )
    assert "tomorrow" in text and version == 2 and edited_at is not None


async def test_an_edit_without_a_real_change_is_not_recorded(ingestion, database_url):
    await ingestion.handle(event("message_text"))
    await ingestion.handle(event("message_edit"))
    await ingestion.handle(event("message_edit_unchanged"))
    assert rows(database_url, "SELECT count(*) FROM ledger_entry") == [(2,)]
    assert rows(database_url, "SELECT count(*) FROM record_version") == [(2,)]


async def test_an_edit_of_a_message_thoth_never_saw_is_recorded_as_a_new_record(
    ingestion, database_url
):
    await ingestion.handle(event("message_edit"))
    assert entry(database_url, 1)["type"] == "record.created"
    assert rows(database_url, "SELECT message_id, version FROM record") == [(8812, 1)]


async def test_a_reply_references_the_record_it_answers(ingestion, database_url):
    await ingestion.handle(event("message_text"))
    await ingestion.handle(event("message_reply"))
    e = entry(database_url, 2)
    assert e["type"] == "record.created" and e["refs"] == [1]
    n = payload(database_url, 2)["normalized"]
    assert n["reply_to"] == {"message_id": 8812, "seq": 1}
    assert rows(database_url, "SELECT reply_to_seq FROM record WHERE seq = 2") == [(1,)]


async def test_a_forwarded_message_keeps_its_original_sender_and_time(ingestion, database_url):
    await ingestion.handle(event("message_forward"))
    n = payload(database_url, 1)["normalized"]
    assert n["origin"] == "forwarded"
    assert n["forward"] == {
        "kind": "user",
        "sender_name": "Greg S.",
        "sender_id": 51234568,
        "sent_at": "2026-10-29T12:40:00Z",
    }
    ((origin, name, sender_id, sent_at),) = rows(
        database_url,
        "SELECT origin, forward_sender_name, forward_sender_id, forward_sent_at FROM record",
    )
    assert (origin, name, sender_id) == ("forwarded", "Greg S.", 51234568)
    assert sent_at.astimezone(UTC) == datetime(2026, 10, 29, 12, 40, tzinfo=UTC)


async def test_a_second_delivery_of_the_same_update_creates_no_second_entry(
    ingestion, database_url
):
    ack = Ack(database_url)
    await ingestion.handle(event("message_text", ack))
    await ingestion.handle(event("message_text", ack))
    assert rows(database_url, "SELECT count(*) FROM ledger_entry") == [(1,)]
    assert rows(database_url, "SELECT count(*) FROM record") == [(1,)]
    assert ack.calls == [1, 1], "the mark is (re)set after both deliveries"


async def test_attachments_are_listed_in_the_entry_and_the_record(ingestion, database_url):
    await ingestion.handle(event("message_document"))
    n = payload(database_url, 1)["normalized"]
    assert n["caption"] == "Signed contract #contract" and n["tags"] == ["contract"]
    assert n["attachments"] == [
        {
            "kind": "document",
            "file_id": "BQACDOC1",
            "file_unique_id": "DOC1",
            "size": 348112,
            "mime": "application/pdf",
            "file_name": "contract-v2.pdf",
        }
    ]
    assert rows(database_url, "SELECT media_kinds, text, caption FROM record") == [
        (["document"], None, "Signed contract #contract")
    ]


async def test_five_messages_are_recorded_well_within_two_seconds(ingestion, database_url):
    names = [
        "message_text",
        "message_reply",
        "message_forward",
        "message_document",
        "message_voice",
    ]
    started = time.perf_counter()
    for name in names:
        await ingestion.handle(event(name))
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, elapsed
    assert rows(database_url, "SELECT count(*) FROM record") == [(5,)]


async def test_record_text_never_reaches_the_logs(ingestion, database_url):
    from app.core.logging import configure_logging

    buf = io.StringIO()
    configure_logging("DEBUG", version="0.1.0", stream=buf)
    try:
        await ingestion.handle(event("message_text"))
        await ingestion.handle(event("message_edit"))
        await ingestion.handle(event("message_forward"))
    finally:
        configure_logging("WARNING", version="0.1.0", stream=io.StringIO())
    output = buf.getvalue()
    assert "record recorded" in output
    for sentinel in ("Greg called", "kickoff", "12 February", "Anna", "annak", "Greg S."):
        assert sentinel not in output, sentinel


async def test_other_commands_and_taps_are_left_to_the_command_layer(ingestion, database_url):
    await ingestion.handle(event("command_log_pending"))
    await ingestion.handle(event("callback_query"))
    assert rows(database_url, "SELECT count(*) FROM ledger_entry") == [(0,)]
