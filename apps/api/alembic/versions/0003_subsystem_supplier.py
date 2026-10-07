"""Add subsystems.supplier_id, suppliers.code, suppliers.contact_name,
suppliers.contact_email; FK subsystems + supplier_id.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29 16:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # ----- suppliers：补三个字段，并给 code 加唯一索引 -----
    sup_cols = {c["name"] for c in inspector.get_columns("suppliers")}

    if "code" not in sup_cols:
        # 先加为 nullable：旧库里的行还没有这个值，必须先 backfill 再改成 NOT NULL，
        # 直接 NOT NULL 会让 0001 → 0003 升级在第一条 UPDATE 前就报
        #   null value in column "code" violates not-null constraint
        op.add_column(
            "suppliers",
            sa.Column("code", sa.String(32), nullable=True),
        )
        # backfill：取 id 的前 8 位 hex 作 fallback code，保证唯一稳定
        # （id 是 UUID，截前 8 位 → 16^8 ≈ 42 亿空间，撞码概率极低）。
        # `id::text` 在 pg 里就是 8-4-4-4-12 的标准字符串，前 8 位正好是没有连字符的前缀。
        op.execute(
            "UPDATE suppliers "
            "SET code = 'SUP-' || UPPER(SUBSTRING(REPLACE(id::text, '-', '') FROM 1 FOR 8)) "
            "WHERE code IS NULL"
        )
        # 把 code 改成 NOT NULL，再加 unique index（先 NOT NULL 才能加 NOT NULL unique）
        op.alter_column("suppliers", "code", nullable=False)
        op.create_index(
            "ix_suppliers_code", "suppliers", ["code"], unique=True
        )

    if "contact_name" not in sup_cols:
        op.add_column(
            "suppliers",
            sa.Column("contact_name", sa.String(64), nullable=True),
        )

    if "contact_email" not in sup_cols:
        op.add_column(
            "suppliers",
            sa.Column("contact_email", sa.String(255), nullable=True),
        )

    # ----- subsystems：加 supplier_id 外键 -----
    sub_cols = {c["name"] for c in inspector.get_columns("subsystems")}
    sub_fks = {fk["name"] for fk in inspector.get_foreign_keys("subsystems") if fk.get("name")}

    if "supplier_id" not in sub_cols:
        op.add_column(
            "subsystems",
            sa.Column(
                "supplier_id",
                postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
        )

    fk_name = "fk_subsystems_supplier_id"
    if fk_name not in sub_fks:
        op.create_foreign_key(
            fk_name,
            "subsystems",
            "suppliers",
            ["supplier_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # 顺手建个普通（非唯一）索引：supplier_id 将来按供应商汇总子系统会用到
    sub_indexes = {ix["name"] for ix in inspector.get_indexes("subsystems")}
    if "ix_subsystems_supplier_id" not in sub_indexes:
        op.create_index(
            "ix_subsystems_supplier_id", "subsystems", ["supplier_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    sub_fks = {fk["name"] for fk in inspector.get_foreign_keys("subsystems") if fk.get("name")}
    if "fk_subsystems_supplier_id" in sub_fks:
        op.drop_constraint("fk_subsystems_supplier_id", "subsystems", type_="foreignkey")

    sub_indexes = {ix["name"] for ix in inspector.get_indexes("subsystems")}
    if "ix_subsystems_supplier_id" in sub_indexes:
        op.drop_index("ix_subsystems_supplier_id", table_name="subsystems")

    sub_cols = {c["name"] for c in inspector.get_columns("subsystems")}
    if "supplier_id" in sub_cols:
        op.drop_column("subsystems", "supplier_id")

    sup_cols = {c["name"] for c in inspector.get_columns("suppliers")}
    sup_indexes = {ix["name"] for ix in inspector.get_indexes("suppliers")}
    if "ix_suppliers_code" in sup_indexes:
        op.drop_index("ix_suppliers_code", table_name="suppliers")
    for col in ("contact_email", "contact_name", "code"):
        if col in sup_cols:
            op.drop_column("suppliers", col)


__all__ = ["upgrade", "downgrade"]
