"""quotes 增加 based_on_quote_id（增项关联）。

Revision ID: 0040
Revises: 0039

用户要求：归档后的报价单不可改，但可以**新建一张关联原单的报价单**用于增项。
本迁移只加一列（自引用外键，删除原单时置空）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("based_on_quote_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_quotes_based_on_quote_id",
        "quotes",
        "quotes",
        ["based_on_quote_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_quotes_based_on_quote_id", "quotes", type_="foreignkey")
    op.drop_column("quotes", "based_on_quote_id")
