"""Which database cluster is this? The answer tells a restore from a restart.

PostgreSQL gives every cluster a system identifier at `initdb`. Each start of
Thoth (and each `python -m app audit`) records it in `boot`. When the identifier
recorded last differs from the one the connection sees, the data was restored
into another cluster: the deep audit then runs before anything else starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.storage.models import boot

UNKNOWN = "unknown"


@dataclass(frozen=True)
class BootState:
    identifier: str
    previous: str | None
    restored: bool


async def cluster_identifier(conn: AsyncConnection) -> str:
    try:
        result = await conn.execute(text("SELECT system_identifier FROM pg_control_system()"))
        return str(result.scalar_one())
    except Exception:  # noqa: BLE001 — a hosted PostgreSQL may hide the control file
        await conn.rollback()
        return UNKNOWN


async def detect_restore(conn: AsyncConnection) -> BootState:
    identifier = await cluster_identifier(conn)
    row = (
        await conn.execute(select(boot.c.system_identifier).order_by(boot.c.id.desc()).limit(1))
    ).first()
    previous = str(row[0]) if row is not None else None
    restored = (
        previous is not None and UNKNOWN not in (previous, identifier) and previous != identifier
    )
    return BootState(identifier, previous, restored)


async def record_boot(
    conn: AsyncConnection,
    state: BootState,
    *,
    kind: str,
    version: str,
    audit_seq: int | None = None,
    now: datetime | None = None,
) -> None:
    await conn.execute(
        insert(boot).values(
            at=now or datetime.now(UTC),
            system_identifier=state.identifier,
            kind=kind,
            restored=state.restored,
            audit_seq=audit_seq,
            version=version,
        )
    )
