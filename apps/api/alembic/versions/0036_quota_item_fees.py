"""quota_items 增加定额三费：人工费 / 材料费 / 机械费。

Revision ID: 0036
Revises: 0035

阶段 0（2026-10-05）：广联达模式要在主设备行下挂「定额人工 / 定额辅材 / 定额机械」，
但 quota_items 只有人工消耗量与基价，缺材料/机械。本迁移只加三列金额，
**不改变任何既有计算口径**（报价计算暂不读这三列）。
数据由视觉识读回填（闸门：人工+材料+机械 = 基价，894/894 通过）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("labor_fee", "material_fee", "machine_fee"):
        op.add_column(
            "quota_items",
            sa.Column(col, sa.Numeric(12, 2), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for col in ("machine_fee", "material_fee", "labor_fee"):
        op.drop_column("quota_items", col)
