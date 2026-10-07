"""AI 对话：会话与消息表（2026-10-04）。

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    now = sa.text("now()")
    empty_json = sa.text("'[]'::jsonb")
    empty_obj = sa.text("'{}'::jsonb")

    if "ai_conversations" not in tables:
        op.create_table(
            "ai_conversations",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("title", sa.String(120), nullable=False),
            sa.Column("user_id", _UUID, nullable=True),
            sa.Column("user_name", sa.String(64), nullable=True),
            sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("archived", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
            sa.ForeignKeyConstraint(
                ["user_id"], ["users.id"], name="fk_ai_conversations_user_id",
                ondelete="CASCADE",
            ),
        )
        op.create_index("ix_ai_conversations_user_id", "ai_conversations", ["user_id"])
        op.create_index(
            "ix_ai_conversations_last_message_at", "ai_conversations", ["last_message_at"]
        )

    if "ai_messages" not in tables:
        op.create_table(
            "ai_messages",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("conversation_id", _UUID, nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("tool_calls", postgresql.JSONB(), nullable=False, server_default=empty_json),
            sa.Column("tool_call_id", sa.String(64), nullable=True),
            sa.Column("tool_name", sa.String(64), nullable=True),
            sa.Column("attachments", postgresql.JSONB(), nullable=False, server_default=empty_json),
            sa.Column("meta", postgresql.JSONB(), nullable=False, server_default=empty_obj),
            sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
            sa.ForeignKeyConstraint(
                ["conversation_id"], ["ai_conversations.id"],
                name="fk_ai_messages_conversation_id", ondelete="CASCADE",
            ),
        )
        op.create_index("ix_ai_messages_conversation_id", "ai_messages", ["conversation_id"])
        op.create_index("ix_ai_messages_created_at", "ai_messages", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "ai_messages" in tables:
        op.drop_table("ai_messages")
    if "ai_conversations" in tables:
        op.drop_table("ai_conversations")
