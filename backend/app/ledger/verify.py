"""Fast verification: what every answer and `/verify` check for one entry."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC

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
class SealInfo:
    no: int
    up_to_seq: int
    created_at: str
    anchors: tuple[tuple[str, str], ...] = ()  # (authority host, time) per trusted timestamp


@dataclass(frozen=True)
class Verification:
    status: Status
    checks: list[tuple[str, bool]] = field(default_factory=list)
    entry: dict | None = None
    seal: SealInfo | None = None

    def checks_line(self) -> str:
        return " · ".join(f"{name} {'ok' if ok else 'FAILED'}" for name, ok in self.checks)


class Verifier:
    """The fast check with seal coverage: the chain proof to the seal and its signature."""

    def __init__(self, genesis: bytes, public_key: bytes) -> None:
        self.genesis = genesis
        self.public_key = public_key

    async def verify(self, conn: AsyncConnection, seq: int) -> Verification:
        from app.ledger.chain import fold_chain
        from app.ledger.seals import covering_seal, verify_seal_signature

        base = await fast_verify(conn, seq, self.genesis)
        if base.status.label == "Failed" or base.entry is None:
            return base
        sealed = await covering_seal(conn, seq)
        if sealed is None:
            return base
        rows = (
            await conn.execute(
                select(ledger_entry.c.seq, ledger_entry.c.entry, ledger_entry.c.entry_hash)
                .where(ledger_entry.c.seq >= seq - 1, ledger_entry.c.seq <= sealed.up_to_seq)
                .order_by(ledger_entry.c.seq)
            )
        ).all()
        by_seq = {int(r.seq): r for r in rows}
        before = self.genesis if seq == 1 else None
        if seq > 1 and (seq - 1) in by_seq:
            before_row = (
                await conn.execute(
                    select(ledger_entry.c.chain_hash).where(ledger_entry.c.seq == seq - 1)
                )
            ).first()
            before = bytes(before_row.chain_hash) if before_row else None
        later = [
            bytes(by_seq[s].entry_hash) for s in range(seq + 1, sealed.up_to_seq + 1) if s in by_seq
        ]
        proof_ok = (
            before is not None
            and seq in by_seq
            and len(later) == sealed.up_to_seq - seq
            and "sha256:" + fold_chain(before, bytes(by_seq[seq].entry), later).hex()
            == sealed.fields.get("chain_head")
        )
        signature_ok = verify_seal_signature(sealed.body, sealed.signature, self.public_key)
        checks = [
            *base.checks,
            (f"chain proof to seal {sealed.no}", proof_ok),
            ("seal signature", signature_ok),
        ]
        from app.ledger.anchors import host_of
        from app.storage.models import seal_anchor

        anchor_rows = (
            await conn.execute(
                select(seal_anchor.c.issuer, seal_anchor.c.gen_time)
                .where(seal_anchor.c.seal_no == sealed.no, seal_anchor.c.gen_time.is_not(None))
                .order_by(seal_anchor.c.id)
            )
        ).all()
        anchors = tuple(
            (host_of(r.issuer), r.gen_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for r in anchor_rows
        )
        info = SealInfo(sealed.no, sealed.up_to_seq, sealed.fields.get("created_at", ""), anchors)
        if proof_ok and signature_ok:
            return Verification(Status("Verified", sealed.no), checks, base.entry, info)
        reason = "chain proof mismatch" if not proof_ok else "seal signature invalid"
        return Verification(Status("Failed", detail=reason), checks, base.entry, info)


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
