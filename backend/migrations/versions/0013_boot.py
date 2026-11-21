"""boot: one row per start (or manual audit) with the database cluster's identifier.

A restored database carries the old cluster's rows, so the first start after a
restore sees a different identifier and runs the deep audit before it serves.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "boot",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("system_identifier", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("restored", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("audit_seq", sa.BigInteger),
        sa.Column("version", sa.Text),
    )
    for statement in grants.operational_table_sql("boot"):
        op.execute(statement)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO factual_runtime")


def downgrade() -> None:
    op.drop_table("boot")
