"""FAC-08: the chain verifies after the process is killed during appends and restarted."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from app.ledger.ledger import Ledger
from app.storage.db import Database
from tests.helpers.appender import PUBLIC_KEY

BACKEND = Path(__file__).resolve().parents[1]


def run_appender_until_killed(database_url: str, start: int, min_lines: int = 15) -> int:
    """Start the appender, wait for some commits, SIGKILL it. Returns the last printed index."""
    proc = subprocess.Popen(  # noqa: S603 — fixed argv
        [sys.executable, "-m", "tests.helpers.appender", database_url, str(start)],
        cwd=BACKEND,
        stdout=subprocess.PIPE,
        text=True,
    )
    last = start - 1
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        last = int(line)
        if last - start + 1 >= min_lines:
            break
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)
    assert proc.returncode == -signal.SIGKILL
    return last


async def test_the_chain_verifies_after_a_kill_during_appends_and_a_restart(database_url):
    killed_at = run_appender_until_killed(database_url, start=1)
    assert killed_at >= 15

    db = Database(database_url)
    try:
        ledger = await Ledger.open(db, public_key=PUBLIC_KEY)
        async with db.engine.connect() as conn:
            report = await ledger.verify_chain(conn)
        assert report.ok, report.failure
        assert report.head_seq >= killed_at  # a commit may have landed after the last print

        # started again: appends continue from the head without a gap
        resumed_at = run_appender_until_killed(database_url, start=report.head_seq + 1, min_lines=5)
        assert resumed_at > report.head_seq
        async with db.engine.connect() as conn:
            after = await ledger.verify_chain(conn)
        assert after.ok, after.failure
        assert after.head_seq >= resumed_at and after.entries == after.head_seq
    finally:
        await db.dispose()
