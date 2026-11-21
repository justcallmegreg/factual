"""Appends entries in a loop until killed. Used by the crash-and-replay test.

usage: python -m tests.helpers.appender DATABASE_URL START_INDEX
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from app.ledger.ledger import Ledger
from app.storage.db import Database

PUBLIC_KEY = b"\x07" * 32


async def main(database_url: str, start: int) -> None:
    db = Database(database_url)
    ledger = await Ledger.open(db, public_key=PUBLIC_KEY)
    i = start
    while True:
        async with db.engine.begin() as conn:
            await ledger.append(
                conn,
                type="record.created",
                source={"connector": "test", "update_id": i},
                source_event=f"test:{i}",
                observed_at=datetime.now(UTC),
                payload={"i": i},
            )
        print(i, flush=True)
        i += 1


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
