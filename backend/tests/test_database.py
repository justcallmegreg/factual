"""FAC-07: schema under migration control; evidence tables are insert-only."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege, RaiseException
from sqlalchemy import text

from app.connectors.null import NullConnector

EVIDENCE_TABLES = ("ledger_entry", "evidence_payload")


def connect(url: str, role: str | None = None) -> psycopg.Connection:
    conn = psycopg.connect(url, autocommit=True)
    if role:
        conn.execute(f"SET ROLE {role}")
    return conn


def insert_entry(conn: psycopg.Connection, seq: int = 1) -> None:
    conn.execute(
        "INSERT INTO ledger_entry"
        " (seq, type, entry, entry_hash, chain_hash, source_event, received_at)"
        " VALUES (%s, 'record.created', %s, %s, %s, %s, %s)",
        (seq, b"{}", b"\x01" * 32, b"\x02" * 32, f"telegram:{seq}", datetime.now(UTC)),
    )
    conn.execute(
        "INSERT INTO evidence_payload (seq, salt, payload) VALUES (%s, %s, %s)",
        (seq, b"\x03" * 32, b"{}"),
    )


def columns(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchall()
    return dict(rows)


def test_the_first_migration_creates_the_evidence_tables_and_the_project_projection(database_url):
    with connect(database_url) as conn:
        assert columns(conn, "ledger_entry") == {
            "seq": "bigint",
            "type": "text",
            "entry": "bytea",
            "entry_hash": "bytea",
            "chain_hash": "bytea",
            "source_event": "text",
            "received_at": "timestamp with time zone",
        }
        assert columns(conn, "evidence_payload") == {
            "seq": "bigint",
            "salt": "bytea",
            "payload": "bytea",
        }
        assert columns(conn, "project") == {
            "name": "text",
            "title": "text",
            "archived_at": "timestamp with time zone",
            "created_at": "timestamp with time zone",
        }
        (head,) = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        assert head


def test_running_the_migrations_twice_is_a_no_op(database_url):
    from app.storage.db import run_migrations

    run_migrations(database_url)
    run_migrations(database_url)
    with connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM alembic_version").fetchone() == (1,)


def test_the_project_name_rule_is_enforced_by_the_database(database_url):
    with connect(database_url) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO project (name, created_at) VALUES ('Client Acme', now())",
        )


@pytest.mark.parametrize("table", EVIDENCE_TABLES)
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE {table} SET seq = seq WHERE seq = 1",
        "DELETE FROM {table} WHERE seq = 1",
        "TRUNCATE {table} CASCADE",
    ],
)
def test_a_trigger_rejects_update_delete_and_truncate_even_for_the_owner(
    database_url, table, statement
):
    with connect(database_url) as conn:
        insert_entry(conn)
        with pytest.raises(RaiseException, match="insert-only"):
            conn.execute(statement.format(table=table))
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)  # noqa: S608


@pytest.mark.parametrize("table", EVIDENCE_TABLES)
def test_the_runtime_role_cannot_update_or_delete_evidence_rows(database_url, table):
    with connect(database_url) as owner:
        insert_entry(owner)
    with connect(database_url, role="factual_runtime") as runtime:
        with pytest.raises(InsufficientPrivilege):
            runtime.execute(f"UPDATE {table} SET seq = seq WHERE seq = 1")  # noqa: S608
        with pytest.raises(InsufficientPrivilege):
            runtime.execute(f"DELETE FROM {table} WHERE seq = 1")  # noqa: S608
        assert runtime.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)  # noqa: S608


def test_the_runtime_role_can_insert_evidence_and_maintain_projections(database_url):
    with connect(database_url, role="factual_runtime") as runtime:
        insert_entry(runtime, seq=7)
        runtime.execute("INSERT INTO project (name, created_at) VALUES ('family', now())")
        runtime.execute("UPDATE project SET archived_at = now() WHERE name = 'family'")
        runtime.execute("DELETE FROM project WHERE name = 'family'")


async def test_the_app_database_connects_with_the_runtime_role(database_url):
    from app.storage.db import Database

    db = Database(database_url)
    try:
        async with db.engine.connect() as conn:
            role = (await conn.execute(text("SELECT current_role"))).scalar_one()
            assert role == "factual_runtime"
        assert await db.check() is None
    finally:
        await db.dispose()


async def test_the_database_check_reports_an_unreachable_server():
    from app.storage.db import Database

    db = Database("postgresql://factual:factual@127.0.0.1:1/factual", connect_timeout=1)
    try:
        problem = await db.check()
        assert problem and "connection" in problem.lower() or "refused" in problem.lower()
    finally:
        await db.dispose()


def test_sqlalchemy_url_uses_the_psycopg_driver():
    from app.storage.db import sqlalchemy_url

    assert sqlalchemy_url("postgresql://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert sqlalchemy_url("postgres://u:p@h/d") == "postgresql+psycopg://u:p@h/d"


def test_the_app_applies_the_migrations_at_start(settings, template_database, key_files):
    """A database that was never migrated gets its tables when the app starts."""
    from sqlalchemy.engine import make_url

    from app.main import create_app
    from tests.conftest import TEST_DATABASE_URL, _admin_connect

    name = "factual_fresh_start"
    with _admin_connect() as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    try:
        started = settings.model_copy(
            update={
                "database_url": url,
                "signing_key_file": key_files[0],
                "master_key_file": key_files[1],
            }
        )
        with TestClient(create_app(settings=started, connector_factory=lambda _: NullConnector())):
            with connect(url) as conn:
                assert set(columns(conn, "ledger_entry")) == {
                    "seq",
                    "type",
                    "entry",
                    "entry_hash",
                    "chain_hash",
                    "source_event",
                    "received_at",
                }
    finally:
        with _admin_connect() as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_readyz_reports_the_database_state(db_settings):
    from app.main import create_app

    app = create_app(settings=db_settings, connector_factory=lambda _: NullConnector())
    with TestClient(app) as client:
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "database": "ok"}

        class Broken:
            async def check(self):
                return "connection refused"

        app.state.db = Broken()
        down = client.get("/readyz")
        assert down.status_code == 503
        assert down.json() == {"status": "not ready", "database": "connection refused"}
