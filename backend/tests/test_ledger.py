"""FAC-08: append, hash chain, verification and chain proofs against PostgreSQL."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime

import psycopg
import pytest
from sqlalchemy import text

from app.ledger.chain import verify_proof
from app.ledger.hashing import chain_hash, entry_hash, payload_commitment
from app.ledger.ledger import Ledger
from app.storage.db import Database

NOW = datetime(2026, 10, 29, 13, 3, 12, tzinfo=UTC)


@pytest.fixture
async def db(database_url):
    database = Database(database_url)
    yield database
    await database.dispose()


@pytest.fixture
async def ledger(db):
    """A ledger whose instance row (public key, creation time, genesis) exists."""
    return await Ledger.open(db, public_key=os.urandom(32))


def source(update_id: int) -> dict:
    return {
        "connector": "telegram",
        "chat_id": -1001,
        "message_id": update_id,
        "update_id": update_id,
    }


async def append_n(ledger: Ledger, db: Database, count: int, start: int = 1) -> list:
    results = []
    for i in range(start, start + count):
        async with db.engine.begin() as conn:
            results.append(
                await ledger.append(
                    conn,
                    type="record.created",
                    source=source(i),
                    source_event=f"telegram:update:{i}",
                    observed_at=NOW,
                    payload={"raw": {"update_id": i, "message": {"text": f"note {i}"}}},
                )
            )
    return results


async def test_append_writes_entry_payload_and_chain_in_one_transaction(ledger, db, database_url):
    (appended,) = await append_n(ledger, db, 1)
    assert appended.seq == 1
    with psycopg.connect(database_url) as conn:
        entry_row = conn.execute("SELECT * FROM ledger_entry WHERE seq = 1").fetchone()
        payload_row = conn.execute(
            "SELECT salt, payload FROM evidence_payload WHERE seq = 1"
        ).fetchone()
    seq, type_, entry_bytes, stored_entry_hash, stored_chain_hash, source_event, received_at = (
        entry_row
    )
    salt, payload_bytes = payload_row
    assert type_ == "record.created" and source_event == "telegram:update:1"
    assert len(salt) == 32
    entry = json.loads(entry_bytes)
    assert entry["v"] == 1 and entry["seq"] == 1 and entry["type"] == "record.created"
    assert entry["source"] == source(1)
    assert entry["observed_at"] == "2026-10-29T13:03:12Z"
    assert entry["received_at"].endswith("Z") and len(entry["received_at"]) == 24
    assert entry["refs"] == []
    assert entry["payload_commitment"] == "sha256:" + payload_commitment(salt, payload_bytes).hex()
    assert json.loads(payload_bytes) == {"raw": {"update_id": 1, "message": {"text": "note 1"}}}
    assert stored_entry_hash == entry_hash(entry_bytes) == appended.entry_hash
    assert stored_chain_hash == chain_hash(ledger.genesis, stored_entry_hash) == appended.chain_hash


async def test_entry_bytes_are_canonical_json(ledger, db):
    (appended,) = await append_n(ledger, db, 1)
    assert b" " not in appended.entry_bytes and b"\n" not in appended.entry_bytes
    assert list(json.loads(appended.entry_bytes)) == sorted(json.loads(appended.entry_bytes))


async def test_sequence_numbers_have_no_gaps_under_concurrent_appends(ledger, db):
    async def one(i: int):
        async with db.engine.begin() as conn:
            return await ledger.append(
                conn,
                type="record.created",
                source=source(i),
                source_event=f"telegram:update:{i}",
                observed_at=NOW,
                payload={"i": i},
            )

    results = await asyncio.gather(*(one(i) for i in range(1, 26)))
    assert sorted(r.seq for r in results) == list(range(1, 26))
    async with db.engine.connect() as conn:
        report = await ledger.verify_chain(conn)
    assert report.ok and report.head_seq == 25


async def test_a_second_delivery_of_the_same_event_creates_no_second_entry(ledger, db):
    (first,) = await append_n(ledger, db, 1)
    (again,) = await append_n(ledger, db, 1)
    assert again.seq == first.seq and again.duplicate and not first.duplicate
    async with db.engine.connect() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM ledger_entry"))).scalar_one() == 1


async def test_salts_are_random(ledger, db, database_url):
    await append_n(ledger, db, 3)
    with psycopg.connect(database_url) as conn:
        salts = [row[0] for row in conn.execute("SELECT salt FROM evidence_payload").fetchall()]
    assert len({bytes(s) for s in salts}) == 3


async def test_verify_chain_passes_on_an_untouched_ledger(ledger, db):
    await append_n(ledger, db, 10)
    async with db.engine.connect() as conn:
        report = await ledger.verify_chain(conn)
    assert report.ok and report.head_seq == 10 and report.entries == 10 and report.failure is None


def tamper(database_url: str, statement: str, params=()) -> None:
    """What a database admin could do: switch the guard off and change a row."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE ledger_entry DISABLE TRIGGER ALL")
        conn.execute("ALTER TABLE evidence_payload DISABLE TRIGGER ALL")
        conn.execute(statement, params)
        conn.execute("ALTER TABLE ledger_entry ENABLE TRIGGER ALL")
        conn.execute("ALTER TABLE evidence_payload ENABLE TRIGGER ALL")


@pytest.mark.parametrize(
    ("statement", "expected_seq", "reason"),
    [
        ("UPDATE evidence_payload SET payload = %s WHERE seq = 4", 4, "payload"),
        ("UPDATE ledger_entry SET entry = %s WHERE seq = 4", 4, "entry"),
        ("UPDATE ledger_entry SET chain_hash = %s WHERE seq = 4", 4, "chain"),
        ("DELETE FROM evidence_payload WHERE seq = 4", 4, "payload"),
        ("DELETE FROM ledger_entry WHERE seq = 4", 5, "gap"),
    ],
)
async def test_verify_chain_fails_after_any_manual_change(
    ledger, db, database_url, statement, expected_seq, reason
):
    await append_n(ledger, db, 6)
    params = (b'{"i":99}',) if "payload =" in statement else ()
    if "entry =" in statement:
        params = (b'{"forged":true}',)
    if "chain_hash =" in statement:
        params = (os.urandom(32),)
    if reason == "gap":
        tamper(database_url, "DELETE FROM evidence_payload WHERE seq = 4")
    tamper(database_url, statement, params)
    async with db.engine.connect() as conn:
        report = await ledger.verify_chain(conn)
    assert not report.ok
    assert report.failure.seq == expected_seq
    assert reason in report.failure.reason


async def test_a_chain_proof_for_one_entry_verifies_against_the_head(ledger, db):
    appended = await append_n(ledger, db, 7)
    async with db.engine.connect() as conn:
        proof = await ledger.proof(conn, seq=3, up_to_seq=7)
    assert proof.seq == 3
    assert proof.chain_before == appended[1].chain_hash
    assert proof.later_entry_hashes == [a.entry_hash for a in appended[3:]]
    assert proof.chain_head == appended[-1].chain_hash
    assert verify_proof(proof)
    # the proof discloses only the one entry; others appear as hashes
    assert json.loads(proof.entry_bytes)["seq"] == 3
    assert all(len(h) == 32 for h in proof.later_entry_hashes)


async def test_a_proof_for_the_first_entry_starts_from_the_genesis(ledger, db):
    await append_n(ledger, db, 2)
    async with db.engine.connect() as conn:
        proof = await ledger.proof(conn, seq=1, up_to_seq=2)
    assert proof.chain_before == ledger.genesis
    assert verify_proof(proof)


async def test_a_forged_proof_does_not_verify(ledger, db):
    await append_n(ledger, db, 3)
    async with db.engine.connect() as conn:
        proof = await ledger.proof(conn, seq=2, up_to_seq=3)
    forged = proof.__class__(**{**proof.__dict__, "entry_bytes": b'{"seq":2,"forged":true}'})
    assert not verify_proof(forged)


async def test_the_instance_row_is_created_once_and_fixes_the_genesis(db, database_url):
    key = os.urandom(32)
    first = await Ledger.open(db, public_key=key)
    second = await Ledger.open(db, public_key=key)
    assert first.genesis == second.genesis
    with psycopg.connect(database_url) as conn:
        (count,) = conn.execute("SELECT count(*) FROM instance").fetchone()
        (stored_key, created_at, genesis) = conn.execute(
            "SELECT public_key, created_at, genesis_hash FROM instance"
        ).fetchone()
    assert count == 1 and bytes(stored_key) == key
    created = created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert bytes(genesis) == hashlib.sha256(b"ledger-genesis-v1" + key + created.encode()).digest()
    assert first.genesis == bytes(genesis)
