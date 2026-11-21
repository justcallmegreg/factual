"""FAC-24: the nightly deep audit finds silent damage within a day."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest

from app.connectors.telegram.mapping import map_update
from app.ledger.audit import DeepAudit, NightlyAudit
from tests.chat_harness import Chat
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load
from tests.tsa_server import LocalTsa

FILE_ONE = b"%PDF-1.7\n" + b"one " * 300
FILE_TWO = b"\xff\xd8\xff\xe0" + b"two " * 300
AMS = ZoneInfo("Europe/Amsterdam")


@pytest.fixture(scope="module")
def tsa():
    server = LocalTsa()
    yield server
    server.close()


@pytest.fixture
async def chat(database_url, tsa):
    chat = await Chat.open(
        database_url, attachments=True, seals=True, tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")]
    )
    chat.connector.files["BQACDOC1"] = FILE_ONE
    chat.connector.files["AgACP1y"] = FILE_TWO
    await chat.say("/project new client-acme")
    await chat.say("Greg called about the kickoff")
    for name in ("message_document", "message_photo_1"):
        await chat.ingestion.handle(
            map_update(load(name), capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
        )
    while await chat.worker.run_once():
        pass
    chat.clock.advance(300)
    await chat.sealer.seal_once()
    while await chat.worker.run_once():
        pass
    chat.connector.sent.clear()
    yield chat
    await chat.close()


def audit_for(chat: Chat, **kw) -> DeepAudit:
    return chat.audit(**kw)


def tamper(database_url: str, table: str, statement: str, params=()) -> None:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER ALL")  # noqa: S608
        conn.execute(statement, params)
        conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER ALL")  # noqa: S608


async def test_a_clean_ledger_passes_and_the_run_is_recorded(chat):
    result = await audit_for(chat).run(reason="test")
    assert result.ok and result.failures == []
    assert result.entries >= 4 and result.seals == 1 and result.tokens == 2 and result.blobs == 2
    entry = chat.entries()[-1]
    assert entry["type"] == "audit.completed"
    payload = chat.payload(entry["seq"])
    assert payload["ok"] is True and payload["reason"] == "test" and payload["blobs"] == 2
    assert payload["checks"] == ["chain", "seals", "tokens", "attachments", "certificates"]
    assert chat.connector.sent == [], "nothing to report when everything passes"
    await chat.say("/status")
    assert "Last audit: passed" in chat.replies[-1]


async def test_a_changed_row_is_found_and_posted_with_the_exact_entry(chat):
    tamper(
        chat.database_url,
        "evidence_payload",
        "UPDATE evidence_payload SET payload = %s WHERE seq = 2",
        (b'{"x":1}',),
    )
    result = await audit_for(chat).run()
    assert not result.ok
    assert any(f.kind == "entry" and f.ref == "E2" for f in result.failures)
    notice = chat.connector.sent[-1][1].text
    assert (
        notice.startswith("Deep audit FAILED") and "E2" in notice and "payload commitment" in notice
    )
    payload = chat.payload(chat.entries()[-1]["seq"])
    assert payload["ok"] is False and payload["failures"][0]["ref"] == "E2"
    await chat.say("/status")
    assert "Last audit: FAILED" in chat.replies[-1] and "E2" in chat.replies[-1]


async def test_a_changed_object_is_found_and_named(chat):
    ((key, version),) = chat.rows("SELECT object_key, version_id FROM blob ORDER BY seq LIMIT 1")
    store = chat.store
    store.objects[key][0].data = b"tampered ciphertext bytes"
    result = await audit_for(chat).run()
    assert not result.ok
    failure = next(f for f in result.failures if f.kind == "object")
    assert failure.ref == f"{key}@{version}"
    assert key in chat.connector.sent[-1][1].text


async def test_a_bad_token_is_found_and_named(chat):
    tamper(
        chat.database_url,
        "seal_anchor",
        "UPDATE seal_anchor SET proof = %s WHERE id = (SELECT min(id) FROM seal_anchor)",
        (b"\x30\x03\x02\x01\x00",),
    )
    result = await audit_for(chat).run()
    assert not result.ok
    failure = next(f for f in result.failures if f.kind == "token")
    assert failure.ref.startswith("seal 1 ·") and "127.0.0.1" in failure.ref
    assert "seal 1" in chat.connector.sent[-1][1].text


async def test_a_bad_seal_signature_is_found(chat):
    tamper(chat.database_url, "seal", "UPDATE seal SET signature = %s WHERE no = 1", (bytes(64),))
    result = await audit_for(chat).run()
    assert not result.ok and any(f.kind == "seal" and f.ref == "seal 1" for f in result.failures)


async def test_rehashing_is_throttled(chat):
    started = time.perf_counter()
    result = await audit_for(chat, rehash_per_second=10).run()
    elapsed = time.perf_counter() - started
    assert result.blobs == 2 and elapsed >= 0.09, (
        elapsed
    )  # two objects at 10 per second: one gap of 0.1 s


async def test_missing_tokens_are_requested_again(chat, tsa):
    chat.anchoring.urls.append(tsa.url("tsa3"))  # a third authority configured after the seal
    result = await audit_for(chat).run()
    assert result.ok
    assert chat.rows(
        "SELECT count(*) FROM job WHERE kind = 'seal.anchor' AND status = 'queued'"
    ) == [(1,)]
    while await chat.worker.run_once():
        pass
    assert chat.rows("SELECT count(*) FROM seal_anchor") == [(3,)]


async def test_the_nightly_job_runs_once_per_night_within_its_window(chat):
    nightly = NightlyAudit(audit_for(chat), chat.db, timezone="Europe/Amsterdam", clock=chat.clock)
    chat.clock.now = datetime(2026, 11, 22, 2, 59, tzinfo=AMS).astimezone(UTC)
    assert await nightly.tick() is False
    chat.clock.now = datetime(2026, 11, 22, 3, 0, tzinfo=AMS).astimezone(UTC)
    assert await nightly.tick() is True
    assert await nightly.tick() is False
    assert chat.rows("SELECT count(*) FROM ledger_entry WHERE type = 'audit.completed'") == [(1,)]
    chat.clock.now = datetime(2026, 11, 23, 5, 30, tzinfo=AMS).astimezone(UTC)
    assert await nightly.tick() is True, "late but within the window"
    chat.clock.now = datetime(2026, 11, 24, 12, 0, tzinfo=AMS).astimezone(UTC)
    assert await nightly.tick() is False, "outside the window the night is skipped"


async def test_the_api_and_the_daily_help_show_the_last_audit(chat):
    await audit_for(chat).run()
    from app.ledger.audit import last_audit_line

    async with chat.db.engine.connect() as conn:
        line = await last_audit_line(conn)
    assert line.startswith("passed")


def test_the_audit_command_exists(tmp_path):
    from app import __main__ as entrypoint

    assert "audit" in entrypoint.__doc__.lower()
