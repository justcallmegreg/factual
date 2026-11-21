"""Table definitions, mirrored by the hand-written Alembic migrations.

Three kinds of tables: insert-only evidence tables (the source of truth),
projections that can be rebuilt from them, and a little operational state.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

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

EVIDENCE_TABLES = ("ledger_entry", "evidence_payload", "instance", "blob", "seal", "seal_anchor")

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

filing = Table(
    "filing",
    metadata,
    Column("seq", BigInteger, ForeignKey("ledger_entry.seq"), primary_key=True),
    Column("record_seq", BigInteger, ForeignKey("record.seq"), nullable=False),
    Column("project", Text, nullable=False),
    Column("previous", Text),
    Column("by_id", BigInteger),
    Column("by_name", Text),
    Column("via", Text, nullable=False),
    Column("filed_at", DateTime(timezone=True), nullable=False),
    Index("filing_record_idx", "record_seq"),
    Index("filing_project_idx", "project", "filed_at"),
)

picker = Table(
    "picker",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("user_id", BigInteger),
    Column("message_id", BigInteger),
    Column("record_seqs", ARRAY(BigInteger), nullable=False),
    Column("page", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_record_at", DateTime(timezone=True), nullable=False),
    Column("filed_project", Text),
    Index("picker_share_idx", "chat_id", "user_id", "last_record_at"),
)

log_session = Table(
    "log_session",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("chat_id", BigInteger, nullable=False),
    Column("user_id", BigInteger, nullable=False),
    Column("project", Text),
    Column("message_id", BigInteger),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("last_activity_at", DateTime(timezone=True), nullable=False),
    Column("ended_at", DateTime(timezone=True)),
    Column("filed_count", Integer, nullable=False, server_default="0"),
    Index("log_session_user_idx", "chat_id", "user_id", "ended_at"),
)

search_chunk = Table(
    "search_chunk",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("record_seq", BigInteger, ForeignKey("record.seq"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("source", Text),
    Column("content", Text, nullable=False),
    Column("tsv", TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("search_chunk_tsv_idx", "tsv", postgresql_using="gin"),
    Index("search_chunk_record_idx", "record_seq", "kind"),
)

job = Table(
    "job",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("kind", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("max_attempts", Integer, nullable=False, server_default="5"),
    Column("run_at", DateTime(timezone=True), nullable=False),
    Column("locked_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Index("job_due_idx", "status", "run_at"),
)

blob = Table(
    "blob",
    metadata,
    Column("seq", BigInteger, ForeignKey("ledger_entry.seq"), primary_key=True),
    Column("sha256_plain", LargeBinary, nullable=False),
    Column("sha256_stored", LargeBinary, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("mime", Text, nullable=False),
    Column("object_key", Text, nullable=False),
    Column("version_id", Text),
    Column("wrapped_key", LargeBinary, nullable=False),
    Column("aad", Text, nullable=False),
    Column("retain_until", DateTime(timezone=True)),
)

attachment = Table(
    "attachment",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("record_seq", BigInteger, ForeignKey("record.seq"), nullable=False),
    Column("file_unique_id", Text, nullable=False),
    Column("file_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("file_name", Text),
    Column("mime", Text),
    Column("size_bytes", BigInteger),
    Column("status", Text, nullable=False, server_default="pending"),
    Column("entry_seq", BigInteger),
    Column("object_key", Text),
    Column("version_id", Text),
    Column("sha256_plain", LargeBinary),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("attachment_record_file_idx", "record_seq", "file_unique_id", unique=True),
)

seal = Table(
    "seal",
    metadata,
    Column("no", BigInteger, primary_key=True),
    Column("up_to_seq", BigInteger, ForeignKey("ledger_entry.seq"), nullable=False),
    Column("body", LargeBinary, nullable=False, comment="canonical seal"),
    Column("signature", LargeBinary, nullable=False),
    Column("key_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("seal_up_to_idx", "up_to_seq"),
)

seal_anchor = Table(
    "seal_anchor",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("seal_no", BigInteger, ForeignKey("seal.no"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("issuer", Text, nullable=False),
    Column("proof", LargeBinary, nullable=False),
    Column("chain_pem", LargeBinary),
    Column("gen_time", DateTime(timezone=True)),
    Column("policy", Text),
    Column("added_at", DateTime(timezone=True), nullable=False),
    Index("seal_anchor_seal_idx", "seal_no", "kind"),
)

qa_run = Table(
    "qa_run",
    metadata,
    Column("seq", BigInteger, ForeignKey("ledger_entry.seq"), primary_key=True),
    Column("kind", Text, nullable=False),
    Column("asked_by_id", BigInteger),
    Column("asked_at", DateTime(timezone=True), nullable=False),
    Column("model", Text),
    Column("called_claude", Boolean, nullable=False, server_default="false"),
    Column("verdict", Text),
    Index("qa_run_day_idx", "kind", "asked_at"),
)

schedule_run = Table(
    "schedule_run",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("job", Text, nullable=False),
    Column("scheduled_for", DateTime(timezone=True), nullable=False),
    Column("ran_at", DateTime(timezone=True), nullable=False),
    Column("chat_id", BigInteger),
    Column("message_id", BigInteger),
    Column("status", Text, nullable=False),
    Column("detail", Text),
    Index("schedule_run_job_idx", "job", "scheduled_for", unique=True),
)

derived_text = Table(
    "derived_text",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("record_seq", BigInteger, ForeignKey("record.seq"), nullable=False),
    Column("blob_seq", BigInteger, ForeignKey("blob.seq"), nullable=False),
    Column("file_name", Text),
    Column("mime", Text),
    Column("model", Text),
    Column("extracted_at", DateTime(timezone=True), nullable=False),
    Column("text", Text),
    Column("status", Text, nullable=False),
    Column("pages", Integer),
    Column("chars", Integer),
    Index("derived_text_blob_idx", "blob_seq", unique=True),
    Index("derived_text_record_idx", "record_seq"),
)
