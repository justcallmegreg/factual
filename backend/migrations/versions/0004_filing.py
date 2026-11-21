"""Filing: the filing projection, pickers and /log sessions.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.storage import grants

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "filing",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), primary_key=True),
        sa.Column("record_seq", sa.BigInteger, sa.ForeignKey("record.seq"), nullable=False),
        sa.Column("project", sa.Text, nullable=False),
        sa.Column("previous", sa.Text),
        sa.Column("by_id", sa.BigInteger),
        sa.Column("by_name", sa.Text),
        sa.Column("via", sa.Text, nullable=False),
        sa.Column("filed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("filing_record_idx", "filing", ["record_seq"])
    op.create_index("filing_project_idx", "filing", ["project", "filed_at"])
    op.create_table(
        "picker",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("user_id", sa.BigInteger),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("record_seqs", postgresql.ARRAY(sa.BigInteger), nullable=False),
        sa.Column("page", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_record_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("filed_project", sa.Text),
    )
    op.create_index("picker_share_idx", "picker", ["chat_id", "user_id", "last_record_at"])
    op.create_table(
        "log_session",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("chat_id", sa.BigInteger, nullable=False),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("project", sa.Text),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("filed_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.create_index("log_session_user_idx", "log_session", ["chat_id", "user_id", "ended_at"])
    for table in ("filing", "picker", "log_session"):
        for statement in grants.operational_table_sql(table):
            op.execute(statement)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO factual_runtime")


def downgrade() -> None:
    op.drop_table("log_session")
    op.drop_table("picker")
    op.drop_table("filing")
