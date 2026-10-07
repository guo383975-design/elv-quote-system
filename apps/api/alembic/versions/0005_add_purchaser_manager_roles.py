"""新增角色 purchaser(采购) / manager(经理) 到 user_role 枚举。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL 12+ 支持在事务内执行 ADD VALUE，但不能在同一事务里立刻使用新值；
    # 本迁移只加值、不使用，安全。SQLite 测试环境不跑迁移。
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'purchaser'")
    op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'manager'")


def downgrade() -> None:
    # PostgreSQL 无法从枚举中删除值而不重建类型并重写所有依赖列；
    # 这里有意不实现删除，保留新增值。如需回滚需人工重建 user_role 类型。
    pass
