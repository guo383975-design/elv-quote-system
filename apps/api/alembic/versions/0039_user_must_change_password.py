"""users 增加 must_change_password（初始/重置口令强制修改）。

Revision ID: 0039
Revises: 0038

用户要求（2026-10-05）：admin 的初始口令必须强制修改。
本迁移只加一列，并把现有 admin 置为 true（一次性提醒；即便已改过口令，
也只会多要求改一次，代价可接受）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # 现有 admin 强制改一次 ✓（其余账号不动 ✓）
    op.execute("UPDATE users SET must_change_password = true WHERE username = 'admin'")


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
