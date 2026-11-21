"""Derived fields of a record: the stated event time and the hashtags.

A note that starts with `@09:30` or `@2026-11-19 16:00` states when the event
happened; it is stored as "stated by the author", next to the time of writing.
Hashtags such as `#call` become tags for search.
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

STATED = re.compile(
    r"^@(?:(?P<date>\d{4}-\d{2}-\d{2})(?:[ T](?P<time>\d{1,2}:\d{2}))?"
    r"|(?P<tod>\d{1,2}:\d{2}))(?=\s|$)"
)
HASHTAG = re.compile(r"#(\w+)")


def stated_time(text: str | None, *, written_at: datetime, zone: ZoneInfo) -> datetime | None:
    """The time the author states at the start of the note, in the configured zone."""
    if not text:
        return None
    match = STATED.match(text.lstrip())
    if match is None:
        return None
    try:
        if match.group("tod"):
            hour, minute = (int(part) for part in match.group("tod").split(":"))
            day = written_at.astimezone(zone)
            return day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        date = datetime.strptime(match.group("date"), "%Y-%m-%d")
        hour, minute = 0, 0
        if match.group("time"):
            hour, minute = (int(part) for part in match.group("time").split(":"))
        return datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone)
    except ValueError:
        return None


def hashtags(*texts: str | None) -> list[str]:
    """Lower-case tags, unique, in order of appearance across text and caption."""
    seen: list[str] = []
    for text in texts:
        for tag in HASHTAG.findall(text or ""):
            lowered = tag.lower()
            if lowered not in seen:
                seen.append(lowered)
    return seen
