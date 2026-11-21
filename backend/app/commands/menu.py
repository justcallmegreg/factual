"""The one command list: shown by /help and /start, published with setMyCommands."""

from __future__ import annotations

# (name, description) in the order Telegram shows them; descriptions must be 1-256 characters
COMMANDS: list[tuple[str, str]] = [
    ("log", "pick a project first, then send your records; with text, record that text"),
    ("project", "list projects; /project new <name> creates one"),
    ("ask", "search all records and get Claude's verdict"),
    ("proof", "reply to a record to get its evidence pack"),
    ("verify", "reply to a record to check it"),
    ("respond", "draft a calm, evidence-only reply to a claim"),
    ("status", "polling, the last seal, the last audit and the key fingerprint"),
    ("help", "list the commands"),
]

HELP = """Capture
  (any message)      is recorded at once; tap its project
  /log               pick a project first, then send your records
  /log pending       records that still need a project
Find and prove
  /ask <question>    search all records
  /proof             reply to a record to get its evidence pack
  /verify            reply to a record to check it
  /respond <claim>   draft a calm, evidence-only reply
Projects
  /project           list projects; /project new <name> creates one
Status
  /status            polling, the last seal, the last audit and the key fingerprint
  /help              this list"""


def help_text() -> str:
    return HELP
