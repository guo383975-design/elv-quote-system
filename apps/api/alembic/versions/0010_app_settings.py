"""系统设置表 app_settings（2026-10-04）。

键值 + JSONB：设置项一起读、一起改，比"一列一个开关"好扩展。
默认值在 `app/services/settings.py` 的 DEFAULTS 里，与旧硬编码常量保持一致
（公司抬头、有效期 30 天、6 条报价说明），不建表也能跑 —— 表是"覆盖默认值"用的。

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_settings" in set(inspector.get_table_names()):
        return
    op.create_table(
        "app_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_app_settings_key", "app_settings", ["key"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_settings" in set(inspector.get_table_names()):
        op.drop_table("app_settings")
