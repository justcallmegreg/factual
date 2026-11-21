"""The instance row: public key, creation time and the chain's genesis value.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instance",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("public_key", sa.LargeBinary, nullable=False, comment="raw Ed25519 public key"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("genesis_hash", sa.LargeBinary, nullable=False),
        sa.CheckConstraint("id = 1", name="instance_single_row"),
    )
    for statement in grants.evidence_table_sql("instance"):
        op.execute(statement)


def downgrade() -> None:
    op.drop_table("instance")
