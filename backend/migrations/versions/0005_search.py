"""Full-text search: search_chunk with an english tsvector and a GIN index.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.storage import grants

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_chunk",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("record_seq", sa.BigInteger, sa.ForeignKey("record.seq"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),  # text | caption | derived
        sa.Column("source", sa.Text),  # the file a derived text came from
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR,
            sa.Computed("to_tsvector('english', content)", persisted=True),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("search_chunk_tsv_idx", "search_chunk", ["tsv"], postgresql_using="gin")
    op.create_index("search_chunk_record_idx", "search_chunk", ["record_seq", "kind"])
    for statement in grants.operational_table_sql("search_chunk"):
        op.execute(statement)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO factual_runtime")


def downgrade() -> None:
    op.drop_table("search_chunk")
