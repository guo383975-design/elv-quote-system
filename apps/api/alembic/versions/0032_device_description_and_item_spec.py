"""设备描述 + 报价明细的设备参数

Revision ID: 0032
Revises: 0031

2026-10-05：
  · devices.description     设备描述/性能参数（多行文本）——选产品时悬浮显示；
  · quote_items.spec_text   报价单的「设备参数」栏（按设备描述快照，可手工改）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "quote_items", sa.Column("spec_text", sa.String(length=512), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("quote_items", "spec_text")
    op.drop_column("devices", "description")
