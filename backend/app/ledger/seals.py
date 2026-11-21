"""Seals: every interval, a signed commitment to every entry so far.

A seal names the last entry, carries the chain head and the hash of the previous
seal, and is signed with the instance's Ed25519 key. Trusted timestamps on the
seals (the anchors) are added by hooks after each seal.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.clock import Clock, system_clock
from app.core.logging import ctx
from app.ledger.canonical import canonicalize
from app.ledger.keys import key_fingerprint
from app.ledger.ledger import LEDGER_LOCK_KEY, Ledger, iso_seconds
from app.storage.db import Database
from app.storage.models import ledger_entry
from app.storage.models import seal as seal_table

log = logging.getLogger("factual.sealer")

SEAL_TAG = b"ledger-seal-v1"
SEAL_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Seal:
    no: int
    up_to_seq: int
    body: bytes
    signature: bytes
    key_id: str
    created_at: datetime

    @property
    def fields(self) -> dict:
        return json.loads(self.body)


def seal_hash(body: bytes) -> bytes:
    """SHA-256("ledger-seal-v1" ‖ canonical seal): what the next seal's prev_seal names."""
    return hashlib.sha256(SEAL_TAG + body).digest()


def stamped_bytes(body: bytes, signature: bytes) -> bytes:
    """The bytes a time-stamping authority stamps: seal ‖ signature.

    The TSA request carries SHA-256 of these bytes; `openssl ts -verify -data` hashes
    the same bytes again, so the pack stores them as `seals/<no>.stamped`.
    """
    return body + signature


def sign_seal(key: Ed25519PrivateKey, body: bytes) -> bytes:
    return key.sign(body)


def verify_seal_signature(body: bytes, signature: bytes, public_key: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, body)
    except (InvalidSignature, ValueError):
        return False
    return True


def genesis_seal_hash(genesis: bytes) -> bytes:
    return hashlib.sha256(genesis).digest()


SealHook = Callable[[Seal], Awaitable[None]]


class Sealer:
    def __init__(
        self,
        ledger: Ledger,
        db: Database,
        signing_key: Ed25519PrivateKey,
        *,
        interval: float,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = ledger
        self.db = db
        self.key = signing_key
        self.key_id = key_fingerprint(signing_key)
        self.interval = interval
        self.clock = clock or system_clock
        self.after_seal: list[SealHook] = []
        self._stopping = False

    async def seal_once(self) -> Seal | None:
        """Write one seal when entries exist beyond the last seal; None otherwise."""
        async with self.db.engine.begin() as conn:
            await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LEDGER_LOCK_KEY})
            head_seq, head_hash = await self.ledger.head(conn)
            last = (
                await conn.execute(select(seal_table).order_by(seal_table.c.no.desc()).limit(1))
            ).first()
            if head_seq == 0 or (last is not None and head_seq <= int(last.up_to_seq)):
                return None
            no = int(last.no) + 1 if last is not None else 1
            prev = (
                seal_hash(bytes(last.body))
                if last is not None
                else genesis_seal_hash(self.ledger.genesis)
            )
            created = self.clock()
            body = canonicalize(
                {
                    "v": SEAL_FORMAT_VERSION,
                    "no": no,
                    "up_to_seq": head_seq,
                    "chain_head": "sha256:" + head_hash.hex(),
                    "prev_seal": "sha256:" + prev.hex(),
                    "created_at": iso_seconds(created),
                    "key_id": self.key_id,
                }
            )
            signature = sign_seal(self.key, body)
            await conn.execute(
                insert(seal_table).values(
                    no=no,
                    up_to_seq=head_seq,
                    body=body,
                    signature=signature,
                    key_id=self.key_id,
                    created_at=created,
                )
            )
            sealed = Seal(no, head_seq, body, signature, self.key_id, created)
        log.info("seal written", extra=ctx(action="seal.created", seal=no, entry=head_seq))
        for hook in self.after_seal:
            try:
                await hook(sealed)
            except Exception as exc:  # noqa: BLE001 — anchors retry on their own
                log.warning("seal hook failed: %s", type(exc).__name__, extra=ctx(seal=no))
        return sealed

    async def start(self) -> None:
        self._stopping = False
        while not self._stopping:
            try:
                await self.seal_once()
            except Exception as exc:  # noqa: BLE001 — try again next interval
                log.error("sealing failed: %s", type(exc).__name__, extra=ctx(action="seal.failed"))
            slept = 0.0
            while slept < self.interval and not self._stopping:
                step = min(0.5, self.interval - slept)
                await asyncio.sleep(step)
                slept += step

    async def stop(self) -> None:
        self._stopping = True


@dataclass(frozen=True)
class SealFailure:
    seal_no: int
    reason: str


@dataclass(frozen=True)
class SealReport:
    ok: bool
    seals: int
    failure: SealFailure | None = None


async def load_seals(conn: AsyncConnection) -> list[Seal]:
    rows = (await conn.execute(select(seal_table).order_by(seal_table.c.no))).all()
    return [
        Seal(int(r.no), int(r.up_to_seq), bytes(r.body), bytes(r.signature), r.key_id, r.created_at)
        for r in rows
    ]


async def covering_seal(conn: AsyncConnection, seq: int) -> Seal | None:
    """The first seal that covers an entry."""
    row = (
        await conn.execute(
            select(seal_table)
            .where(seal_table.c.up_to_seq >= seq)
            .order_by(seal_table.c.no)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return Seal(
        int(row.no),
        int(row.up_to_seq),
        bytes(row.body),
        bytes(row.signature),
        row.key_id,
        row.created_at,
    )


async def verify_seals(conn: AsyncConnection, public_key: bytes, genesis: bytes) -> SealReport:
    """Every seal: numbered without gaps, chained, signed, and matching the ledger's chain."""
    seals = await load_seals(conn)
    previous_hash = genesis_seal_hash(genesis)
    previous_up_to = 0
    for expected_no, sealed in enumerate(seals, start=1):
        if sealed.no != expected_no:
            return SealReport(
                False, len(seals), SealFailure(sealed.no, f"gap: expected seal {expected_no}")
            )
        try:
            fields = json.loads(sealed.body)
        except ValueError:
            return SealReport(False, len(seals), SealFailure(sealed.no, "body is not JSON"))
        if canonicalize(fields) != sealed.body or fields.get("no") != sealed.no:
            return SealReport(False, len(seals), SealFailure(sealed.no, "body is not canonical"))
        if fields.get("prev_seal") != "sha256:" + previous_hash.hex():
            return SealReport(False, len(seals), SealFailure(sealed.no, "prev_seal mismatch"))
        if not verify_seal_signature(sealed.body, sealed.signature, public_key):
            return SealReport(False, len(seals), SealFailure(sealed.no, "signature invalid"))
        if fields.get("up_to_seq") != sealed.up_to_seq or sealed.up_to_seq <= previous_up_to:
            return SealReport(
                False, len(seals), SealFailure(sealed.no, "up_to_seq does not advance")
            )
        head = (
            await conn.execute(
                select(ledger_entry.c.chain_hash).where(ledger_entry.c.seq == sealed.up_to_seq)
            )
        ).first()
        if head is None or fields.get("chain_head") != "sha256:" + bytes(head.chain_hash).hex():
            return SealReport(False, len(seals), SealFailure(sealed.no, "chain head mismatch"))
        previous_hash = seal_hash(sealed.body)
        previous_up_to = sealed.up_to_seq
    return SealReport(True, len(seals))
