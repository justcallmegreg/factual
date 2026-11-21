"""Connector discovery: the built-in Telegram connector plus `factual.connectors` entry points."""

from __future__ import annotations

from importlib.metadata import entry_points

ENTRY_POINT_GROUP = "factual.connectors"


def available_connectors() -> dict[str, type]:
    """Connector classes by type name."""
    from app.connectors.telegram.connector import TelegramConnector

    found: dict[str, type] = {TelegramConnector.type: TelegramConnector}
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        found[entry.name] = entry.load()
    return found
