"""Gate 1 · Core loop works: send a note, see ✍, file it with one tap, find it with /ask;
the chain verifies after a crash (tests/test_crash_replay.py covers the crash)."""

from __future__ import annotations

import pytest

from tests.chat_harness import Chat


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url)
    await chat.say("/project new client-acme")
    chat.connector.sent.clear()
    chat.connector.acknowledged.clear()
    yield chat
    await chat.close()


async def test_the_core_loop(chat):
    # 1. send a note
    message_id = await chat.say("Greg called. He may move the kickoff to 12 February.")
    # 2. see ✍: the mark was set on that message, after the commit
    assert [ref.message_id for ref in chat.connector.acknowledged] == [message_id]
    assert chat.rows("SELECT count(*) FROM record") == [(1,)]
    # 3. file it with one tap
    assert "Which project?" in chat.last.text
    await chat.tap(chat.button("client-acme").data, on_message=chat.last_message_id)
    assert chat.rows("SELECT project FROM record") == [("client-acme",)]
    assert "client-acme" in chat.last.text and "Change project" in [
        b.label for row in chat.last.buttons for b in row
    ]
    # 4. find it with /ask
    chat.clock.advance(5)
    await chat.say("/ask kickoff February")
    reply = chat.replies[-1]
    assert '"Greg called. He may move the kickoff to 12 February."' in reply
    assert "client-acme" in reply and "Pending seal" in reply
    # 5. the chain verifies
    async with chat.db.engine.connect() as conn:
        report = await chat.ledger.verify_chain(conn)
    assert report.ok and report.entries == report.head_seq >= 3
    # and /verify agrees
    await chat.say("/verify", reply_to=message_id)
    assert "Checks: entry hash ok · payload commitment ok · chain link ok" in chat.replies[-1]
