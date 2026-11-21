"""FAC-14: /ask shows the matching records as verbatim quotes, with no AI involved."""

from __future__ import annotations

import os
import statistics
import time

import psycopg
import pytest

from app.records.render import render_hit
from app.records.search import search, searched_words
from tests.chat_harness import Chat

GREG = {"id": 51234568, "is_bot": False, "first_name": "Greg", "last_name": "S."}


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url)
    for name in ("family", "client-acme"):
        await chat.say(f"/project new {name}")
    yield chat
    await chat.close()


async def file_last(chat: Chat, project: str) -> None:
    await chat.tap(chat.button(project).data, on_message=chat.last_message_id)


async def test_text_and_caption_are_indexed_and_an_edit_replaces_the_text(chat):
    note = await chat.say("Greg called about the kickoff")
    assert chat.rows("SELECT kind, content FROM search_chunk ORDER BY id") == [
        ("text", "Greg called about the kickoff")
    ]
    from app.connectors.telegram.mapping import map_update
    from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

    doc = load("message_document")
    doc["update_id"] = 700000001
    doc["message"]["message_id"] = note + 500
    doc["message"]["caption"] = "Signed contract, final version"
    await chat.ingestion.handle(
        map_update(doc, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    assert ("caption", "Signed contract, final version") in chat.rows(
        "SELECT kind, content FROM search_chunk"
    )
    raw = chat._raw_message("Greg called about the venue instead")
    raw["message"]["message_id"] = note
    raw["edited_message"] = raw.pop("message")
    raw["edited_message"]["edit_date"] = raw["edited_message"]["date"] + 60
    await chat.ingestion.handle(
        map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    )
    contents = [c for (c,) in chat.rows("SELECT content FROM search_chunk WHERE kind = 'text'")]
    assert contents == ["Greg called about the venue instead"]
    assert (
        chat.rows("SELECT indexdef FROM pg_indexes WHERE indexname = 'search_chunk_tsv_idx'")[0][0]
        .lower()
        .startswith("create index search_chunk_tsv_idx on public.search_chunk using gin")
    )


async def test_ask_returns_verbatim_quotes_with_number_project_time_and_status(chat):
    await chat.say("Greg called. He may move the kickoff to 12 February.")
    await file_last(chat, "client-acme")
    seq = chat.rows("SELECT max(seq) FROM record")[0][0]
    chat.clock.advance(60)
    await chat.say("/ask did Greg move the kickoff?")
    reply = chat.replies[-1]
    lines = reply.splitlines()
    assert lines[0] == f"E{seq} · client-acme · Note by Anna K."
    assert lines[1] == '"Greg called. He may move the kickoff to 12 February."'
    assert lines[2].startswith("Recorded Thu 29 Oct 2026, 14:03 CET · Pending seal")


async def test_search_uses_the_english_configuration_so_word_forms_match(chat):
    await chat.say("The kickoffs were moved twice")
    chat.clock.advance(60)
    await chat.say("/ask kickoff moving")
    assert "kickoffs were moved" in chat.replies[-1]


async def test_a_project_named_in_the_question_puts_its_records_first(chat):
    await chat.say("Kickoff with the family on Sunday", user=GREG)
    await file_last(chat, "family")
    chat.clock.advance(20)
    await chat.say("Kickoff with the client on Monday", user=GREG)
    await file_last(chat, "client-acme")
    chat.clock.advance(20)
    await chat.say("/ask client-acme kickoff")
    reply = chat.replies[-1]
    assert reply.index("client on Monday") < reply.index("family on Sunday")
    await chat.say("/ask family kickoff")
    reply = chat.replies[-1]
    assert reply.index("family on Sunday") < reply.index("client on Monday")


async def test_records_that_match_more_words_rank_first(chat):
    await chat.say("kickoff")
    chat.clock.advance(20)
    await chat.say("kickoff venue budget")
    chat.clock.advance(20)
    await chat.say("/ask kickoff venue budget")
    reply = chat.replies[-1]
    assert reply.index("kickoff venue budget") < reply.index('"kickoff"')


async def test_without_a_match_thoth_says_so_and_lists_the_words_it_searched_for(chat):
    await chat.say("Nothing relevant here")
    chat.clock.advance(20)
    await chat.say("/ask did the Greg say anything about a kickoff?")
    assert chat.replies[-1] == "No records match. I searched for: greg, say, anything, kickoff."


async def test_ask_without_a_question_shows_the_usage(chat):
    await chat.say("/ask")
    assert chat.replies[-1].startswith("Usage: /ask <question>")


async def test_a_forwarded_hit_shows_its_origin_and_a_media_hit_shows_its_kind(chat):
    from app.connectors.telegram.mapping import map_update
    from tests.fixtures.telegram import CAPTURE_CHAT_ID, load

    for name in ("message_forward", "message_document"):
        await chat.ingestion.handle(
            map_update(load(name), capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
        )
    chat.clock.advance(20)
    await chat.say("/ask kickoff office")
    reply = chat.replies[-1]
    assert "Forwarded from Greg S., sent Thu 29 Oct 2026, 13:40 CET" in reply
    await chat.say("/ask signed contract")
    reply = chat.replies[-1]
    assert "[document contract-v2.pdf]" in reply and '"Signed contract #contract"' in reply


async def test_the_renderer_prints_only_stored_text(chat):
    await chat.say("Stored text only")
    chat.clock.advance(20)
    async with chat.db.engine.connect() as conn:
        result = await search(conn, "stored text please", limit=5)
    (hit,) = result.hits
    rendered = render_hit(hit, chat.commands.zone)
    assert '"Stored text only"' in rendered
    assert "please" not in rendered
    assert rendered.count("\n") == 2


async def test_searched_words_leave_out_stop_words_and_keep_the_original_forms(chat):
    async with chat.db.engine.connect() as conn:
        assert await searched_words(conn, "Did the Greg say anything about Kickoffs?") == [
            "Greg",
            "say",
            "anything",
            "Kickoffs",
        ]


@pytest.mark.skipif(not os.environ.get("FACTUAL_PERF"), reason="set FACTUAL_PERF=1 to run")
async def test_search_answers_within_two_seconds_on_fifty_thousand_records(chat):
    words = (
        "kickoff venue budget invoice contract meeting call mail phone deadline agreed moved "
        "confirmed cancelled delivery payment review draft signed office february march"
    ).split()
    import random

    rng = random.Random(42)  # noqa: S311 — reproducible test data, not security
    rows = []
    for i in range(1, 50_001):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(5, 25)))
        rows.append((i, text))
    with psycopg.connect(chat.database_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE record DROP CONSTRAINT record_seq_fkey")
        with conn.cursor() as cur:
            with cur.copy(
                "COPY record (seq, connector, chat_id, message_id, text, origin, observed_at, "
                "received_at, version) FROM STDIN"
            ) as copy:
                for seq, text in rows:
                    copy.write_row(
                        (
                            seq + 1000,
                            "telegram",
                            -1001234567890,
                            seq + 100000,
                            text,
                            "note",
                            "2026-10-29T13:03:12Z",
                            "2026-10-29T13:03:12Z",
                            1,
                        )
                    )
            with cur.copy(
                "COPY search_chunk (record_seq, kind, content, created_at) FROM STDIN"
            ) as copy:
                for seq, text in rows:
                    copy.write_row((seq + 1000, "text", text, "2026-10-29T13:03:12Z"))
        conn.execute("ANALYZE search_chunk")
    timings = []
    async with chat.db.engine.connect() as conn:
        questions = [
            "kickoff venue budget",
            "did they confirm the invoice by mail",
            "signed contract office february",
            "deadline moved cancelled",
        ] * 5
        for question in questions:
            started = time.perf_counter()
            result = await search(conn, question, limit=5)
            timings.append(time.perf_counter() - started)
            assert result.hits
    median_ms = statistics.median(timings) * 1000
    print(
        f"\nsearch over 50,000 records: median {median_ms:.0f} ms, max {max(timings) * 1000:.0f} ms"
    )
    assert max(timings) < 2.0
