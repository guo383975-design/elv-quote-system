"""报价单版本快照表 quote_revisions（2026-10-04）。

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quote_revisions" in set(inspector.get_table_names()):
        return
    op.create_table(
        "quote_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column(
            "totals", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "snapshot", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"], ["quotes.id"], name="fk_quote_revisions_quote_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_quote_revisions_created_by",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("quote_id", "seq", name="uq_quote_revisions_quote_seq"),
    )
    op.create_index("ix_quote_revisions_quote_id", "quote_revisions", ["quote_id"])
    op.create_index("ix_quote_revisions_reason", "quote_revisions", ["reason"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quote_revisions" in set(inspector.get_table_names()):
        op.drop_table("quote_revisions")
