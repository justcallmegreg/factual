"""FAC-13: filing a record takes one tap."""

from __future__ import annotations

import pytest

from app.connectors.base import Button
from tests.chat_harness import Chat

ANNA = {
    "id": 51234567,
    "is_bot": False,
    "first_name": "Anna",
    "last_name": "K.",
    "username": "annak",
}
GREG = {"id": 51234568, "is_bot": False, "first_name": "Greg", "last_name": "S."}


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url)
    for name in ("family", "personal", "client-acme"):
        await chat.say(f"/project new {name}")
    chat.connector.sent.clear()
    yield chat
    await chat.close()


def buttons(msg) -> list[list[Button]]:
    return [list(row) for row in msg.buttons]


def labels(msg) -> list[str]:
    return [b.label for row in buttons(msg) for b in row]


def filed_entries(chat) -> list[dict]:
    return [e for e in chat.entries() if e["type"] == "record.filed"]


async def test_after_a_record_thoth_replies_with_project_buttons(chat):
    message_id = await chat.say("Greg called about the kickoff")
    picker = chat.last
    assert picker.reply_to.message_id == message_id
    assert picker.text.startswith("Recorded E") and picker.text.endswith("Which project?")
    assert "Thu 29 Oct 2026, 14:" in picker.text and "CET" in picker.text
    assert set(labels(picker)) == {"client-acme", "family", "personal"}
    (data,) = {b.data.rsplit(":", 1)[0] for b in buttons(picker)[0]}
    assert data.startswith("p:")


async def test_most_recently_used_projects_come_first_six_buttons_then_more(chat):
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        await chat.say(f"/project new {name}")
    await chat.say("first note")
    await chat.tap(
        f"{chat.last.buttons[0][0].data.rsplit(':', 1)[0]}:gamma", on_message=chat.last_message_id
    )
    chat.clock.advance(20)
    await chat.say("second note")
    picker = chat.last
    shown = labels(picker)
    assert shown[0] == "gamma", "most recently used first"
    assert shown[-1] == "More" and len(shown) == 7, "six project buttons, then More"
    assert all(len(b.data.encode()) <= 64 for row in buttons(picker) for b in row)
    more = buttons(picker)[-1][0]
    await chat.tap(more.data, on_message=chat.last_message_id)
    second_page = chat.last
    assert second_page.edit_of is not None and "More" not in labels(second_page)[:-1]
    assert len([lb for lb in labels(second_page) if lb not in ("More", "Back")]) == 8 - 6


async def test_a_tap_files_the_record_and_turns_the_picker_into_a_confirmation(chat):
    message_id = await chat.say("Greg called about the kickoff")
    picker, picker_message_id = chat.last, chat.last_message_id
    tap = next(b for row in buttons(picker) for b in row if b.label == "client-acme")
    await chat.tap(tap.data, on_message=picker_message_id)
    (filed,) = filed_entries(chat)
    record_seq = chat.rows("SELECT seq FROM record WHERE message_id = %s", (message_id,))[0][0]
    assert filed["refs"] == [record_seq]
    payload = chat.payload(filed["seq"])
    assert payload["project"] == "client-acme" and payload["record_seq"] == record_seq
    assert (
        payload["previous"] is None and payload["by"]["id"] == 51234567 and payload["via"] == "tap"
    )
    assert chat.rows("SELECT project, filed_seq FROM record WHERE seq = %s", (record_seq,)) == [
        ("client-acme", filed["seq"])
    ]
    confirmation = chat.last
    assert confirmation.edit_of.message_id == picker_message_id
    assert confirmation.text == f"E{record_seq} · client-acme · Thu 29 Oct 2026, 14:03 CET"
    assert labels(confirmation) == ["Change project"]
    assert confirmation.answer_choice is not None


async def test_change_project_opens_the_picker_again_and_a_new_choice_is_a_new_entry(chat):
    await chat.say("note")
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    picker_id = chat.last_message_id
    await chat.tap(chat.button("family").data, on_message=picker_id)
    await chat.tap(chat.button("Change project").data, on_message=picker_id)
    assert chat.last.edit_of.message_id == picker_id and "Which project?" in chat.last.text
    await chat.tap(chat.button("personal").data, on_message=picker_id)
    first, second = filed_entries(chat)
    assert chat.payload(second["seq"])["previous"] == "family"
    assert chat.payload(second["seq"])["project"] == "personal"
    assert chat.rows("SELECT project FROM record WHERE seq = %s", (seq,)) == [("personal",)]
    assert chat.rows("SELECT project FROM filing WHERE record_seq = %s ORDER BY seq", (seq,)) == [
        ("family",),
        ("personal",),
    ]


async def test_records_one_person_sends_within_ten_seconds_share_one_picker(chat):
    await chat.say("first")
    first_picker_id = chat.last_message_id
    chat.clock.advance(4)
    await chat.say("second")
    assert chat.last.edit_of.message_id == first_picker_id, "the picker was edited, not re-sent"
    assert chat.last.text.startswith("Recorded E") and "," in chat.last.text.split("·")[0]
    chat.clock.advance(11)
    await chat.say("third")
    assert chat.last.edit_of is None, "after 10 seconds a new picker"
    await chat.tap(chat.button("family", message_index=0).data, on_message=first_picker_id)
    assert len(filed_entries(chat)) == 2, "one tap filed both records of the shared picker"
    assert chat.rows("SELECT count(*) FROM record WHERE project = 'family'") == [(2,)]


async def test_different_people_get_their_own_pickers(chat):
    await chat.say("from anna")
    anna_picker = chat.last_message_id
    chat.clock.advance(2)
    await chat.say("from greg", user=GREG)
    assert chat.last.edit_of is None and chat.last_message_id != anna_picker


async def test_log_without_text_asks_for_the_project_first_then_files_everything(chat):
    await chat.say("/log")
    session_picker = chat.last
    assert "project" in session_picker.text.lower() and "Which project" in session_picker.text
    assert all(b.data.startswith("s:") for row in buttons(session_picker) for b in row)
    session_id = chat.last_message_id
    await chat.tap(chat.button("client-acme").data, on_message=session_id)
    assert chat.last.edit_of.message_id == session_id
    assert "client-acme" in chat.last.text and labels(chat.last) == ["Done"]
    sent_before = len(chat.connector.sent)
    await chat.say("first in session")
    await chat.say("/log second in session")
    assert len(chat.connector.sent) == sent_before, "no picker while the session runs"
    assert chat.rows("SELECT count(*) FROM record WHERE project = 'client-acme'") == [(2,)]
    assert [chat.payload(e["seq"])["via"] for e in filed_entries(chat)] == ["session", "session"]
    chat.clock.advance(3)
    await chat.say("from greg meanwhile", user=GREG)
    assert "Which project?" in chat.last.text, "another person still gets the picker"
    await chat.tap(chat.button("Done", message_index=1).data, on_message=session_id)
    assert chat.last.edit_of.message_id == session_id and "Done" in chat.last.text
    assert "2 records" in chat.last.text
    await chat.say("after done")
    assert chat.last.reply_to is not None and "Which project?" in chat.last.text


async def test_a_session_ends_after_ten_minutes_of_silence(chat):
    await chat.say("/log")
    await chat.tap(chat.button("family").data, on_message=chat.last_message_id)
    await chat.say("in session")
    chat.clock.advance(9 * 60)
    await chat.say("still in session")
    assert chat.rows("SELECT count(*) FROM record WHERE project = 'family'") == [(2,)]
    chat.clock.advance(10 * 60 + 1)
    await chat.say("after the silence")
    assert "Which project?" in chat.last.text
    assert chat.rows("SELECT count(*) FROM record WHERE project = 'family'") == [(2,)]


async def test_a_reply_to_a_record_takes_the_project_of_that_record(chat):
    note = await chat.say("Kickoff on 5 February")
    await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    sent_before = len(chat.connector.sent)
    reply = await chat.say("He confirmed by mail", user=GREG, reply_to=note)
    assert len(chat.connector.sent) == sent_before, "no picker for a reply to a filed record"
    assert chat.rows("SELECT project FROM record WHERE message_id = %s", (reply,)) == [
        ("client-acme",)
    ]
    assert chat.payload(filed_entries(chat)[-1]["seq"])["via"] == "reply"
    unfiled = await chat.say("Unfiled note", user=GREG)
    chat.clock.advance(30)
    await chat.say("reply to unfiled", reply_to=unfiled)
    assert "Which project?" in chat.last.text, "a reply to an unfiled record gets the picker"


async def test_log_pending_lists_the_records_without_a_project_each_with_buttons(chat):
    first = await chat.say("one")
    chat.clock.advance(30)
    await chat.say("two")
    await chat.tap(chat.button("family").data, on_message=chat.last_message_id)
    chat.clock.advance(30)
    third = await chat.say("three")
    chat.connector.sent.clear()
    await chat.say("/log pending")
    pending = chat.connector.sent
    assert len(pending) == 2
    texts = [m.text for _, m in pending]
    assert all("Which project?" in t for t in texts)
    assert {m.reply_to.message_id for _, m in pending} == {first, third}
    await chat.tap(pending[0][1].buttons[0][0].data.rsplit(":", 1)[0] + ":personal", on_message=1)
    chat.connector.sent.clear()
    await chat.say("/log pending")
    assert len(chat.connector.sent) == 1


async def test_log_pending_with_nothing_pending_says_so(chat):
    await chat.say("/log pending")
    assert chat.replies[-1] == "No records without a project."


async def test_a_tap_on_an_unknown_picker_is_answered_and_changes_nothing(chat):
    await chat.say("note")
    await chat.tap("p:999999:family", on_message=chat.last_message_id)
    answered = chat.last
    assert answered.answer_choice is not None and "no longer valid" in (answered.answer_text or "")
    assert filed_entries(chat) == []


async def test_a_tap_for_an_archived_project_is_refused_and_the_picker_refreshed(chat):
    await chat.say("note")
    picker_id = chat.last_message_id
    family = chat.button("family").data
    await chat.say("/project archive family")
    await chat.tap(family, on_message=picker_id)
    assert "archived" in (chat.last.answer_text or "")
    assert chat.last.edit_of.message_id == picker_id and "family" not in labels(chat.last)
    assert filed_entries(chat) == []


async def test_a_redelivered_tap_files_only_once(chat):
    await chat.say("note")
    picker_id = chat.last_message_id
    raw = chat.tap_raw(chat.button("family").data, on_message=picker_id)
    from app.connectors.telegram.mapping import map_update
    from tests.fixtures.telegram import CAPTURE_CHAT_ID

    event = map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    await chat.ingestion.handle(event)
    await chat.ingestion.handle(event)
    assert len(filed_entries(chat)) == 1
