"""Filing: a record gets its project by one tap, before or after it is recorded.

Record first, file later: the project is a label added by a `record.filed`
entry that references the record. The record itself never changes, and a new
choice is a new entry, so `/verify` can show the filing history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.connectors.base import Button, Connector, MessageRef, OutboundMessage, SourceEvent
from app.core.clock import Clock, format_local, system_clock
from app.core.logging import ctx
from app.ledger.ledger import Appended, Ledger
from app.records.projects import actor_of, event_source, get_project, picker_projects
from app.storage.db import Database
from app.storage.models import filing, log_session, picker, record

log = logging.getLogger("factual.filing")

PICKER_SHARE_SECONDS = 10
SESSION_SILENCE_SECONDS = 10 * 60
PAGE_SIZE = 6
BUTTONS_PER_ROW = 3
PENDING_LIMIT = 10
INVALID_PICKER = "This picker is no longer valid."


@dataclass(frozen=True)
class AutoProject:
    project: str
    via: str  # session | reply


class Filing:
    def __init__(
        self,
        ledger: Ledger,
        db: Database,
        connector: Connector,
        *,
        timezone: str,
        clock: Clock | None = None,
    ) -> None:
        self.ledger = ledger
        self.db = db
        self.connector = connector
        self.zone = ZoneInfo(timezone)
        self.clock = clock or system_clock

    # --- inside the record transaction -------------------------------------------------

    async def auto_project(
        self, conn: AsyncConnection, event: SourceEvent, reply_to_seq: int | None
    ) -> AutoProject | None:
        """A running /log session of the sender wins; else the project of the replied record."""
        session = await self._active_session(conn, event)
        if session is not None and session.project:
            now = self.clock()
            await conn.execute(
                update(log_session)
                .where(log_session.c.id == session.id)
                .values(last_activity_at=now, filed_count=log_session.c.filed_count + 1)
            )
            return AutoProject(session.project, "session")
        if reply_to_seq is not None:
            row = (
                await conn.execute(select(record.c.project).where(record.c.seq == reply_to_seq))
            ).first()
            if row is not None and row.project:
                return AutoProject(row.project, "reply")
        return None

    async def file(
        self,
        conn: AsyncConnection,
        *,
        record_seq: int,
        project: str,
        event: SourceEvent,
        via: str,
        source_event: str,
    ) -> Appended:
        """Append record.filed for one record and move the projection to the project."""
        current = (
            await conn.execute(select(record.c.project).where(record.c.seq == record_seq))
        ).first()
        previous = current.project if current is not None else None
        appended = await self.ledger.append(
            conn,
            type="record.filed",
            source=event_source(event),
            source_event=source_event,
            observed_at=event.occurred_at,
            payload={
                "record_seq": record_seq,
                "project": project,
                "previous": previous,
                "by": actor_of(event),
                "via": via,
                "raw": event.raw,
            },
            refs=[record_seq],
        )
        if not appended.duplicate:
            actor = actor_of(event) or {}
            await conn.execute(
                insert(filing).values(
                    seq=appended.seq,
                    record_seq=record_seq,
                    project=project,
                    previous=previous,
                    by_id=actor.get("id"),
                    by_name=actor.get("name"),
                    via=via,
                    filed_at=self.clock(),
                )
            )
            await conn.execute(
                update(record)
                .where(record.c.seq == record_seq)
                .values(project=project, filed_seq=appended.seq)
            )
            log.info(
                "record filed",
                extra=ctx(action="record.filed", entry=appended.seq, project=project),
            )
        return appended

    # --- the picker -------------------------------------------------------------------

    async def offer_picker(self, event: SourceEvent, record_seq: int) -> None:
        """After a record: edit the sender's picker of the last 10 seconds, or send a new one."""
        now = self.clock()
        when = event.occurred_at  # the record's own time: a backlog does not merge hours into one
        chat = event.chat
        user_id = event.actor.user_id if event.actor else None
        async with self.db.engine.begin() as conn:
            shared = (
                await conn.execute(
                    select(picker)
                    .where(
                        picker.c.chat_id == chat.chat_id,
                        picker.c.user_id == user_id,
                        picker.c.filed_project.is_(None),
                        picker.c.message_id.is_not(None),
                        picker.c.last_record_at >= when - timedelta(seconds=PICKER_SHARE_SECONDS),
                    )
                    .order_by(picker.c.id.desc())
                    .limit(1)
                )
            ).first()
            if shared is not None:
                seqs = [int(s) for s in shared.record_seqs] + [record_seq]
                await conn.execute(
                    update(picker)
                    .where(picker.c.id == shared.id)
                    .values(record_seqs=seqs, last_record_at=when, page=0)
                )
                text = await self._picker_text(conn, seqs)
                buttons = await self._project_buttons(conn, "p", int(shared.id), 0)
                message_id = int(shared.message_id)
            else:
                result = await conn.execute(
                    insert(picker)
                    .values(
                        chat_id=chat.chat_id,
                        user_id=user_id,
                        record_seqs=[record_seq],
                        created_at=now,
                        last_record_at=when,
                    )
                    .returning(picker.c.id)
                )
                picker_id = int(result.scalar_one())
                text = await self._picker_text(conn, [record_seq])
                buttons = await self._project_buttons(conn, "p", picker_id, 0)
                message_id = None
        if message_id is not None:
            await self.connector.send(
                chat, OutboundMessage(text, edit_of=MessageRef(chat, message_id), buttons=buttons)
            )
            return
        sent = await self.connector.send(
            chat, OutboundMessage(text, reply_to=event.message, buttons=buttons)
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(picker)
                .where(picker.c.id == picker_id)
                .values(message_id=sent.message.message_id)
            )

    async def _picker_text(self, conn: AsyncConnection, seqs: list[int]) -> str:
        first = (
            await conn.execute(select(record.c.observed_at).where(record.c.seq == seqs[0]))
        ).first()
        when = format_local(first.observed_at, self.zone) if first else ""
        numbers = ", ".join(f"E{seq}" for seq in seqs)
        return f"Recorded {numbers} · {when}. Which project?"

    async def _project_buttons(
        self, conn: AsyncConnection, prefix: str, ident: int, page: int
    ) -> tuple[tuple[Button, ...], ...]:
        projects = await picker_projects(conn)
        pages = max(1, -(-len(projects) // PAGE_SIZE))
        page %= pages
        shown = projects[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
        rows: list[tuple[Button, ...]] = []
        for start in range(0, len(shown), BUTTONS_PER_ROW):
            rows.append(
                tuple(
                    Button(info.name, f"{prefix}:{ident}:{info.name}")
                    for info in shown[start : start + BUTTONS_PER_ROW]
                )
            )
        if pages > 1:
            label = "More" if page < pages - 1 else "Back"
            rows.append((Button(label, f"more:{prefix}:{ident}:{page + 1}"),))
        return tuple(rows)

    # --- taps -------------------------------------------------------------------------

    async def tap(self, event: SourceEvent) -> None:
        data = event.choice.data if event.choice else ""
        parts = data.split(":")
        kind = parts[0]
        try:
            if kind == "p" and len(parts) == 3:
                await self._tap_file(event, int(parts[1]), parts[2])
            elif kind == "chg" and len(parts) == 2:
                await self._tap_change(event, int(parts[1]))
            elif kind == "more" and len(parts) == 4:
                await self._tap_more(event, parts[1], int(parts[2]), int(parts[3]))
            elif kind == "s" and len(parts) == 3:
                await self._tap_session_project(event, int(parts[1]), parts[2])
            elif kind == "sdone" and len(parts) == 2:
                await self._tap_session_done(event, int(parts[1]))
            else:
                await self._answer(event, INVALID_PICKER)
        except ValueError:
            await self._answer(event, INVALID_PICKER)

    async def _answer(self, event: SourceEvent, text: str) -> None:
        await self.connector.send(
            event.chat,
            OutboundMessage(text="", answer_choice=event.choice.query_id, answer_text=text),
        )

    async def _tap_file(self, event: SourceEvent, picker_id: int, project: str) -> None:
        async with self.db.engine.begin() as conn:
            row = (await conn.execute(select(picker).where(picker.c.id == picker_id))).first()
            if row is None or row.message_id is None:
                await self._answer(event, INVALID_PICKER)
                return
            info = await get_project(conn, project)
            if info is None or info.archived_at is not None:
                reason = "archived" if info is not None else "unknown"
                text = await self._picker_text(conn, [int(s) for s in row.record_seqs])
                buttons = await self._project_buttons(conn, "p", picker_id, 0)
                await self.connector.send(
                    event.chat,
                    OutboundMessage(
                        text,
                        edit_of=MessageRef(event.chat, int(row.message_id)),
                        buttons=buttons,
                        answer_choice=event.choice.query_id,
                        answer_text=f"Project {project} is {reason}. Pick another one.",
                    ),
                )
                return
            seqs = [int(s) for s in row.record_seqs]
            for seq in seqs:
                await self.file(
                    conn,
                    record_seq=seq,
                    project=project,
                    event=event,
                    via="tap",
                    source_event=f"{event.event_id}:{seq}",
                )
            await conn.execute(
                update(picker).where(picker.c.id == picker_id).values(filed_project=project)
            )
            first = (
                await conn.execute(select(record.c.observed_at).where(record.c.seq == seqs[0]))
            ).first()
            when = format_local(first.observed_at, self.zone)
        numbers = ", ".join(f"E{seq}" for seq in seqs)
        await self.connector.send(
            event.chat,
            OutboundMessage(
                f"{numbers} · {project} · {when}",
                edit_of=MessageRef(event.chat, int(row.message_id)),
                buttons=((Button("Change project", f"chg:{picker_id}"),),),
                answer_choice=event.choice.query_id,
            ),
        )

    async def _tap_change(self, event: SourceEvent, picker_id: int) -> None:
        async with self.db.engine.begin() as conn:
            row = (await conn.execute(select(picker).where(picker.c.id == picker_id))).first()
            if row is None or row.message_id is None:
                await self._answer(event, INVALID_PICKER)
                return
            await conn.execute(
                update(picker).where(picker.c.id == picker_id).values(filed_project=None, page=0)
            )
            text = await self._picker_text(conn, [int(s) for s in row.record_seqs])
            buttons = await self._project_buttons(conn, "p", picker_id, 0)
        await self.connector.send(
            event.chat,
            OutboundMessage(
                text,
                edit_of=MessageRef(event.chat, int(row.message_id)),
                buttons=buttons,
                answer_choice=event.choice.query_id,
            ),
        )

    async def _tap_more(self, event: SourceEvent, prefix: str, ident: int, page: int) -> None:
        async with self.db.engine.begin() as conn:
            if prefix == "p":
                row = (await conn.execute(select(picker).where(picker.c.id == ident))).first()
                if row is None or row.message_id is None:
                    await self._answer(event, INVALID_PICKER)
                    return
                await conn.execute(update(picker).where(picker.c.id == ident).values(page=page))
                text = await self._picker_text(conn, [int(s) for s in row.record_seqs])
            elif prefix == "s":
                row = (
                    await conn.execute(select(log_session).where(log_session.c.id == ident))
                ).first()
                if row is None or row.message_id is None or row.ended_at is not None:
                    await self._answer(event, INVALID_PICKER)
                    return
                text = SESSION_QUESTION
            else:
                await self._answer(event, INVALID_PICKER)
                return
            buttons = await self._project_buttons(conn, prefix, ident, page)
        await self.connector.send(
            event.chat,
            OutboundMessage(
                text,
                edit_of=MessageRef(event.chat, int(row.message_id)),
                buttons=buttons,
                answer_choice=event.choice.query_id,
            ),
        )

    # --- /log sessions ----------------------------------------------------------------

    async def _active_session(self, conn: AsyncConnection, event: SourceEvent):
        if event.actor is None:
            return None
        now = self.clock()
        row = (
            await conn.execute(
                select(log_session)
                .where(
                    log_session.c.chat_id == event.chat.chat_id,
                    log_session.c.user_id == event.actor.user_id,
                    log_session.c.ended_at.is_(None),
                )
                .order_by(log_session.c.id.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        if row.last_activity_at < now - timedelta(seconds=SESSION_SILENCE_SECONDS):
            await conn.execute(
                update(log_session).where(log_session.c.id == row.id).values(ended_at=now)
            )
            return None
        return row

    async def start_session(self, event: SourceEvent) -> None:
        """`/log` without text: ask for the project first."""
        now = self.clock()
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(log_session)
                .where(
                    log_session.c.chat_id == event.chat.chat_id,
                    log_session.c.user_id == event.actor.user_id,
                    log_session.c.ended_at.is_(None),
                )
                .values(ended_at=now)
            )
            result = await conn.execute(
                insert(log_session)
                .values(
                    chat_id=event.chat.chat_id,
                    user_id=event.actor.user_id,
                    started_at=now,
                    last_activity_at=now,
                )
                .returning(log_session.c.id)
            )
            session_id = int(result.scalar_one())
            buttons = await self._project_buttons(conn, "s", session_id, 0)
        if not buttons:
            await self.connector.send(
                event.chat,
                OutboundMessage(
                    "No projects yet. Create one with /project new <name>.", reply_to=event.message
                ),
            )
            return
        sent = await self.connector.send(
            event.chat, OutboundMessage(SESSION_QUESTION, reply_to=event.message, buttons=buttons)
        )
        async with self.db.engine.begin() as conn:
            await conn.execute(
                update(log_session)
                .where(log_session.c.id == session_id)
                .values(message_id=sent.message.message_id)
            )

    async def _tap_session_project(self, event: SourceEvent, session_id: int, project: str) -> None:
        now = self.clock()
        async with self.db.engine.begin() as conn:
            row = (
                await conn.execute(select(log_session).where(log_session.c.id == session_id))
            ).first()
            if row is None or row.ended_at is not None or row.message_id is None:
                await self._answer(event, INVALID_PICKER)
                return
            info = await get_project(conn, project)
            if info is None or info.archived_at is not None:
                await self._answer(event, f"Project {project} is not available. Pick another one.")
                return
            await conn.execute(
                update(log_session)
                .where(log_session.c.id == session_id)
                .values(project=project, last_activity_at=now)
            )
        await self.connector.send(
            event.chat,
            OutboundMessage(
                f"Filing your records under {project}. Tap Done when you are finished; "
                "after 10 minutes of silence the session ends by itself.",
                edit_of=MessageRef(event.chat, int(row.message_id)),
                buttons=((Button("Done", f"sdone:{session_id}"),),),
                answer_choice=event.choice.query_id,
            ),
        )

    async def _tap_session_done(self, event: SourceEvent, session_id: int) -> None:
        now = self.clock()
        async with self.db.engine.begin() as conn:
            row = (
                await conn.execute(select(log_session).where(log_session.c.id == session_id))
            ).first()
            if row is None or row.message_id is None:
                await self._answer(event, INVALID_PICKER)
                return
            if row.ended_at is None:
                await conn.execute(
                    update(log_session).where(log_session.c.id == session_id).values(ended_at=now)
                )
        count = int(row.filed_count)
        noun = "record" if count == 1 else "records"
        await self.connector.send(
            event.chat,
            OutboundMessage(
                f"Done. {count} {noun} filed under {row.project}.",
                edit_of=MessageRef(event.chat, int(row.message_id)),
                answer_choice=event.choice.query_id,
            ),
        )

    # --- /log pending -----------------------------------------------------------------

    async def list_pending(self, event: SourceEvent) -> None:
        now = self.clock()
        async with self.db.engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(record.c.seq, record.c.message_id, record.c.observed_at)
                    .where(record.c.project.is_(None))
                    .order_by(record.c.seq)
                    .limit(PENDING_LIMIT + 1)
                )
            ).all()
        if not rows:
            await self.connector.send(
                event.chat, OutboundMessage("No records without a project.", reply_to=event.message)
            )
            return
        more = len(rows) > PENDING_LIMIT
        for row in rows[:PENDING_LIMIT]:
            async with self.db.engine.begin() as conn:
                result = await conn.execute(
                    insert(picker)
                    .values(
                        chat_id=event.chat.chat_id,
                        user_id=event.actor.user_id if event.actor else None,
                        record_seqs=[int(row.seq)],
                        created_at=now,
                        last_record_at=now - timedelta(seconds=PICKER_SHARE_SECONDS + 1),
                    )
                    .returning(picker.c.id)
                )
                picker_id = int(result.scalar_one())
                text = await self._picker_text(conn, [int(row.seq)])
                buttons = await self._project_buttons(conn, "p", picker_id, 0)
            sent = await self.connector.send(
                event.chat,
                OutboundMessage(
                    text,
                    reply_to=MessageRef(event.chat, int(row.message_id))
                    if row.message_id
                    else None,
                    buttons=buttons,
                ),
            )
            async with self.db.engine.begin() as conn:
                await conn.execute(
                    update(picker)
                    .where(picker.c.id == picker_id)
                    .values(message_id=sent.message.message_id)
                )
        if more:
            await self.connector.send(
                event.chat,
                OutboundMessage(
                    f"These are the first {PENDING_LIMIT}; run /log pending again for the rest."
                ),
            )


SESSION_QUESTION = "Which project for the records you send next?"
