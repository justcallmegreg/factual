"""qa_run: the operational trace of /ask and /respond runs (for the daily limit).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "qa_run",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("asked_by_id", sa.BigInteger),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("called_claude", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("verdict", sa.Text),
    )
    op.create_index("qa_run_day_idx", "qa_run", ["kind", "asked_at"])
    for statement in grants.operational_table_sql("qa_run"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("qa_run")
