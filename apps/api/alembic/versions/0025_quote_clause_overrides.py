"""单据级条款与有效期覆盖（2026-10-05）。

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quotes" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("quotes")}
    if "clauses_override" not in cols:
        op.add_column(
            "quotes", sa.Column("clauses_override", postgresql.JSONB(), nullable=True)
        )
    if "valid_days_override" not in cols:
        op.add_column(
            "quotes", sa.Column("valid_days_override", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quotes" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("quotes")}
        if "valid_days_override" in cols:
            op.drop_column("quotes", "valid_days_override")
        if "clauses_override" in cols:
            op.drop_column("quotes", "clauses_override")
