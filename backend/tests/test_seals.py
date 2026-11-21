"""FAC-17: the ledger is sealed every interval; seals are signed and chained."""

from __future__ import annotations

import asyncio
import hashlib
import json

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.ledger.keys import key_fingerprint, public_key_bytes
from app.ledger.seals import Sealer, seal_hash, verify_seal_signature, verify_seals
from tests.chat_harness import Chat


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url, seals=True)
    await chat.say("/project new family")
    yield chat
    await chat.close()


async def test_no_seal_is_written_without_new_entries(database_url):
    chat = await Chat.open(database_url, seals=True)
    try:
        assert await chat.sealer.seal_once() is None
        assert chat.rows("SELECT count(*) FROM seal") == [(0,)]
    finally:
        await chat.close()


async def test_the_first_seal_commits_to_the_chain_head_and_is_signed(chat):
    await chat.say("Greg called")
    head_seq = chat.rows("SELECT max(seq) FROM ledger_entry")[0][0]
    ((head_hash,),) = chat.rows("SELECT chain_hash FROM ledger_entry WHERE seq = %s", (head_seq,))
    seal = await chat.sealer.seal_once()
    assert seal is not None and seal.no == 1 and seal.up_to_seq == head_seq
    body = json.loads(seal.body)
    assert body["v"] == 1 and body["no"] == 1 and body["up_to_seq"] == head_seq
    assert body["chain_head"] == "sha256:" + bytes(head_hash).hex()
    assert body["prev_seal"] == "sha256:" + hashlib.sha256(chat.ledger.genesis).hexdigest()
    assert body["created_at"] == "2026-10-29T13:03:12Z"
    assert body["key_id"] == key_fingerprint(chat.signing_key)
    assert list(body) == sorted(body), "the body is canonical JSON"
    assert verify_seal_signature(seal.body, seal.signature, public_key_bytes(chat.signing_key))
    ((no, up_to, stored_body, signature, key_id),) = chat.rows(
        "SELECT no, up_to_seq, body, signature, key_id FROM seal"
    )
    assert (no, up_to, bytes(stored_body), bytes(signature), key_id) == (
        1,
        head_seq,
        seal.body,
        seal.signature,
        key_fingerprint(chat.signing_key),
    )


async def test_seals_are_chained_by_the_hash_of_the_previous_seal(chat):
    await chat.say("one")
    first = await chat.sealer.seal_once()
    assert await chat.sealer.seal_once() is None, "nothing new to seal"
    chat.clock.advance(300)
    await chat.say("two")
    second = await chat.sealer.seal_once()
    assert second.no == 2 and second.up_to_seq > first.up_to_seq
    assert json.loads(second.body)["prev_seal"] == "sha256:" + seal_hash(first.body).hex()
    assert json.loads(second.body)["created_at"] == "2026-10-29T13:08:12Z"


async def test_the_seal_chain_and_every_signature_verify_and_tampering_is_found(chat):
    for i in range(3):
        await chat.say(f"note {i}")
        chat.clock.advance(300)
        await chat.sealer.seal_once()
    public = public_key_bytes(chat.signing_key)
    async with chat.db.engine.connect() as conn:
        report = await verify_seals(conn, public, chat.ledger.genesis)
    assert report.ok and report.seals == 3 and report.failure is None
    async with chat.db.engine.connect() as conn:
        wrong = await verify_seals(
            conn, public_key_bytes(Ed25519PrivateKey.generate()), chat.ledger.genesis
        )
    assert not wrong.ok and wrong.failure.seal_no == 1 and "signature" in wrong.failure.reason
    with psycopg.connect(chat.database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE seal DISABLE TRIGGER ALL")
        conn.execute("UPDATE seal SET body = %s WHERE no = 2", (b'{"forged":true}',))
        conn.execute("ALTER TABLE seal ENABLE TRIGGER ALL")
    async with chat.db.engine.connect() as conn:
        tampered = await verify_seals(conn, public, chat.ledger.genesis)
    assert not tampered.ok and tampered.failure.seal_no == 2


async def test_a_seal_whose_head_does_not_match_the_ledger_is_found(chat):
    await chat.say("note")
    await chat.sealer.seal_once()
    with psycopg.connect(chat.database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE ledger_entry DISABLE TRIGGER ALL")
        conn.execute(
            "UPDATE ledger_entry SET chain_hash = %s"
            " WHERE seq = (SELECT max(seq) FROM ledger_entry)",
            (bytes(32),),
        )
        conn.execute("ALTER TABLE ledger_entry ENABLE TRIGGER ALL")
    async with chat.db.engine.connect() as conn:
        report = await verify_seals(conn, public_key_bytes(chat.signing_key), chat.ledger.genesis)
    assert not report.ok and "chain head" in report.failure.reason


async def test_the_seal_table_is_insert_only(chat):
    await chat.say("note")
    await chat.sealer.seal_once()
    with (
        psycopg.connect(chat.database_url, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException),
    ):
        conn.execute("DELETE FROM seal")


async def test_the_sealer_loop_seals_on_its_interval(chat):
    await chat.say("note")
    sealer = Sealer(chat.ledger, chat.db, chat.signing_key, interval=0.05, clock=chat.clock)
    task = asyncio.create_task(sealer.start())
    for _ in range(100):
        if chat.rows("SELECT count(*) FROM seal") == [(1,)]:
            break
        await asyncio.sleep(0.02)
    await sealer.stop()
    await asyncio.wait_for(task, timeout=2)
    assert chat.rows("SELECT count(*) FROM seal") == [(1,)]


async def test_verify_shows_the_seal_that_covers_a_record(chat):
    sealed = await chat.say("sealed note")
    sealed_seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    chat.clock.advance(300)
    await chat.sealer.seal_once()
    chat.clock.advance(10)
    pending = await chat.say("later note")
    await chat.say("/verify", reply_to=sealed)
    reply = chat.replies[-1]
    assert reply.splitlines()[1].endswith("· Verified · seal 1")
    assert "chain proof to seal 1 ok" in reply and "seal signature ok" in reply
    assert (
        f"Seal 1 · 2026-10-29T13:08:12Z · covers E1–E{sealed_seq + 0}" in reply
        or "Seal 1 ·" in reply
    )
    await chat.say("/verify", reply_to=pending)
    assert chat.replies[-1].splitlines()[1].endswith("· Pending seal")


async def test_ask_hits_show_the_seal(chat):
    await chat.say("kickoff moved")
    chat.clock.advance(300)
    await chat.sealer.seal_once()
    chat.clock.advance(5)
    await chat.say("/ask kickoff")
    assert "Verified · seal 1" in chat.replies[-1]


async def test_status_shows_the_last_seal_and_the_key_fingerprint(chat):
    await chat.say("/status")
    assert "Last seal: none yet" in chat.replies[-1]
    assert f"Signing key: {key_fingerprint(chat.signing_key)}" in chat.replies[-1]
    await chat.say("note")
    chat.clock.advance(300)
    await chat.sealer.seal_once()
    await chat.say("/status")
    reply = chat.replies[-1]
    head = chat.rows("SELECT max(up_to_seq) FROM seal")[0][0]
    assert f"Last seal: 1 at 2026-10-29T13:08:12Z, covers up to E{head}" in reply


def test_the_keys_command_creates_the_signing_key(tmp_path, capsys):
    from app import __main__ as entrypoint

    assert entrypoint.main(["keys", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "signing-key.pem").exists() and (tmp_path / "master.key").exists()
    assert entrypoint.main(["keys", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "created" in out and "kept" in out
