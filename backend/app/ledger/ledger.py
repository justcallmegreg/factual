"""The ledger: append-only entries in one hash chain, verified end to end."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.ledger.canonical import canonicalize
from app.ledger.chain import ChainProof
from app.ledger.hashing import (
    chain_hash,
    commitment_string,
    entry_hash,
    genesis_hash,
    payload_commitment,
)
from app.storage.db import Database
from app.storage.models import evidence_payload, instance, ledger_entry

LEDGER_LOCK_KEY = 0x46414354  # "FACT": the one advisory lock that serialises appends
ENTRY_FORMAT_VERSION = 1


def iso_seconds(moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    if moment.microsecond:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_millis(moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class Appended:
    seq: int
    entry_bytes: bytes
    entry_hash: bytes
    chain_hash: bytes
    duplicate: bool = False


@dataclass(frozen=True)
class ChainFailure:
    seq: int
    reason: str


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    entries: int
    head_seq: int
    head_hash: bytes
    failure: ChainFailure | None = None


class Ledger:
    """Appends and checks entries. One instance per process, opened at start-up."""

    def __init__(self, db: Database, *, genesis: bytes, public_key: bytes, created_at: datetime):
        self.db = db
        self.genesis = genesis
        self.public_key = public_key
        self.created_at = created_at

    @classmethod
    async def open(cls, db: Database, *, public_key: bytes) -> Ledger:
        """Load the instance row, creating it (and so the genesis) on the first start."""
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LEDGER_LOCK_KEY})
            row = (await conn.execute(select(instance))).first()
            if row is None:
                created = datetime.now(UTC).replace(microsecond=0)
                genesis = genesis_hash(public_key, iso_seconds(created))
                await conn.execute(
                    insert(instance).values(
                        id=1, public_key=public_key, created_at=created, genesis_hash=genesis
                    )
                )
                return cls(db, genesis=genesis, public_key=public_key, created_at=created)
            return cls(
                db,
                genesis=bytes(row.genesis_hash),
                public_key=bytes(row.public_key),
                created_at=row.created_at,
            )

    async def find(self, conn: AsyncConnection, source_event: str) -> Appended | None:
        """The entry a platform event produced, if it was recorded before."""
        row = (
            await conn.execute(
                select(ledger_entry).where(ledger_entry.c.source_event == source_event)
            )
        ).first()
        if row is None:
            return None
        return Appended(
            seq=int(row.seq),
            entry_bytes=bytes(row.entry),
            entry_hash=bytes(row.entry_hash),
            chain_hash=bytes(row.chain_hash),
            duplicate=True,
        )

    async def head(self, conn: AsyncConnection) -> tuple[int, bytes]:
        row = (
            await conn.execute(
                select(ledger_entry.c.seq, ledger_entry.c.chain_hash)
                .order_by(ledger_entry.c.seq.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return 0, self.genesis
        return int(row.seq), bytes(row.chain_hash)

    async def append(
        self,
        conn: AsyncConnection,
        *,
        type: str,
        source: dict,
        source_event: str | None,
        observed_at: datetime,
        payload: dict,
        refs: Sequence[int] = (),
        received_at: datetime | None = None,
    ) -> Appended:
        """Write entry, payload and chain value in the caller's transaction.

        Serialised by one advisory lock, so `seq` has no gaps. A second delivery of
        the same `source_event` returns the existing entry instead of a new one.
        """
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LEDGER_LOCK_KEY})
        if source_event is not None:
            existing = (
                await conn.execute(
                    select(ledger_entry).where(ledger_entry.c.source_event == source_event)
                )
            ).first()
            if existing is not None:
                return Appended(
                    seq=int(existing.seq),
                    entry_bytes=bytes(existing.entry),
                    entry_hash=bytes(existing.entry_hash),
                    chain_hash=bytes(existing.chain_hash),
                    duplicate=True,
                )
        head_seq, previous = await self.head(conn)
        seq = head_seq + 1
        salt = os.urandom(32)
        payload_bytes = canonicalize(payload)
        commitment = payload_commitment(salt, payload_bytes)
        received = received_at or datetime.now(UTC)
        entry = {
            "v": ENTRY_FORMAT_VERSION,
            "seq": seq,
            "type": type,
            "source": source,
            "observed_at": iso_seconds(observed_at),
            "received_at": iso_millis(received),
            "payload_commitment": commitment_string(commitment),
            "refs": [int(ref) for ref in refs],
        }
        entry_bytes = canonicalize(entry)
        digest = entry_hash(entry_bytes)
        chained = chain_hash(previous, digest)
        await conn.execute(
            insert(ledger_entry).values(
                seq=seq,
                type=type,
                entry=entry_bytes,
                entry_hash=digest,
                chain_hash=chained,
                source_event=source_event,
                received_at=received,
            )
        )
        await conn.execute(
            insert(evidence_payload).values(seq=seq, salt=salt, payload=payload_bytes)
        )
        return Appended(seq=seq, entry_bytes=entry_bytes, entry_hash=digest, chain_hash=chained)

    async def verify_chain(self, conn: AsyncConnection, *, batch_size: int = 1000) -> ChainReport:
        """Recompute every hash from the genesis; stop at the first row that does not fit."""
        expected_seq = 1
        previous = self.genesis
        count = 0
        last_seq = 0
        after = 0
        while True:
            rows = (
                await conn.execute(
                    select(
                        ledger_entry.c.seq,
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
                    .where(ledger_entry.c.seq > after)
                    .order_by(ledger_entry.c.seq)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                break
            for row in rows:
                seq = int(row.seq)
                after = seq
                failure = self._check_row(row, seq, expected_seq, previous)
                if failure is not None:
                    return ChainReport(False, count, last_seq, previous, failure)
                previous = bytes(row.chain_hash)
                last_seq = seq
                expected_seq = seq + 1
                count += 1
        return ChainReport(True, count, last_seq, previous)

    @staticmethod
    def _check_row(row, seq: int, expected_seq: int, previous: bytes) -> ChainFailure | None:
        if seq != expected_seq:
            return ChainFailure(seq, f"gap: expected seq {expected_seq}, found {seq}")
        if row.payload is None or row.salt is None:
            return ChainFailure(seq, "payload missing")
        entry_bytes = bytes(row.entry)
        try:
            entry = json.loads(entry_bytes)
        except ValueError:
            return ChainFailure(seq, "entry bytes are not JSON")
        if not isinstance(entry, dict) or entry.get("seq") != seq:
            return ChainFailure(seq, "entry does not carry its own seq")
        if canonicalize(entry) != entry_bytes:
            return ChainFailure(seq, "entry bytes are not canonical")
        digest = entry_hash(entry_bytes)
        if digest != bytes(row.entry_hash):
            return ChainFailure(seq, "entry hash mismatch")
        commitment = commitment_string(payload_commitment(bytes(row.salt), bytes(row.payload)))
        if commitment != entry.get("payload_commitment"):
            return ChainFailure(seq, "payload commitment mismatch")
        if chain_hash(previous, digest) != bytes(row.chain_hash):
            return ChainFailure(seq, "chain hash mismatch")
        return None

    async def proof(self, conn: AsyncConnection, *, seq: int, up_to_seq: int) -> ChainProof:
        """The chain value before `seq`, the entry, and the later entry hashes up to `up_to_seq`."""
        row = (
            await conn.execute(select(ledger_entry.c.entry).where(ledger_entry.c.seq == seq))
        ).first()
        if row is None:
            raise LookupError(f"no entry {seq}")
        if seq == 1:
            before = self.genesis
        else:
            before_row = (
                await conn.execute(
                    select(ledger_entry.c.chain_hash).where(ledger_entry.c.seq == seq - 1)
                )
            ).first()
            if before_row is None:
                raise LookupError(f"no entry {seq - 1}")
            before = bytes(before_row.chain_hash)
        later = (
            await conn.execute(
                select(ledger_entry.c.entry_hash)
                .where(ledger_entry.c.seq > seq, ledger_entry.c.seq <= up_to_seq)
                .order_by(ledger_entry.c.seq)
            )
        ).all()
        head_row = (
            await conn.execute(
                select(ledger_entry.c.chain_hash).where(ledger_entry.c.seq == up_to_seq)
            )
        ).first()
        if head_row is None:
            raise LookupError(f"no entry {up_to_seq}")
        return ChainProof(
            seq=seq,
            entry_bytes=bytes(row.entry),
            chain_before=before,
            later_entry_hashes=[bytes(r.entry_hash) for r in later],
            chain_head=bytes(head_row.chain_hash),
            up_to_seq=up_to_seq,
        )
