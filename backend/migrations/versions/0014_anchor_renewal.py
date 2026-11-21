"""seal_anchor.renews: a token over an older token, made before the authority's certificate expires.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "seal_anchor", sa.Column("renews", sa.BigInteger, sa.ForeignKey("seal_anchor.id"))
    )


def downgrade() -> None:
    op.drop_column("seal_anchor", "renews")
