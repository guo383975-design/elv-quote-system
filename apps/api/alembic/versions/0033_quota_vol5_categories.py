"""定额章节枚举补齐第五册 5-2 / 5-4 / 5-8 三章。

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-06 14:40:00

⚠ 2026-10-05：本文件原编号 0003，与原 0003_subsystem_supplier.py **撞 revision id**，
   导致 alembic 报 "Multiple head revisions are present"、
   **每次部署都在第 3 步（alembic upgrade head）直接失败** ✗。
   已改号为 0033 并接到 0032 之后 —— 本迁移只 ADD VALUE，与顺序无关，接链尾安全。

变更背景：
    quota_category 枚举在 0001 里只建了 5-1 / 5-3 / 5-5 / 5-6 / 5-7 / other，
    而且 5-1 的语义是「综合布线」、5-7 是「管槽」—— 与《浙江省通用安装工程
    预算定额（2018版）》第五册的真值不符：综合布线与管槽实际都在 5-2，
    5-7 是电涌保护，另外 5-4（广播与有线电视）、5-8（家居智能化）两整章缺失。

    2026-10-06 从广联达浙江定额库安装包解析出第五册全部 843 条子目后，
    按真实章节校正了 app/models/labor.py 的 QuotaCategory 与
    app/core/labels.py 的中文映射；本迁移负责把 **新增的三个取值** 补进
    PostgreSQL 的 quota_category 类型，否则导入真值子目（5-2-13 等）
    会直接撞 `invalid input value for enum quota_category`。

设计约束：
    - PostgreSQL 12+ 允许在事务块内执行 ALTER TYPE ... ADD VALUE，但新取值
      要到该事务提交后才可用；本迁移只加值、不插数据，因此安全。
    - 用 ADD VALUE IF NOT EXISTS 保证可重复执行（同一环境多次 upgrade head
      不会报 duplicate）。
    - SQLite（测试环境）没有独立的枚举类型，列是带 CHECK 约束的 VARCHAR，
      由 models 建表生成，不需要也无法 ALTER；此处按方言跳过。
    - 旧取值（5-1 曾表示「综合布线」）**不能**用 ALTER TYPE 删除，
      PostgreSQL 不支持；语义修正由 labels 映射 + 数据订正脚本承担。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

# 真值章节里 0001 没有的三个取值
_NEW_VALUES = ("5-2", "5-4", "5-8")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite 用 VARCHAR + CHECK 存枚举，取值集合来自 models，无需迁移。
        return
    for value in _NEW_VALUES:
        op.execute(f"ALTER TYPE quota_category ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # PostgreSQL 无法删除枚举取值（要重建类型并迁移所有引用列）。
    # 本迁移只新增取值，回滚到 0032 时保留这些取值不会破坏任何约束，
    # 因此有意做成无损空操作，而不是伪装成可逆。
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _ = sa.text("SELECT 1")  # 占位：保持 sa 导入被使用，说明这里刻意不动 schema
