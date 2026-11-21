"""App factory and lifespan.

The lifespan starts everything that lives inside the one container: the Telegram
connector, the job workers, the sealer and the scheduler. Each later story adds
its part here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.logging import configure_logging
from app.core.settings import Settings
from app.core.version import read_version
from app.storage.db import Database, run_migrations

DESCRIPTION = (
    "Factual turns what you send to one Telegram chat into tamper-evident evidence "
    "and finds it again when you ask. Its bot, Thoth, records every message in an "
    "append-only, hash-chained ledger that is sealed and timestamped every 5 minutes. "
    "The `/api/v1` routes are read-only and need the bearer token from `API_TOKEN`."
)


def create_app(settings: Settings | None = None, *, start_services: bool = True) -> FastAPI:
    """Build the FastAPI app.

    Settings are read at start-up unless injected. With `start_services` the lifespan
    applies the migrations and opens the database; route tests that need neither pass
    False.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = active = settings if settings is not None else Settings.load()
        configure_logging(active.log_level, app.version, secrets=active.secret_values())
        log = logging.getLogger("factual")
        db: Database | None = None
        if start_services:
            log.info("applying migrations")
            await asyncio.to_thread(run_migrations, active.database_url)
            app.state.db = db = Database(active.database_url)
        log.info("factual backend started")
        yield
        if db is not None:
            await db.dispose()
        log.info("factual backend stopped")

    app = FastAPI(
        title="Factual",
        description=DESCRIPTION,
        version=read_version(),
        lifespan=lifespan,
    )
    app.include_router(health_router)
    return app


app = create_app()
