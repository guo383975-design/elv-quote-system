"""导入批次留档表 import_batches（2026-10-04，原型里的 #IM-xxx）。

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# 枚举用 UUIDType/JSONType 的等价物：这里直接写 PG 原生类型
_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 枚举类型（幂等）
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE import_kind AS ENUM ('device', 'inquiry'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE import_status AS ENUM ('success', 'partial', 'failed'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )

    if "import_batches" in set(inspector.get_table_names()):
        return

    op.create_table(
        "import_batches",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("batch_no", sa.String(32), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM("device", "inquiry", name="import_kind", create_type=False),
            nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            postgresql.ENUM(
                "success", "partial", "failed", name="import_status", create_type=False
            ),
            nullable=False,
        ),
        # server_default 必须走 sa.text()：写成普通字符串时 SQLAlchemy 会把它当
        # 字面量再加一层引号（`'''[]''::jsonb'`），真 PG 上直接建表失败 ——
        # 这个坑就是 deploy/verify-migrations.sh 第一次跑时抓出来的。
        sa.Column(
            "summary", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "errors", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_by", _UUID, nullable=True),
        sa.Column("created_by_name", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_import_batches_created_by",
            ondelete="SET NULL",
        ),
    )
    # unique=True：模型写的是 unique=True + index=True（唯一索引），
    # 迁移必须一致，否则 alembic check 会报"要删约束、改建唯一索引"
    op.create_index(
        "ix_import_batches_batch_no", "import_batches", ["batch_no"], unique=True
    )
    op.create_index("ix_import_batches_kind", "import_batches", ["kind"])
    op.create_index("ix_import_batches_status", "import_batches", ["status"])
    op.create_index("ix_import_batches_created_by", "import_batches", ["created_by"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "import_batches" in set(inspector.get_table_names()):
        op.drop_table("import_batches")
    op.execute("DROP TYPE IF EXISTS import_status")
    op.execute("DROP TYPE IF EXISTS import_kind")
