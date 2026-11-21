"""Attachments: the job queue, the blob evidence table and the attachment projection.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.storage import grants

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("job_due_idx", "job", ["status", "run_at"])
    op.create_table(
        "blob",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), primary_key=True),
        sa.Column("sha256_plain", sa.LargeBinary, nullable=False),
        sa.Column("sha256_stored", sa.LargeBinary, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("mime", sa.Text, nullable=False),
        sa.Column("object_key", sa.Text, nullable=False),
        sa.Column("version_id", sa.Text),
        sa.Column("wrapped_key", sa.LargeBinary, nullable=False, comment="per-file key, wrapped"),
        sa.Column("aad", sa.Text, nullable=False, comment="associated data of the file encryption"),
        sa.Column("retain_until", sa.DateTime(timezone=True)),
    )
    for statement in grants.evidence_table_sql("blob"):
        op.execute(statement)
    op.create_table(
        "attachment",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("record_seq", sa.BigInteger, sa.ForeignKey("record.seq"), nullable=False),
        sa.Column("file_unique_id", sa.Text, nullable=False),
        sa.Column("file_id", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("file_name", sa.Text),
        sa.Column("mime", sa.Text),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("entry_seq", sa.BigInteger),
        sa.Column("object_key", sa.Text),
        sa.Column("version_id", sa.Text),
        sa.Column("sha256_plain", sa.LargeBinary),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "attachment_record_file_idx", "attachment", ["record_seq", "file_unique_id"], unique=True
    )
    for table in ("job", "attachment"):
        for statement in grants.operational_table_sql(table):
            op.execute(statement)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO factual_runtime")


def downgrade() -> None:
    op.drop_table("attachment")
    op.drop_table("blob")
    op.drop_table("job")
