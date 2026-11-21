"""FAC-27: optional anchors.

One seal a day gets a qualified timestamp when QUALIFIED_TSA_URL is set; the deep
audit re-timestamps tokens before their authority's certificate expires (RFC 4998's
approach: a new token over the old one); packs carry both and verify.py checks them.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.ledger.tsa import verify_token_with_openssl
from tests.chat_harness import Chat
from tests.tsa_server import LocalTsa

BACKEND = Path(__file__).resolve().parents[1]
SYSTEM_PYTHON = shutil.which("python3") or sys.executable


@pytest.fixture(scope="module")
def tsa():
    server = LocalTsa()
    yield server
    server.close()


@pytest.fixture(scope="module")
def short_tsa():
    """An authority whose certificate expires in 30 days."""
    server = LocalTsa(days=30)
    yield server
    server.close()


def anchors(chat: Chat) -> list[tuple]:
    return chat.rows("SELECT seal_no, kind, issuer, renews FROM seal_anchor ORDER BY id")


def queued(chat: Chat) -> list[dict]:
    return [p for (p,) in chat.rows("SELECT payload FROM job WHERE status = 'queued' ORDER BY id")]


async def drain(chat: Chat) -> None:
    while await chat.worker.run_once():
        pass


async def sealed_note(chat: Chat, text: str) -> int:
    await chat.say(text)
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    await chat.sealer.seal_once()
    await drain(chat)
    return seq


def unpack(pack, where: Path) -> dict:
    zipfile.ZipFile(io.BytesIO(pack.data)).extractall(where)
    return json.loads((where / "manifest.json").read_text())


def run_verify(where: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SYSTEM_PYTHON, "-I", "-S", "verify.py"], cwd=where, capture_output=True, text=True
    )


async def test_the_first_seal_of_each_day_gets_a_qualified_timestamp(database_url, tsa):
    chat = await Chat.open(
        database_url,
        seals=True,
        tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")],
        qualified_tsa_url=tsa.url("qualified"),
    )
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        assert len(queued(chat)) == 3, "two ordinary tokens and the qualified one"
        await drain(chat)
        assert anchors(chat) == [
            (1, "tsa", tsa.url("tsa1"), None),
            (1, "tsa", tsa.url("tsa2"), None),
            (1, "qualified_tsa", tsa.url("qualified"), None),
        ]
        chat.clock.advance(3600)
        await sealed_note(chat, "later the same day")
        assert [k for _, k, _, _ in anchors(chat)].count("qualified_tsa") == 1
        chat.clock.advance(24 * 3600)
        await sealed_note(chat, "the next day")
        assert [(no, k) for no, k, _, _ in anchors(chat) if k == "qualified_tsa"] == [
            (1, "qualified_tsa"),
            (3, "qualified_tsa"),
        ]
        assert chat.rows("SELECT count(*) FROM schedule_run WHERE job = 'qualified_tsa'") == [(2,)]
        assert tsa.requests.count("/qualified") == 2
    finally:
        await chat.close()


async def test_without_the_setting_no_qualified_stamp_is_requested(database_url, tsa):
    chat = await Chat.open(database_url, seals=True, tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")])
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        await drain(chat)
        assert [k for _, k, _, _ in anchors(chat)] == ["tsa", "tsa"]
        assert chat.rows("SELECT count(*) FROM schedule_run") == [(0,)]
    finally:
        await chat.close()


async def test_qualified_tsa_credentials_stay_out_of_the_database(database_url, tsa):
    secret_url = tsa.url("qualified").replace("http://", "http://account:s3cret@")
    chat = await Chat.open(
        database_url,
        seals=True,
        tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")],
        qualified_tsa_url=secret_url,
    )
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        assert all("s3cret" not in json.dumps(p) for p in queued(chat))
        await drain(chat)
        (qualified,) = [a for a in anchors(chat) if a[1] == "qualified_tsa"]
        assert qualified[2] == tsa.url("qualified"), "the issuer is stored without the credentials"
        assert "/qualified" in tsa.requests
    finally:
        await chat.close()


async def test_the_pack_carries_the_qualified_stamp_and_verify_py_checks_it(
    database_url, tsa, tmp_path
):
    chat = await Chat.open(
        database_url,
        seals=True,
        tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")],
        qualified_tsa_url=tsa.url("qualified"),
    )
    try:
        await chat.say("/project new family")
        seq = await sealed_note(chat, "a sealed note")
        pack = await chat.commands.options["pack_builder"].build([seq])
    finally:
        await chat.close()
    manifest = unpack(pack, tmp_path)
    (seal,) = manifest["seals"]
    kinds = [a.get("kind", "tsa") for a in seal["anchors"]]
    assert kinds == ["tsa", "tsa", "qualified_tsa"]
    assert all((tmp_path / a["token"]).exists() for a in seal["anchors"])
    result = run_verify(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "qualified timestamp" in result.stdout
    assert result.stdout.count("ts -verify: OK") == 3


async def test_the_audit_renews_timestamps_before_a_certificate_expires(
    database_url, tsa, short_tsa
):
    chat = await Chat.open(
        database_url, seals=True, tsa_urls=[short_tsa.url("tsa1"), tsa.url("tsa2")]
    )
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        await drain(chat)
        ((short_id,),) = chat.rows(
            "SELECT id FROM seal_anchor WHERE issuer = %s", (short_tsa.url("tsa1"),)
        )
        result = await chat.audit().run()
        assert result.ok and result.tokens == 2
        assert any("expires on" in w for w in result.warnings), result.warnings
        assert any("renew" in w for w in result.warnings), result.warnings
        (job,) = queued(chat)
        assert job == {"seal_no": 1, "url": tsa.url("tsa2"), "kind": "tsa", "renews": short_id}
        await drain(chat)
        assert anchors(chat)[-1] == (1, "tsa", tsa.url("tsa2"), short_id)
        chat.clock.advance(60)  # a second run in the same second would be the same audit entry
        again = await chat.audit().run()
        assert again.ok and again.tokens == 3, [f.line() for f in again.failures]
        assert queued(chat) == [], "a renewed token is not renewed again"
        payload = chat.payload(chat.entries()[-1]["seq"])
        assert payload["tokens"] == 3
    finally:
        await chat.close()


async def test_a_token_whose_certificate_expired_passes_only_through_a_verified_renewal(
    database_url, tsa, short_tsa
):
    chat = await Chat.open(
        database_url, seals=True, tsa_urls=[short_tsa.url("tsa1"), tsa.url("tsa2")]
    )
    try:
        await chat.say("/project new family")
        await chat.sealer.seal_once()
        await drain(chat)
        ((short_chain,),) = chat.rows(
            "SELECT chain_pem FROM seal_anchor WHERE issuer = %s", (short_tsa.url("tsa1"),)
        )
        calls: list = []

        def as_if_expired(response_bytes, data, chain_pem, *, at=None):
            """The short authority's certificate has expired: its tokens verify only at a
            time inside the validity, which a verified renewal supplies."""
            calls.append(at)
            if bytes(chain_pem) == bytes(short_chain) and at is None:
                return False
            return verify_token_with_openssl(response_bytes, data, chain_pem, at=at)

        before = await chat.audit(token_verifier=as_if_expired).run()
        assert not before.ok
        (failure,) = before.failures
        assert failure.kind == "token" and short_tsa.url("tsa1") in failure.ref
        await drain(chat)  # the renewal the audit requested
        assert anchors(chat)[-1][3] is not None
        after = await chat.audit(token_verifier=as_if_expired).run()
        assert after.ok, [f.line() for f in after.failures]
        assert any(at is not None for at in calls), "the old token was verified at the renewal time"
    finally:
        await chat.close()


async def test_renewal_tokens_travel_in_the_pack_and_verify_py_checks_them(
    database_url, tsa, short_tsa, tmp_path
):
    chat = await Chat.open(
        database_url, seals=True, tsa_urls=[short_tsa.url("tsa1"), tsa.url("tsa2")]
    )
    try:
        await chat.say("/project new family")
        seq = await sealed_note(chat, "a sealed note")
        await chat.audit().run()
        await drain(chat)
        assert len(anchors(chat)) == 3
        pack = await chat.commands.options["pack_builder"].build([seq])
    finally:
        await chat.close()
    manifest = unpack(pack, tmp_path)
    (seal,) = manifest["seals"]
    renewal = seal["anchors"][-1]
    assert renewal["renews"] == seal["anchors"][0]["token"]
    result = run_verify(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "renewal" in result.stdout and result.stdout.count("ts -verify: OK") == 3


def test_verify_token_at_a_time_outside_the_certificate_validity_fails(tsa):
    import asyncio
    from datetime import UTC, datetime

    import httpx

    from app.ledger.tsa import TimestampClient

    async def token():
        async with httpx.AsyncClient() as http:
            return await TimestampClient(http).request(tsa.url("tsa1"), b"some sealed bytes")

    granted = asyncio.run(token())
    assert verify_token_with_openssl(
        granted.response_bytes, b"some sealed bytes", granted.chain_pem
    )
    assert verify_token_with_openssl(
        granted.response_bytes, b"some sealed bytes", granted.chain_pem, at=datetime.now(UTC)
    )
    assert not verify_token_with_openssl(
        granted.response_bytes,
        b"some sealed bytes",
        granted.chain_pem,
        at=datetime(2050, 1, 1, tzinfo=UTC),
    )
