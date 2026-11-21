"""Fix pass after the whole-branch review: the evidence core.

A TSA token is verified with openssl before it is stored; TSA errors carry no
credentials; "Verified" needs a trusted timestamp and /verify checks the stored
attachments; renewal works per token, renews renewals and skips expired chains; a job
left "running" by a crash is run again; a separate login role can be the runtime
connection; object keys do not name the plaintext hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from sqlalchemy import text

from app.connectors.telegram.mapping import map_update
from app.ledger.tsa import TimestampClient, public_url
from app.storage.db import Database
from app.storage.jobs import JobWorker, enqueue
from tests.chat_harness import Chat
from tests.conftest import TEST_DATABASE_URL
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load
from tests.tsa_server import LocalTsa

FILE = b"%PDF-1.7\n" + b"one " * 300


@pytest.fixture(scope="module")
def tsa():
    server = LocalTsa()
    yield server
    server.close()


@pytest.fixture(scope="module")
def short_tsa():
    server = LocalTsa(days=30)
    yield server
    server.close()


@pytest.fixture(scope="module")
def mid_tsa():
    server = LocalTsa(days=60)
    yield server
    server.close()


async def drain(chat: Chat) -> None:
    while await chat.worker.run_once():
        pass


def anchors(chat: Chat) -> list[tuple]:
    return chat.rows("SELECT seal_no, kind, issuer, renews FROM seal_anchor ORDER BY id")


def queued(chat: Chat) -> list[dict]:
    return [p for (p,) in chat.rows("SELECT payload FROM job WHERE status = 'queued' ORDER BY id")]


class TamperingClient:
    """Returns real tokens with the last byte of the signature flipped."""

    def __init__(self, real: TimestampClient) -> None:
        self.real = real

    async def request(self, url: str, data: bytes):
        token = await self.real.request(url, data)
        bad = token.response_bytes[:-1] + bytes([token.response_bytes[-1] ^ 0x01])
        return replace(token, response_bytes=bad)


async def test_a_token_that_does_not_verify_is_not_stored_and_the_job_retries(database_url, tsa):
    chat = await Chat.open(database_url, seals=True, tsa_urls=[tsa.url("tsa1")])
    try:
        real = chat.anchoring.client
        chat.anchoring.client = TamperingClient(real)
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        await chat.worker.run_once()
        assert chat.rows("SELECT count(*) FROM seal_anchor") == [(0,)]
        ((status, attempts, error),) = chat.rows(
            "SELECT status, attempts, last_error FROM job WHERE kind = 'seal.anchor'"
        )
        assert (status, attempts) == ("queued", 1) and "does not verify" in error
        chat.anchoring.client = real
        chat.clock.advance(3600)
        await drain(chat)
        assert chat.rows("SELECT count(*) FROM seal_anchor") == [(1,)]
    finally:
        await chat.close()


def test_tsa_errors_name_the_authority_without_its_credentials():
    assert public_url("https://account:s3cret@tsa.example/tsr") == "https://tsa.example/tsr"
    assert public_url("http://127.0.0.1:9/x") == "http://127.0.0.1:9/x"


async def test_a_failing_qualified_authority_leaks_no_credential(database_url, tsa):
    secret_url = tsa.url("broken").replace("http://", "http://account:s3cret@")
    tsa.broken = True
    chat = await Chat.open(
        database_url, seals=True, tsa_urls=[tsa.url("tsa1")], qualified_tsa_url=secret_url
    )
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        for _ in range(3):
            await drain(chat)
            chat.clock.advance(3600)
        errors = [
            e for (e,) in chat.rows("SELECT last_error FROM job WHERE last_error IS NOT NULL")
        ]
        assert errors and all("s3cret" not in e and "account" not in e for e in errors), errors
        notices = [m.text for _, m in chat.connector.sent if "time-stamping" in m.text.lower()]
        assert notices and all("s3cret" not in n for n in notices), notices
        assert all(public_url(secret_url) in n for n in notices)
    finally:
        tsa.broken = False
        await chat.close()


async def test_verified_needs_a_trusted_timestamp(database_url, tsa):
    chat = await Chat.open(database_url, seals=True, tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")])
    try:
        await chat.say("/project new family")
        note = await chat.say("sealed note")
        chat.clock.advance(300)
        await chat.sealer.seal_once()
        await chat.say("/verify", reply_to=note)
        assert "Sealed, timestamp pending · seal 1" in chat.replies[-1], "no token yet"
        await drain(chat)
        await chat.say("/verify", reply_to=note)
        assert "Verified · seal 1" in chat.replies[-1]
    finally:
        await chat.close()


async def test_verify_checks_the_stored_attachment_against_the_object_store(database_url):
    chat = await Chat.open(database_url, attachments=True, seals=True)
    try:
        chat.connector.files["BQACDOC1"] = FILE
        await chat.say("/project new family")
        raw = load("message_document")
        await chat.ingestion.handle(
            map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
        )
        await drain(chat)
        message_id = raw["message"]["message_id"]
        await chat.say("/verify", reply_to=message_id)
        assert "attachment contract-v2.pdf checksum ok" in chat.replies[-1]
        ((key, version),) = chat.rows("SELECT object_key, version_id FROM blob")
        import base64

        replaced = chat.store.objects[key][0]  # as if another object had been put in its place
        replaced.data = b"something else"
        replaced.checksum = base64.b64encode(hashlib.sha256(b"something else").digest()).decode()
        await chat.say("/verify", reply_to=message_id)
        assert "attachment contract-v2.pdf checksum FAILED" in chat.replies[-1]
        assert "· Failed" in chat.replies[-1]
    finally:
        await chat.close()


async def test_renewals_are_renewed_and_expiring_targets_are_not_used(
    database_url, short_tsa, mid_tsa, tsa
):
    chat = await Chat.open(
        database_url, seals=True, tsa_urls=[short_tsa.url("tsa1"), mid_tsa.url("tsa2")]
    )
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        await drain(chat)
        result = await chat.audit().run()
        assert result.ok and queued(chat) == [], (
            "both authorities expire soon: nothing to renew with"
        )
        assert any("postponed" in w for w in result.warnings), result.warnings
        chat.anchoring.urls.append(tsa.url("tsa3"))  # a healthy third authority joins
        chat.clock.advance(60)
        again = await chat.audit().run()
        assert again.ok
        jobs = [j for j in queued(chat) if "renews" in j]  # the audit also asks tsa3 for seal 1
        assert len(jobs) == 2 and {j["url"] for j in jobs} == {tsa.url("tsa3")}
        assert {j["renews"] for j in jobs} == {
            a[0] for a in chat.rows("SELECT id FROM seal_anchor")
        }
        await drain(chat)
        # a renewal made by an expiring authority is itself renewed
        ((short_id,),) = chat.rows(
            "SELECT id FROM seal_anchor WHERE issuer = %s", (short_tsa.url("tsa1"),)
        )
        async with chat.db.engine.begin() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM seal_anchor WHERE id = :id"), {"id": short_id}
                )
            ).first()
            await chat.anchoring.renew(conn, row, mid_tsa.url("tsa2"), chat.clock())
        await drain(chat)
        chat.clock.advance(60)
        third = await chat.audit().run()
        assert third.ok
        (job,) = [j for j in queued(chat) if "renews" in j]
        renewal_by_mid = chat.rows(
            "SELECT id FROM seal_anchor WHERE issuer = %s AND renews = %s",
            (mid_tsa.url("tsa2"), short_id),
        )[0][0]
        assert job["renews"] == renewal_by_mid and job["url"] == tsa.url("tsa3")
    finally:
        await chat.close()


async def test_an_expired_chain_is_reported_but_not_sent_for_renewal(database_url, short_tsa, tsa):
    chat = await Chat.open(
        database_url, seals=True, tsa_urls=[short_tsa.url("tsa1"), tsa.url("tsa2")]
    )
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        await drain(chat)
        chat.clock.advance(400 * 86400)
        result = await chat.audit().run()
        assert any("expired on" in w for w in result.warnings), result.warnings
        assert queued(chat) == [], "a renewal made now cannot cover an expired token"
    finally:
        await chat.close()


class Counting:
    def __init__(self) -> None:
        self.runs: list[int] = []

    async def run(self, job) -> None:
        self.runs.append(job.id)

    async def give_up(self, job, error: str) -> None:
        return None


async def test_a_job_left_running_by_a_crash_is_run_again_after_the_lease(database_url):
    from datetime import UTC, datetime

    db = Database(database_url)
    handler = Counting()
    worker = JobWorker(db, {"demo": handler}, poll_interval=0.01, stale_after=600)
    now = datetime.now(UTC)
    try:
        async with db.engine.begin() as conn:
            job_id = await enqueue(conn, "demo", {"n": 1}, now=now)
            await conn.execute(
                text(
                    "UPDATE job SET status = 'running', attempts = 1, locked_at = :t WHERE id = :id"
                ),
                {"t": now - timedelta(seconds=601), "id": job_id},
            )
        assert await worker.run_once() is True
        assert handler.runs == [job_id]
        ((status, attempts),) = [
            tuple(r) for r in (await _rows(db, "SELECT status, attempts FROM job"))
        ]
        assert (status, attempts) == ("done", 2)
        async with db.engine.begin() as conn:
            fresh = await enqueue(conn, "demo", {"n": 2}, now=now)
            await conn.execute(
                text("UPDATE job SET status = 'running', locked_at = :t WHERE id = :id"),
                {"t": now - timedelta(seconds=10), "id": fresh},
            )
        assert await worker.run_once() is False, "a job running for 10 s is still someone's"
    finally:
        await db.dispose()


async def _rows(db: Database, sql: str):
    async with db.engine.connect() as conn:
        return (await conn.execute(text(sql))).all()


async def test_a_login_role_can_be_the_runtime_connection(database_url, settings):
    from sqlalchemy.engine import make_url

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'factual_app') THEN "
            "CREATE ROLE factual_app LOGIN PASSWORD 'factual-app-secret' IN ROLE factual_runtime; "
            "END IF; END $$"
        )
    url = (
        make_url(database_url)
        .set(username="factual_app", password="factual-app-secret")
        .render_as_string(hide_password=False)
    )
    configured = settings.model_copy(update={"runtime_database_url": url})
    assert "factual-app-secret" in configured.secret_values()
    db = Database(url, role=None)
    try:
        assert await db.check() is None
        async with db.engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "factual_app"
            assert (await conn.execute(text("SELECT count(*) FROM ledger_entry"))).scalar_one() >= 0
            with pytest.raises(Exception, match="must be owner|permission denied"):
                await conn.execute(text("ALTER TABLE ledger_entry DISABLE TRIGGER ALL"))
    finally:
        await db.dispose()


def test_the_runtime_url_is_a_documented_setting():
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    assert "RUNTIME_DATABASE_URL" in (backend / ".env.example").read_text()
    assert "RUNTIME_DATABASE_URL" in (backend / "README.md").read_text()


async def test_object_keys_name_the_ciphertext_not_the_document(database_url):
    chat = await Chat.open(database_url, attachments=True)
    try:
        chat.connector.files["BQACDOC1"] = FILE
        await chat.say("/project new family")
        await chat.ingestion.handle(
            map_update(
                load("message_document"),
                capture_chat_id=CAPTURE_CHAT_ID,
                bot_username="FactualThothBot",
            )
        )
        await drain(chat)
        ((key, stored),) = chat.rows("SELECT object_key, sha256_stored FROM blob")
        assert key == f"blobs/{bytes(stored).hex()}"
        assert hashlib.sha256(FILE).hexdigest() not in key
    finally:
        await chat.close()
