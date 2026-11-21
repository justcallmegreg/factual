"""The deep audit: nightly and after any restore, the whole ledger against its anchors.

It checks the whole chain against every seal, every seal signature and every
timestamp token, re-hashes every stored attachment from object storage at a
limited rate, warns about expiring TSA certificates and asks for tokens that a
seal still lacks. Each run appends `audit.completed`; a failure is posted in the
capture chat with the exact entry, seal or object.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.connectors.base import ChatRef, Connector, OutboundMessage
from app.core.clock import Clock, format_utc, system_clock
from app.core.logging import ctx
from app.ledger.anchors import ANCHOR_JOB, MAX_ATTEMPTS, SealAnchoring, host_of, public_url
from app.ledger.ledger import Ledger, iso_seconds
from app.ledger.seals import load_seals, stamped_bytes, verify_seals
from app.ledger.tsa import verify_token_with_openssl
from app.storage.blobs import ObjectStore, decrypt, unwrap_key
from app.storage.db import Database
from app.storage.jobs import enqueue
from app.storage.models import audit_run, blob, job, schedule_run, seal_anchor

log = logging.getLogger("factual.audit")

CERTIFICATE_WARNING_DAYS = 90
CHECKS = ["chain", "seals", "tokens", "attachments", "certificates"]


@dataclass(frozen=True)
class AuditFailure:
    kind: str  # entry | seal | token | object
    ref: str  # E41233 | seal 482 | seal 482 · https://tsa… | blobs/…@version
    reason: str

    def line(self) -> str:
        return f"{self.ref}: {self.reason}"


@dataclass
class AuditResult:
    ok: bool
    failures: list[AuditFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entries: int = 0
    seals: int = 0
    tokens: int = 0
    blobs: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    entry_seq: int | None = None


class DeepAudit:
    def __init__(
        self,
        ledger: Ledger,
        db: Database,
        store: ObjectStore | None,
        master_key: bytes | None,
        connector: Connector | None,
        capture_chat: ChatRef | None,
        *,
        public_key: bytes,
        clock: Clock | None = None,
        rehash_per_second: float = 2.0,
        anchoring: SealAnchoring | None = None,
        token_verifier=verify_token_with_openssl,
    ) -> None:
        self.ledger = ledger
        self.db = db
        self.store = store
        self.master_key = master_key
        self.connector = connector
        self.capture_chat = capture_chat
        self.public_key = public_key
        self.clock = clock or system_clock
        self.rehash_per_second = rehash_per_second
        self.anchoring = anchoring
        self.token_verifier = token_verifier

    async def run(self, reason: str = "nightly") -> AuditResult:
        result = AuditResult(ok=True, started_at=self.clock())
        async with self.db.engine.connect() as conn:
            chain = await self.ledger.verify_chain(conn)
            result.entries = chain.entries
            if not chain.ok and chain.failure is not None:
                result.failures.append(
                    AuditFailure("entry", f"E{chain.failure.seq}", chain.failure.reason)
                )
            seals = await verify_seals(conn, self.public_key, self.ledger.genesis)
            result.seals = seals.seals
            if not seals.ok and seals.failure is not None:
                result.failures.append(
                    AuditFailure("seal", f"seal {seals.failure.seal_no}", seals.failure.reason)
                )
            result.tokens = await self._check_tokens(conn, result)
            expiring = await self._check_certificates(conn, result)
        result.blobs = await self._rehash_attachments(result)
        if self.anchoring is not None:
            await self._request_missing_tokens(result)
            await self._renew_expiring(result, expiring)
        result.ok = not result.failures
        result.finished_at = self.clock()
        await self._record(result, reason)
        if not result.ok:
            await self._report(result)
        log.info(
            "deep audit finished",
            extra=ctx(
                action="audit.completed",
                entry=result.entry_seq,
                status="passed" if result.ok else "failed",
                count=len(result.failures),
            ),
        )
        return result

    async def _check_tokens(self, conn: AsyncConnection, result: AuditResult) -> int:
        """Every token verifies with openssl. A renewal (a token over an older token) is
        checked first; the token it renews is then checked at the renewal's time, so a
        token whose authority's certificate has since expired still counts."""
        seals = {s.no: s for s in await load_seals(conn)}
        anchors = (
            await conn.execute(
                select(seal_anchor)
                .where(seal_anchor.c.kind.in_(["tsa", "qualified_tsa"]))
                .order_by(seal_anchor.c.id.desc())
            )
        ).all()
        by_id = {int(anchor.id): anchor for anchor in anchors}
        renewed_at: dict[int, datetime] = {}  # anchor id → time of its verified renewal
        checked = 0
        for anchor in anchors:
            sealed = seals.get(int(anchor.seal_no))
            ref = f"seal {anchor.seal_no} · {anchor.issuer}"
            if anchor.renews is not None:
                ref += f" (renews #{anchor.renews})"
            if sealed is None:
                result.failures.append(AuditFailure("token", ref, "seal missing"))
                continue
            if anchor.renews is not None:
                old = by_id.get(int(anchor.renews))
                if old is None:
                    result.failures.append(AuditFailure("token", ref, "renewed token missing"))
                    continue
                data = bytes(old.proof)
            else:
                data = stamped_bytes(sealed.body, sealed.signature)
            checked += 1
            at = renewed_at.get(int(anchor.id))
            ok = await asyncio.to_thread(
                self.token_verifier,
                bytes(anchor.proof),
                data,
                bytes(anchor.chain_pem or b""),
                at=at,
            )
            if not ok:
                reason = "token does not verify"
                if at is not None:
                    reason += f" (checked at its renewal time {at:%Y-%m-%d})"
                result.failures.append(AuditFailure("token", ref, reason))
                continue
            if anchor.renews is not None and anchor.gen_time is not None:
                renewed_at[int(anchor.renews)] = anchor.gen_time
        return checked

    async def _check_certificates(
        self, conn: AsyncConnection, result: AuditResult
    ) -> list[tuple[str, datetime]]:
        """Warn about authority certificates that expire within 90 days and return them."""
        from cryptography import x509

        all_rows = (
            await conn.execute(
                select(seal_anchor.c.issuer, seal_anchor.c.chain_pem)
                .where(seal_anchor.c.chain_pem.is_not(None))
                .order_by(seal_anchor.c.id.desc())
            )
        ).all()
        latest: dict[str, bytes] = {}
        for issuer, chain_pem in all_rows:
            latest.setdefault(issuer, chain_pem)  # the newest chain per authority
        now = self.clock()
        expiring: list[tuple[str, datetime]] = []
        for issuer, chain_pem in latest.items():
            try:
                certs = x509.load_pem_x509_certificates(bytes(chain_pem))
            except ValueError:
                result.warnings.append(f"{host_of(issuer)}: certificate chain unreadable")
                continue
            if not certs:
                continue
            after = min(
                getattr(c, "not_valid_after_utc", None) or c.not_valid_after.replace(tzinfo=UTC)
                for c in certs
            )
            if after < now:
                result.warnings.append(
                    f"{host_of(issuer)}: TSA certificate expired on {after:%Y-%m-%d}"
                )
                expiring.append((issuer, after))
            elif after < now + timedelta(days=CERTIFICATE_WARNING_DAYS):
                result.warnings.append(
                    f"{host_of(issuer)}: TSA certificate expires on {after:%Y-%m-%d}"
                )
                expiring.append((issuer, after))
        return expiring

    async def _renew_expiring(
        self, result: AuditResult, expiring: list[tuple[str, datetime]]
    ) -> None:
        """One renewal per token of an expiring authority, from every other authority."""
        if not expiring or self.anchoring is None:
            return
        async with self.db.engine.begin() as conn:
            anchors = (
                await conn.execute(
                    select(seal_anchor).where(seal_anchor.c.kind.in_(["tsa", "qualified_tsa"]))
                )
            ).all()
            renewed = {(int(a.renews), a.issuer) for a in anchors if a.renews is not None}
            pending = (
                await conn.execute(
                    select(job.c.payload).where(
                        job.c.kind == ANCHOR_JOB, job.c.status.in_(["queued", "running"])
                    )
                )
            ).all()
            queued = {
                (int(p.payload["renews"]), p.payload.get("url"))
                for p in pending
                if p.payload.get("renews") is not None
            }
            now = self.clock()
            requested = 0
            for issuer, after in expiring:
                targets = [u for u in self.anchoring.urls if public_url(u) != issuer]
                targets = targets or list(self.anchoring.urls)
                for anchor in anchors:
                    if anchor.issuer != issuer or anchor.renews is not None:
                        continue
                    for url in targets:
                        key = (int(anchor.id), public_url(url))
                        if key in renewed or (int(anchor.id), url) in queued:
                            continue
                        await self.anchoring.renew(conn, anchor, url, now)
                        requested += 1
                if requested:
                    result.warnings.append(
                        f"{requested} token(s) sent for renewal before {host_of(issuer)}'s "
                        f"certificate expires on {after:%Y-%m-%d}"
                    )

    async def _rehash_attachments(self, result: AuditResult) -> int:
        if self.store is None:
            return 0
        async with self.db.engine.connect() as conn:
            rows = (await conn.execute(select(blob).order_by(blob.c.seq))).all()
        pause = 1.0 / self.rehash_per_second if self.rehash_per_second > 0 else 0.0
        checked = 0
        for index, row in enumerate(rows):
            if index and pause:
                await asyncio.sleep(pause)
            ref = f"{row.object_key}@{row.version_id}"
            try:
                stored = await self.store.get(row.object_key, row.version_id)
            except Exception as exc:  # noqa: BLE001 — a missing object is a finding
                result.failures.append(
                    AuditFailure("object", ref, f"cannot fetch: {type(exc).__name__}")
                )
                continue
            checked += 1
            if hashlib.sha256(stored).digest() != bytes(row.sha256_stored):
                result.failures.append(AuditFailure("object", ref, "stored bytes changed"))
                continue
            if self.master_key is not None:
                aad = row.aad.encode()
                try:
                    plain = decrypt(
                        stored, unwrap_key(self.master_key, bytes(row.wrapped_key), aad), aad
                    )
                except ValueError as exc:
                    result.failures.append(AuditFailure("object", ref, f"cannot decrypt: {exc}"))
                    continue
                if hashlib.sha256(plain).digest() != bytes(row.sha256_plain):
                    result.failures.append(AuditFailure("object", ref, "decrypted content changed"))
        return checked

    async def _request_missing_tokens(self, result: AuditResult) -> None:
        async with self.db.engine.begin() as conn:
            seals = await load_seals(conn)
            have = {
                (int(r.seal_no), r.issuer)
                for r in (
                    await conn.execute(
                        select(seal_anchor.c.seal_no, seal_anchor.c.issuer).where(
                            seal_anchor.c.kind == "tsa", seal_anchor.c.renews.is_(None)
                        )
                    )
                ).all()
            }
            pending = (
                await conn.execute(
                    select(job.c.payload).where(
                        job.c.kind == ANCHOR_JOB, job.c.status.in_(["queued", "running"])
                    )
                )
            ).all()
            queued = {
                (int(p.payload["seal_no"]), p.payload.get("url"))
                for p in pending
                if p.payload.get("renews") is None
            }
            now = self.clock()
            requested = 0
            for sealed in seals:
                for url in self.anchoring.urls:
                    if (sealed.no, url) in have or (sealed.no, url) in queued:
                        continue
                    await enqueue(
                        conn,
                        ANCHOR_JOB,
                        {"seal_no": sealed.no, "url": url, "kind": "tsa"},
                        now=now,
                        max_attempts=MAX_ATTEMPTS,
                    )
                    requested += 1
        if requested:
            result.warnings.append(f"{requested} missing timestamp(s) requested again")

    async def _record(self, result: AuditResult, reason: str) -> None:
        payload = {
            "kind": "deep",
            "reason": reason,
            "started_at": iso_seconds(result.started_at),
            "finished_at": iso_seconds(result.finished_at),
            "checks": CHECKS,
            "ok": result.ok,
            "entries": result.entries,
            "seals": result.seals,
            "tokens": result.tokens,
            "blobs": result.blobs,
            "failures": [
                {"kind": f.kind, "ref": f.ref, "reason": f.reason} for f in result.failures
            ],
            "warnings": result.warnings,
        }
        async with self.db.engine.begin() as conn:
            appended = await self.ledger.append(
                conn,
                type="audit.completed",
                source={"connector": "factual", "chat_id": 0},
                source_event=f"audit:{iso_seconds(result.started_at)}",
                observed_at=result.finished_at,
                payload=payload,
            )
            if not appended.duplicate:
                await conn.execute(
                    insert(audit_run).values(
                        seq=appended.seq,
                        kind="deep",
                        reason=reason,
                        started_at=result.started_at,
                        finished_at=result.finished_at,
                        ok=result.ok,
                        failures=len(result.failures),
                        summary=summary_line(result),
                    )
                )
        result.entry_seq = appended.seq

    async def _report(self, result: AuditResult) -> None:
        problems = "; ".join(f.line() for f in result.failures[:10])
        text = f"Deep audit FAILED ({len(result.failures)} problem(s)): {problems}"
        if len(result.failures) > 10:
            text += f"; and {len(result.failures) - 10} more"
        if self.connector is None or self.capture_chat is None:
            log.critical(text, extra=ctx(action="audit.failed", count=len(result.failures)))
            return
        try:
            await self.connector.send(self.capture_chat, OutboundMessage(text))
        except Exception as exc:  # noqa: BLE001 — the log still carries it
            log.critical("%s (chat notice failed: %s)", text, type(exc).__name__)


def summary_line(result: AuditResult) -> str:
    when = format_utc(result.finished_at) if result.finished_at else "?"
    if result.ok:
        return (
            f"passed at {when} ({result.entries} entries, {result.seals} seals, "
            f"{result.tokens} tokens, {result.blobs} files)"
        )
    first = result.failures[0].line() if result.failures else "unknown"
    return f"FAILED at {when}: {first}" + (
        f" and {len(result.failures) - 1} more" if len(result.failures) > 1 else ""
    )


async def last_audit_line(conn: AsyncConnection) -> str:
    row = (await conn.execute(select(audit_run).order_by(audit_run.c.seq.desc()).limit(1))).first()
    return row.summary if row is not None else "not yet run"


async def last_audit_info(conn: AsyncConnection) -> dict | None:
    row = (await conn.execute(select(audit_run).order_by(audit_run.c.seq.desc()).limit(1))).first()
    if row is None:
        return None
    return {
        "ok": bool(row.ok),
        "at": row.finished_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "failures": int(row.failures),
        "summary": row.summary,
        "entry_seq": int(row.seq),
    }


class NightlyAudit:
    """The scheduled job: once per night between 03:00 and 06:00 local time."""

    name = "nightly_audit"

    def __init__(
        self,
        audit: DeepAudit,
        db: Database,
        *,
        timezone: str,
        at: str = "03:00",
        until: str = "06:00",
        clock: Clock | None = None,
    ) -> None:
        self.audit = audit
        self.db = db
        self.zone = ZoneInfo(timezone)
        self.at = tuple(int(p) for p in at.split(":"))
        self.until = tuple(int(p) for p in until.split(":"))
        self.clock = clock or system_clock

    def slot_for(self, now: datetime) -> datetime | None:
        local = now.astimezone(self.zone)
        start = local.replace(hour=self.at[0], minute=self.at[1], second=0, microsecond=0)
        end = local.replace(hour=self.until[0], minute=self.until[1], second=0, microsecond=0)
        return start.astimezone(UTC) if start <= local < end else None

    async def tick(self) -> bool:
        now = self.clock()
        slot = self.slot_for(now)
        if slot is None:
            return False
        async with self.db.engine.connect() as conn:
            done = (
                await conn.execute(
                    select(schedule_run.c.id).where(
                        schedule_run.c.job == self.name, schedule_run.c.scheduled_for == slot
                    )
                )
            ).first()
        if done is not None:
            return False
        result = await self.audit.run(reason="nightly")
        async with self.db.engine.begin() as conn:
            await conn.execute(
                insert(schedule_run).values(
                    job=self.name,
                    scheduled_for=slot,
                    ran_at=now,
                    status="passed" if result.ok else "failed",
                    detail=summary_line(result),
                )
            )
        return True
