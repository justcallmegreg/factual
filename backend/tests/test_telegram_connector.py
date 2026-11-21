"""FAC-09: Thoth polls, serves one chat, and advances its offset only after the commit."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from app.connectors.base import Button, ChatRef, Connector, MessageRef, OutboundMessage, SourceEvent
from app.connectors.telegram.connector import TelegramConnector, TelegramSettings
from tests.fake_telegram import FakeTelegramApi, StopPolling
from tests.fixtures.telegram import CAPTURE_CHAT_ID, OTHER_CHAT_ID, load


@dataclass
class RecordingSink:
    events: list[SourceEvent] = field(default_factory=list)
    fail_first: int = 0
    seen_offsets_at_handle: list[list[int | None]] = field(default_factory=list)
    api: FakeTelegramApi | None = None

    async def handle(self, event: SourceEvent) -> None:
        if self.api is not None:
            self.seen_offsets_at_handle.append(list(self.api.offsets))
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("ledger unavailable")
        self.events.append(event)


def connector(api: FakeTelegramApi, chat_id: int | None = CAPTURE_CHAT_ID) -> TelegramConnector:
    settings = TelegramSettings(
        bot_token="123456:TEST-TOKEN",
        capture_chat_id=chat_id,
        ack_reaction="✍",
        bot_username="FactualThothBot",
    )
    return TelegramConnector(settings, api, retry_delay=0)


async def run(conn: TelegramConnector, sink) -> None:
    with pytest.raises(StopPolling):
        await conn.start(sink)


def test_the_telegram_connector_satisfies_the_protocol_and_declares_its_capabilities():
    conn = connector(FakeTelegramApi())
    assert isinstance(conn, Connector)
    assert TelegramConnector.type == "telegram"
    caps = TelegramConnector.capabilities
    assert caps.edits_reported and not caps.deletions_reported
    assert caps.acknowledgement == "reaction" and not caps.public_endpoint_needed
    assert caps.max_file_bytes == 20 * 1024 * 1024


async def test_messages_in_the_capture_chat_reach_the_sink_and_the_offset_advances_after_each():
    api = FakeTelegramApi(batches=[[load("message_text"), load("message_reply")]])
    sink = RecordingSink(api=api)
    await run(connector(api), sink)
    assert [e.event_id for e in sink.events] == [
        "telegram:update:553201177",
        "telegram:update:553201180",
    ]
    # first poll without offset; the next poll asks for the update after the last handled one
    assert api.offsets == [None, 553201181]
    # while the first event was being handled, no newer offset had been requested yet
    assert sink.seen_offsets_at_handle[0] == [None]


async def test_the_offset_is_not_advanced_when_the_sink_fails_so_the_update_is_delivered_again():
    api = FakeTelegramApi(batches=[[load("message_text")], [load("message_text")]])
    sink = RecordingSink(api=api, fail_first=1)
    await run(connector(api), sink)
    assert [e.event_id for e in sink.events] == ["telegram:update:553201177"]
    assert api.offsets == [None, None, 553201178]


async def test_allowed_updates_cover_messages_edits_taps_and_membership():
    api = FakeTelegramApi(batches=[[load("message_text")]])
    conn = connector(api)
    await run(conn, RecordingSink())
    assert set(conn.allowed_updates) == {
        "message",
        "edited_message",
        "callback_query",
        "my_chat_member",
    }


async def test_while_the_chat_id_is_unset_thoth_records_nothing_and_reports_the_group_id():
    api = FakeTelegramApi(batches=[[load("message_text"), load("message_reply")]])
    sink = RecordingSink()
    await run(connector(api, chat_id=None), sink)
    assert sink.events == []
    sent = api.named("send_message")
    assert len(sent) == 1, "one reply per group, not one per message"
    assert sent[0]["chat_id"] == CAPTURE_CHAT_ID
    assert str(CAPTURE_CHAT_ID) in sent[0]["text"] and "TELEGRAM_CAPTURE_CHAT_ID" in sent[0]["text"]
    assert api.offsets[-1] == 553201181


async def test_thoth_leaves_other_groups_and_records_nothing_from_them():
    api = FakeTelegramApi(batches=[[load("message_other_group"), load("my_chat_member_added")]])
    sink = RecordingSink()
    await run(connector(api), sink)
    assert sink.events == []
    assert api.named("leave_chat") == [{"chat_id": OTHER_CHAT_ID}]  # once, not per update
    assert api.named("send_message") == []


async def test_a_private_message_gets_one_fixed_line():
    api = FakeTelegramApi(batches=[[load("message_private")]])
    sink = RecordingSink()
    await run(connector(api), sink)
    assert sink.events == []
    (reply,) = api.named("send_message")
    assert reply["chat_id"] == 51234567
    assert reply["text"] == "Thoth works only in its capture chat."


async def test_service_messages_and_the_bots_own_messages_are_skipped():
    api = FakeTelegramApi(batches=[[load("message_from_bot"), load("message_service")]])
    sink = RecordingSink()
    await run(connector(api), sink)
    assert sink.events == [] and api.calls == []
    assert api.offsets[-1] == 553201195


async def test_edits_commands_and_taps_reach_the_sink():
    api = FakeTelegramApi(
        batches=[[load("message_edit"), load("command_log_pending"), load("callback_query")]]
    )
    sink = RecordingSink()
    await run(connector(api), sink)
    assert [e.kind for e in sink.events] == ["record.edited", "command", "choice"]


async def test_acknowledge_sets_the_configured_reaction():
    api = FakeTelegramApi()
    conn = connector(api)
    chat = ChatRef("telegram", CAPTURE_CHAT_ID)
    await conn.acknowledge(MessageRef(chat, 8812))
    assert api.named("set_message_reaction") == [
        {"chat_id": CAPTURE_CHAT_ID, "message_id": 8812, "emoji": "✍"}
    ]


async def test_the_events_ack_is_the_connectors_acknowledge_and_is_never_called_by_the_connector():
    api = FakeTelegramApi(batches=[[load("message_text")]])
    sink = RecordingSink()
    await run(connector(api), sink)
    assert api.named("set_message_reaction") == []
    await sink.events[0].ack()
    assert api.named("set_message_reaction") == [
        {"chat_id": CAPTURE_CHAT_ID, "message_id": 8812, "emoji": "✍"}
    ]


async def test_send_posts_a_reply_with_buttons_and_returns_its_reference():
    api = FakeTelegramApi()
    conn = connector(api)
    chat = ChatRef("telegram", CAPTURE_CHAT_ID)
    sent = await conn.send(
        chat,
        OutboundMessage(
            text="Which project?",
            reply_to=MessageRef(chat, 8812),
            buttons=((Button("family", "f:1:family"), Button("More", "more:1")),),
        ),
    )
    assert sent.message.chat == chat and sent.message.message_id == 9001
    (call,) = api.named("send_message")
    assert call["reply_to_message_id"] == 8812
    assert call["buttons"] == [[("family", "f:1:family"), ("More", "more:1")]]


async def test_send_can_edit_an_earlier_message_and_answer_a_tap():
    api = FakeTelegramApi()
    conn = connector(api)
    chat = ChatRef("telegram", CAPTURE_CHAT_ID)
    sent = await conn.send(
        chat,
        OutboundMessage(
            text="E1 · family", edit_of=MessageRef(chat, 8813), answer_choice="4382bfdc1d4a3ad7"
        ),
    )
    assert sent.message.message_id == 8813
    assert api.named("edit_message_text")[0]["message_id"] == 8813
    assert api.named("answer_callback_query") == [
        {"callback_query_id": "4382bfdc1d4a3ad7", "text": None}
    ]


async def test_fetch_blob_streams_the_file_through_get_file():
    api = FakeTelegramApi()
    conn = connector(api)
    from app.connectors.base import BlobRef

    chunks = [chunk async for chunk in conn.fetch_blob(BlobRef("BQACDOC1", "DOC1", "document"))]
    assert chunks == [b"abc"]
    assert api.named("get_file") == [{"file_id": "BQACDOC1"}]


async def test_stop_ends_the_polling_loop():
    api = FakeTelegramApi(batches=[], stop_when_empty=False)
    conn = connector(api)
    sink = RecordingSink()
    task = asyncio.create_task(conn.start(sink))
    await asyncio.sleep(0.05)
    await conn.stop()
    await asyncio.wait_for(task, timeout=2)
    assert sink.events == []


async def test_a_telegram_api_error_is_retried_with_a_delay_not_a_crash():
    api = FakeTelegramApi(batches=[[load("message_text")]])

    calls = {"n": 0}
    real = api.get_updates

    async def flaky(offset, poll_timeout, allowed_updates):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("network down")
        return await real(offset, poll_timeout, allowed_updates)

    api.get_updates = flaky
    sink = RecordingSink()
    await run(connector(api), sink)
    assert len(sink.events) == 1
