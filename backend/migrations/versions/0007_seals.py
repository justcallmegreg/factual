"""Seals: signed commitments to the chain head, every few minutes.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seal",
        sa.Column("no", sa.BigInteger, primary_key=True),
        sa.Column("up_to_seq", sa.BigInteger, sa.ForeignKey("ledger_entry.seq"), nullable=False),
        sa.Column("body", sa.LargeBinary, nullable=False, comment="canonical seal"),
        sa.Column("signature", sa.LargeBinary, nullable=False),
        sa.Column("key_id", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("seal_up_to_idx", "seal", ["up_to_seq"])
    for statement in grants.evidence_table_sql("seal"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("seal")
