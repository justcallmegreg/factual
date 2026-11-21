"""`/project`, `/project new <name>` and `/project archive <name>`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.connectors.base import SourceEvent
from app.records.projects import (
    NAME_RULE,
    archive_project,
    create_project,
    list_projects,
    normalize_name,
)

if TYPE_CHECKING:
    from app.commands.dispatcher import CommandDispatcher

USAGE = (
    "Usage: /project lists the projects; /project new <name> creates one; "
    "/project archive <name> hides one from the picker (its records stay)."
)


def _plural(count: int) -> str:
    return "1 record" if count == 1 else f"{count} records"


async def handle(dispatcher: CommandDispatcher, event: SourceEvent) -> None:
    args = (event.command.args if event.command else "").strip()
    if not args:
        async with dispatcher.db.engine.connect() as conn:
            projects = await list_projects(conn)
        if not projects:
            await dispatcher.reply(event, "No projects yet. Create one with /project new <name>.")
            return
        lines = ["Projects:"]
        for info in projects:
            suffix = " (archived)" if info.archived_at else ""
            lines.append(f"• {info.name} · {_plural(info.records)}{suffix}")
        await dispatcher.reply(event, "\n".join(lines))
        return

    action, _, rest = args.partition(" ")
    action = action.lower()
    if action not in ("new", "archive"):
        await dispatcher.reply(event, USAGE)
        return
    name = normalize_name(rest)
    if name is None:
        await dispatcher.reply(event, NAME_RULE)
        return
    async with dispatcher.db.engine.begin() as conn:
        if action == "new":
            outcome = await create_project(conn, dispatcher.ledger, name=name, event=event)
            text = outcome if isinstance(outcome, str) else f"Created project {name}."
        else:
            outcome = await archive_project(conn, dispatcher.ledger, name=name, event=event)
            text = (
                outcome
                if isinstance(outcome, str)
                else f"Archived project {name}. Its records stay."
            )
    await dispatcher.reply(event, text)
