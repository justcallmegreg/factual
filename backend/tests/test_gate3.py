"""Gate 3, simulated: thirty nightly audits in a row pass over a living ledger.

Thirty real nights cannot pass inside one session. This runs the nightly job itself
across thirty simulated nights (a fake clock, a note and a seal with timestamps each
day) and checks that every audit passed, was recorded once per night and raised no
notice. The restore drill, the gate's other half, is in test_backup_restore.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.connectors.telegram.mapping import map_update
from app.ledger.audit import NightlyAudit
from tests.chat_harness import Chat
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load
from tests.tsa_server import LocalTsa

AMS = ZoneInfo("Europe/Amsterdam")
NIGHTS = 30


@pytest.fixture(scope="module")
def tsa():
    server = LocalTsa()
    yield server
    server.close()


async def drain(chat: Chat) -> None:
    while await chat.worker.run_once():
        pass


async def test_thirty_nightly_audits_in_a_row_pass(database_url, tsa):
    chat = await Chat.open(
        database_url, attachments=True, seals=True, tsa_urls=[tsa.url("tsa1"), tsa.url("tsa2")]
    )
    try:
        chat.connector.files["BQACDOC1"] = b"%PDF-1.7\n" + b"one " * 300
        chat.connector.files["AgACP1y"] = b"\xff\xd8\xff\xe0" + b"two " * 300
        await chat.say("/project new client-acme")
        await chat.say("Greg called about the kickoff")
        for name in ("message_document", "message_photo_1"):
            await chat.ingestion.handle(
                map_update(
                    load(name), capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot"
                )
            )
        await drain(chat)
        nightly = NightlyAudit(
            chat.audit(rehash_per_second=0), chat.db, timezone="Europe/Amsterdam", clock=chat.clock
        )
        first_night = datetime(2026, 11, 22, 3, 30, tzinfo=AMS)
        for night in range(NIGHTS):
            chat.clock.now = (first_night + timedelta(days=night)).astimezone(UTC)
            await chat.say(f"note of night {night + 1}")
            await chat.sealer.seal_once()
            await drain(chat)
            assert await nightly.tick() is True, f"night {night + 1} audited"
            assert await nightly.tick() is False, "once per night"
        audits = chat.rows(
            "SELECT convert_from(p.payload, 'UTF8')::jsonb FROM ledger_entry e"
            " JOIN evidence_payload p ON p.seq = e.seq WHERE e.type = 'audit.completed'"
            " ORDER BY e.seq"
        )
        assert len(audits) == NIGHTS
        assert all(payload["ok"] is True and payload["failures"] == [] for (payload,) in audits)
        assert audits[-1][0]["seals"] == NIGHTS and audits[-1][0]["blobs"] == 2  # one seal a night
        assert chat.rows(
            "SELECT count(*) FROM schedule_run WHERE job = 'nightly_audit' AND status = 'passed'"
        ) == [(NIGHTS,)]
        assert not [m for _, m in chat.connector.sent if "audit" in m.text.lower()], "no alarm"
        await chat.say("/status")
        assert "Last audit: passed" in chat.replies[-1]
    finally:
        await chat.close()
