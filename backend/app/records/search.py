"""Full-text search over record text, captions and derived text (PostgreSQL, `english`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.ledger.verify import Status, fast_status

WORD = re.compile(r"\w[\w'-]*")
PROJECT_BOOST = 10.0


@dataclass(frozen=True)
class Hit:
    seq: int
    project: str | None
    kind: str  # text | caption | derived
    content: str
    source: str | None
    origin: str
    author_name: str | None
    forward_sender_name: str | None
    forward_sent_at: datetime | None
    observed_at: datetime
    media_kinds: list[str]
    file_name: str | None
    rank: float
    status: Status


@dataclass(frozen=True)
class SearchResult:
    hits: list[Hit]
    words: list[str]  # the words that were searched for, in their original form
    project: str | None  # a project the question named


def question_words(question: str) -> list[str]:
    return WORD.findall(question)


async def searched_words(conn: AsyncConnection, question: str) -> list[str]:
    """The question's words without stop words, as typed."""
    words = question_words(question)
    if not words:
        return []
    rows = (
        await conn.execute(
            text(
                "SELECT w, ts_lexize('english_stem', lower(w)) AS lex"
                " FROM unnest(CAST(:words AS text[])) WITH ORDINALITY AS t(w, n) ORDER BY n"
            ),
            {"words": words},
        )
    ).all()
    return [row.w for row in rows if row.lex]


async def named_project(conn: AsyncConnection, words: list[str]) -> str | None:
    if not words:
        return None
    row = (
        await conn.execute(
            text(
                "SELECT name FROM project WHERE name = ANY(CAST(:names AS text[]))"
                " ORDER BY array_position(CAST(:names AS text[]), name) LIMIT 1"
            ),
            {"names": [w.lower() for w in words]},
        )
    ).first()
    return row.name if row else None


async def search(
    conn: AsyncConnection, question: str, *, limit: int = 5, verifier=None
) -> SearchResult:
    """The best matches: one per record, ranked, the named project's records first."""
    words = await searched_words(conn, question)
    project = await named_project(conn, question_words(question))
    search_words = [w for w in words if w.lower() != project] if project else words
    if not search_words:
        return SearchResult([], search_words, project)
    lexemes = (
        (
            await conn.execute(
                text("SELECT lexeme FROM unnest(to_tsvector('english', :q))"),
                {"q": " ".join(search_words)},
            )
        )
        .scalars()
        .all()
    )
    if not lexemes:
        return SearchResult([], search_words, project)
    tsquery = " | ".join(f"'{lexeme.replace(chr(39), chr(39) * 2)}'" for lexeme in lexemes)
    rows = (
        await conn.execute(
            text(
                """
                WITH q AS (SELECT to_tsquery('simple', :tsquery) AS tsq),
                ranked AS (
                    SELECT c.record_seq, c.kind, c.content, c.source,
                           ts_rank_cd(c.tsv, q.tsq)
                             + CASE WHEN r.project = :project THEN :boost ELSE 0 END AS rank,
                           row_number() OVER (
                               PARTITION BY c.record_seq ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC
                           ) AS n
                    FROM search_chunk c JOIN record r ON r.seq = c.record_seq, q
                    WHERE c.tsv @@ q.tsq
                )
                SELECT ranked.record_seq, ranked.kind, ranked.content, ranked.source, ranked.rank,
                       r.project, r.origin, r.author_name, r.forward_sender_name,
                       r.forward_sent_at, r.observed_at, r.media_kinds,
                       convert_from(p.payload, 'UTF8')::json
                         #>> '{normalized,attachments,0,file_name}' AS file_name
                FROM ranked
                JOIN record r ON r.seq = ranked.record_seq
                LEFT JOIN evidence_payload p ON p.seq = r.seq
                WHERE ranked.n = 1
                ORDER BY ranked.rank DESC, ranked.record_seq DESC
                LIMIT :limit
                """
            ),
            {"tsquery": tsquery, "project": project, "boost": PROJECT_BOOST, "limit": limit},
        )
    ).all()
    hits = []
    for row in rows:
        hits.append(
            Hit(
                seq=int(row.record_seq),
                project=row.project,
                kind=row.kind,
                content=row.content,
                source=row.source,
                origin=row.origin,
                author_name=row.author_name,
                forward_sender_name=row.forward_sender_name,
                forward_sent_at=row.forward_sent_at,
                observed_at=row.observed_at,
                media_kinds=list(row.media_kinds or []),
                file_name=row.file_name,
                rank=float(row.rank),
                status=(
                    (await verifier.verify(conn, int(row.record_seq))).status
                    if verifier is not None
                    else await fast_status(conn, int(row.record_seq))
                ),
            )
        )
    return SearchResult(hits, search_words, project)
