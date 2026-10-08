"""恢复出厂设置留痕表（2026-10-08 事故后新增）。

Revision ID: 0041
Revises: 0040

2026-10-08 生产库被清空，而 factory_reset 的清理范围**包含三张 *_audit_logs**，
于是"谁清的、什么时候清的"查无对证。这张表是那次事故的直接修复：
它记录每次 reset 的操作者、范围、备份路径、清理前后各表行数，
并且**不在任何清理清单里**（见 services/factory_reset.py），scope=all 也删不掉。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "factory_reset_logs" in set(inspector.get_table_names()):
        return  # 幂等（与 0029 一致：重复执行不报错）
    op.create_table(
        "factory_reset_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # 刻意不加外键：scope=all 会把 users 删掉，留痕必须活下来
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operator_username", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("backup_path", sa.String(length=512), nullable=True),
        sa.Column("backup_filename", sa.String(length=256), nullable=True),
        sa.Column(
            "counts", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "deleted", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("total_deleted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
    )
    op.create_index("ix_factory_reset_logs_operator_id", "factory_reset_logs", ["operator_id"])
    op.create_index(
        "ix_factory_reset_logs_operator_username", "factory_reset_logs", ["operator_username"]
    )
    op.create_index("ix_factory_reset_logs_scope", "factory_reset_logs", ["scope"])
    op.create_index("ix_factory_reset_logs_status", "factory_reset_logs", ["status"])
    op.create_index("ix_factory_reset_logs_created_at", "factory_reset_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("factory_reset_logs")
