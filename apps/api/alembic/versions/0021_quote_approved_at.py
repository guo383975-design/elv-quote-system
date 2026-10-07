"""quotes.approved_at（2026-10-04）。

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quotes" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("quotes")}
    if "approved_at" not in cols:
        op.add_column(
            "quotes", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.create_index("ix_quotes_approved_at", "quotes", ["approved_at"])
        # 回填：从批准版快照取（新库没有历史数据，这一步是给已有库用的）
        op.execute(
            """
            UPDATE quotes q SET approved_at = r.created_at
            FROM (
                SELECT quote_id, MAX(created_at) AS created_at
                FROM quote_revisions WHERE reason = 'approved' GROUP BY quote_id
            ) r
            WHERE r.quote_id = q.id AND q.status = 'approved' AND q.approved_at IS NULL
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "quotes" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("quotes")}
        if "approved_at" in cols:
            op.drop_index("ix_quotes_approved_at", table_name="quotes")
            op.drop_column("quotes", "approved_at")
