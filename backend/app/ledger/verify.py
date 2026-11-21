"""Fast verification: what every answer and `/verify` check for one entry."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.ledger.hashing import chain_hash, commitment_string, entry_hash, payload_commitment
from app.storage.models import evidence_payload, ledger_entry


@dataclass(frozen=True)
class Status:
    """Verified (sealed), Pending seal (captured, not yet sealed) or Failed."""

    label: str  # Verified | Pending seal | Failed
    seal_no: int | None = None
    detail: str = ""

    def __str__(self) -> str:
        if self.label == "Verified" and self.seal_no is not None:
            return f"Verified · seal {self.seal_no}"
        return self.label


@dataclass(frozen=True)
class Verification:
    status: Status
    checks: list[tuple[str, bool]] = field(default_factory=list)
    entry: dict | None = None

    def checks_line(self) -> str:
        return " · ".join(f"{name} {'ok' if ok else 'FAILED'}" for name, ok in self.checks)


async def fast_verify(
    conn: AsyncConnection, seq: int, genesis: bytes | None = None
) -> Verification:
    """Recompute the entry hash, the payload commitment and the chain link of one entry.

    Seal coverage (which turns Pending seal into Verified) is added with the seals.
    """
    row = (
        await conn.execute(
            select(
                ledger_entry.c.entry,
                ledger_entry.c.entry_hash,
                ledger_entry.c.chain_hash,
                evidence_payload.c.salt,
                evidence_payload.c.payload,
            )
            .select_from(
                ledger_entry.outerjoin(
                    evidence_payload, evidence_payload.c.seq == ledger_entry.c.seq
                )
            )
            .where(ledger_entry.c.seq == seq)
        )
    ).first()
    if row is None:
        return Verification(Status("Failed", detail=f"no entry {seq}"))
    entry_bytes = bytes(row.entry)
    digest = entry_hash(entry_bytes)
    checks: list[tuple[str, bool]] = [("entry hash", digest == bytes(row.entry_hash))]
    try:
        entry = json.loads(entry_bytes)
    except ValueError:
        entry = None
    if row.payload is None or row.salt is None or entry is None:
        checks.append(("payload commitment", False))
    else:
        commitment = commitment_string(payload_commitment(bytes(row.salt), bytes(row.payload)))
        checks.append(("payload commitment", entry.get("payload_commitment") == commitment))
    if genesis is not None:
        if seq == 1:
            previous = genesis
        else:
            before = (
                await conn.execute(
                    select(ledger_entry.c.chain_hash).where(ledger_entry.c.seq == seq - 1)
                )
            ).first()
            previous = bytes(before.chain_hash) if before is not None else None
        linked = previous is not None and chain_hash(previous, digest) == bytes(row.chain_hash)
        checks.append(("chain link", linked))
    failed = [name for name, ok in checks if not ok]
    if failed:
        return Verification(Status("Failed", detail=f"{failed[0]} mismatch"), checks, entry)
    return Verification(Status("Pending seal"), checks, entry)


async def fast_status(conn: AsyncConnection, seq: int) -> Status:
    return (await fast_verify(conn, seq)).status
