"""schedule_run: one row per scheduled job run, so a restart never sends twice.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedule_run",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("job", sa.Text, nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("detail", sa.Text),
    )
    op.create_index("schedule_run_job_idx", "schedule_run", ["job", "scheduled_for"], unique=True)
    for statement in grants.operational_table_sql("schedule_run"):
        op.execute(statement)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO factual_runtime")


def downgrade() -> None:
    op.drop_table("schedule_run")
