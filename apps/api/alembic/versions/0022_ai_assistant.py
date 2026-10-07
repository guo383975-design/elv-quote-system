"""AI 助理：调用留痕 + 解析任务表（2026-10-04）。

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    now = sa.text("now()")

    if "ai_usage_logs" not in tables:
        op.create_table(
            "ai_usage_logs",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("user_id", _UUID, nullable=True),
            sa.Column("username", sa.String(64), nullable=True),
            sa.Column("task", sa.String(32), nullable=False),
            sa.Column("model", sa.String(64), nullable=True),
            sa.Column("provider", sa.String(32), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("ok", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("ref_type", sa.String(32), nullable=True),
            sa.Column("ref_id", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name="fk_ai_usage_logs_user_id",
                ondelete="SET NULL",
            ),
        )
        op.create_index("ix_ai_usage_logs_user_id", "ai_usage_logs", ["user_id"])
        op.create_index("ix_ai_usage_logs_task", "ai_usage_logs", ["task"])
        op.create_index("ix_ai_usage_logs_ok", "ai_usage_logs", ["ok"])
        op.create_index("ix_ai_usage_logs_created_at", "ai_usage_logs", ["created_at"])

    if "ai_parse_jobs" not in tables:
        op.create_table(
            "ai_parse_jobs",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("kind", sa.String(32), nullable=False, server_default="supplier_quote"),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("filename", sa.String(255), nullable=True),
            sa.Column("file_sha256", sa.String(64), nullable=True),
            sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mime", sa.String(128), nullable=True),
            sa.Column("supplier_id", _UUID, nullable=True),
            sa.Column("project_id", _UUID, nullable=True),
            sa.Column("extracted_text", sa.Text(), nullable=True),
            sa.Column(
                "result", postgresql.JSONB(), nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "commit_info", postgresql.JSONB(), nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("created_by", _UUID, nullable=True),
            sa.Column("created_by_name", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
            sa.ForeignKeyConstraint(
                ["supplier_id"], ["suppliers.id"], name="fk_ai_parse_jobs_supplier_id",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.id"], name="fk_ai_parse_jobs_project_id",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], name="fk_ai_parse_jobs_created_by",
                ondelete="SET NULL",
            ),
        )
        op.create_index("ix_ai_parse_jobs_status", "ai_parse_jobs", ["status"])
        op.create_index("ix_ai_parse_jobs_file_sha256", "ai_parse_jobs", ["file_sha256"])
        op.create_index("ix_ai_parse_jobs_created_by", "ai_parse_jobs", ["created_by"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "ai_parse_jobs" in tables:
        op.drop_table("ai_parse_jobs")
    if "ai_usage_logs" in tables:
        op.drop_table("ai_usage_logs")
