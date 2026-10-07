"""客户档案表 customers + projects.customer_id（2026-10-04）。

原来客户只是 projects 上的三个自由文本字段，同一个甲方能写成十种名字，
按客户统计和开票信息都做不了。这里加主数据表，并给 projects 加可空外键。

**不回填**：老项目的 customer_name 保持原样（它们本来就是文本），
引用关系留给用户在界面上按需挂接 —— 自动按名字猜客户会造出重复档案。

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "customers" not in tables:
        op.create_table(
            "customers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("code", sa.String(32), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("short_name", sa.String(64), nullable=True),
            sa.Column("contact_name", sa.String(64), nullable=True),
            sa.Column("contact_phone", sa.String(32), nullable=True),
            sa.Column("contact_email", sa.String(128), nullable=True),
            sa.Column("address", sa.String(255), nullable=True),
            sa.Column("tax_no", sa.String(64), nullable=True),
            sa.Column("bank_name", sa.String(128), nullable=True),
            sa.Column("bank_account", sa.String(64), nullable=True),
            sa.Column("industry", sa.String(64), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["created_by"], ["users.id"], name="fk_customers_created_by",
                ondelete="SET NULL",
            ),
        )
        # unique=True：与模型 unique=True + index=True 对齐
        op.create_index("ix_customers_code", "customers", ["code"], unique=True)
        op.create_index("ix_customers_name", "customers", ["name"])
        op.create_index("ix_customers_is_active", "customers", ["is_active"])

    if "projects" in tables:
        cols = {c["name"] for c in inspector.get_columns("projects")}
        if "customer_id" not in cols:
            op.add_column(
                "projects",
                sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=True),
            )
            op.create_foreign_key(
                "fk_projects_customer_id", "projects", "customers",
                ["customer_id"], ["id"], ondelete="SET NULL",
            )
            op.create_index("ix_projects_customer_id", "projects", ["customer_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "projects" in tables:
        cols = {c["name"] for c in inspector.get_columns("projects")}
        if "customer_id" in cols:
            op.drop_index("ix_projects_customer_id", table_name="projects")
            op.drop_constraint("fk_projects_customer_id", "projects", type_="foreignkey")
            op.drop_column("projects", "customer_id")
    if "customers" in tables:
        op.drop_table("customers")
