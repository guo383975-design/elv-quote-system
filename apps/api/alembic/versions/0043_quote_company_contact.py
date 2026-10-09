"""quotes 增加 company_address / company_phone / company_email（抬头的另外三项）。

Revision ID: 0043
Revises: 0042

用户补充（2026-10-09）：原话「地址和联系电话和邮箱还是要修改一下的」——
第 0042 号迁移只加了 `company_name`（当时用户说"只要公司名称"），现在补上三项。

⚠ 与 `company_name` **同一套快照语义**：存的是**当时那个值本身**，不是指向
   `app_settings.company.letterheads` 里某一条的 id / 下标 ⇒ 以后改 / 删抬头库，
   **不会**追溯改写已开出的报价单 ✓。

⚠ 取值是**逐字段**独立回落 ✓（本单这一项空 → 用系统设置的那一项 ✓）：
   只给抬头填了名称的，地址/电话/邮箱要各自用「系统设置」那套，
   而不是整组变空白 ✓（见 services/export_data._export_settings ✓）。

本迁移**只加三列**：
  · 全部 nullable ✓ —— 老单这三列为 NULL，导出时各自回落系统设置，
    行为与加这个功能之前**完全一致** ✓；
  · **不设 server_default** ✓ —— 不写任何值到既存行（老单数据一行不改 ✓）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("company_address", sa.String(length=255), nullable=True))
    op.add_column("quotes", sa.Column("company_phone", sa.String(length=64), nullable=True))
    op.add_column("quotes", sa.Column("company_email", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("quotes", "company_email")
    op.drop_column("quotes", "company_phone")
    op.drop_column("quotes", "company_address")
