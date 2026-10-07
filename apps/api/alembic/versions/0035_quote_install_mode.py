"""报价单加「安装单价方式」：manual / quota。

Revision ID: 0035
Revises: 0034

用户设计（2026-10-05）：
  · manual：手动填安装单价，界面不显示定额列
  · quota ：套定额模式，套定额后基价自动填进安装单价
默认 manual（不影响历史报价单 ✓）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quotes",
        sa.Column("install_mode", sa.String(length=16), nullable=False,
                  server_default="manual"),
    )


def downgrade() -> None:
    op.drop_column("quotes", "install_mode")
