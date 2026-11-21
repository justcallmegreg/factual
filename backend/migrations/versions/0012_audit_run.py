"""audit_run: the operational trace of deep audits (the latest result for /status).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_run",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("reason", sa.Text),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("summary", sa.Text, nullable=False),
    )
    for statement in grants.operational_table_sql("audit_run"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("audit_run")
