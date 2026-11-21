"""FAC-12: projects are labels with permanent short names."""

from __future__ import annotations

import pytest

from tests.chat_harness import Chat

GREG = {"id": 51234568, "is_bot": False, "first_name": "Greg", "last_name": "S."}


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url)
    yield chat
    await chat.close()


async def test_project_new_creates_a_project_and_appends_project_created(chat):
    await chat.say("/project new client-acme")
    assert chat.replies[-1] == "Created project client-acme."
    (entry,) = chat.entries()
    assert entry["type"] == "project.created"
    assert chat.payload(1)["name"] == "client-acme"
    assert chat.payload(1)["by"]["id"] == 51234567
    assert chat.rows("SELECT name, archived_at FROM project") == [("client-acme", None)]


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("family", "family"),
        ("Client_Acme", "client_acme"),
        ("A", "a"),
        ("x" * 32, "x" * 32),
        ("2026-taxes", "2026-taxes"),
    ],
)
async def test_valid_names_are_stored_in_lower_case(chat, typed, stored):
    await chat.say(f"/project new {typed}")
    assert chat.rows("SELECT name FROM project") == [(stored,)]
    assert chat.replies[-1] == f"Created project {stored}."


@pytest.mark.parametrize(
    "typed",
    ["Client Acme", "x" * 33, "café", "client.acme", "client/acme", "#family", "", "client:acme"],
)
async def test_invalid_names_are_rejected_with_a_clear_message_and_no_entry(chat, typed):
    await chat.say(f"/project new {typed}".rstrip())
    reply = chat.replies[-1]
    assert "1 to 32 characters" in reply and "a-z" in reply and "_" in reply and "-" in reply
    assert chat.entries() == [] and chat.rows("SELECT count(*) FROM project") == [(0,)]


async def test_a_duplicate_name_is_refused_and_appends_nothing(chat):
    await chat.say("/project new family")
    await chat.say("/project new Family")
    assert chat.replies[-1] == "Project family exists already."
    assert len(chat.entries()) == 1


async def test_project_lists_projects_with_their_record_counts(chat):
    await chat.say("/project new family")
    await chat.say("/project new client-acme")
    first = await chat.say("Greg called about the kickoff")
    await chat.say("Second note")
    # filing is the next story; the projection column is what the count reads
    with __import__("psycopg").connect(chat.database_url, autocommit=True) as conn:
        conn.execute("UPDATE record SET project = 'client-acme' WHERE message_id = %s", (first,))
    await chat.say("/project")
    reply = chat.replies[-1]
    assert reply.splitlines()[0] == "Projects:"
    assert "client-acme · 1 record" in reply
    assert "family · 0 records" in reply
    assert "archived" not in reply.lower()


async def test_project_with_no_projects_tells_how_to_create_one(chat):
    await chat.say("/project")
    assert chat.replies[-1] == "No projects yet. Create one with /project new <name>."


async def test_archive_hides_a_project_from_the_picker_and_keeps_its_records(chat):
    await chat.say("/project new client-acme")
    note = await chat.say("Kickoff on 5 February")
    with __import__("psycopg").connect(chat.database_url, autocommit=True) as conn:
        conn.execute("UPDATE record SET project = 'client-acme' WHERE message_id = %s", (note,))
    await chat.say("/project archive client-acme", user=GREG)
    assert chat.replies[-1] == "Archived project client-acme. Its records stay."
    entries = chat.entries()
    assert entries[-1]["type"] == "project.archived"
    assert chat.payload(entries[-1]["seq"])["name"] == "client-acme"
    assert chat.payload(entries[-1]["seq"])["by"]["id"] == 51234568
    ((archived_at,),) = chat.rows("SELECT archived_at FROM project WHERE name = 'client-acme'")
    assert archived_at is not None
    assert chat.rows("SELECT project FROM record WHERE message_id = %s", (note,)) == [
        ("client-acme",)
    ]
    await chat.say("/project")
    assert "client-acme · 1 record (archived)" in chat.replies[-1]
    from app.records.projects import picker_projects

    async with chat.db.engine.connect() as conn:
        assert [p.name for p in await picker_projects(conn)] == []


async def test_archiving_twice_or_an_unknown_project_is_refused(chat):
    await chat.say("/project archive nothing")
    assert chat.replies[-1] == "No project named nothing."
    await chat.say("/project new family")
    await chat.say("/project archive family")
    await chat.say("/project archive family")
    assert chat.replies[-1] == "Project family is archived already."
    assert [e["type"] for e in chat.entries()] == ["project.created", "project.archived"]


async def test_creating_an_archived_name_again_is_refused_because_names_are_permanent(chat):
    await chat.say("/project new family")
    await chat.say("/project archive family")
    await chat.say("/project new family")
    assert chat.replies[-1] == "Project family is archived; names are permanent, so pick a new one."


async def test_project_with_an_unknown_subcommand_shows_the_usage(chat):
    await chat.say("/project delete family")
    assert chat.replies[-1].startswith("Usage: /project")
    assert (
        "/project new <name>" in chat.replies[-1] and "/project archive <name>" in chat.replies[-1]
    )


async def test_a_redelivered_project_command_creates_no_second_entry(chat):
    raw = chat._raw_message("/project new family")
    from app.connectors.telegram.mapping import map_update
    from tests.fixtures.telegram import CAPTURE_CHAT_ID

    event = map_update(raw, capture_chat_id=CAPTURE_CHAT_ID, bot_username="FactualThothBot")
    await chat.ingestion.handle(event)
    await chat.ingestion.handle(event)
    assert len(chat.entries()) == 1
    assert chat.replies == ["Created project family.", "Created project family."]


async def test_replies_go_to_the_capture_chat_as_a_reply_to_the_command(chat):
    message_id = await chat.say("/project new family")
    chat_ref, message = chat.connector.sent[-1]
    assert chat_ref.chat_id == -1001234567890
    assert message.reply_to.message_id == message_id
