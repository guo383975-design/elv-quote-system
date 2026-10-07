"""辅材「按工程量」：明细与设备的单位辅材费（2026-10-04）。

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_COMMENT = "单位工程量辅材费（元/单位）；辅材口径选「按工程量」时按 Σ(数量×本列) 计"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    for table in ("quote_items", "devices"):
        if table not in tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if "auxiliary_unit_price" not in cols:
            op.add_column(
                table,
                sa.Column(
                    "auxiliary_unit_price", sa.Numeric(12, 4), nullable=False,
                    server_default="0", comment=_COMMENT,
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table in ("quote_items", "devices"):
        if table in tables:
            cols = {c["name"] for c in inspector.get_columns(table)}
            if "auxiliary_unit_price" in cols:
                op.drop_column(table, "auxiliary_unit_price")
