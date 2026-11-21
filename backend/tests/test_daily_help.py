"""FAC-21: the command list every morning at 06:00, once, with yesterday's message deleted."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.commands.menu import COMMANDS
from app.connectors.base import ChatRef
from app.scheduler.daily_help import DailyHelp
from app.scheduler.scheduler import Scheduler
from tests.chat_harness import Chat
from tests.fixtures.telegram import CAPTURE_CHAT_ID

AMS = ZoneInfo("Europe/Amsterdam")
CHAT = ChatRef("telegram", CAPTURE_CHAT_ID)


@pytest.fixture
async def chat(database_url):
    chat = await Chat.open(database_url, seals=True)
    await chat.say("/project new family")
    await chat.say("one unfiled note")
    chat.clock.advance(30)
    await chat.say("another unfiled note")
    yield chat
    await chat.close()


def daily(chat: Chat, **kw) -> DailyHelp:
    return DailyHelp(
        chat.db, chat.connector, CHAT, timezone="Europe/Amsterdam", clock=chat.clock, **kw
    )


def set_local(chat: Chat, year, month, day, hour, minute) -> None:
    chat.clock.now = datetime(year, month, day, hour, minute, tzinfo=AMS).astimezone(UTC)


def help_messages(chat: Chat) -> list:
    return [m for _, m in chat.connector.sent if m.text.startswith("Good morning.")]


async def test_at_six_local_time_thoth_posts_the_commands_and_a_status_line(chat):
    helper = daily(chat)
    set_local(chat, 2026, 11, 21, 5, 59)
    assert await helper.tick() is False
    set_local(chat, 2026, 11, 21, 6, 0)
    assert await helper.tick() is True
    (message,) = help_messages(chat)
    assert message.text.startswith("Good morning. Thoth's commands for Sat 21 Nov 2026:")
    for name, _ in COMMANDS:
        assert f"/{name}" in message.text
    status = message.text.splitlines()[-1]
    assert status.startswith("Status: 2 records without a project · last seal ")
    assert status.endswith("· last audit not yet run")
    assert message.reply_to is None
    assert chat.rows("SELECT job, status FROM schedule_run") == [("daily_help", "sent")]


async def test_the_message_is_sent_once_a_day_even_across_restarts(chat):
    set_local(chat, 2026, 11, 21, 6, 0)
    assert await daily(chat).tick() is True
    chat.clock.advance(60)
    assert await daily(chat).tick() is False, "a restarted process must not send again"
    chat.clock.advance(3600)
    assert await daily(chat).tick() is False
    assert len(help_messages(chat)) == 1


async def test_yesterdays_message_is_deleted_when_todays_is_posted(chat):
    helper = daily(chat)
    set_local(chat, 2026, 11, 21, 6, 0)
    await helper.tick()
    first_id = chat.connector.message_ids[-1]
    set_local(chat, 2026, 11, 22, 6, 0)
    await helper.tick()
    assert len(help_messages(chat)) == 2
    assert [ref.message_id for ref in chat.connector.deleted] == [first_id]
    assert "Sun 22 Nov 2026" in help_messages(chat)[-1].text


async def test_after_an_outage_the_message_is_still_sent_until_nine_then_skipped(chat):
    helper = daily(chat)
    set_local(chat, 2026, 11, 21, 8, 59)
    assert await helper.tick() is True, "late, but before 09:00: still sent"
    set_local(chat, 2026, 11, 22, 9, 0)
    assert await helper.tick() is False, "from 09:00 the day is skipped"
    set_local(chat, 2026, 11, 22, 23, 0)
    assert await helper.tick() is False
    set_local(chat, 2026, 11, 23, 6, 30)
    assert await helper.tick() is True
    assert chat.rows("SELECT count(*) FROM schedule_run WHERE status = 'sent'") == [(2,)]
    assert chat.rows("SELECT scheduled_for::date FROM schedule_run ORDER BY scheduled_for") == [
        (datetime(2026, 11, 21).date(),),
        (datetime(2026, 11, 23).date(),),
    ]


@pytest.mark.parametrize(
    ("year", "month", "day", "utc_hour"),
    [
        (2026, 3, 28, 5),  # CET: 06:00 Amsterdam is 05:00 UTC
        (2026, 3, 29, 4),  # the day the clocks go forward: 06:00 CEST is 04:00 UTC
        (2026, 10, 24, 4),  # CEST
        (2026, 10, 25, 5),  # the day the clocks go back: 06:00 CET is 05:00 UTC
    ],
)
async def test_six_o_clock_follows_the_daylight_saving_changes(chat, year, month, day, utc_hour):
    helper = daily(chat)
    chat.clock.now = datetime(year, month, day, utc_hour, 0, tzinfo=UTC) - __import__(
        "datetime"
    ).timedelta(minutes=1)
    assert await helper.tick() is False, "one minute before 06:00 local"
    chat.clock.now = datetime(year, month, day, utc_hour, 0, tzinfo=UTC)
    assert await helper.tick() is True, "06:00 local"
    assert help_messages(chat)[-1].text.startswith(
        f"Good morning. Thoth's commands for {chat.clock.now.astimezone(AMS):%a} {day} "
    )


async def test_the_scheduler_runs_the_job_on_its_interval(chat):
    helper = daily(chat)
    set_local(chat, 2026, 11, 21, 6, 0)
    scheduler = Scheduler([helper], interval=0.02)
    task = asyncio.create_task(scheduler.start())
    for _ in range(100):
        if help_messages(chat):
            break
        await asyncio.sleep(0.02)
    await scheduler.stop()
    await asyncio.wait_for(task, timeout=2)
    assert len(help_messages(chat)) == 1


async def test_a_failed_send_is_retried_at_the_next_tick(chat):
    helper = daily(chat)
    set_local(chat, 2026, 11, 21, 6, 0)
    original = chat.connector.send

    async def failing(*args, **kwargs):
        raise ConnectionError("telegram down")

    chat.connector.send = failing
    assert await helper.tick() is False
    assert chat.rows("SELECT count(*) FROM schedule_run WHERE status = 'sent'") == [(0,)]
    chat.connector.send = original
    chat.clock.advance(60)
    assert await helper.tick() is True
    assert len(help_messages(chat)) == 1
