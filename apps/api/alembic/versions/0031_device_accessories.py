"""配件/授权：device_accessory_groups + device_accessory_options + quote_items.parent_item_id

Revision ID: 0031
Revises: 0030

2026-10-05：产品库支持"配件/授权"。
  · 主设备（如防火墙）可以配若干**槽**（授权/电源模块…），每个槽里放若干**候选**，
    候选本身也是产品库条目（授权有独立编码与价格，能单独报价）；
  · 报价明细加 parent_item_id：记下"这行是那台设备的配件"。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "device_accessory_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("select_mode", sa.String(length=16), nullable=False,
                  server_default="one"),
        sa.Column("quantity_mode", sa.String(length=16), nullable=False,
                  server_default="per_unit"),
        sa.Column("default_quantity", sa.Numeric(precision=12, scale=3),
                  nullable=False, server_default="1"),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_device_accessory_groups_device_id",
                    "device_accessory_groups", ["device_id"])

    op.create_table(
        "device_accessory_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accessory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["group_id"], ["device_accessory_groups.id"],
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["accessory_id"], ["devices.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("group_id", "accessory_id",
                            name="uq_device_accessory_option"),
    )
    op.create_index("ix_device_accessory_options_group_id",
                    "device_accessory_options", ["group_id"])
    op.create_index("ix_device_accessory_options_accessory_id",
                    "device_accessory_options", ["accessory_id"])

    op.add_column(
        "quote_items",
        sa.Column("parent_item_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_quote_items_parent_item_id", "quote_items", "quote_items",
        ["parent_item_id"], ["id"], ondelete="CASCADE",
    )
    op.create_index("ix_quote_items_parent_item_id", "quote_items", ["parent_item_id"])


def downgrade() -> None:
    op.drop_index("ix_quote_items_parent_item_id", table_name="quote_items")
    op.drop_constraint("fk_quote_items_parent_item_id", "quote_items", type_="foreignkey")
    op.drop_column("quote_items", "parent_item_id")
    op.drop_index("ix_device_accessory_options_accessory_id",
                  table_name="device_accessory_options")
    op.drop_index("ix_device_accessory_options_group_id",
                  table_name="device_accessory_options")
    op.drop_table("device_accessory_options")
    op.drop_index("ix_device_accessory_groups_device_id",
                  table_name="device_accessory_groups")
    op.drop_table("device_accessory_groups")
