"""derived_text: what Claude read from images and PDFs; a projection, never evidence.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.storage import grants

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "derived_text",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("record_seq", sa.BigInteger, sa.ForeignKey("record.seq"), nullable=False),
        sa.Column("blob_seq", sa.BigInteger, sa.ForeignKey("blob.seq"), nullable=False),
        sa.Column("file_name", sa.Text),
        sa.Column("mime", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("pages", sa.Integer),
        sa.Column("chars", sa.Integer),
    )
    op.create_index("derived_text_blob_idx", "derived_text", ["blob_seq"], unique=True)
    op.create_index("derived_text_record_idx", "derived_text", ["record_seq"])
    for statement in grants.operational_table_sql("derived_text"):
        op.execute(statement)
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO factual_runtime")


def downgrade() -> None:
    op.drop_table("derived_text")
