"""设置/权限变更留痕表（2026-10-05）。

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "setting_audit_logs" in set(inspector.get_table_names()):
        return  # 幂等
    op.create_table(
        "setting_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=512), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
    )
    op.create_index("ix_setting_audit_logs_user_id", "setting_audit_logs", ["user_id"])
    op.create_index("ix_setting_audit_logs_action", "setting_audit_logs", ["action"])
    op.create_index("ix_setting_audit_logs_created_at", "setting_audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("setting_audit_logs")
