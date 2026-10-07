"""项目操作留痕表 project_audit_logs（2026-10-02）。

报价单一直有 quote_audit_logs，项目状态却是裸 setattr、改完不留痕 ——
「谁把这一单标成已中标的」答不上来。本迁移补一张同构表。

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "project_audit_logs" in inspector.get_table_names():
        return

    op.create_table(
        "project_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # audit_action 这个枚举类型由 0001 建好，这里复用（create_type=False 语义）
        sa.Column(
            "action",
            postgresql.ENUM(name="audit_action", create_type=False),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_project_audit_logs_project_id", "project_audit_logs", ["project_id"])
    op.create_index("ix_project_audit_logs_action", "project_audit_logs", ["action"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "project_audit_logs" in inspector.get_table_names():
        op.drop_table("project_audit_logs")
