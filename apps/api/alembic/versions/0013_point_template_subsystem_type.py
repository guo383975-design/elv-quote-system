"""消掉 `alembic check` 在真 PG 上报出的两处漂移 + 列类型对齐（2026-10-04，M-1）。

这三件事都是 `deploy/verify-migrations.sh`（真 PG 上从零跑迁移 + 模型比对）
第一次跑就抓出来的 —— 以前没有这道校验，它们会一直躺着：

## ① `point_templates.subsystem_type` 类型不对（我 0009 写错的）

0009 里写成 `sa.String(32)`，而模型是 `sa_enum(SubsystemType, "subsystem_type")`。
枚举名与 `subsystems.subsystem_type` 相同、取值也一致，所以能跑 —— 但结构是漂的。
对齐到枚举；演示数据 3 行取值都在枚举里，`USING ...::subsystem_type` 安全。

## ② `suppliers.contact_person` 是遗留列，但**线上还有 4 行有值**

0001 建的是 `contact_person`，0003 加了 `contact_name` 之后前者就没再用过
（代码零引用，只剩那行迁移）。但直接 DROP 会丢数据 —— 30 家供应商里有 4 家
的联系人**只写在旧列里**。所以先搬数据（contact_name 为空时用 contact_person 补），
再删列。这类"遗留列带数据"是最容易被 DROP 掉的坑：漂移报告只说"多了一列"。

## ③ `devices.install_unit_price` 的列注释只写在模型里

模型有注释 `点位单价（元/点）；0 表示无安装费`，迁移没写 → 每次比对报 modify_comment。
把注释补到库上（对齐模型），而不是删模型的注释（注释是有用的文档）。

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INSTALL_PRICE_COMMENT = "点位单价（元/点）；0 表示无安装费"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ---- ① point_templates.subsystem_type → 枚举 ----
    if "point_templates" in tables:
        col = next(
            (c for c in inspector.get_columns("point_templates")
             if c["name"] == "subsystem_type"),
            None,
        )
        if col is not None and isinstance(col["type"], sa.String):   # 幂等
            op.execute(
                "ALTER TABLE point_templates "
                "ALTER COLUMN subsystem_type TYPE subsystem_type "
                "USING subsystem_type::subsystem_type"
            )

    # ---- ② 遗留列 suppliers.contact_person：先搬数据再删 ----
    if "suppliers" in tables:
        cols = {c["name"] for c in inspector.get_columns("suppliers")}
        if "contact_person" in cols:
            if "contact_name" in cols:
                op.execute(
                    "UPDATE suppliers SET contact_name = contact_person "
                    "WHERE contact_person IS NOT NULL "
                    "  AND (contact_name IS NULL OR contact_name = '')"
                )
            op.drop_column("suppliers", "contact_person")

    # ---- ③ 补列注释（对齐模型）----
    if "devices" in tables:
        target = next(
            (c for c in inspector.get_columns("devices")
             if c["name"] == "install_unit_price"),
            None,
        )
        if target is not None and not target.get("comment"):
            op.alter_column(
                "devices",
                "install_unit_price",
                existing_type=sa.Numeric(12, 2),
                comment=_INSTALL_PRICE_COMMENT,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "devices" in tables:
        op.alter_column(
            "devices", "install_unit_price", existing_type=sa.Numeric(12, 2), comment=None
        )
    if "suppliers" in tables:
        cols = {c["name"] for c in inspector.get_columns("suppliers")}
        if "contact_person" not in cols:
            # 只恢复列结构；数据搬回去不可逆（contact_name 本来就有值的行分不清来源）
            op.add_column(
                "suppliers", sa.Column("contact_person", sa.String(64), nullable=True)
            )
    if "point_templates" in tables:
        col = next(
            (c for c in inspector.get_columns("point_templates")
             if c["name"] == "subsystem_type"),
            None,
        )
        if col is not None and not isinstance(col["type"], sa.String):
            op.execute(
                "ALTER TABLE point_templates "
                "ALTER COLUMN subsystem_type TYPE VARCHAR(32) "
                "USING subsystem_type::text"
            )
