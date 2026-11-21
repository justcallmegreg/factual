"""FAC-11: the ✍ mark follows the commit, survives rate limits, and covers the backlog."""

from __future__ import annotations

import asyncio
import io
import json
import time
from dataclasses import dataclass, field

import pytest

from app.connectors.base import ChatRef, MessageRef, SourceEvent
from app.connectors.telegram.connector import TelegramConnector, TelegramSettings
from app.core.logging import configure_logging
from tests.fake_telegram import FakeTelegramApi, StopPolling
from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

CHAT = ChatRef("telegram", CAPTURE_CHAT_ID)


def connector(api: FakeTelegramApi) -> TelegramConnector:
    settings = TelegramSettings(
        bot_token="1:x",
        capture_chat_id=CAPTURE_CHAT_ID,
        ack_reaction="✍",
        bot_username="FactualThothBot",
    )
    return TelegramConnector(settings, api, retry_delay=0.02)


@dataclass
class CommittingSink:
    """Like ingestion: 'commit', then call the event's ack."""

    api: FakeTelegramApi
    committed: list[str] = field(default_factory=list)
    marks_before_return: list[int] = field(default_factory=list)

    async def handle(self, event: SourceEvent) -> None:
        self.marks_before_return.append(len(self.api.named("set_message_reaction")))
        self.committed.append(event.event_id)
        await event.ack()


async def test_a_rate_limit_delays_the_mark_but_does_not_lose_it():
    api = FakeTelegramApi(fail_reactions=2, retry_after_seconds=0.03)
    conn = connector(api)
    started = time.perf_counter()
    await conn.acknowledge(MessageRef(CHAT, 8812))
    assert time.perf_counter() - started < 0.5, "acknowledge returns at once; the retry is queued"
    assert conn.acks.pending == 1
    await conn.acks.drain(2)
    reactions = api.named("set_message_reaction")
    assert len(reactions) == 3 and all(r["message_id"] == 8812 for r in reactions)
    assert conn.acks.pending == 0


async def test_a_network_error_is_retried_as_well():
    api = FakeTelegramApi()
    original = api.set_message_reaction
    calls = {"n": 0}

    async def flaky(chat_id, message_id, emoji):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("reset by peer")
        await original(chat_id, message_id, emoji)

    api.set_message_reaction = flaky
    conn = connector(api)
    await conn.acknowledge(MessageRef(CHAT, 8812))
    await conn.acks.drain(2)
    assert calls["n"] == 2 and api.named("set_message_reaction") == [
        {"chat_id": CAPTURE_CHAT_ID, "message_id": 8812, "emoji": "✍"}
    ]


async def test_the_mark_is_given_up_after_repeated_failures_with_an_error_in_the_log():
    api = FakeTelegramApi(fail_reactions=100, retry_after_seconds=0.01)
    conn = connector(api)
    buf = io.StringIO()
    configure_logging("INFO", version="0.1.0", stream=buf)
    try:
        await conn.acknowledge(MessageRef(CHAT, 8812))
        await conn.acks.drain(5)
    finally:
        configure_logging("WARNING", version="0.1.0", stream=io.StringIO())
    assert len(api.named("set_message_reaction")) == conn.acks.max_attempts
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    errors = [line for line in lines if line["log.level"] == "error"]
    assert errors and errors[-1]["factual"]["message_id"] == 8812
    assert errors[-1]["event"]["action"] == "ack.failed"


async def test_the_mark_is_set_only_after_the_commit():
    api = FakeTelegramApi(batches=[[load("message_text"), load("message_reply")]])
    sink = CommittingSink(api)
    with pytest.raises(StopPolling):
        await connector(api).start(sink)
    assert sink.marks_before_return == [0, 1], (
        "no mark before the first commit, one before the second"
    )
    assert [r["message_id"] for r in api.named("set_message_reaction")] == [8812, 8814]


async def test_after_downtime_the_backlog_is_recorded_and_every_message_is_marked_late():
    backlog = [load("message_text"), load("message_reply"), load("message_forward")]
    api = FakeTelegramApi(batches=[backlog])
    sink = CommittingSink(api)
    buf = io.StringIO()
    configure_logging("INFO", version="0.1.0", stream=buf)
    try:
        with pytest.raises(StopPolling):
            await connector(api).start(sink)
    finally:
        configure_logging("WARNING", version="0.1.0", stream=io.StringIO())
    assert len(sink.committed) == 3
    assert [r["message_id"] for r in api.named("set_message_reaction")] == [8812, 8814, 8815]
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    backlog_lines = [line for line in lines if line.get("event", {}).get("action") == "backlog"]
    assert backlog_lines and backlog_lines[0]["factual"]["count"] == 3


async def test_stop_waits_briefly_for_queued_marks():
    api = FakeTelegramApi(
        batches=[], stop_when_empty=False, fail_reactions=1, retry_after_seconds=0.02
    )
    conn = connector(api)
    task = asyncio.create_task(conn.start(CommittingSink(api)))
    await asyncio.sleep(0.02)
    await conn.acknowledge(MessageRef(CHAT, 8812))
    await conn.stop()
    await asyncio.wait_for(task, timeout=2)
    assert len(api.named("set_message_reaction")) == 2
