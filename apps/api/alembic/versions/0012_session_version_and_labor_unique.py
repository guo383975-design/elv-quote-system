"""会话版本 + 人工信息价唯一约束（2026-10-04，H-8 / M-10）。

两件事，一次迁移：

## ① users.token_version（H-8 强制下线）

令牌里带 `ver`，与这一列比对。强制下线或改密码时 +1 → 该用户手上所有已发出的令牌
**立刻** 401，不用等它自然过期。没有这一列之前，"让人下线"只能靠改密码或等过期。

## ② labor_rates 唯一约束（M-10）

审计报告 M-10：`(region, labor_class, effective_date)` 没有唯一约束，同一期次可以
重复录入；而 `is_current` 是按"地区 + 工种"翻转的，两行同名同期的数据会被静默折叠，
界面上看起来是一行 —— 谁都不知道录重了。

加约束之前必须**先去重**：保留每组里"最新更新"的那行（同等条件下优先保留
`is_current=true` 的），其余删除。这一步是幂等的（跑第二遍没有重复可删）。

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_UQ = "uq_labor_rates_region_class_date"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ---- ① users.token_version ----
    if "users" in tables:
        cols = {c["name"] for c in inspector.get_columns("users")}
        if "token_version" not in cols:
            op.add_column(
                "users",
                sa.Column(
                    "token_version", sa.Integer(), nullable=False, server_default="0"
                ),
            )

    # ---- ② labor_rates：先去重，再加唯一约束 ----
    if "labor_rates" in tables:
        # 同一个 (region, labor_class, effective_date) 只留一行：
        # 优先留 is_current 的，其次留 updated_at 最新的，最后按 id 稳定排序。
        op.execute(
            """
            DELETE FROM labor_rates a
            USING labor_rates b
            WHERE a.region = b.region
              AND a.labor_class = b.labor_class
              AND a.effective_date = b.effective_date
              AND (
                    (a.is_current IS DISTINCT FROM b.is_current AND a.is_current = FALSE)
                 OR (a.is_current = b.is_current AND a.updated_at < b.updated_at)
                 OR (a.is_current = b.is_current AND a.updated_at = b.updated_at
                     AND a.id::text > b.id::text)
              )
            """
        )
        existing = {c.get("name") for c in inspector.get_unique_constraints("labor_rates")}
        if _UQ not in existing:
            op.create_unique_constraint(
                _UQ, "labor_rates", ["region", "labor_class", "effective_date"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "labor_rates" in tables:
        existing = {c.get("name") for c in inspector.get_unique_constraints("labor_rates")}
        if _UQ in existing:
            op.drop_constraint(_UQ, "labor_rates", type_="unique")

    if "users" in tables:
        cols = {c["name"] for c in inspector.get_columns("users")}
        if "token_version" in cols:
            op.drop_column("users", "token_version")
