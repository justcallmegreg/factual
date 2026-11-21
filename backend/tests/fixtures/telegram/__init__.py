"""Raw Telegram updates for the connector tests."""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPTURE_CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009876543210
BOT_ID = 7000000001


def load(name: str) -> dict:
    return json.loads((HERE / f"{name}.json").read_text(encoding="utf-8"))
