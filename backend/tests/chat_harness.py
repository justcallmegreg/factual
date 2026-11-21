"""A test harness: ingestion + commands wired to a null connector, driven by fixture events."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import partial

import psycopg

from app.commands.dispatcher import CommandDispatcher
from app.connectors.null import NullConnector
from app.connectors.telegram.mapping import map_update
from app.ledger.ledger import Ledger
from app.records.ingest import Ingestion
from app.storage.db import Database
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load


class FakeClock:
    """A clock the tests move by hand; message dates follow it."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@dataclass
class Chat:
    database_url: str
    db: Database
    ledger: Ledger
    connector: NullConnector
    ingestion: Ingestion
    commands: CommandDispatcher
    clock: FakeClock
    store: object = None
    master_key: bytes = b""
    worker: object = None
    signing_key: object = None
    sealer: object = None
    extractor: object = None
    anchoring: object = None
    next_update: int = field(default=600000000)
    next_message: int = field(default=20000)

    @classmethod
    async def open(
        cls,
        database_url: str,
        *,
        timezone: str = "Europe/Amsterdam",
        attachments: bool = False,
        seals: bool = False,
        tsa_urls: list[str] | None = None,
        signing_key=None,
        extractor=None,
        **options,
    ) -> Chat:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        from app.ledger.keys import public_key_bytes
        from app.ledger.seals import Sealer
        from app.ledger.verify import Verifier
        from app.records.attachments import AttachmentFetcher, AttachmentIntake
        from app.records.filing import Filing
        from app.storage.blobs import InMemoryObjectStore
        from app.storage.jobs import JobWorker

        clock = FakeClock(datetime(2026, 10, 29, 13, 3, 12, tzinfo=UTC))
        db = Database(database_url)
        signing_key = signing_key or Ed25519PrivateKey.generate()
        ledger = await Ledger.open(db, public_key=public_key_bytes(signing_key))
        connector = NullConnector()
        verifier = Verifier(ledger.genesis, public_key_bytes(signing_key))
        sealer = Sealer(ledger, db, signing_key, interval=300, clock=clock) if seals else None
        filing = Filing(ledger, db, connector, timezone=timezone, clock=clock)
        commands = CommandDispatcher(
            ledger,
            db,
            connector,
            timezone=timezone,
            filing=filing,
            clock=clock,
            verifier=verifier,
            signing_key=signing_key,
            **options,
        )
        store = master_key = worker = intake = None
        handlers: dict = {}
        if attachments:
            store = InMemoryObjectStore()
            master_key = os.urandom(32)
            intake = AttachmentIntake(max_bytes=20 * 1024 * 1024, clock=clock)
            handlers[AttachmentFetcher.KIND] = AttachmentFetcher(
                ledger,
                db,
                connector,
                store,
                master_key,
                lock_days=3650,
                clock=clock,
                timezone=timezone,
                extract=extractor is not None,
            )
            if extractor is not None:
                from app.ai.extract import ExtractionJob

                handlers[ExtractionJob.KIND] = ExtractionJob(
                    db, store, master_key, extractor, clock=clock
                )
        if sealer is not None and tsa_urls:
            import httpx

            from app.connectors.base import ChatRef
            from app.ledger.anchors import ANCHOR_JOB, SealAnchoring
            from app.ledger.tsa import TimestampClient

            anchoring = SealAnchoring(
                db,
                connector,
                TimestampClient(httpx.AsyncClient()),
                urls=tsa_urls,
                capture_chat=ChatRef("telegram", CAPTURE_CHAT_ID),
                clock=clock,
            )
            sealer.after_seal.append(anchoring.after_seal)
            handlers[ANCHOR_JOB] = anchoring
            options["_anchoring"] = anchoring
        if handlers:
            worker = JobWorker(db, handlers, clock=clock, poll_interval=0.01)
        from app.ledger.keys import public_key_pem
        from app.ledger.pack import PackBuilder

        commands.options["pack_builder"] = PackBuilder(
            db,
            ledger,
            public_key_pem=public_key_pem(signing_key),
            store=store,
            master_key=master_key,
            timezone=timezone,
        )
        ingestion = Ingestion(
            ledger,
            db,
            timezone=timezone,
            commands=commands,
            filing=filing,
            attachments=intake,
            connector=connector,
        )
        chat = cls(
            database_url,
            db,
            ledger,
            connector,
            ingestion,
            commands,
            clock,
            store,
            master_key or b"",
            worker,
            signing_key,
            sealer,
        )
        chat.anchoring = options.pop("_anchoring", None)
        return chat

    async def close(self) -> None:
        await self.db.dispose()

    def _raw_message(
        self, text: str, *, user: dict | None = None, reply_to: int | None = None
    ) -> dict:
        raw = copy.deepcopy(load("message_text"))
        self.next_update += 1
        self.next_message += 1
        raw["update_id"] = self.next_update
        msg = raw["message"]
        msg["message_id"] = self.next_message
        msg["text"] = text
        msg["date"] = int(self.clock().timestamp())
        if user:
            msg["from"] = user
        if reply_to is not None:
            msg["reply_to_message"] = {
                "message_id": reply_to,
                "chat": msg["chat"],
                "date": msg["date"] - 60,
                "text": "…",
            }
        if text.startswith("/"):
            first = text.split()[0]
            msg["entities"] = [{"offset": 0, "length": len(first), "type": "bot_command"}]
        else:
            msg.pop("entities", None)
        return raw

    async def say(self, text: str, *, user: dict | None = None, reply_to: int | None = None):
        """Send a message or a command from a member; returns the message id."""
        raw = self._raw_message(text, user=user, reply_to=reply_to)
        event = map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
        # like the Telegram connector: the ✍ callback is the connector's acknowledge
        event = replace(event, ack=partial(self.connector.acknowledge, event.message))
        await self.ingestion.handle(event)
        return raw["message"]["message_id"]

    def tap_raw(self, data: str, *, on_message: int, user: dict | None = None) -> dict:
        raw = copy.deepcopy(load("callback_query"))
        self.next_update += 1
        raw["update_id"] = self.next_update
        raw["callback_query"]["id"] = f"q{self.next_update}"
        raw["callback_query"]["data"] = data
        raw["callback_query"]["message"]["message_id"] = on_message
        if user:
            raw["callback_query"]["from"] = user
        return raw

    async def tap(self, data: str, *, on_message: int, user: dict | None = None) -> None:
        raw = self.tap_raw(data, on_message=on_message, user=user)
        event = map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
        await self.ingestion.handle(event)

    @property
    def last_message_id(self) -> int:
        """The message id of the last message Thoth sent or edited."""
        _, msg = self.connector.sent[-1]
        if msg.edit_of is not None:
            return msg.edit_of.message_id
        return self.connector.message_ids[-1]

    def button(self, label: str, *, message_index: int = -1):
        """The button with this label on Thoth's n-th message (default: the last)."""
        _, msg = self.connector.sent[message_index]
        for row in msg.buttons:
            for button in row:
                if button.label == label:
                    return button
        raise LookupError(f"no button {label!r} on {msg.text!r}")

    def audit(self, **kw):
        """A deep audit over this chat's ledger, store and anchors."""
        from app.connectors.base import ChatRef
        from app.ledger.audit import DeepAudit
        from app.ledger.keys import public_key_bytes

        return DeepAudit(
            self.ledger,
            self.db,
            self.store,
            self.master_key or None,
            self.connector,
            ChatRef("telegram", CAPTURE_CHAT_ID),
            public_key=public_key_bytes(self.signing_key),
            clock=self.clock,
            anchoring=self.anchoring,
            **kw,
        )

    @property
    def public_key_pem(self) -> bytes:
        from app.ledger.keys import public_key_pem

        return public_key_pem(self.signing_key)

    @property
    def replies(self) -> list[str]:
        return [msg.text for _, msg in self.connector.sent]

    @property
    def last(self):
        return self.connector.sent[-1][1]

    def rows(self, sql: str, params=()) -> list[tuple]:
        with psycopg.connect(self.database_url) as conn:
            return conn.execute(sql, params).fetchall()

    def entries(self) -> list[dict]:
        return [
            json.loads(bytes(row[0]))
            for row in self.rows("SELECT entry FROM ledger_entry ORDER BY seq")
        ]

    def payload(self, seq: int) -> dict:
        ((payload_bytes,),) = self.rows(
            "SELECT payload FROM evidence_payload WHERE seq = %s", (seq,)
        )
        return json.loads(bytes(payload_bytes))
