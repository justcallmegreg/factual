"""Table definitions, mirrored by the hand-written Alembic migrations.

Three kinds of tables: insert-only evidence tables (the source of truth),
projections that can be rebuilt from them, and a little operational state.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY

metadata = MetaData()

PROJECT_NAME_PATTERN = r"^[a-z0-9_-]{1,32}$"

# --- evidence tables: insert-only ------------------------------------------------------

ledger_entry = Table(
    "ledger_entry",
    metadata,
    Column("seq", BigInteger, primary_key=True),
    Column("type", Text, nullable=False),
    Column("entry", LargeBinary, nullable=False, comment="canonical JCS bytes"),
    Column("entry_hash", LargeBinary, nullable=False),
    Column("chain_hash", LargeBinary, nullable=False, comment="rolling hash up to this entry"),
    Column("source_event", Text, unique=True, comment="connector + platform event ID"),
    Column("received_at", DateTime(timezone=True), nullable=False),
)

evidence_payload = Table(
    "evidence_payload",
    metadata,
    Column("seq", BigInteger, ForeignKey("ledger_entry.seq"), primary_key=True),
    Column("salt", LargeBinary, nullable=False),
    Column("payload", LargeBinary, nullable=False, comment="canonical payload bytes"),
)

instance = Table(
    "instance",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("public_key", LargeBinary, nullable=False, comment="raw Ed25519 public key"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("genesis_hash", LargeBinary, nullable=False),
    CheckConstraint("id = 1", name="instance_single_row"),
)

EVIDENCE_TABLES = ("ledger_entry", "evidence_payload", "instance")

# --- projections -----------------------------------------------------------------------

project = Table(
    "project",
    metadata,
    Column("name", Text, primary_key=True),
    Column("title", Text),
    Column("archived_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(f"name ~ '{PROJECT_NAME_PATTERN}'", name="project_name_rule"),
)

record = Table(
    "record",
    metadata,
    Column("seq", BigInteger, ForeignKey("ledger_entry.seq"), primary_key=True),
    Column("connector", Text, nullable=False),
    Column("chat_id", BigInteger, nullable=False),
    Column("message_id", BigInteger),
    Column("author_id", BigInteger),
    Column("author_name", Text),
    Column("author_username", Text),
    Column("text", Text),
    Column("caption", Text),
    Column("origin", Text, nullable=False, server_default="note"),
    Column("forward_sender_name", Text),
    Column("forward_sender_id", BigInteger),
    Column("forward_sent_at", DateTime(timezone=True)),
    Column("reply_to_seq", BigInteger, ForeignKey("record.seq")),
    Column("album_id", Text),
    Column("media_kinds", ARRAY(Text), nullable=False, server_default="{}"),
    Column("stated_at", DateTime(timezone=True)),
    Column("tags", ARRAY(Text), nullable=False, server_default="{}"),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("project", Text, ForeignKey("project.name")),
    Column("filed_seq", BigInteger),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("edited_at", DateTime(timezone=True)),
    Index("record_message_idx", "chat_id", "message_id", unique=True),
    Index("record_observed_idx", "observed_at"),
    Index("record_project_idx", "project"),
)

record_version = Table(
    "record_version",
    metadata,
    Column("seq", BigInteger, ForeignKey("ledger_entry.seq"), primary_key=True),
    Column("record_seq", BigInteger, ForeignKey("record.seq"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("text", Text),
    Column("caption", Text),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Index("record_version_record_idx", "record_seq", "version", unique=True),
)
