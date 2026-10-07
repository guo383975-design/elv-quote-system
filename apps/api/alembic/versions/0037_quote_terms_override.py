"""quotes.terms_override：每单条款数字覆盖。

Revision ID: 0037
Revises: 0036

用户要求（2026-10-05）：每张报价单的条款数字（有效期/预付/验收/质保金/质保期/工期）
可以不一样 ✓，但**不改变任何既有行为** —— 该字段为空时完全沿用系统设置 ✓。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quotes",
        sa.Column("terms_override", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quotes", "terms_override")
