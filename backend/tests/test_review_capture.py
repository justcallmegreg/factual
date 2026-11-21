"""Fix pass after the whole-branch review: the capture loop.

A failing command, tap or ignored update never blocks the offset; a record that keeps
failing is escalated once; outbound sends tolerate a stale tap, an unchanged edit and
a rate limit; set-up and a group migration do not make Thoth leave its chat; an edit
that changes only the media is recorded; the picker window uses the record's own
time; the daily help window follows DAILY_HELP_TIME; readiness hides driver errors;
database and TSA passwords are redacted; entry numbers are bounded.
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.connectors.base import ChatRef, MessageRef, OutboundMessage
from app.connectors.telegram.api import TelegramApiError
from app.connectors.telegram.mapping import Ignored, map_update
from tests.chat_harness import Chat
from tests.fake_telegram import FakeTelegramApi, RetryAfterError
from tests.fixtures.telegram import CAPTURE_CHAT_ID, OTHER_CHAT_ID, load
from tests.test_telegram_connector import RecordingSink, connector, run

NEW_CHAT_ID = -1009876543210
CAPTURE = ChatRef("telegram", CAPTURE_CHAT_ID)


class FailingSink(RecordingSink):
    """Raises for the event kinds listed, as a deterministic bug would."""

    def __init__(self, fail_kinds: set[str], **kw) -> None:
        super().__init__(**kw)
        self.fail_kinds = fail_kinds

    async def handle(self, event) -> None:
        if event.kind in self.fail_kinds:
            raise RuntimeError("bigint out of range")
        await super().handle(event)


def command_update(text: str, update_id: int, message_id: int = 777) -> dict:
    raw = copy.deepcopy(load("message_text"))
    raw["update_id"] = update_id
    raw["message"]["message_id"] = message_id
    raw["message"]["text"] = text
    raw["message"]["entities"] = [
        {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
    ]
    return raw


def migration_messages() -> tuple[dict, dict]:
    old = copy.deepcopy(load("message_text"))
    old["update_id"] = 600000010
    old["message"].pop("text", None)
    old["message"].pop("entities", None)
    old["message"]["migrate_to_chat_id"] = NEW_CHAT_ID
    new = copy.deepcopy(load("message_text"))
    new["update_id"] = 600000011
    new["message"].pop("text", None)
    new["message"].pop("entities", None)
    new["message"]["chat"] = {"id": NEW_CHAT_ID, "type": "supergroup", "title": "Family"}
    new["message"]["migrate_from_chat_id"] = CAPTURE_CHAT_ID
    return old, new


async def test_a_failing_command_is_answered_and_does_not_block_the_offset():
    cmd = command_update("/verify E99999999999999999999", update_id=553201170)
    api = FakeTelegramApi(batches=[[cmd, load("message_text")]])
    sink = FailingSink({"command"}, api=api)
    await run(connector(api), sink)
    assert [e.kind for e in sink.events] == ["record.created"], "the record behind it is handled"
    assert api.offsets[-1] == 553201178
    notice = api.named("send_message")[-1]
    assert "could not run" in notice["text"] and notice["reply_to_message_id"] == 777


async def test_a_failing_tap_is_answered_and_does_not_block_the_offset():
    api = FakeTelegramApi(batches=[[load("callback_query"), load("message_text")]])
    sink = FailingSink({"choice"}, api=api)
    await run(connector(api), sink)
    assert [e.kind for e in sink.events] == ["record.created"]
    answer = api.named("answer_callback_query")[-1]
    assert "could not handle" in (answer["text"] or "")


async def test_an_ignored_update_whose_reaction_fails_does_not_block_the_offset():
    api = FakeTelegramApi(batches=[[load("message_private"), load("message_text")]])
    api.failures["send_message"] = [TelegramApiError("Forbidden: bot was blocked by the user")]
    sink = RecordingSink(api=api)
    await run(connector(api), sink)
    assert [e.kind for e in sink.events] == ["record.created"]
    assert api.offsets[-1] == 553201178


async def test_a_record_that_keeps_failing_is_escalated_once_and_still_retried(caplog):
    api = FakeTelegramApi(batches=[[load("message_text")] for _ in range(7)])
    sink = RecordingSink(api=api, fail_first=7)
    with caplog.at_level(logging.CRITICAL, logger="factual.telegram"):
        await run(connector(api), sink)
    notices = [m for m in api.named("send_message") if "cannot record" in m["text"]]
    assert len(notices) == 1 and notices[0]["chat_id"] == CAPTURE_CHAT_ID
    assert "RuntimeError" in notices[0]["text"] and "5 attempts" in notices[0]["text"]
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert api.offsets[-1] is None, "evidence is never skipped"


async def test_sending_tolerates_a_stale_tap_and_an_unchanged_edit():
    api = FakeTelegramApi()
    api.failures["answer_callback_query"] = [TelegramApiError("Bad Request: query is too old")]
    api.failures["edit_message_text"] = [TelegramApiError("Bad Request: message is not modified")]
    sent = await connector(api).send(
        CAPTURE, OutboundMessage("same text", edit_of=MessageRef(CAPTURE, 42), answer_choice="q1")
    )
    assert sent.message.message_id == 42


async def test_a_rate_limited_send_waits_and_retries():
    api = FakeTelegramApi()
    api.failures["send_message"] = [RetryAfterError(retry_after=0.01)]
    sent = await connector(api).send(CAPTURE, OutboundMessage("hello"))
    assert sent.message.message_id and api.failures["send_message"] == [], "retried after the wait"
    assert len(api.named("send_message")) == 1, "the fake records a call once it gets through"


async def test_before_configuration_joining_a_group_reports_its_id_and_stays():
    joined = copy.deepcopy(load("my_chat_member_added"))
    joined["my_chat_member"]["chat"]["id"] = CAPTURE_CHAT_ID
    api = FakeTelegramApi(batches=[[joined]])
    await run(connector(api, chat_id=None), RecordingSink(api=api))
    assert api.named("leave_chat") == []
    assert any(f"chat ID is {CAPTURE_CHAT_ID}" in m["text"] for m in api.named("send_message"))


async def test_an_unknown_group_is_left_on_its_first_message_not_on_joining():
    outcome = map_update(load("my_chat_member_added"), capture_chat_id=CAPTURE_CHAT_ID)
    assert isinstance(outcome, Ignored) and outcome.reason == "joined_other_chat"
    api = FakeTelegramApi(batches=[[load("my_chat_member_added")]])
    await run(connector(api), RecordingSink(api=api))
    assert api.named("leave_chat") == []
    api = FakeTelegramApi(batches=[[load("message_other_group")]])
    await run(connector(api), RecordingSink(api=api))
    assert api.named("leave_chat") == [{"chat_id": OTHER_CHAT_ID}]


async def test_a_group_migration_is_reported_in_the_new_chat_and_thoth_stays(caplog):
    old, new = migration_messages()
    api = FakeTelegramApi(batches=[[old, new]])
    with caplog.at_level(logging.CRITICAL, logger="factual.telegram"):
        await run(connector(api), RecordingSink(api=api))
    assert api.named("leave_chat") == []
    notices = [m for m in api.named("send_message") if "moved" in m["text"]]
    assert len(notices) == 1 and notices[0]["chat_id"] == NEW_CHAT_ID
    assert (
        str(NEW_CHAT_ID) in notices[0]["text"] and "TELEGRAM_CAPTURE_CHAT_ID" in notices[0]["text"]
    )
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url, attachments=True)
    await chat.say("/project new family")
    yield chat
    await chat.close()


async def test_an_edit_that_changes_only_the_media_is_recorded_and_the_new_file_fetched(chat):
    raw = load("message_photo_1")
    chat.connector.files["AgACP1y"] = b"\xff\xd8\xff\xe0" + b"old " * 100
    await chat.ingestion.handle(
        map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    while await chat.worker.run_once():
        pass
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    edited = copy.deepcopy(raw)
    edited["update_id"] += 1
    edited["edited_message"] = edited.pop("message")
    edited["edited_message"]["edit_date"] = edited["edited_message"]["date"] + 60
    for size in edited["edited_message"]["photo"]:
        size["file_id"] += "-v2"
        size["file_unique_id"] += "-v2"
    chat.connector.files["AgACP1y-v2"] = b"\xff\xd8\xff\xe0" + b"new " * 100
    await chat.ingestion.handle(
        map_update(edited, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    assert "record.edited" in [e["type"] for e in chat.entries()]
    ids = [
        u
        for (u,) in chat.rows("SELECT file_unique_id FROM attachment WHERE record_seq = %s", (seq,))
    ]
    assert len(ids) == 2 and any(u.endswith("-v2") for u in ids)
    assert chat.rows(
        "SELECT count(*) FROM job WHERE kind = 'attachment.fetch' AND status = 'queued'"
    ) == [(1,)]


async def test_picker_sharing_uses_the_records_own_time(chat):
    now = int(chat.clock().timestamp())
    for text, age in (("two hours ago", 7200), ("one hour ago", 3600)):
        raw = chat._raw_message(text)
        raw["message"]["date"] = now - age
        await chat.ingestion.handle(
            map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
        )
    assert chat.rows("SELECT count(*) FROM picker") == [(2,)], "hours apart: not one picker"


def test_the_daily_help_window_follows_daily_help_time():
    from app.scheduler.daily_help import DailyHelp

    help_ = DailyHelp(None, None, None, timezone="Europe/Amsterdam", at="10:00")
    ams = ZoneInfo("Europe/Amsterdam")
    assert help_.slot_for(datetime(2026, 11, 22, 10, 30, tzinfo=ams)) is not None
    assert help_.slot_for(datetime(2026, 11, 22, 12, 59, tzinfo=ams)) is not None
    assert help_.slot_for(datetime(2026, 11, 22, 13, 30, tzinfo=ams)) is None


def test_readiness_hides_the_driver_error(db_settings, capsys):
    from fastapi.testclient import TestClient

    from app.connectors.null import NullConnector
    from app.main import create_app

    app = create_app(settings=db_settings, connector_factory=lambda _: NullConnector())
    with TestClient(app) as client:

        class Broken:
            async def check(self):
                return 'connection to server at "db.internal" failed: password authentication'

        app.state.db = Broken()
        down = client.get("/readyz")
        assert down.status_code == 503 and down.json()["database"] == "unavailable"
        assert "db.internal" not in down.text
    assert "db.internal" in capsys.readouterr().out, "the detail is in the log"


def test_database_and_tsa_passwords_are_redacted(settings):
    configured = settings.model_copy(
        update={
            "database_url": "postgresql://factual:dbpassw0rd-long@db:5432/factual",
            "qualified_tsa_url": "https://account:qualified-secret@tsa.example/tsr",
        }
    )
    secrets = configured.secret_values()
    assert "dbpassw0rd-long" in secrets and "qualified-secret" in secrets
    assert "account" not in secrets and "factual" not in secrets


async def test_entry_numbers_beyond_the_ledger_are_refused_politely(database_url):
    chat = await Chat.open(database_url)
    try:
        await chat.say("/verify E99999999999999999999")
        assert "No such entry" in chat.replies[-1]
        await chat.say("/proof E99999999999999999999")
        assert "99999999999999999999" in chat.replies[-1] and "not" in chat.replies[-1].lower()
    finally:
        await chat.close()
