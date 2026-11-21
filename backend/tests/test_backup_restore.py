"""FAC-26: a lost server does not mean lost evidence.

The database is dumped and restored into a fresh database, the deep audit passes
against the stored seals, and Thoth notices a restored database at start-up and
audits it before it starts recording again.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.connectors.null import NullConnector
from app.ledger.keys import load_signing_key
from app.storage.boot import cluster_identifier, detect_restore
from tests.chat_harness import Chat
from tests.conftest import BACKEND_DIR, TEST_DATABASE_URL
from tests.tsa_server import LocalTsa

FOREIGN_CLUSTER = "4242424242424242424"  # the identifier of a server that is gone
FILE_ONE = b"%PDF-1.7\n" + b"one " * 300
FILE_TWO = b"\xff\xd8\xff\xe0" + b"two " * 300


@pytest.fixture(scope="module")
def tsa():
    server = LocalTsa()
    yield server
    server.close()


def boots(url: str) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT kind, restored, audit_seq, system_identifier FROM boot ORDER BY id"
        ).fetchall()


def audits(url: str) -> list[tuple]:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT e.seq, convert_from(p.payload, 'UTF8')::jsonb->>'reason' FROM ledger_entry e"
            " JOIN evidence_payload p ON p.seq = e.seq"
            " WHERE e.type = 'audit.completed' ORDER BY e.seq"
        ).fetchall()


def seed_foreign_boot(url: str) -> None:
    """As if the dump came from the old server: its last start is on record."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO boot (at, system_identifier, kind, restored, version)"
            " VALUES (now(), %s, 'start', false, '0.0.0')",
            (FOREIGN_CLUSTER,),
        )


def real_identifier(url: str) -> str:
    with psycopg.connect(url) as conn:
        return str(conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0])


class ConnectorThatLooks(NullConnector):
    """Records how many audits were on record at the moment it was started."""

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url
        self.audits_at_start: int | None = None

    async def start(self, sink) -> None:
        self.audits_at_start = len(audits(self.url))
        await super().start(sink)


def start_until_settled(settings, connector) -> tuple[int, dict]:
    """Start the app, wait until the start-up (and its audit, if any) has settled, stop it.
    Returns the last /readyz status code and body."""
    from app.main import create_app

    with TestClient(create_app(settings=settings, connector_factory=lambda _: connector)) as client:
        assert client.get("/healthz").status_code == 200, "alive while the start-up runs"
        deadline = time.monotonic() + 60
        while True:
            ready = client.get("/readyz")
            body = ready.json()
            if body.get("startup") == "ready" or "failed" in body.get("startup", ""):
                return ready.status_code, body
            assert time.monotonic() < deadline, body
            time.sleep(0.05)


def start_and_stop(settings, connector) -> None:
    code, body = start_until_settled(settings, connector)
    assert code == 200 and body == {"status": "ready", "database": "ok", "startup": "ready"}, body


async def populate(url: str, *, signing_key=None, attachments: bool = False, tsa_urls=None):
    chat = await Chat.open(
        url, attachments=attachments, seals=True, signing_key=signing_key, tsa_urls=tsa_urls
    )
    await chat.say("/project new client-acme")
    await chat.say("Greg called about the kickoff")
    if attachments:
        from app.connectors.telegram.mapping import map_update
        from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

        chat.connector.files["BQACDOC1"] = FILE_ONE
        chat.connector.files["AgACP1y"] = FILE_TWO
        for name in ("message_document", "message_photo_1"):
            await chat.ingestion.handle(
                map_update(
                    load(name), capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot"
                )
            )
        while await chat.worker.run_once():
            pass
    chat.clock.advance(300)
    await chat.sealer.seal_once()
    if attachments:
        while await chat.worker.run_once():
            pass
    return chat


def test_the_first_start_records_the_cluster_and_runs_no_audit(db_settings):
    connector = ConnectorThatLooks(db_settings.database_url)
    start_and_stop(db_settings, connector)
    assert boots(db_settings.database_url) == [
        ("start", False, None, real_identifier(db_settings.database_url))
    ]
    assert connector.audits_at_start == 0 and audits(db_settings.database_url) == []


def test_a_restored_database_is_audited_before_the_connector_starts(db_settings):
    url = db_settings.database_url
    asyncio.run(
        _populate_and_close(url, signing_key=load_signing_key(db_settings.signing_key_file))
    )
    seed_foreign_boot(url)
    connector = ConnectorThatLooks(url)
    start_and_stop(db_settings, connector)
    ((seq, reason),) = audits(url)
    assert reason == "restore"
    assert connector.audits_at_start == 1, "the audit finished before the connector started"
    kind, restored, audit_seq, identifier = boots(url)[-1]
    assert (kind, restored, audit_seq, identifier) == ("start", True, seq, real_identifier(url))


async def _populate_and_close(url: str, **kw) -> None:
    chat = await populate(url, **kw)
    await chat.close()


def test_a_failing_audit_after_a_restore_stops_the_start(db_settings):
    url = db_settings.database_url
    asyncio.run(
        _populate_and_close(url, signing_key=load_signing_key(db_settings.signing_key_file))
    )
    seed_foreign_boot(url)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("ALTER TABLE evidence_payload DISABLE TRIGGER ALL")
        conn.execute("UPDATE evidence_payload SET payload = %s WHERE seq = 2", (b'{"x":1}',))
        conn.execute("ALTER TABLE evidence_payload ENABLE TRIGGER ALL")
    connector = ConnectorThatLooks(url)
    code, body = start_until_settled(db_settings, connector)
    assert code == 503 and body["status"] == "starting" and body["database"] == "ok"
    assert body["startup"].startswith("audit failed") and "E2" in body["startup"]
    assert connector.audits_at_start is None, "Thoth did not start recording"
    assert boots(url)[-1][:2] == ("start", False), "the failed start is not recorded as a boot"


def test_audit_on_start_can_be_forced_or_switched_off(db_settings):
    url = db_settings.database_url
    forced = db_settings.model_copy(update={"audit_on_start": "always"})
    start_and_stop(forced, NullConnector())
    assert [reason for _, reason in audits(url)] == ["start"]
    seed_foreign_boot(url)
    off = db_settings.model_copy(update={"audit_on_start": "never"})
    start_and_stop(off, NullConnector())
    assert len(audits(url)) == 1, "no second audit"
    assert boots(url)[-1][:3] == ("start", True, None), "the restore is still on record"


def test_the_audit_command_records_the_cluster_so_the_start_does_not_audit_twice(
    db_settings, required_env, key_files
):
    url = db_settings.database_url
    signing, master = key_files
    asyncio.run(_populate_and_close(url, signing_key=load_signing_key(signing)))
    seed_foreign_boot(url)
    env = os.environ | {
        "DATABASE_URL": url,
        "SIGNING_KEY_FILE": str(signing),
        "MASTER_KEY_FILE": str(master),
        "LOG_LEVEL": "WARNING",
    }
    run = subprocess.run(
        [sys.executable, "-m", "app", "audit", "--reason", "restore"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    summary = run.stdout.strip().splitlines()[-1]
    assert summary.startswith("PASSED:") and "1 seals" in summary, run.stdout
    assert boots(url)[-1][:2] == ("audit", True)
    connector = ConnectorThatLooks(url)
    start_and_stop(db_settings, connector)
    assert [reason for _, reason in audits(url)] == ["restore"], "no second audit at start"
    assert connector.audits_at_start == 1
    assert boots(url)[-1][:2] == ("start", False)


async def test_detect_restore_compares_with_the_last_recorded_start(database_url):
    from app.storage.db import Database

    db = Database(database_url)
    try:
        async with db.engine.connect() as conn:
            identifier = await cluster_identifier(conn)
            first = await detect_restore(conn)
        assert identifier == real_identifier(database_url)
        assert first.identifier == identifier and first.previous is None and not first.restored
        seed_foreign_boot(database_url)
        async with db.engine.connect() as conn:
            state = await detect_restore(conn)
        assert state.restored and state.previous == FOREIGN_CLUSTER
    finally:
        await db.dispose()


# --- the object store after a restore -------------------------------------------------

TEST_S3_ENDPOINT = os.environ.get("TEST_S3_ENDPOINT", "http://localhost:9000")


def s3_available() -> bool:
    import httpx

    try:
        return httpx.get(f"{TEST_S3_ENDPOINT}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.docker
@pytest.mark.skipif(not s3_available(), reason="no S3-compatible store at TEST_S3_ENDPOINT")
async def test_a_restored_bucket_serves_an_object_whose_version_id_changed():
    """A bucket synced back from a backup gives every object a new version id; the stored
    version id is a locator, the hash in the ledger is the binding, so the current version
    is served when the stored one is unknown and the caller checks the hash."""
    from app.storage.blobs import S3ObjectStore

    def store_for(bucket: str) -> S3ObjectStore:
        return S3ObjectStore(
            endpoint=TEST_S3_ENDPOINT,
            bucket=bucket,
            access_key_id=os.environ.get("TEST_S3_ACCESS_KEY_ID", "factual"),
            secret_access_key=os.environ.get("TEST_S3_SECRET_ACCESS_KEY", "factual-dev-secret"),
            lock_days=1,
        )

    original, restored = (
        store_for(f"factual-src-{uuid.uuid4().hex[:8]}"),
        store_for(f"factual-dst-{uuid.uuid4().hex[:8]}"),
    )
    data = b"ciphertext " * 50
    checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
    await original.ensure_bucket()
    await restored.ensure_bucket()
    try:
        put = await original.put("blobs/1/file", data, checksum, None)
        synced = await restored.put("blobs/1/file", data, checksum, None)  # as a sync tool would
        assert put.version_id and synced.version_id and put.version_id != synced.version_id
        assert await restored.get("blobs/1/file", put.version_id) == data
        with pytest.raises(Exception, match="NoSuchKey|Not Found|404"):
            await restored.get("blobs/1/missing", put.version_id)
    finally:
        for store, version in ((original, put.version_id), (restored, synced.version_id)):
            await store.delete("blobs/1/file", version)
            await asyncio.to_thread(store.client.delete_bucket, Bucket=store.bucket)


# --- the drill -----------------------------------------------------------------------


def server_major(url: str) -> int:
    with psycopg.connect(url) as conn:
        return int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000


def tool_major(command: list[str]) -> int | None:
    try:
        out = subprocess.run([*command, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\s(\d+)[.\s]", out.stdout)
    return int(match.group(1)) if match else None


def pg_tool(name: str, needed: int) -> list[str] | None:
    """`pg_dump` must be at least the server's major version; `FACTUAL_PG_DUMP` may name
    another command (`docker exec -i factual-postgres-1 pg_dump`)."""
    override = os.environ.get(f"FACTUAL_{name.upper()}")
    if override:
        return shlex.split(override)
    candidates = [shutil.which(name)]
    for version in (18, 17):
        candidates += [
            f"/opt/homebrew/opt/postgresql@{version}/bin/{name}",
            f"/usr/lib/postgresql/{version}/bin/{name}",
            f"/opt/homebrew/opt/libpq/bin/{name}",
        ]
    for candidate in candidates:
        if candidate and Path(candidate).exists() and (tool_major([candidate]) or 0) >= needed:
            return [candidate]
    return None


def for_tools(url: str) -> str:
    """The URL as the tools see it: `FACTUAL_PG_TOOLS_HOST` replaces localhost when the
    tools run inside a container."""
    host = os.environ.get("FACTUAL_PG_TOOLS_HOST")
    return re.sub(r"@(localhost|127\.0\.0\.1):", f"@{host}:", url) if host else url


async def test_the_restore_drill_dumps_restores_into_a_fresh_database_and_audits(
    database_url, tsa, capsys
):
    """pg_dump → a fresh database (another cluster when FACTUAL_RESTORE_TARGET names one)
    → pg_restore → the deep audit passes against the stored seals, tokens and objects."""
    needed = server_major(database_url)
    pg_dump, pg_restore = pg_tool("pg_dump", needed), pg_tool("pg_restore", needed)
    if pg_dump is None or pg_restore is None:
        message = f"no pg_dump/pg_restore for PostgreSQL {needed} (set FACTUAL_PG_DUMP)"
        if os.environ.get("CI"):
            pytest.fail(message + ": the drill must run in CI")
        pytest.skip(message)
    from app.ledger.audit import DeepAudit
    from app.ledger.keys import public_key_bytes
    from app.ledger.ledger import Ledger
    from app.storage import grants
    from app.storage.db import Database

    chat = await populate(
        database_url, attachments=True, tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")]
    )
    source_entries = chat.rows("SELECT count(*) FROM ledger_entry")[0][0]
    assert chat.rows("SELECT count(*) FROM seal_anchor") == [(2,)]
    target_admin = os.environ.get("FACTUAL_RESTORE_TARGET", TEST_DATABASE_URL)
    fresh = f"factual_drill_{uuid.uuid4().hex[:8]}"
    timings: dict[str, float] = {}
    started = time.perf_counter()
    dump = subprocess.run(
        [*pg_dump, "--format=custom", "--no-owner", "--dbname", for_tools(database_url)],
        capture_output=True,
        timeout=300,
    )
    assert dump.returncode == 0, dump.stderr.decode()
    timings["dump"] = time.perf_counter() - started
    with psycopg.connect(target_admin, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{fresh}"')
    from sqlalchemy.engine import make_url

    fresh_url = make_url(target_admin).set(database=fresh).render_as_string(hide_password=False)
    try:
        with psycopg.connect(fresh_url, autocommit=True) as conn:
            conn.execute(grants.CREATE_RUNTIME_ROLE)  # roles are not part of a database dump
        started = time.perf_counter()
        restore = subprocess.run(
            [*pg_restore, "--no-owner", "--exit-on-error", "--dbname", for_tools(fresh_url)],
            input=dump.stdout,
            capture_output=True,
            timeout=300,
        )
        assert restore.returncode == 0, restore.stderr.decode()
        timings["restore"] = time.perf_counter() - started
        db = Database(fresh_url)
        try:
            started = time.perf_counter()
            ledger = await Ledger.open(db, public_key=public_key_bytes(chat.signing_key))
            audit = DeepAudit(
                ledger,
                db,
                chat.store,
                chat.master_key,
                None,
                None,
                public_key=ledger.public_key,
                rehash_per_second=0,
            )
            result = await audit.run(reason="restore")
            timings["audit"] = time.perf_counter() - started
            assert result.ok, [f.line() for f in result.failures]
            assert (result.entries, result.seals, result.tokens, result.blobs) == (
                source_entries,
                1,
                2,
                2,
            )
            async with db.engine.connect() as conn:
                restored_rows = (
                    await conn.execute(
                        __import__("sqlalchemy").text(
                            "SELECT count(*) FROM ledger_entry WHERE type = 'audit.completed'"
                        )
                    )
                ).scalar_one()
            assert restored_rows == 1
        finally:
            await db.dispose()
    finally:
        await chat.close()
        with psycopg.connect(target_admin, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{fresh}" WITH (FORCE)')
    size_kib = len(dump.stdout) / 1024
    with capsys.disabled():
        print(
            f"\nrestore drill: dump {timings['dump']:.1f} s ({size_kib:.0f} KiB), "
            f"restore {timings['restore']:.1f} s, audit {timings['audit']:.1f} s "
            f"({result.entries} entries, {result.seals} seal, {result.tokens} tokens, "
            f"{result.blobs} files) → {'PASSED' if result.ok else 'FAILED'}"
        )
