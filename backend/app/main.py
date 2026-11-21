"""App factory and lifespan.

The lifespan starts everything that lives inside the one container: the Telegram
connector, the job workers, the sealer and the scheduler. Each later story adds
its part here.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.logging import configure_logging
from app.core.settings import Settings
from app.core.version import read_version

DESCRIPTION = (
    "Factual turns what you send to one Telegram chat into tamper-evident evidence "
    "and finds it again when you ask. Its bot, Thoth, records every message in an "
    "append-only, hash-chained ledger that is sealed and timestamped every 5 minutes. "
    "The `/api/v1` routes are read-only and need the bearer token from `API_TOKEN`."
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI app. Settings are read at start-up unless injected."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = active = settings if settings is not None else Settings.load()
        configure_logging(active.log_level, app.version, secrets=active.secret_values())
        logging.getLogger("factual").info("factual backend started")
        yield
        logging.getLogger("factual").info("factual backend stopped")

    app = FastAPI(
        title="Factual",
        description=DESCRIPTION,
        version=read_version(),
        lifespan=lifespan,
    )
    app.include_router(health_router)
    return app


app = create_app()
