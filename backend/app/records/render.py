"""Render records for the chat. Only stored text is ever printed; nothing is generated."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.core.clock import format_local
from app.records.search import Hit

MAX_QUOTE = 400


def quote(text: str) -> str:
    text = text.strip()
    if len(text) > MAX_QUOTE:
        text = text[: MAX_QUOTE - 1].rstrip() + "…"
    return '"' + text + '"'


def origin_line(hit: Hit, zone: ZoneInfo) -> str:
    project = hit.project or "no project"
    if hit.origin == "forwarded" and hit.forward_sender_name:
        sent = format_local(hit.forward_sent_at, zone) if hit.forward_sent_at else "unknown time"
        who = f"Forwarded from {hit.forward_sender_name}, sent {sent}"
    elif hit.author_name:
        who = f"Note by {hit.author_name}"
    else:
        who = "Note"
    return f"E{hit.seq} · {project} · {who}"


def quote_line(hit: Hit) -> str:
    prefix = ""
    if hit.kind == "caption" and hit.media_kinds:
        name = f" {hit.file_name}" if hit.file_name else ""
        prefix = f"[{hit.media_kinds[0]}{name}] "
    elif hit.kind == "derived":
        prefix = f"[text read from {hit.source or 'a file'}] "
    return prefix + quote(hit.content)


def render_hit(hit: Hit, zone: ZoneInfo) -> str:
    return "\n".join(
        [
            origin_line(hit, zone),
            quote_line(hit),
            f"Recorded {format_local(hit.observed_at, zone)} · {hit.status}",
        ]
    )


def render_hits(hits: list[Hit], zone: ZoneInfo) -> str:
    return "\n\n".join(render_hit(hit, zone) for hit in hits)
