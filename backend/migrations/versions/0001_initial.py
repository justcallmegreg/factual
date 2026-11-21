"""The first evidence tables, the project projection and the two roles.

Revision ID: 0001
Revises:
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(grants.CREATE_RUNTIME_ROLE)
    op.execute(grants.GRANT_SCHEMA)
    op.execute(grants.CREATE_REJECT_FUNCTION)

    op.create_table(
        "ledger_entry",
        sa.Column("seq", sa.BigInteger, primary_key=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("entry", sa.LargeBinary, nullable=False, comment="canonical JCS bytes"),
        sa.Column("entry_hash", sa.LargeBinary, nullable=False),
        sa.Column(
            "chain_hash", sa.LargeBinary, nullable=False, comment="rolling hash up to this entry"
        ),
        sa.Column("source_event", sa.Text, unique=True, comment="connector + platform event ID"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "evidence_payload",
        sa.Column("seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), primary_key=True),
        sa.Column("salt", sa.LargeBinary, nullable=False),
        sa.Column("payload", sa.LargeBinary, nullable=False, comment="canonical payload bytes"),
    )
    for table in ("ledger_entry", "evidence_payload"):
        for statement in grants.evidence_table_sql(table):
            op.execute(statement)

    op.create_table(
        "project",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("title", sa.Text),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("name ~ '^[a-z0-9_-]{1,32}$'", name="project_name_rule"),
    )
    for statement in grants.operational_table_sql("project"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("project")
    op.drop_table("evidence_payload")
    op.drop_table("ledger_entry")
    op.execute(f"DROP FUNCTION IF EXISTS {grants.REJECT_FUNCTION}()")
    # the runtime role is cluster-wide and shared; it stays
