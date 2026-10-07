"""组织架构树：departments 表 + users.department_id + 从既有自由文本回填。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02

## 这个迁移做什么

1. 建 `departments`（自引用树：parent_id / code / name / sort_order / is_active / notes）。
2. `users.department_id`（FK → departments.id, ON DELETE SET NULL）+ 索引。
3. **回填**：把 `users.department` 里出现过的**每一个不同的文本值**建成一个根节点，
   再把对应用户挂上去。这样历史数据一个都不丢，而且回填是数据驱动的 ——
   不假设用户填过哪些部门名。
4. 不回填的：`department` 为空的用户（保持未分类，界面上显示「未分配」）。

## 为什么根节点来自数据而不是写死

生产库里用户填的是自由文本（"设计部" / "设计" / "工程部"…），写死一份清单必然
对不上。演示用的规范部门树（公司 → 各部门 → 组）由 `scripts/seed.py` 交付，
使用方之后在界面上随便改 —— 与本项目处理设备分类树的思路一致。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "departments" not in inspector.get_table_names():
        op.create_table(
            "departments",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "parent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("departments.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("code", sa.String(64), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("notes", sa.String(255), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index("ix_departments_parent_id", "departments", ["parent_id"])
        op.create_index("ix_departments_code", "departments", ["code"], unique=True)

    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if "department_id" not in user_cols:
        op.add_column(
            "users",
            sa.Column(
                "department_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("departments.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index("ix_users_department_id", "users", ["department_id"])

    # ----- 回填：每个不同的 department 文本 → 一个根节点 -----
    # code 用 `legacy.<md5 前 8 位>`：部门名可能含中文/空格/特殊字符，
    # 不适合直接当 code（要唯一、要稳定），所以用内容哈希保证幂等。
    bind.execute(
        sa.text(
            "insert into departments (id, parent_id, code, name, sort_order, is_active, notes, "
            "                         created_at, updated_at) "
            "select gen_random_uuid(), null, "
            "       'legacy.' || substr(md5(d.department), 1, 8), "
            "       d.department, 0, true, "
            "       '由 users.department 历史值回填', now(), now() "
            "from (select distinct department from users "
            "      where department is not null and btrim(department) <> '') d "
            "where not exists (select 1 from departments x where x.name = d.department)"
        )
    )
    bind.execute(
        sa.text(
            "update users u set department_id = d.id "
            "from departments d "
            "where d.name = btrim(u.department) and u.department_id is null"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if "department_id" in user_cols:
        op.drop_index("ix_users_department_id", table_name="users")
        op.drop_column("users", "department_id")
    if "departments" in inspector.get_table_names():
        op.drop_table("departments")
