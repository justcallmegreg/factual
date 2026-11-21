"""Fast verification: what every answer and `/verify` check for one entry."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.ledger.hashing import commitment_string, entry_hash, payload_commitment
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


async def fast_status(conn: AsyncConnection, seq: int) -> Status:
    """Recompute the entry hash and the payload commitment of one entry.

    Seal coverage (which turns Pending seal into Verified) is added with the seals.
    """
    row = (
        await conn.execute(
            select(
                ledger_entry.c.entry,
                ledger_entry.c.entry_hash,
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
        return Status("Failed", detail=f"no entry {seq}")
    entry_bytes = bytes(row.entry)
    if entry_hash(entry_bytes) != bytes(row.entry_hash):
        return Status("Failed", detail="entry hash mismatch")
    if row.payload is None or row.salt is None:
        return Status("Failed", detail="payload missing")
    import json

    entry = json.loads(entry_bytes)
    commitment = commitment_string(payload_commitment(bytes(row.salt), bytes(row.payload)))
    if entry.get("payload_commitment") != commitment:
        return Status("Failed", detail="payload commitment mismatch")
    return Status("Pending seal")
