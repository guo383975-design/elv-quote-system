"""工艺系数结构化字段：为 quote_items 增加 is_shielded / is_cat5e / is_ceiling。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29 16:30:00

变更背景：
    2022.7.13 智能化纪要要求「屏蔽线 / 超五类 / 吊顶内」三种工艺场景要单独
    计价。以前靠 quote_items.notes 文本里 "shielded" 之类的关键字匹配，
    多写一个字就静默失效（详见 quote_compute._compute_quota_total 的注释）。
    改成 boolean 字段后由前端用 checkbox 控制，cat5e_coef / ceiling_coef 也
    真用上 —— 以前只是 schema 里摆了占位，service 层不读。

设计约束：
    - 默认 false，不允许 null（NOT NULL）；不阻断老报价单
    - 保留 shielded_coef / cat5e_coef / ceiling_coef 三列作为历史字段，
      至少在本任务范围内前端不再使用
    - 迁移必须可重放：用 ``ADD COLUMN IF NOT EXISTS`` 防止同一环境上
      多次跑 upgrade head 时报 duplicate column；downgrade 用
      ``DROP COLUMN IF EXISTS`` 保持对称
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL 9.6+ 支持 ``ADD COLUMN IF NOT EXISTS``，跑多次不会冲突。
    # 用 server_default=false 把已有行填上 false，比给个 NULL 然后强制 NOT NULL
    # 安全得多 —— 后者要全表扫一次写锁，前者在 catalog 里一次元数据变更搞定。
    op.execute(
        "ALTER TABLE quote_items "
        "ADD COLUMN IF NOT EXISTS is_shielded BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE quote_items "
        "ADD COLUMN IF NOT EXISTS is_cat5e BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE quote_items "
        "ADD COLUMN IF NOT EXISTS is_ceiling BOOLEAN NOT NULL DEFAULT FALSE"
    )


def downgrade() -> None:
    # 反向：丢掉三个结构化字段。
    # 不动 shielded_coef / cat5e_coef / ceiling_coef 三列（它们本来就归
    # quota_items 表 + 0001 迁移管，本迁移未触列）。
    op.execute("ALTER TABLE quote_items DROP COLUMN IF EXISTS is_shielded")
    op.execute("ALTER TABLE quote_items DROP COLUMN IF EXISTS is_cat5e")
    op.execute("ALTER TABLE quote_items DROP COLUMN IF EXISTS is_ceiling")
