"""quotes 增加 company_name（每单可选的「公司抬头」）。

Revision ID: 0042
Revises: 0041

用户需求（2026-10-09）：有时要用**别家公司抬头**去报价。只要公司**名称**一个字段
—— 地址/电话/邮箱/税号/Logo/盖章位一律不要。

⚠ 存的是**当时选定的那个公司名字符串本身**，不是抬头库（app_settings.company.
   letterheads）里的下标或 id。这是**天然快照**：以后改 / 删抬头库，
   **不会**追溯改写已开出的报价单（用户硬约束：「历史单据绝不能跟着变」）。

本迁移**只加一列**：
  · nullable ✓ —— 老单该列为 NULL，导出时回落「系统设置 → 公司名称」（XX科技），
    行为与加这个功能之前**完全一致** ✓；
  · **不设 server_default** ✓ —— 不写任何值到既存行（老单数据一行不改 ✓）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("company_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("quotes", "company_name")
