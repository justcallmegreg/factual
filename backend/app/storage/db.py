"""PostgreSQL access: the URL, the migrations at start-up and the runtime engine."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.storage.grants import RUNTIME_ROLE

BACKEND_DIR = Path(__file__).resolve().parents[2]


def sqlalchemy_url(database_url: str) -> str:
    """`postgresql://…` as operators write it → `postgresql+psycopg://…` for SQLAlchemy."""
    return (
        make_url(database_url)
        .set(drivername="postgresql+psycopg")
        .render_as_string(hide_password=False)
    )


def run_migrations(database_url: str) -> None:
    """Apply every Alembic migration up to head, as the owner role. Idempotent."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["sqlalchemy_url"] = sqlalchemy_url(database_url)
    command.upgrade(config, "head")


class Database:
    """The application's connection pool, every connection set to the runtime role."""

    def __init__(
        self,
        database_url: str,
        *,
        role: str | None = RUNTIME_ROLE,
        connect_timeout: int = 5,
    ) -> None:
        connect_args: dict[str, object] = {"connect_timeout": connect_timeout}
        if role:
            connect_args["options"] = f"-c role={role}"
        self.timeout = connect_timeout
        self.engine: AsyncEngine = create_async_engine(
            sqlalchemy_url(database_url), connect_args=connect_args, pool_pre_ping=True
        )

    async def check(self) -> str | None:
        """None when a round trip works, else a short description of the problem."""
        try:
            async with asyncio.timeout(self.timeout + 1), self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 — any failure is "not ready"
            return f"{type(exc).__name__}: {exc}".splitlines()[0][:200]
        return None

    async def dispose(self) -> None:
        await self.engine.dispose()
