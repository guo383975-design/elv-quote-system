"""留痕加固：IP/UA/单号快照 + 删单不销毁留痕 + 登录留痕（2026-10-04，H-10）。

## 为什么

审计报告 H-10 指出三件事，这一版一起修：

1. **完全没有 IP**：出了事回答不了"是谁、从哪台机器干的"。
2. **留痕会被删单/删项目连带抹掉**：`quote_audit_logs.quote_id` 与
   `project_audit_logs.project_id` 的外键是 `ON DELETE CASCADE` ——
   删掉一张报价单，它的全部操作记录一起消失。留着这条记录的人恰恰可能是要掩盖什么的人。
   改为 **可空 + `ON DELETE SET NULL`**，并新增 `subject_code` 存**单号快照**，
   这样删单之后留痕仍然看得出"这是哪张单的事"。
3. **动作覆盖不全**：新增 `cost_view`（看成本也算敏感操作）、登录尝试另立一张
   `login_audit_logs`（既审计，也是登录失败锁定的计数依据）。

## 为什么外键可以变可空

留痕行是**事实记录**：它引用报价单，但不"属于"报价单。业务对象没了，
记录本身仍应存在（现实里纸质台账就是这么留的）。

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_UUID = postgresql.UUID(as_uuid=True)

# (表, 外键列, 引用表)
_AUDIT_TABLES = (
    ("quote_audit_logs", "quote_id", "quotes"),
    ("project_audit_logs", "project_id", "projects"),
)


def _fk_name(inspector: sa.Inspector, table: str, column: str) -> str | None:
    """找出某个列上的外键名（PG 自动命名的名字不一定叫 *_fkey，得查）。"""
    for fk in inspector.get_foreign_keys(table):
        if fk.get("constrained_columns") == [column] or column in (
            fk.get("constrained_columns") or []
        ):
            return fk.get("name")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ---- ① 枚举加一个动作（PG 12+ 允许在事务内 ADD VALUE）----
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'cost_view'")

    # ---- ② 两张留痕表：加 IP / UA / 单号快照，外键改 SET NULL 且可空 ----
    for table, column, ref in _AUDIT_TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "subject_code" not in cols:
            op.add_column(table, sa.Column("subject_code", sa.String(64), nullable=True))
            op.create_index(f"ix_{table}_subject_code", table, ["subject_code"])
        if "ip" not in cols:
            op.add_column(table, sa.Column("ip", sa.String(64), nullable=True))
        if "user_agent" not in cols:
            op.add_column(table, sa.Column("user_agent", sa.String(256), nullable=True))

        # 外键：先删旧的（CASCADE），再建 SET NULL；列改成可空
        old = _fk_name(inspector, table, column)
        if old:
            op.drop_constraint(old, table, type_="foreignkey")
        op.alter_column(table, column, existing_type=_UUID, nullable=True)
        op.create_foreign_key(
            f"fk_{table}_{column}", table, ref, [column], ["id"], ondelete="SET NULL"
        )

    # ---- ③ 登录尝试留痕（也是失败锁定的计数依据）----
    if "login_audit_logs" not in tables:
        op.create_table(
            "login_audit_logs",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("username", sa.String(64), nullable=False),
            sa.Column("user_id", _UUID, nullable=True),
            sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("reason", sa.String(128), nullable=True),
            sa.Column("ip", sa.String(64), nullable=True),
            sa.Column("user_agent", sa.String(256), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_login_audit_logs_username", "login_audit_logs", ["username"])
        op.create_index("ix_login_audit_logs_success", "login_audit_logs", ["success"])
        op.create_index("ix_login_audit_logs_ip", "login_audit_logs", ["ip"])
        op.create_index("ix_login_audit_logs_created_at", "login_audit_logs", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "login_audit_logs" in tables:
        op.drop_table("login_audit_logs")

    for table, column, ref in _AUDIT_TABLES:
        if table not in tables:
            continue
        old = _fk_name(inspector, table, column)
        if old:
            op.drop_constraint(old, table, type_="foreignkey")
        op.create_foreign_key(
            f"fk_{table}_{column}", table, ref, [column], ["id"], ondelete="CASCADE"
        )
        for col in ("subject_code", "ip", "user_agent"):
            cols = {c["name"] for c in inspector.get_columns(table)}
            if col in cols:
                op.drop_column(table, col)
