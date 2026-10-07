"""点位模板：point_templates + point_template_items（2026-10-04）。

## 为什么

原型里「点位模板」被引用了四处（顶栏导航、添加设备第 2 个页签、
报价编制的「套用点位模板」「批量添加点位」），但后端一直没有这两张表 ——
前端那几处只能做成置灰占位。

一个模板 = 一个「点位」的物料构成 + 该点位的安装单价：

    球机点位模板（视频监控）  安装单价 180 元/点
      · 网络高清球型摄像机 × 1   ← 主设备（is_primary）
      · 壁装支架           × 1
      · 电源适配器         × 1

套用到报价单：点位数 N → 每行数量 = 模板数量 × N；
**安装单价只计一次/点**，落在主设备行（其余行安装费 0）—— 否则一个点位里
3 个组件会各收一次"180 元/点"，安装费翻 3 倍。

## 为什么 device_id 是 SET NULL

设备被停用（is_active=False）时模板行要保留，否则历史模板会莫名缺行；
只有设备被真删时才 SET NULL，套用时跳过并如实报告"缺设备的行"。

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# UUID 列必须用 postgresql.UUID(as_uuid=True)（与 0007 等历史迁移一致）：
# devices.id 是原生 uuid，用 String(36) 建外键会因类型不匹配直接失败。
_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "point_templates" not in tables:
        op.create_table(
            "point_templates",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("code", sa.String(32), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("subsystem_type", sa.String(32), nullable=True),
            sa.Column(
                "install_unit_price",
                sa.Numeric(12, 2),
                nullable=False,
                server_default="0",
            ),
            sa.Column("notes", sa.String(2048), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
        )
        op.create_index("ix_point_templates_code", "point_templates", ["code"], unique=True)
        op.create_index("ix_point_templates_name", "point_templates", ["name"])
        op.create_index("ix_point_templates_is_active", "point_templates", ["is_active"])

    if "point_template_items" not in tables:
        op.create_table(
            "point_template_items",
            sa.Column("id", _UUID, primary_key=True),
            sa.Column("template_id", _UUID, nullable=False),
            sa.Column("device_id", _UUID, nullable=True),
            sa.Column("quantity", sa.Numeric(12, 2), nullable=False, server_default="1"),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("notes", sa.String(512), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.ForeignKeyConstraint(
                ["template_id"], ["point_templates.id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
        )
        op.create_index(
            "ix_point_template_items_template_id", "point_template_items", ["template_id"]
        )
        op.create_index(
            "ix_point_template_items_device_id", "point_template_items", ["device_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "point_template_items" in tables:
        op.drop_table("point_template_items")
    if "point_templates" in tables:
        op.drop_table("point_templates")

