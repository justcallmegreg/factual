"""Projections of record.created and record.edited: record and record_version.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.storage import grants

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "record",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), primary_key=True),
        sa.Column("connector", sa.Text, nullable=False),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("author_id", sa.BigInteger),
        sa.Column("author_name", sa.Text),
        sa.Column("author_username", sa.Text),
        sa.Column("text", sa.Text),
        sa.Column("caption", sa.Text),
        sa.Column("origin", sa.Text, nullable=False, server_default="note"),
        sa.Column("forward_sender_name", sa.Text),
        sa.Column("forward_sender_id", sa.BigInteger),
        sa.Column("forward_sent_at", sa.DateTime(timezone=True)),
        sa.Column("reply_to_seq", sa.BigInteger, sa.ForeignKey("record.seq")),
        sa.Column("album_id", sa.Text),
        sa.Column("media_kinds", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("stated_at", sa.DateTime(timezone=True)),
        sa.Column("tags", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project", sa.Text, sa.ForeignKey("project.name")),
        sa.Column("filed_seq", sa.BigInteger),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
    )
    op.create_index("record_message_idx", "record", ["chat_id", "message_id"], unique=True)
    op.create_index("record_observed_idx", "record", ["observed_at"])
    op.create_index("record_project_idx", "record", ["project"])
    op.create_table(
        "record_version",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), primary_key=True),
        sa.Column("record_seq", sa.BigInteger, sa.ForeignKey("record.seq"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("text", sa.Text),
        sa.Column("caption", sa.Text),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "record_version_record_idx", "record_version", ["record_seq", "version"], unique=True
    )
    for table in ("record", "record_version"):
        for statement in grants.operational_table_sql(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_table("record_version")
    op.drop_table("record")
