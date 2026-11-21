"""FAC-10: the stated event time (@09:30) and hashtags in the normalised view."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.records.normalize import hashtags, stated_time

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
WRITTEN = datetime(2026, 10, 29, 13, 3, 12, tzinfo=UTC)  # 14:03 CET


def test_a_time_of_day_means_that_time_on_the_day_the_note_was_written():
    stated = stated_time("@09:30 Greg called", written_at=WRITTEN, zone=AMSTERDAM)
    assert stated == datetime(2026, 10, 29, 9, 30, tzinfo=AMSTERDAM)
    assert stated.utcoffset().total_seconds() == 3600


def test_a_full_date_and_time_is_taken_as_written_in_the_configured_zone():
    stated = stated_time("@2026-11-19 16:00 kickoff moved", written_at=WRITTEN, zone=AMSTERDAM)
    assert stated == datetime(2026, 11, 19, 16, 0, tzinfo=AMSTERDAM)


def test_a_date_alone_means_that_day_at_midnight():
    stated = stated_time("@2026-07-01 handover", written_at=WRITTEN, zone=AMSTERDAM)
    assert stated == datetime(2026, 7, 1, 0, 0, tzinfo=AMSTERDAM)
    assert stated.utcoffset().total_seconds() == 7200  # CEST


@pytest.mark.parametrize("text", ["Greg called @09:30", "@25:99 nonsense", "@ninish", "", None])
def test_anything_else_is_not_a_stated_time(text):
    assert stated_time(text, written_at=WRITTEN, zone=AMSTERDAM) is None


def test_hashtags_are_lower_case_unique_and_in_order_of_appearance():
    assert hashtags("Call with Greg #call #Promise about #call", "caption #letter") == [
        "call",
        "promise",
        "letter",
    ]
    assert hashtags(None, None) == []
    assert hashtags("no tags here, not even a # sign", None) == []
