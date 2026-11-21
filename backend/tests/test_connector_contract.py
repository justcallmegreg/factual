"""The contract every connector must honour, replayed from recorded fixtures.

A new connector registers itself in CONNECTORS below with a factory that
builds the connector around its own fixtures; the assertions stay the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.connectors.base import Connector, SourceEvent
from app.connectors.registry import available_connectors


@dataclass
class CollectingSink:
    events: list[SourceEvent] = field(default_factory=list)
    fail_first: int = 0

    async def handle(self, event: SourceEvent) -> None:
        if self.fail_first:
            self.fail_first -= 1
            raise RuntimeError("commit failed")
        self.events.append(event)


@dataclass
class Scene:
    """The fixtures a connector provides for the contract: raw platform events per scene."""

    new_message: dict
    edit: dict
    attachment: dict
    tap: dict
    connector_factory: object  # (batches: list[list[dict]]) -> (Connector, run coroutine)


def telegram_scene() -> Scene:
    from app.connectors.telegram.connector import TelegramConnector, TelegramSettings
    from tests.fake_telegram import FakeTelegramApi, StopPolling
    from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

    def factory(batches):
        api = FakeTelegramApi(batches=batches)
        settings = TelegramSettings(
            bot_token="1:x",
            capture_chat_id=CAPTURE_CHAT_ID,
            ack_reaction="✍",
            bot_username="FactualThothBot",
        )
        conn = TelegramConnector(settings, api, retry_delay=0)

        async def run(sink):
            with pytest.raises(StopPolling):
                await conn.start(sink)
            return api

        return conn, run

    return Scene(
        load("message_text"),
        load("message_edit"),
        load("message_document"),
        load("callback_query"),
        factory,
    )


SCENES = {"telegram": telegram_scene}


def test_every_available_connector_has_a_contract_scene():
    assert set(available_connectors()) == set(SCENES)
    for cls in available_connectors().values():
        assert isinstance(cls.type, str) and cls.capabilities.max_file_bytes > 0


@pytest.fixture(params=sorted(SCENES))
def scene(request) -> Scene:
    return SCENES[request.param]()


async def test_a_new_message_is_a_record_created_event_with_the_raw_payload(scene):
    conn, run = scene.connector_factory([[scene.new_message]])
    assert isinstance(conn, Connector)
    sink = CollectingSink()
    await run(sink)
    (event,) = sink.events
    assert event.kind == "record.created"
    assert event.raw == scene.new_message
    assert event.event_id.startswith(conn.type + ":")
    assert event.normalized is not None and event.normalized.text
    assert event.message is not None and event.actor is not None


async def test_an_edit_is_a_record_edited_event_on_the_same_message(scene):
    conn, run = scene.connector_factory([[scene.new_message, scene.edit]])
    sink = CollectingSink()
    await run(sink)
    created, edited = sink.events
    assert edited.kind == "record.edited"
    assert edited.message == created.message
    assert edited.event_id != created.event_id


async def test_an_attachment_is_listed_as_a_blob_with_size_and_type(scene):
    conn, run = scene.connector_factory([[scene.attachment]])
    sink = CollectingSink()
    await run(sink)
    (event,) = sink.events
    assert event.blobs and event.blobs[0].size and event.blobs[0].mime


async def test_a_duplicate_delivery_yields_the_same_event_id(scene):
    """After a restart the platform delivers unconfirmed updates again; the event id
    depends only on the platform event, so the ledger's source_event guard drops it."""
    first_sink, second_sink = CollectingSink(), CollectingSink()
    _, run_first = scene.connector_factory([[scene.new_message]])
    await run_first(first_sink)
    _, run_again = scene.connector_factory([[scene.new_message]])
    await run_again(second_sink)
    assert first_sink.events[0].event_id == second_sink.events[0].event_id
    assert first_sink.events[0].raw == second_sink.events[0].raw


async def test_a_tap_is_a_choice_event(scene):
    conn, run = scene.connector_factory([[scene.tap]])
    sink = CollectingSink()
    await run(sink)
    (event,) = sink.events
    assert event.kind == "choice" and event.choice is not None and event.choice.data


async def test_ack_follows_the_commit_the_connector_never_acknowledges_on_its_own(scene):
    conn, run = scene.connector_factory([[scene.new_message]])
    sink = CollectingSink()
    api = await run(sink)
    assert not [c for c in api.calls if c[0] == "set_message_reaction"]
    await sink.events[0].ack()
    assert [c for c in api.calls if c[0] == "set_message_reaction"]


async def test_a_failed_commit_means_redelivery(scene):
    conn, run = scene.connector_factory([[scene.new_message], [scene.new_message]])
    sink = CollectingSink(fail_first=1)
    await run(sink)
    assert len(sink.events) == 1
