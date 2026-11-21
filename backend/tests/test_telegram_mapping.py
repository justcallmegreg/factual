"""FAC-09: raw Telegram updates become platform-neutral events (or are ignored)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.connectors.telegram.mapping import Ignored, map_update
from tests.fixtures.telegram import CAPTURE_CHAT_ID, OTHER_CHAT_ID, load


def mapped(name: str):
    return map_update(load(name), capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")


def test_a_text_message_becomes_a_record_created_event():
    event = mapped("message_text")
    assert event.kind == "record.created"
    assert event.event_id == "telegram:update:553201177"
    assert event.chat.chat_id == CAPTURE_CHAT_ID and event.chat.kind == "group"
    assert event.message.message_id == 8812
    assert event.actor.user_id == 51234567 and event.actor.display_name == "Anna K."
    assert event.actor.username == "annak"
    assert event.occurred_at == datetime(2026, 10, 29, 13, 3, 12, tzinfo=UTC)
    assert event.raw == load("message_text")  # exactly as received
    assert "Greg called." in event.normalized.text
    assert event.blobs == [] and event.command is None and event.choice is None


def test_an_edit_becomes_a_record_edited_event_for_the_same_message():
    event = mapped("message_edit")
    assert event.kind == "record.edited"
    assert event.message.message_id == 8812
    assert event.normalized.edited_at == datetime(2026, 10, 29, 13, 4, 47, tzinfo=UTC)
    assert event.occurred_at == event.normalized.edited_at
    assert "tomorrow" in event.normalized.text


def test_a_reply_references_the_replied_message():
    event = mapped("message_reply")
    assert event.normalized.reply_to.message_id == 8812
    assert event.normalized.reply_to.chat.chat_id == CAPTURE_CHAT_ID


def test_a_forwarded_message_keeps_its_origin():
    event = mapped("message_forward")
    origin = event.normalized.forwarded_from
    assert (
        origin.kind == "user" and origin.sender_name == "Greg S." and origin.sender_id == 51234568
    )
    assert origin.sent_at == datetime(2026, 10, 29, 12, 40, tzinfo=UTC)


def test_photos_list_the_largest_size_as_one_blob_with_the_album_id():
    event = mapped("message_photo_1")
    assert event.normalized.album_id == "13553201182"
    assert event.normalized.caption == "The letter, page 1 and 2"
    (blob,) = event.blobs
    assert blob.kind == "photo" and blob.file_id == "AgACP1y" and blob.size == 102400
    assert event.normalized.media_kinds == ("photo",)


def test_a_document_and_a_voice_note_carry_name_type_and_size():
    doc = mapped("message_document").blobs[0]
    assert (doc.kind, doc.file_name, doc.mime, doc.size) == (
        "document",
        "contract-v2.pdf",
        "application/pdf",
        348112,
    )
    voice = mapped("message_voice").blobs[0]
    assert (voice.kind, voice.mime, voice.size) == ("voice", "audio/ogg", 58211)


def test_a_location_is_a_record_without_blobs():
    event = mapped("message_location")
    assert event.kind == "record.created" and event.blobs == []
    assert event.normalized.media_kinds == ("location",)


def test_a_bot_command_becomes_a_command_event():
    event = mapped("command_log_pending")
    assert event.kind == "command"
    assert event.command.name == "log" and event.command.args == "pending"
    assert event.message.message_id == 8821


def test_a_command_addressed_to_the_bot_by_name_is_still_a_command():
    event = mapped("command_with_mention")
    assert event.kind == "command" and event.command.name == "help" and event.command.args == ""


def test_a_command_addressed_to_another_bot_is_ignored():
    outcome = map_update(
        load("command_with_mention"), capture_chat_id=CAPTURE_CHAT_ID, bot_username="OtherBot"
    )
    assert isinstance(outcome, Ignored) and outcome.reason == "other_bot_command"


def test_a_button_tap_becomes_a_choice_event():
    event = mapped("callback_query")
    assert event.kind == "choice"
    assert event.event_id == "telegram:update:553201189"
    assert event.choice.data == "f:1:client-acme" and event.choice.query_id == "4382bfdc1d4a3ad7"
    assert event.choice.message.message_id == 8813
    assert event.actor.user_id == 51234567
    assert event.chat.chat_id == CAPTURE_CHAT_ID


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("message_private", "private"),
        ("message_other_group", "other_chat"),
        ("my_chat_member_added", "joined_other_chat"),
        ("message_from_bot", "from_bot"),
        ("message_service", "service"),
    ],
)
def test_updates_that_are_not_records_are_ignored_with_a_reason(name, reason):
    outcome = mapped(name)
    assert isinstance(outcome, Ignored)
    assert outcome.reason == reason


def test_an_ignored_update_still_tells_where_it_came_from():
    private = mapped("message_private")
    assert private.chat.kind == "private" and private.chat.chat_id == 51234567
    other = mapped("message_other_group")
    assert other.chat.chat_id == OTHER_CHAT_ID and other.chat.kind == "group"


def test_without_a_capture_chat_every_group_message_is_ignored_as_unconfigured():
    outcome = map_update(load("message_text"), capture_chat_id=None, bot_username="FactualThothBot")
    assert isinstance(outcome, Ignored) and outcome.reason == "unconfigured"
    assert outcome.chat.chat_id == CAPTURE_CHAT_ID
