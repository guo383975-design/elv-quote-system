"""阶梯价表 + 设备底价 + 越权放行留痕动作（2026-10-04）。

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # 低于底价放行也要留痕 —— 审计动作枚举加一个值
    op.execute("ALTER TYPE audit_action ADD VALUE IF NOT EXISTS 'price_floor_override'")

    if "devices" in tables:
        cols = {c["name"] for c in inspector.get_columns("devices")}
        if "min_price" not in cols:
            op.add_column("devices", sa.Column("min_price", sa.Numeric(12, 2), nullable=True))

    if "device_price_tiers" not in tables:
        op.create_table(
            "device_price_tiers",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("device_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("min_qty", sa.Numeric(12, 2), nullable=False),
            sa.Column("max_qty", sa.Numeric(12, 2), nullable=True),
            sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
            sa.Column("note", sa.String(128), nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["device_id"], ["devices.id"],
                name="fk_device_price_tiers_device_id", ondelete="CASCADE",
            ),
            sa.UniqueConstraint(
                "device_id", "min_qty", name="uq_device_price_tiers_device_min"
            ),
        )
        op.create_index("ix_device_price_tiers_device_id", "device_price_tiers", ["device_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "device_price_tiers" in set(inspector.get_table_names()):
        op.drop_table("device_price_tiers")
    if "devices" in set(inspector.get_table_names()):
        cols = {c["name"] for c in inspector.get_columns("devices")}
        if "min_price" in cols:
            op.drop_column("devices", "min_price")
