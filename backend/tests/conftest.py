"""Shared fixtures: valid settings that point nowhere real."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://factual:factual@localhost:5432/factual"
)

REQUIRED_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
    "DATABASE_URL": "postgresql://factual:factual@localhost:5432/factual",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_BUCKET": "factual",
    "S3_ACCESS_KEY_ID": "minioadmin",
    "S3_SECRET_ACCESS_KEY": "minioadmin",
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "SIGNING_KEY_FILE": "/nonexistent/signing.pem",
    "MASTER_KEY_FILE": "/nonexistent/master.key",
    "TSA_URLS": "http://tsa-one.example/tsr,http://tsa-two.example/tsr",
}

ALL_ENV_NAMES = [
    "LOG_LEVEL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CAPTURE_CHAT_ID",
    "DATABASE_URL",
    "S3_ENDPOINT",
    "S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
    "S3_OBJECT_LOCK_DAYS",
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "ASK_DAILY_LIMIT",
    "SIGNING_KEY_FILE",
    "MASTER_KEY_FILE",
    "TSA_URLS",
    "QUALIFIED_TSA_URL",
    "SEAL_INTERVAL_SECONDS",
    "TIMEZONE",
    "DAILY_HELP_TIME",
    "ACK_REACTION",
    "API_TOKEN",
]

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every Factual variable from the environment."""
    for name in ALL_ENV_NAMES + ["APP_VERSION"]:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def required_env(clean_env):
    """Set only the required variables."""
    for name, value in REQUIRED_ENV.items():
        clean_env.setenv(name, value)
    return clean_env


@pytest.fixture
def settings(required_env):
    from app.core.settings import Settings

    return Settings(_env_file=None)


def env_names_in(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(line.split("=", 1)[0])
    return names


def pytest_collection_modifyitems(config, items):
    """Tests marked `docker` build and run the image; skip them without a daemon."""
    if any(item.get_closest_marker("docker") for item in items):
        from tests.test_container import docker_available

        if not docker_available():
            skip = pytest.mark.skip(reason="no Docker daemon")
            for item in items:
                if item.get_closest_marker("docker"):
                    item.add_marker(skip)


# --- PostgreSQL ------------------------------------------------------------------------
# The admin connection (TEST_DATABASE_URL) creates one migrated template database per
# session; each test that needs a clean database clones the template, which takes
# milliseconds and needs no migration run. Evidence tables cannot be emptied (that is
# the point), so a clone per test is the only way to isolate them.


def _admin_connect():
    import psycopg

    return psycopg.connect(TEST_DATABASE_URL, autocommit=True)


def _url_for(name: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def template_database():
    """A database migrated to head, used as the template for per-test clones."""
    from app.storage.db import run_migrations

    name = f"factual_tpl_{uuid.uuid4().hex[:8]}"
    with _admin_connect() as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        run_migrations(_url_for(name))
        yield name
    finally:
        with _admin_connect() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def database_url(template_database):
    """A fresh clone of the migrated template; dropped after the test."""
    name = f"factual_t_{uuid.uuid4().hex[:8]}"
    with _admin_connect() as conn:
        conn.execute(f'CREATE DATABASE "{name}" TEMPLATE "{template_database}"')
    try:
        yield _url_for(name)
    finally:
        with _admin_connect() as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def key_files(tmp_path):
    """A fresh signing key and master key, as the operator would create with `make keys`."""
    from app.ledger.keys import generate_master_key, generate_signing_key

    signing, master = tmp_path / "signing-key.pem", tmp_path / "master.key"
    generate_signing_key(signing)
    generate_master_key(master)
    return signing, master


@pytest.fixture
def db_settings(settings, database_url, key_files):
    """Settings whose DATABASE_URL points at a fresh test database, with real key files."""
    signing, master = key_files
    return settings.model_copy(
        update={
            "database_url": database_url,
            "signing_key_file": signing,
            "master_key_file": master,
        }
    )
