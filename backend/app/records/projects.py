"""Projects: labels with permanent short names, projected from ledger entries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.connectors.base import SourceEvent
from app.ledger.ledger import Appended, Ledger
from app.storage.models import PROJECT_NAME_PATTERN, filing, project, record

NAME_PATTERN = re.compile(PROJECT_NAME_PATTERN)
NAME_RULE = (
    "A project name has 1 to 32 characters from a-z, 0-9, _ and -. Capitals become lowercase."
)


@dataclass(frozen=True)
class ProjectInfo:
    name: str
    records: int
    archived_at: datetime | None = None
    title: str | None = None
    last_used_at: datetime | None = None


def normalize_name(raw: str) -> str | None:
    """Lower-case the typed name; None when it breaks the rule."""
    name = raw.strip().lower()
    return name if NAME_PATTERN.fullmatch(name) else None


def actor_of(event: SourceEvent) -> dict | None:
    actor = event.actor
    if actor is None:
        return None
    return {"id": actor.user_id, "name": actor.display_name, "username": actor.username}


def event_source(event: SourceEvent) -> dict:
    source: dict = {"connector": event.chat.connector, "chat_id": event.chat.chat_id}
    if event.message is not None:
        source["message_id"] = event.message.message_id
    if isinstance(event.raw, dict) and "update_id" in event.raw:
        source["update_id"] = event.raw["update_id"]
    return source


async def list_projects(conn: AsyncConnection) -> list[ProjectInfo]:
    counts = (
        select(record.c.project.label("name"), func.count().label("records"))
        .where(record.c.project.is_not(None))
        .group_by(record.c.project)
        .subquery()
    )
    rows = (
        await conn.execute(
            select(
                project.c.name,
                project.c.title,
                project.c.archived_at,
                func.coalesce(counts.c.records, 0).label("records"),
            )
            .select_from(project.outerjoin(counts, counts.c.name == project.c.name))
            .order_by(project.c.archived_at.is_not(None), project.c.name)
        )
    ).all()
    return [
        ProjectInfo(name=r.name, records=int(r.records), archived_at=r.archived_at, title=r.title)
        for r in rows
    ]


async def picker_projects(conn: AsyncConnection) -> list[ProjectInfo]:
    """Active projects, most recently used first, then by name."""
    last_used = (
        select(filing.c.project.label("name"), func.max(filing.c.filed_at).label("used"))
        .group_by(filing.c.project)
        .subquery()
    )
    rows = (
        await conn.execute(
            select(project.c.name, project.c.title, last_used.c.used)
            .select_from(project.outerjoin(last_used, last_used.c.name == project.c.name))
            .where(project.c.archived_at.is_(None))
            .order_by(last_used.c.used.desc().nulls_last(), project.c.name)
        )
    ).all()
    return [ProjectInfo(r.name, 0, None, r.title, r.used) for r in rows]


async def get_project(conn: AsyncConnection, name: str):
    return (await conn.execute(select(project).where(project.c.name == name))).first()


async def create_project(
    conn: AsyncConnection, ledger: Ledger, *, name: str, event: SourceEvent
) -> Appended | str:
    """Append project.created and the projection row; a message when it cannot be done."""
    existing = await get_project(conn, name)
    if existing is not None:
        if await ledger.find(conn, event.event_id) is not None:
            return await ledger.append(  # a redelivery: the same entry comes back
                conn,
                type="project.created",
                source=event_source(event),
                source_event=event.event_id,
                observed_at=event.occurred_at,
                payload={},
            )
        if existing.archived_at is not None:
            return f"Project {name} is archived; names are permanent, so pick a new one."
        return f"Project {name} exists already."
    appended = await ledger.append(
        conn,
        type="project.created",
        source=event_source(event),
        source_event=event.event_id,
        observed_at=event.occurred_at,
        payload={"name": name, "by": actor_of(event), "raw": event.raw},
    )
    if not appended.duplicate:
        await conn.execute(insert(project).values(name=name, created_at=datetime.now(UTC)))
    return appended


async def archive_project(
    conn: AsyncConnection, ledger: Ledger, *, name: str, event: SourceEvent
) -> Appended | str:
    existing = await get_project(conn, name)
    if existing is None:
        return f"No project named {name}."
    if existing.archived_at is not None:
        if await ledger.find(conn, event.event_id) is not None:
            return await ledger.append(
                conn,
                type="project.archived",
                source=event_source(event),
                source_event=event.event_id,
                observed_at=event.occurred_at,
                payload={},
            )
        return f"Project {name} is archived already."
    appended = await ledger.append(
        conn,
        type="project.archived",
        source=event_source(event),
        source_event=event.event_id,
        observed_at=event.occurred_at,
        payload={"name": name, "by": actor_of(event), "raw": event.raw},
    )
    if not appended.duplicate:
        await conn.execute(
            update(project).where(project.c.name == name).values(archived_at=datetime.now(UTC))
        )
    return appended
