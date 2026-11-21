"""Seal anchors: RFC 3161 tokens (and later optional anchors) for each seal.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "seal_anchor",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("seal_no", sa.BigInteger, sa.ForeignKey("seal.no"), nullable=False),
        sa.Column(
            "kind", sa.Text, nullable=False
        ),  # tsa | qualified_tsa | ots_pending | ots_complete
        sa.Column("issuer", sa.Text, nullable=False),  # TSA or calendar URL
        sa.Column("proof", sa.LargeBinary, nullable=False),  # the token or .ots file
        sa.Column("chain_pem", sa.LargeBinary),  # the TSA's certificate chain
        sa.Column("gen_time", sa.DateTime(timezone=True)),
        sa.Column("policy", sa.Text),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("seal_anchor_seal_idx", "seal_anchor", ["seal_no", "kind"])
    for statement in grants.evidence_table_sql("seal_anchor"):
        op.execute(statement)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO factual_runtime")


def downgrade() -> None:
    op.drop_table("seal_anchor")
