"""设备库分类树：device_categories 表 + devices.category_id + 存量回填。

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30 09:00:00

## 这个迁移做什么

1. 建 `device_categories`（自引用树：parent_id / code / name / sort_order / is_active /
   legacy_enum / code_prefix）。
2. 按 `DeviceCategory` 枚举**派生**出 12 个一级节点（code = 枚举值，name = 中文标签，
   legacy_enum = 枚举值，code_prefix = app.core.device_tree 里那份映射）。
   不另写一份分类清单 —— 枚举与标签是这个项目里既有的、稳定的唯一来源。
3. `devices.category_id`（FK → device_categories.id, ON DELETE SET NULL）+ 索引。
4. 把存量设备的 `category_id` 回填到与 `devices.category` 对应的一级节点上。
   回填之后每个设备至少落在一级分类里，界面不会出现"未分类"，也不会丢数据。
5. 二级分类不在这里建：那是**业务数据**，由 `scripts/seed.py` 交付一套初值，
   之后由使用方在界面上自己维护（见 app/core/device_tree.py 的 SEED_CHILDREN）。

## 为什么一级节点放迁移里

它们与已有的 NOT NULL 枚举列一一对应，属于"结构的一部分"（没有它们，
`category_id` 就无法回填、树就是空的）。二级分类才是可变的业务数据。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _root_rows() -> list[dict]:
    """12 个一级节点。枚举 + labels 是唯一来源，前缀取自 core.device_tree。"""
    from app.core.device_tree import CODE_PREFIX_BY_CATEGORY
    from app.core.labels import label_of
    from app.models.device import DeviceCategory

    rows = []
    for i, member in enumerate(DeviceCategory):
        value = str(member)
        rows.append(
            {
                "code": value,
                "name": label_of("device_category", value),
                "sort_order": i,
                "legacy_enum": value,
                "code_prefix": CODE_PREFIX_BY_CATEGORY.get(value),
            }
        )
    return rows


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ----- 1) 分类树表 -----
    if "device_categories" not in inspector.get_table_names():
        op.create_table(
            "device_categories",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "parent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("device_categories.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("code", sa.String(64), nullable=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("legacy_enum", sa.String(32), nullable=True),
            sa.Column("code_prefix", sa.String(4), nullable=True),
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
        op.create_index("ix_device_categories_parent_id", "device_categories", ["parent_id"])
        op.create_index("ix_device_categories_code", "device_categories", ["code"], unique=True)
        op.create_index(
            "ix_device_categories_legacy_enum", "device_categories", ["legacy_enum"]
        )

    # ----- 2) 12 个一级节点（幂等：按 code 跳过已存在的）-----
    for row in _root_rows():
        exists = bind.execute(
            sa.text("select 1 from device_categories where code = :c"), {"c": row["code"]}
        ).first()
        if exists:
            continue
        bind.execute(
            sa.text(
                "insert into device_categories "
                "(id, parent_id, code, name, sort_order, is_active, legacy_enum, "
                " code_prefix, notes, created_at, updated_at) "
                "values (gen_random_uuid(), null, :code, :name, :sort_order, true, "
                "        :legacy_enum, :code_prefix, null, now(), now())"
            ),
            row,
        )

    # ----- 3) devices.category_id -----
    dev_cols = {c["name"] for c in inspector.get_columns("devices")}
    if "category_id" not in dev_cols:
        op.add_column(
            "devices",
            sa.Column(
                "category_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("device_categories.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index("ix_devices_category_id", "devices", ["category_id"])

    # ----- 4) 回填：按 devices.category（枚举值）对上 legacy_enum -----
    bind.execute(
        sa.text(
            "update devices d set category_id = c.id "
            "from device_categories c "
            "where c.legacy_enum = d.category::text and d.category_id is null"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dev_cols = {c["name"] for c in inspector.get_columns("devices")}
    if "category_id" in dev_cols:
        op.drop_index("ix_devices_category_id", table_name="devices")
        op.drop_column("devices", "category_id")
    if "device_categories" in inspector.get_table_names():
        op.drop_table("device_categories")
