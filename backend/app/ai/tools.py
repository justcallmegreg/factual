"""Read-only tools Claude may call while it investigates. Results are JSON data."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.clock import format_local
from app.records.search import search
from app.storage.db import Database
from app.storage.models import attachment, derived_text, record, record_version

TOOLS: list[dict] = [
    {
        "name": "search_records",
        "description": (
            "Full-text search over all records (text, captions, derived text). "
            "Returns the best matching records with their entry numbers."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {
                    "type": ["string", "null"],
                    "description": "limit to one project, or null",
                },
                "limit": {"type": "integer", "description": "1 to 10"},
            },
            "required": ["query", "project", "limit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_context",
        "description": "The records recorded just before and after one record, in the same chat.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "seq": {"type": "integer"},
                "before": {"type": "integer", "description": "0 to 5"},
                "after": {"type": "integer", "description": "0 to 5"},
            },
            "required": ["seq", "before", "after"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_versions",
        "description": "All versions of one record: the original text and every edit, with times.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"seq": {"type": "integer"}},
            "required": ["seq"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_attachment_text",
        "description": "The files attached to a record and the text read from them, if any.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"seq": {"type": "integer"}},
            "required": ["seq"],
            "additionalProperties": False,
        },
    },
]


class UnknownTool(LookupError):
    pass


def _when(moment: datetime, zone: ZoneInfo) -> str:
    return format_local(moment, zone)


def record_view(row, zone: ZoneInfo) -> dict:
    """What Claude sees of a record: labelled fields, the text as data."""
    origin = "forwarded message" if row.origin == "forwarded" else "note by a member"
    view = {
        "seq": int(row.seq),
        "project": row.project,
        "origin": origin,
        "author": row.author_name,
        "recorded_at": _when(row.observed_at, zone),
        "text": row.text,
        "caption": row.caption,
        "media": list(row.media_kinds or []),
        "edited": int(row.version) > 1,
    }
    if row.origin == "forwarded":
        view["forwarded_from"] = row.forward_sender_name
        view["forwarded_sent_at"] = (
            _when(row.forward_sent_at, zone) if row.forward_sent_at else None
        )
    if row.stated_at is not None:
        view["stated_event_time"] = _when(row.stated_at, zone)
    return view


class ToolExecutor:
    """Runs one tool call against the database. Remembers every record it showed."""

    def __init__(self, db: Database, *, timezone: str, verifier=None) -> None:
        self.db = db
        self.zone = ZoneInfo(timezone)
        self.verifier = verifier
        self.seen: set[int] = set()

    async def run(self, name: str, arguments: dict) -> str:
        handler = {
            "search_records": self._search,
            "get_context": self._context,
            "get_versions": self._versions,
            "get_attachment_text": self._attachment_text,
        }.get(name)
        if handler is None:
            raise UnknownTool(name)
        async with self.db.engine.connect() as conn:
            result = await handler(conn, arguments)
        return json.dumps(result, ensure_ascii=False, default=str)

    async def _search(self, conn: AsyncConnection, arguments: dict) -> dict:
        query = str(arguments.get("query", ""))
        project = arguments.get("project")
        if project:
            query = f"{project} {query}"
        limit = int(arguments.get("limit") or 5)
        result = await search(conn, query, limit=max(1, min(limit, 10)), verifier=self.verifier)
        seqs = [hit.seq for hit in result.hits]
        rows = (
            (await conn.execute(select(record).where(record.c.seq.in_(seqs)))).all() if seqs else []
        )
        by_seq = {int(r.seq): r for r in rows}
        hits = [record_view(by_seq[seq], self.zone) for seq in seqs if seq in by_seq]
        self.seen.update(seqs)
        return {"hits": hits, "words": result.words}

    async def _context(self, conn: AsyncConnection, arguments: dict) -> dict:
        seq = int(arguments["seq"])
        before = max(0, min(int(arguments.get("before", 2)), 5))
        after = max(0, min(int(arguments.get("after", 2)), 5))
        anchor = (await conn.execute(select(record).where(record.c.seq == seq))).first()
        if anchor is None:
            return {"error": f"no record E{seq}"}
        earlier = (
            await conn.execute(
                select(record)
                .where(record.c.chat_id == anchor.chat_id, record.c.seq < seq)
                .order_by(record.c.seq.desc())
                .limit(before)
            )
        ).all()
        later = (
            await conn.execute(
                select(record)
                .where(record.c.chat_id == anchor.chat_id, record.c.seq > seq)
                .order_by(record.c.seq)
                .limit(after)
            )
        ).all()
        rows = list(reversed(earlier)) + [anchor] + list(later)
        self.seen.update(int(r.seq) for r in rows)
        return {"records": [record_view(r, self.zone) for r in rows]}

    async def _versions(self, conn: AsyncConnection, arguments: dict) -> dict:
        seq = int(arguments["seq"])
        rows = (
            await conn.execute(
                select(record_version)
                .where(record_version.c.record_seq == seq)
                .order_by(record_version.c.version)
            )
        ).all()
        if not rows:
            return {"error": f"no record E{seq}"}
        self.seen.add(seq)
        return {
            "seq": seq,
            "versions": [
                {
                    "version": int(r.version),
                    "entry_seq": int(r.seq),
                    "at": _when(r.observed_at, self.zone),
                    "text": r.text,
                    "caption": r.caption,
                }
                for r in rows
            ],
        }

    async def _attachment_text(self, conn: AsyncConnection, arguments: dict) -> dict:
        seq = int(arguments["seq"])
        rows = (await conn.execute(select(attachment).where(attachment.c.record_seq == seq))).all()
        derived = (
            await conn.execute(select(derived_text).where(derived_text.c.record_seq == seq))
        ).all()
        by_blob = {int(d.blob_seq): d for d in derived}
        self.seen.add(seq)
        files = []
        for r in rows:
            d = by_blob.get(int(r.entry_seq)) if r.entry_seq is not None else None
            done = d is not None and d.status == "done"
            files.append(
                {
                    "file_name": r.file_name,
                    "kind": r.kind,
                    "mime": r.mime,
                    "size_bytes": r.size_bytes,
                    "status": r.status,
                    "derived_text": d.text if done else None,
                    "derived_from": d.model if done else None,
                    "derived_status": d.status if d is not None else None,
                }
            )
        return {"seq": seq, "attachments": files}
