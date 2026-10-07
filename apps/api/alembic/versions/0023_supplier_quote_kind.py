"""导入批次加一类：供应商报价（2026-10-04）。

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE import_kind ADD VALUE IF NOT EXISTS 'supplier_quote'"
    )


def downgrade() -> None:
    # PG 不支持从枚举里删值（要重建类型）。保持原样：多一个值不影响任何逻辑。
    pass
