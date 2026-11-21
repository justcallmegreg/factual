"""Evidence packs: everything an outsider needs to verify selected records offline."""

from __future__ import annotations

import io
import json
import mimetypes
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.clock import format_local
from app.ledger.anchors import host_of
from app.ledger.keys import key_fingerprint_of_pem
from app.ledger.ledger import Ledger
from app.ledger.pack_readme import RELATION, readme
from app.ledger.seals import covering_seal
from app.storage.blobs import ObjectStore, decrypt, unwrap_key
from app.storage.db import Database
from app.storage.models import (
    attachment,
    blob,
    evidence_payload,
    filing,
    ledger_entry,
    record,
    record_version,
    seal_anchor,
)

VERIFY_SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "verify.py"


@dataclass(frozen=True)
class Pack:
    filename: str
    data: bytes
    records: list[int]
    unsealed: list[int] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.data)


class PackBuilder:
    def __init__(
        self,
        db: Database,
        ledger: Ledger,
        *,
        public_key_pem: bytes,
        store: ObjectStore | None,
        master_key: bytes | None,
        timezone: str,
        verify_source: Path = VERIFY_SOURCE,
    ) -> None:
        self.db = db
        self.ledger = ledger
        self.public_key_pem = public_key_pem
        self.store = store
        self.master_key = master_key
        self.zone = ZoneInfo(timezone)
        self.verify_source = verify_source

    async def _related(self, conn: AsyncConnection, seq: int) -> list[int]:
        """Entries whose refs name the record: edits, filings, attachments, album items."""
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT e.seq FROM ledger_entry e"
                        " LEFT JOIN evidence_payload p ON p.seq = e.seq"
                        " WHERE (convert_from(e.entry, 'UTF8')::jsonb -> 'refs')"
                        "       @> to_jsonb(ARRAY[:seq]::bigint[])"
                        "   AND (e.type IN ('record.edited', 'record.filed',"
                        "                  'attachment.stored', 'attachment.unavailable')"
                        "        OR (e.type = 'record.created' AND p.payload IS NOT NULL"
                        "            AND convert_from(p.payload, 'UTF8')::jsonb"
                        "                #> '{normalized,album_of}'"
                        "                IS NOT NULL))"
                        " ORDER BY e.seq"
                    ),
                    {"seq": seq},
                )
            )
            .scalars()
            .all()
        )
        return [int(s) for s in rows]

    async def build(self, seqs: list[int]) -> Pack:
        requested = sorted(set(int(s) for s in seqs))
        files: dict[str, bytes] = {}
        manifest: dict = {
            "v": 1,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "key": "keys/instance-ed25519.pub",
            "key_fingerprint": key_fingerprint_of_pem(self.public_key_pem),
            "records": requested,
            "entries": [],
            "proofs": [],
            "seals": [],
            "attachments": [],
            "unsealed": [],
            "record_summaries": [],
            "related": [],
            "authorities": [],
        }
        async with self.db.engine.connect() as conn:
            for seq in requested:
                exists = (
                    await conn.execute(select(ledger_entry.c.seq).where(ledger_entry.c.seq == seq))
                ).first()
                if exists is None:
                    raise LookupError(seq)
            disclosed: list[int] = []
            relation_of: dict[int, int] = {}
            for seq in requested:
                disclosed.append(seq)
                for related_seq in await self._related(conn, seq):
                    disclosed.append(related_seq)
                    relation_of.setdefault(related_seq, seq)
            disclosed = sorted(set(disclosed))
            head_seq, _ = await self.ledger.head(conn)
            seals_used: dict[int, dict] = {}
            summaries: list[str] = []
            for seq in disclosed:
                row = (
                    await conn.execute(
                        select(
                            ledger_entry.c.entry,
                            ledger_entry.c.type,
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
                files[f"entries/{seq}.json"] = bytes(row.entry)
                item = {
                    "seq": seq,
                    "type": row.type,
                    "file": f"entries/{seq}.json",
                    "payload": None,
                }
                if seq in relation_of:
                    manifest["related"].append(
                        {
                            "seq": seq,
                            "type": row.type,
                            "of": relation_of[seq],
                            "relation": RELATION.get(row.type, "belongs to"),
                        }
                    )
                if row.payload is not None:
                    files[f"payloads/{seq}.json"] = json.dumps(
                        {"salt": bytes(row.salt).hex(), "payload": json.loads(bytes(row.payload))},
                        indent=2,
                        ensure_ascii=False,
                    ).encode("utf-8")
                    item["payload"] = f"payloads/{seq}.json"
                manifest["entries"].append(item)
                sealed = await covering_seal(conn, seq)
                up_to = sealed.up_to_seq if sealed is not None else head_seq
                proof = await self.ledger.proof(conn, seq=seq, up_to_seq=up_to)
                proof_json = proof.to_json()
                proof_json.pop("entry", None)  # the entry is in entries/, listed once
                proof_json["seal_no"] = sealed.no if sealed is not None else None
                name = (
                    f"proofs/{seq}-in-{sealed.no}.json"
                    if sealed is not None
                    else f"proofs/{seq}-unsealed.json"
                )
                files[name] = json.dumps(proof_json, indent=2).encode("utf-8")
                manifest["proofs"].append(
                    {"seq": seq, "file": name, "seal_no": sealed.no if sealed is not None else None}
                )
                if sealed is None:
                    manifest["unsealed"].append(seq)
                elif sealed.no not in seals_used:
                    anchors = (
                        await conn.execute(
                            select(seal_anchor)
                            .where(seal_anchor.c.seal_no == sealed.no, seal_anchor.c.kind == "tsa")
                            .order_by(seal_anchor.c.id)
                        )
                    ).all()
                    files[f"seals/{sealed.no}.json"] = sealed.body
                    files[f"seals/{sealed.no}.sig"] = sealed.signature
                    files[f"seals/{sealed.no}.stamped"] = sealed.body + sealed.signature
                    seal_item = {
                        "no": sealed.no,
                        "up_to_seq": sealed.up_to_seq,
                        "created_at": sealed.fields.get("created_at"),
                        "body": f"seals/{sealed.no}.json",
                        "signature": f"seals/{sealed.no}.sig",
                        "stamped": f"seals/{sealed.no}.stamped",
                        "anchors": [],
                    }
                    for n, anchor in enumerate(anchors, start=1):
                        manifest["authorities"].append(
                            describe_authority(
                                sealed.no,
                                n,
                                anchor.issuer,
                                bytes(anchor.chain_pem or b""),
                                anchor.gen_time,
                            )
                        )
                        token_name = f"seals/{sealed.no}-{n}.tsr"
                        chain_name = (
                            f"tsa/chain-{n}.pem"
                            if len(seals_used) == 0
                            else f"tsa/chain-{sealed.no}-{n}.pem"
                        )
                        files[token_name] = bytes(anchor.proof)
                        files[chain_name] = bytes(anchor.chain_pem or b"")
                        seal_item["anchors"].append(
                            {
                                "issuer": host_of(anchor.issuer),
                                "url": anchor.issuer,
                                "token": token_name,
                                "chain": chain_name,
                                "gen_time": (
                                    anchor.gen_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                                    if anchor.gen_time
                                    else None
                                ),
                            }
                        )
                    seals_used[sealed.no] = seal_item
                    manifest["seals"].append(seal_item)
            # attachments of the requested records (and of their album items)
            for seq in requested:
                rows = (
                    await conn.execute(
                        select(attachment).where(
                            attachment.c.record_seq == seq, attachment.c.status == "stored"
                        )
                    )
                ).all()
                for att in rows:
                    blob_row = (
                        await conn.execute(select(blob).where(blob.c.seq == att.entry_seq))
                    ).first()
                    if blob_row is None or self.store is None or self.master_key is None:
                        continue
                    stored = await self.store.get(blob_row.object_key, blob_row.version_id)
                    aad = blob_row.aad.encode()
                    file_key = unwrap_key(self.master_key, bytes(blob_row.wrapped_key), aad)
                    plain = decrypt(stored, file_key, aad)
                    ext = (
                        Path(att.file_name).suffix
                        if att.file_name
                        else (mimetypes.guess_extension(blob_row.mime or "") or ".bin")
                    )
                    name = f"blobs/{bytes(blob_row.sha256_plain).hex()}{ext}"
                    files[name] = plain
                    manifest["attachments"].append(
                        {
                            "entry_seq": int(att.entry_seq),
                            "record_seq": seq,
                            "file": name,
                            "file_name": att.file_name,
                            "mime": blob_row.mime,
                            "sha256_plain": bytes(blob_row.sha256_plain).hex(),
                        }
                    )
            for seq in requested:
                rec = (await conn.execute(select(record).where(record.c.seq == seq))).first()
                summary: dict = {"seq": seq}
                if rec is not None:
                    who = (
                        f"forwarded from {rec.forward_sender_name}"
                        if rec.origin == "forwarded" and rec.forward_sender_name
                        else f"note by {rec.author_name or 'a member'}"
                    )
                    text_shown = (rec.text or rec.caption or "").strip().replace("\n", " ")
                    if len(text_shown) > 160:
                        text_shown = text_shown[:159] + "…"
                    edits = (
                        await conn.execute(
                            select(record_version)
                            .where(record_version.c.record_seq == seq, record_version.c.version > 1)
                            .order_by(record_version.c.version)
                        )
                    ).all()
                    filings = (
                        await conn.execute(
                            select(filing).where(filing.c.record_seq == seq).order_by(filing.c.seq)
                        )
                    ).all()
                    summary.update(
                        {
                            "project": rec.project,
                            "who": who,
                            "text": text_shown,
                            "recorded_at": format_local(rec.observed_at, self.zone),
                            "recorded_at_utc": rec.observed_at.astimezone(UTC).strftime(
                                "%Y-%m-%dT%H:%M:%SZ"
                            ),
                            "edits": [
                                {"seq": int(e.seq), "at": format_local(e.observed_at, self.zone)}
                                for e in edits
                            ],
                            "filings": [
                                {
                                    "seq": int(f.seq),
                                    "project": f.project,
                                    "at": format_local(f.filed_at, self.zone),
                                }
                                for f in filings
                            ],
                            "attachments": [
                                a for a in manifest["attachments"] if a["record_seq"] == seq
                            ],
                        }
                    )
                    summaries.append(
                        f"E{seq} · {rec.project or 'no project'} · {who} · recorded "
                        f"{format_local(rec.observed_at, self.zone)}"
                    )
                else:
                    entry_type = next(
                        (e["type"] for e in manifest["entries"] if e["seq"] == seq), "entry"
                    )
                    summary.update({"type": entry_type})
                    summaries.append(f"E{seq} · ledger entry ({entry_type})")
                manifest["record_summaries"].append(summary)
        files["keys/instance-ed25519.pub"] = self.public_key_pem
        files["verify.py"] = self.verify_source.read_bytes()
        files["manifest.json"] = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
        files["README.txt"] = readme(manifest).encode("utf-8")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in sorted(files):
                zf.writestr(name, files[name])
        filename = "proof-" + "-".join(f"E{seq}" for seq in requested) + ".zip"
        return Pack(filename, buffer.getvalue(), requested, manifest["unsealed"])


def describe_authority(seal_no: int, n: int, url: str, chain_pem: bytes, gen_time) -> dict:
    """Who signed a timestamp: certificate subject, issuer and validity, plus a test warning."""
    from cryptography import x509

    subject = issuer = valid_from = valid_to = None
    try:
        certs = x509.load_pem_x509_certificates(chain_pem) if chain_pem else []
    except ValueError:
        certs = []
    if certs:
        cert = certs[0]
        subject = cert.subject.rfc4514_string()
        issuer = cert.issuer.rfc4514_string()
        before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
        after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
        valid_from, valid_to = before.strftime("%Y-%m-%d"), after.strftime("%Y-%m-%d")
    host = host_of(url)
    test = (
        host in ("127.0.0.1", "localhost")
        or (subject is not None and subject == issuer)
        or bool(subject and "test" in subject.lower())
    )
    return {
        "seal_no": seal_no,
        "token": f"seals/{seal_no}-{n}.tsr",
        "chain": f"tsa/chain-{n}.pem"
        if seal_no == min(seal_no, seal_no)
        else f"tsa/chain-{seal_no}-{n}.pem",
        "url": url,
        "host": host,
        "subject": subject,
        "issuer": issuer,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "gen_time": gen_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if gen_time else None,
        "test": test,
    }
