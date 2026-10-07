"""定额章节枚举补齐《浙江省通用安装工程定额（2025版）》第五册：新增 5-9。

Revision ID: 0034
Revises: 0033

背景：
    2025 版第五册比 2018 版多了一章「5-9 时钟系统」（24 个子目 ✓）。
    库里 `quota_category` 枚举只到 5-8 ✗ → 直接写 5-9 会撞
    `invalid input value for enum quota_category` ✗，所以先补枚举值 ✓。

设计约束（与 0033 同一套）：
    - PostgreSQL 12+ 允许事务内 ALTER TYPE ... ADD VALUE，但新取值要到提交后才可用；
      本迁移只加值、不插数据，因此安全 ✓
    - 用 IF NOT EXISTS 保证可重复执行 ✓
    - SQLite（测试环境）用 VARCHAR + CHECK 存枚举，取值来自 models，无需也无法 ALTER ✓
"""

from __future__ import annotations

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

# 2025 版第五册里 0001/0033 都没有的取值
_NEW_VALUES = ("5-9",)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE quota_category ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL 不支持删除枚举取值；本迁移只加值，回滚时保留不影响任何约束 ✓
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
