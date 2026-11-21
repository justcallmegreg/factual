"""FAC-15: /verify, /status and /help from the chat."""

from __future__ import annotations

import psycopg
import pytest

from app.commands.menu import COMMANDS, help_text
from tests.chat_harness import Chat

GREG = {"id": 51234568, "is_bot": False, "first_name": "Greg", "last_name": "S."}


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url)
    for name in ("family", "client-acme"):
        await chat.say(f"/project new {name}")
    yield chat
    await chat.close()


async def filed_note(chat: Chat, text: str = "Greg called about the kickoff") -> tuple[int, int]:
    message_id = await chat.say(text)
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    picker = chat.last_message_id
    await chat.tap(chat.button("client-acme").data, on_message=picker)
    chat.clock.advance(120)
    await chat.tap(chat.button("Change project").data, on_message=picker)
    await chat.tap(chat.button("family").data, on_message=picker)
    chat.clock.advance(30)
    return message_id, seq


async def test_verify_as_a_reply_shows_the_hash_check_and_the_filing_history(chat):
    message_id, seq = await filed_note(chat)
    await chat.say("/verify", user=GREG, reply_to=message_id)
    reply = chat.replies[-1]
    lines = reply.splitlines()
    assert lines[0] == f"E{seq} · family · Note by Anna K."
    assert lines[1] == "Recorded Thu 29 Oct 2026, 14:03 CET · Pending seal"
    assert "Checks: entry hash ok · payload commitment ok · chain link ok" in reply
    assert "Filing history:" in reply
    history = [line for line in lines if line.startswith("• ")]
    assert len(history) == 2
    assert "client-acme" in history[0] and "by Anna K." in history[0] and "14:03 CET" in history[0]
    assert "client-acme → family" in history[1] and "14:05 CET" in history[1]


async def test_verify_with_a_number_works_with_and_without_the_e(chat):
    _, seq = await filed_note(chat)
    await chat.say(f"/verify E{seq}")
    first = chat.replies[-1]
    await chat.say(f"/verify {seq}")
    assert chat.replies[-1] == first
    assert first.startswith(f"E{seq} ·")


async def test_verify_shows_the_versions_of_an_edited_record(chat):
    note = await chat.say("First wording")
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    raw = chat._raw_message("Second wording")
    raw["message"]["message_id"] = note
    raw["edited_message"] = raw.pop("message")
    raw["edited_message"]["edit_date"] = raw["edited_message"]["date"] + 60
    from app.connectors.telegram.mapping import map_update
    from tests.fixtures.telegram import CAPTURE_CHAT_ID

    await chat.ingestion.handle(
        map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    await chat.say(f"/verify {seq}")
    reply = chat.replies[-1]
    assert "Versions: 2" in reply and "edited Thu 29 Oct 2026, 14:04 CET" in reply
    assert "Filing history: no project yet" in reply


async def test_verify_detects_a_tampered_payload(chat):
    _, seq = await filed_note(chat)
    with psycopg.connect(chat.database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE evidence_payload DISABLE TRIGGER ALL")
        conn.execute("UPDATE evidence_payload SET payload = %s WHERE seq = %s", (b'{"x":1}', seq))
        conn.execute("ALTER TABLE evidence_payload ENABLE TRIGGER ALL")
    await chat.say(f"/verify {seq}")
    reply = chat.replies[-1]
    assert "· Failed" in reply.splitlines()[1]
    assert "payload commitment FAILED" in reply


async def test_verify_unknown_or_missing_number(chat):
    await chat.say("/verify 999")
    assert chat.replies[-1] == "No entry E999."
    await chat.say("/verify")
    assert chat.replies[-1].startswith("Usage: /verify")


async def test_verify_a_non_record_entry_still_checks_it(chat):
    await chat.say("/verify 1")  # the project.created entry
    reply = chat.replies[-1]
    assert reply.startswith("E1 · project.created")
    assert "Checks: entry hash ok · payload commitment ok · chain link ok" in reply


async def test_status_shows_polling_and_the_records_without_a_project(chat):
    await chat.say("one")
    chat.clock.advance(20)
    await chat.say("two")
    chat.clock.advance(20)
    await chat.say("/status")
    reply = chat.replies[-1]
    assert reply.splitlines()[0].startswith("Polling: ")
    assert "Records without a project: 2" in reply
    assert "Ledger: " in reply and "entries" in reply
    assert "Last seal: none yet" in reply


async def test_help_and_start_list_every_command(chat):
    await chat.say("/help")
    reply = chat.replies[-1]
    for name, _ in COMMANDS:
        assert f"/{name}" in reply, name
    assert "Capture" in reply and "Find and prove" in reply and "Projects" in reply
    await chat.say("/start")
    assert chat.replies[-1] == reply
    assert help_text() == reply


async def test_commands_that_come_in_a_later_phase_say_so(chat):
    await chat.say("/respond claim")
    assert "not available yet" in chat.replies[-1]


async def test_the_command_menu_is_published_at_start_up():
    from app.connectors.telegram.connector import TelegramConnector, TelegramSettings
    from tests.fake_telegram import FakeTelegramApi, StopPolling
    from tests.fixtures.telegram import CAPTURE_CHAT_ID

    api = FakeTelegramApi(batches=[])
    settings = TelegramSettings(
        bot_token="1:x",
        capture_chat_id=CAPTURE_CHAT_ID,
        ack_reaction="✍",
        bot_username="FactualThothBot",
    )
    conn = TelegramConnector(settings, api, retry_delay=0)
    conn.commands_menu = list(COMMANDS)

    class Sink:
        async def handle(self, event):  # pragma: no cover
            pass

    with pytest.raises(StopPolling):
        await conn.start(Sink())
    (published,) = api.named("set_my_commands")
    assert [name for name, _ in published["commands"]] == [name for name, _ in COMMANDS]
    assert all(1 <= len(desc) <= 256 for _, desc in published["commands"])
