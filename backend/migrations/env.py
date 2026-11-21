"""Alembic environment: online migrations against the URL the app passes in."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from app.storage.models import metadata

config = context.config
url = config.attributes.get("sqlalchemy_url") or os.environ.get("DATABASE_URL")
if not url:
    raise SystemExit("set DATABASE_URL or run the migrations through app.storage.db")


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=metadata,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
