"""Time helpers: an injectable clock and the one way times are shown to people."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


def format_local(moment: datetime, zone: ZoneInfo) -> str:
    """`Thu 29 Oct 2026, 14:03 CET`: the configured zone with its abbreviation."""
    local = moment.astimezone(zone)
    return f"{local:%a} {local.day} {local:%b %Y, %H:%M} {local.tzname()}"


def format_utc(moment: datetime) -> str:
    """Seals and audits show UTC: `2026-10-29T13:05:00Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
