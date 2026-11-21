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
    LargeBinary,
    MetaData,
    Table,
    Text,
)

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

EVIDENCE_TABLES = ("ledger_entry", "evidence_payload")

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
