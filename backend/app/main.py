"""App factory and lifespan.

The lifespan starts everything that lives inside the one container: the Telegram
connector, the job workers, the sealer and the scheduler. Each later story adds
its part here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.commands.dispatcher import CommandDispatcher
from app.commands.menu import COMMANDS
from app.connectors.base import Connector
from app.connectors.telegram.api import AiogramTelegramApi
from app.connectors.telegram.connector import TelegramConnector, TelegramSettings
from app.core.logging import configure_logging
from app.core.settings import Settings
from app.core.version import read_version
from app.ledger.keys import load_signing_key, public_key_bytes
from app.ledger.ledger import Ledger
from app.records.attachments import AttachmentFetcher, AttachmentIntake
from app.records.filing import Filing
from app.records.ingest import Ingestion
from app.storage.blobs import S3ObjectStore
from app.storage.db import Database, run_migrations
from app.storage.jobs import JobWorker

DESCRIPTION = (
    "Factual turns what you send to one Telegram chat into tamper-evident evidence "
    "and finds it again when you ask. Its bot, Thoth, records every message in an "
    "append-only, hash-chained ledger that is sealed and timestamped every 5 minutes. "
    "The `/api/v1` routes are read-only and need the bearer token from `API_TOKEN`."
)


def build_telegram_connector(settings: Settings) -> TelegramConnector:
    api = AiogramTelegramApi(settings.telegram_bot_token)
    return TelegramConnector(
        TelegramSettings(
            bot_token=settings.telegram_bot_token,
            capture_chat_id=settings.telegram_capture_chat_id,
            ack_reaction=settings.ack_reaction,
        ),
        api,
    )


def create_app(
    settings: Settings | None = None,
    *,
    start_services: bool = True,
    connector_factory: Callable[[Settings], Connector] | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Settings are read at start-up unless injected. With `start_services` the lifespan
    applies the migrations, opens the database and starts the connector (Telegram by
    default; tests pass their own factory); route tests that need none of it pass False.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = active = settings if settings is not None else Settings.load()
        configure_logging(active.log_level, app.version, secrets=active.secret_values())
        log = logging.getLogger("factual")
        db: Database | None = None
        connector: Connector | None = None
        polling: asyncio.Task[None] | None = None
        jobs: JobWorker | None = None
        job_task: asyncio.Task[None] | None = None
        if start_services:
            log.info("applying migrations")
            await asyncio.to_thread(run_migrations, active.database_url)
            app.state.db = db = Database(active.database_url)
            signing_key = load_signing_key(active.signing_key_file)
            app.state.ledger = await Ledger.open(db, public_key=public_key_bytes(signing_key))
            factory = connector_factory or build_telegram_connector
            app.state.connector = connector = factory(active)
            if hasattr(connector, "commands_menu"):
                connector.commands_menu = list(COMMANDS)
            filing = Filing(app.state.ledger, db, connector, timezone=active.timezone)
            commands = CommandDispatcher(
                app.state.ledger, db, connector, timezone=active.timezone, filing=filing
            )
            master_key = active.master_key_file.read_bytes()
            if len(master_key) != 32:
                raise RuntimeError(
                    "MASTER_KEY_FILE must hold 32 bytes; create it with `python -m app keys`"
                )
            store = S3ObjectStore(
                endpoint=active.s3_endpoint,
                bucket=active.s3_bucket,
                access_key_id=active.s3_access_key_id,
                secret_access_key=active.s3_secret_access_key,
                lock_days=active.s3_object_lock_days,
            )
            try:
                await store.ensure_bucket()
            except Exception as exc:  # noqa: BLE001 — capture must go on; files retry later
                log.error("object storage not ready: %s", type(exc).__name__)
            app.state.store = store
            fetcher = AttachmentFetcher(
                app.state.ledger,
                db,
                connector,
                store,
                master_key,
                lock_days=active.s3_object_lock_days,
                max_bytes=connector.capabilities.max_file_bytes,
                timezone=active.timezone,
            )
            intake = AttachmentIntake(max_bytes=connector.capabilities.max_file_bytes)
            app.state.sink = sink = Ingestion(
                app.state.ledger,
                db,
                timezone=active.timezone,
                commands=commands,
                filing=filing,
                attachments=intake,
                connector=connector,
            )
            app.state.jobs = jobs = JobWorker(db, {AttachmentFetcher.KIND: fetcher})
            job_task = asyncio.create_task(jobs.start(), name="jobs")
            polling = asyncio.create_task(connector.start(sink), name="connector")
            log.info("connector started", extra={"factual.connector": connector.type})
        log.info("factual backend started")
        yield
        if jobs is not None and job_task is not None:
            await jobs.stop()
            job_task.cancel()
            try:
                await job_task
            except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001 — shutting down
                log.debug("job worker ended: %s", type(exc).__name__)
        if connector is not None and polling is not None:
            await connector.stop()
            polling.cancel()
            try:
                await polling
            except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001 — shutting down
                log.debug("connector task ended: %s", type(exc).__name__)
            close = getattr(getattr(connector, "api", None), "close", None)
            if close is not None:
                await close()
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
