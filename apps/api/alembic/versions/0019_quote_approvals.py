"""分级审批记录表 quote_approvals（2026-10-04）。

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quote_approvals" in set(inspector.get_table_names()):
        return
    op.create_table(
        "quote_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("required_levels", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("comment", sa.String(500), nullable=True),
        sa.Column("approver_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approver_name", sa.String(64), nullable=True),
        sa.Column("approver_role", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["quote_id"], ["quotes.id"], name="fk_quote_approvals_quote_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["approver_id"], ["users.id"], name="fk_quote_approvals_approver_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_quote_approvals_quote_id", "quote_approvals", ["quote_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quote_approvals" in set(inspector.get_table_names()):
        op.drop_table("quote_approvals")
