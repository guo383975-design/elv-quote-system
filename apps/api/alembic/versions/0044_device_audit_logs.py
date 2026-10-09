"""设备库操作留痕表 device_audit_logs（2026-10-09）。

Revision ID: 0044
Revises: 0043

问题现场：报价单 / 项目 / 登录 / 设置都有留痕 ✓，**设备库没有** ✗ ——
改一台设备的进价、把型号标成停产、停用一台设备，事后都答不上
"是谁、什么时候改的、改前是什么"✗（而这些值直接决定报价的成本与毛利 ✗）。

本迁移只**新建一张表** ✓：
  · `operator_id` / `entity_id` **都不加外键** ✗ —— 与 `factory_reset_logs`（0041）
    同一理由 ✓：`factory_reset` 的 `scope=all` 会清空 `devices` /
    `device_categories` / `users`（见 services/factory_reset.py ✓），
    加了 FK（哪怕是 SET NULL）就是给"留痕被级联带走"留后门 ✗。
    对象的代号 / 名称、操作者用户名都另存**字符串快照** ✓。
  · 本表**不在** `factory_reset` 的任何清理清单里 ✓（那张清单是显式白名单 ✓）——
    `scope=all` 也删不掉它 ✓（留痕的价值全在"出事之后它还在" ✓）。
  · `action` 是 **varchar(32)** 而**不是** PG 枚举 ✗（与 `setting_audit_logs.action`
    同一做法 ✓）：`audit_action` 那个枚举被报价单/项目留痕共用 ✓，
    给它加值要 `ALTER TYPE ... ADD VALUE` ✗，而那条语句不能在事务块里执行
    （alembic 默认就在事务里）✗ —— 为几个设备库动作动共享枚举不划算 ✗。
  · 幂等（与 0029 / 0041 一致 ✓）：表已存在就直接返回 ✓。

  ⚠ 本迁移**不改任何既有表/行** ✓ —— 生产部署由项目负责人统一排期 ✓。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "device_audit_logs" in set(inspector.get_table_names()):
        return  # 幂等

    op.create_table(
        "device_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # device / category
        sa.Column("entity", sa.String(length=16), nullable=False),
        # 对象 id：**刻意不加外键**（见模块 docstring）
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        # 对象快照：设备编号 / 分类代号 + 名称
        sa.Column("subject_code", sa.String(length=64), nullable=True),
        sa.Column("subject_name", sa.String(length=255), nullable=True),
        # 谁（**刻意不加外键**）+ 用户名快照
        sa.Column("operator_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("operator_username", sa.String(length=64), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("summary", sa.String(length=512), nullable=True),
        sa.Column(
            "changes", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False,
        ),
    )
    op.create_index("ix_device_audit_logs_entity", "device_audit_logs", ["entity"])
    op.create_index("ix_device_audit_logs_entity_id", "device_audit_logs", ["entity_id"])
    op.create_index("ix_device_audit_logs_subject_code", "device_audit_logs", ["subject_code"])
    op.create_index("ix_device_audit_logs_operator_id", "device_audit_logs", ["operator_id"])
    op.create_index("ix_device_audit_logs_action", "device_audit_logs", ["action"])
    op.create_index("ix_device_audit_logs_created_at", "device_audit_logs", ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "device_audit_logs" in set(inspector.get_table_names()):
        op.drop_table("device_audit_logs")
