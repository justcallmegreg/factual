"""FAC-18: every seal is stamped by two time-stamping authorities; openssl accepts the tokens."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from app.ledger.seals import stamped_bytes
from app.ledger.tsa import TimestampClient, TsaError, verify_token_with_openssl
from tests.chat_harness import Chat
from tests.tsa_server import LocalTsa


@pytest.fixture(scope="module")
def tsa():
    server = LocalTsa()
    yield server
    server.close()


@pytest.fixture
async def chat(database_url, tsa):
    chat = await Chat.open(database_url, seals=True, tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")])
    await chat.say("/project new family")
    yield chat
    await chat.close()


async def test_a_request_yields_a_granted_token_with_its_certificate_chain(tsa):
    client = TimestampClient(httpx.AsyncClient())
    data = b"seal bytes" + os.urandom(8)
    token = await client.request(tsa.url("tsa1"), data)
    assert token.issuer == tsa.url("tsa1")
    assert token.gen_time is not None and token.policy == "1.3.6.1.4.1.99999.1.1"
    assert token.response_bytes[:2] == b"\x30\x82"
    assert token.chain_pem.count(b"BEGIN CERTIFICATE") == 2  # the TSA certificate and its CA
    assert token.message_imprint == hashlib.sha256(data).digest()


async def test_a_response_for_other_data_is_refused(tsa):
    client = TimestampClient(httpx.AsyncClient())
    token = await client.request(tsa.url("tsa1"), b"data one")
    with pytest.raises(TsaError, match="imprint"):
        client.check(token.response_bytes, b"data two", nonce=None)


async def test_a_failing_authority_raises(tsa):
    tsa.broken = True
    try:
        client = TimestampClient(httpx.AsyncClient())
        with pytest.raises(TsaError):
            await client.request(tsa.url("broken"), b"x")
    finally:
        tsa.broken = False


async def test_openssl_ts_verify_accepts_a_stored_token(tsa):
    client = TimestampClient(httpx.AsyncClient())
    data = b"seal" + b"signature"
    token = await client.request(tsa.url("tsa2"), data)
    assert verify_token_with_openssl(token.response_bytes, data, token.chain_pem)
    assert not verify_token_with_openssl(token.response_bytes, data + b"x", token.chain_pem)
    with tempfile.TemporaryDirectory() as d:
        Path(d, "482.tsr").write_bytes(token.response_bytes)
        Path(d, "482.stamped").write_bytes(data)
        Path(d, "chain.pem").write_bytes(token.chain_pem)
        result = subprocess.run(
            [
                "openssl",
                "ts",
                "-verify",
                "-in",
                "482.tsr",
                "-data",
                "482.stamped",
                "-CAfile",
                "chain.pem",
            ],
            cwd=d,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0 and "Verification: OK" in result.stdout + result.stderr


async def test_each_seal_gets_tokens_from_both_authorities(chat, tsa):
    await chat.say("note")
    seal = await chat.sealer.seal_once()
    assert chat.rows("SELECT count(*) FROM job WHERE kind = 'seal.anchor'") == [(2,)]
    while await chat.worker.run_once():
        pass
    rows = chat.rows(
        "SELECT seal_no, kind, issuer, length(proof) > 100, gen_time IS NOT NULL, policy"
        " FROM seal_anchor ORDER BY issuer"
    )
    assert rows == [
        (seal.no, "tsa", tsa.url("tsa1"), True, True, "1.3.6.1.4.1.99999.1.1"),
        (seal.no, "tsa", tsa.url("tsa2"), True, True, "1.3.6.1.4.1.99999.1.1"),
    ]
    ((proof, chain),) = chat.rows(
        "SELECT proof, chain_pem FROM seal_anchor WHERE issuer = %s", (tsa.url("tsa1"),)
    )
    assert verify_token_with_openssl(
        bytes(proof), stamped_bytes(seal.body, seal.signature), bytes(chain)
    )


async def test_a_tsa_failure_is_retried_and_reported_after_three_attempts(database_url, tsa):
    tsa.broken = True
    chat = await Chat.open(database_url, seals=True, tsa_urls=[tsa.url("tsa1"), tsa.url("broken")])
    try:
        await chat.say("/project new family")
        await chat.say("note")
        await chat.sealer.seal_once()
        attempts = 0
        for _ in range(3):
            while await chat.worker.run_once():
                pass
            attempts += 1
            chat.clock.advance(3600)
        notices = [m.text for _, m in chat.connector.sent if "time-stamping" in m.text.lower()]
        assert len(notices) == 1 and "3 attempts" in notices[0] and "broken" in notices[0]
        assert chat.rows("SELECT count(*) FROM seal_anchor") == [(1,)], "the healthy TSA stamped"
        tsa.broken = False
        while await chat.worker.run_once():
            pass
        assert chat.rows("SELECT count(*) FROM seal_anchor") == [(2,)]
        assert chat.rows("SELECT status FROM job WHERE kind = 'seal.anchor' ORDER BY id") == [
            ("done",),
            ("done",),
        ]
    finally:
        tsa.broken = False
        await chat.close()


async def test_verify_shows_the_timestamp_of_the_covering_seal(chat, tsa):
    note = await chat.say("note")
    chat.clock.advance(300)
    await chat.sealer.seal_once()
    while await chat.worker.run_once():
        pass
    await chat.say("/verify", reply_to=note)
    reply = chat.replies[-1]
    assert "Verified · seal 1" in reply
    assert "Timestamped by 127.0.0.1" in reply and reply.count("127.0.0.1") == 2


async def test_the_anchor_table_is_insert_only(chat):
    await chat.say("note")
    await chat.sealer.seal_once()
    while await chat.worker.run_once():
        pass
    import psycopg

    with (
        psycopg.connect(chat.database_url, autocommit=True) as conn,
        pytest.raises(psycopg.errors.RaiseException),
    ):
        conn.execute("DELETE FROM seal_anchor")


def public_tsa_reachable() -> bool:
    try:
        return httpx.get("https://freetsa.org/", timeout=5).status_code < 500
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not public_tsa_reachable(), reason="no network")
async def test_two_public_authorities_answer_a_real_request():
    client = TimestampClient(httpx.AsyncClient())
    data = b"factual public tsa check"
    for url in ("https://freetsa.org/tsr", "http://zeitstempel.dfn.de"):
        token = await client.request(url, data)
        assert token.message_imprint == hashlib.sha256(data).digest()
        assert token.gen_time is not None and token.chain_pem.count(b"BEGIN CERTIFICATE") >= 1
        assert verify_token_with_openssl(token.response_bytes, data, token.chain_pem)
