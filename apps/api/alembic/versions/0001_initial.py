"""Initial schema: users, projects, subsystems, suppliers, devices, quotes,
quote_items, labor_rates, quota_items, quote_audit_logs.

Revision ID: 0001
Revises:
Create Date: 2026-09-28 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Required extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ----- Enums -----
    user_role = postgresql.ENUM(
        "admin", "sales", "estimator", "finance", "viewer",
        name="user_role", create_type=False,
    )
    user_status = postgresql.ENUM(
        "active", "disabled", "pending",
        name="user_status", create_type=False,
    )
    project_type = postgresql.ENUM(
        "commercial", "public", "residential", "data_center", "other",
        name="project_type", create_type=False,
    )
    project_status = postgresql.ENUM(
        "draft", "quoting", "quoted", "won", "lost", "archived",
        name="project_status", create_type=False,
    )
    subsystem_type = postgresql.ENUM(
        "video_surveillance", "access_control", "intrusion",
        "structured_cabling", "network", "broadcast", "conference",
        "server_room", "parking", "info_publish", "other",
        name="subsystem_type", create_type=False,
    )
    device_category = postgresql.ENUM(
        "video", "access", "intrusion", "cabling", "network",
        "broadcast", "conference", "server_room", "parking",
        "info_publish", "power", "accessory",
        name="device_category", create_type=False,
    )
    device_status = postgresql.ENUM(
        "on_sale", "near_eol", "eol", "discontinued",
        name="device_status", create_type=False,
    )
    quote_status = postgresql.ENUM(
        "draft", "reviewing", "submitted", "approved", "rejected", "archived",
        name="quote_status", create_type=False,
    )
    fee_base = postgresql.ENUM(
        "device_only", "device_aux_install", "direct_fee",
        name="fee_base", create_type=False,
    )
    auxiliary_base = postgresql.ENUM(
        "device", "device_install", "by_quantity",
        name="auxiliary_base", create_type=False,
    )
    item_source = postgresql.ENUM(
        "library", "manual", "imported",
        name="item_source", create_type=False,
    )
    labor_class = postgresql.ENUM(
        "class_a", "class_b", "class_c",
        name="labor_class", create_type=False,
    )
    quota_category = postgresql.ENUM(
        "5-1", "5-3", "5-5", "5-6", "5-7", "other",
        name="quota_category", create_type=False,
    )
    audit_action = postgresql.ENUM(
        "create", "update", "delete", "compute", "submit", "approve",
        "reject", "export", "import", "replace_device",
        name="audit_action", create_type=False,
    )

    # create_type=False 的语义是「这些类型由本迁移自己负责建」，
    # 而 SQLAlchemy 只在 create_type 为真时才会在 create_table 之前自动发
    # CREATE TYPE（NamedType._check_for_name_in_memos 在 create_type=False 时直接
    # 返回 True）。所以必须在这里显式建一遍，否则第一条 create_table 就会
    # 报 type "user_role" does not exist，`alembic upgrade head` 根本走不完。
    # downgrade() 里对应的 DROP TYPE IF EXISTS 就是为这套显式创建准备的。
    for _enum in (
        user_role,
        user_status,
        project_type,
        project_status,
        subsystem_type,
        device_category,
        device_status,
        quote_status,
        fee_base,
        auxiliary_base,
        item_source,
        labor_class,
        quota_category,
        audit_action,
    ):
        _enum.create(op.get_bind(), checkfirst=True)

    # ----- users -----
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(128), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("department", sa.String(64), nullable=True),
        sa.Column("role", user_role, nullable=False, server_default="sales"),
        sa.Column("status", user_status, nullable=False, server_default="active"),
        sa.Column("is_superuser", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    # ----- suppliers -----
    op.create_table(
        "suppliers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("contact_person", sa.String(64), nullable=True),
        sa.Column("contact_phone", sa.String(32), nullable=True),
        sa.Column("address", sa.String(512), nullable=True),
        sa.Column("bank_account", sa.String(64), nullable=True),
        sa.Column("notes", sa.String(2048), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_suppliers_name", "suppliers", ["name"], unique=True)

    # ----- projects -----
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("customer_name", sa.String(255), nullable=False),
        sa.Column("customer_contact", sa.String(64), nullable=True),
        sa.Column("customer_phone", sa.String(32), nullable=True),
        sa.Column("address", sa.String(512), nullable=True),
        sa.Column("project_type", project_type, nullable=False, server_default="commercial"),
        sa.Column("status", project_status, nullable=False, server_default="draft"),
        sa.Column("expected_start", sa.Date, nullable=True),
        sa.Column("expected_duration_days", sa.Integer, nullable=True),
        sa.Column("total_area_sqm", sa.Numeric(12, 2), nullable=True),
        sa.Column("notes", sa.String(2048), nullable=True),
        # 项目级费率配置（覆盖全局默认）
        sa.Column("auxiliary_rate", sa.Numeric(5, 4), nullable=False, server_default="0.06"),
        sa.Column("design_rate", sa.Numeric(5, 4), nullable=False, server_default="0.02"),
        sa.Column("warranty_rate", sa.Numeric(5, 4), nullable=False, server_default="0.03"),
        sa.Column("training_fee", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("mgmt_profit_rate", sa.Numeric(5, 4), nullable=False, server_default="0.08"),
        sa.Column("tax_rate", sa.Numeric(5, 4), nullable=False, server_default="0.09"),
        sa.Column("auxiliary_base", sa.String(32), nullable=False, server_default="device_install"),
        sa.Column("fee_base", sa.String(32), nullable=False, server_default="device_aux_install"),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_projects_code", "projects", ["code"], unique=True)
    op.create_index("ix_projects_name", "projects", ["name"])
    op.create_index("ix_projects_customer_name", "projects", ["customer_name"])
    op.create_index("ix_projects_project_type", "projects", ["project_type"])
    op.create_index("ix_projects_status", "projects", ["status"])

    # ----- subsystems -----
    op.create_table(
        "subsystems",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subsystem_type", subsystem_type, nullable=False),
        sa.Column("custom_name", sa.String(128), nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("point_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("equipment_subtotal", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("install_subtotal", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("auxiliary_subtotal", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_subsystems_project_id", "subsystems", ["project_id"])
    op.create_index("ix_subsystems_subsystem_type", "subsystems", ["subsystem_type"])

    # ----- devices -----
    op.create_table(
        "devices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("brand", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("spec", sa.String(512), nullable=True),
        sa.Column("unit", sa.String(16), nullable=False, server_default="台"),
        sa.Column("category", device_category, nullable=False),
        sa.Column("subsystem_default", sa.String(64), nullable=True),
        sa.Column("reference_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("cost_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("install_unit_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("status", device_status, nullable=False, server_default="on_sale"),
        sa.Column("is_owner_supplied", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("is_common", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("replacement_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("devices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("eol_at", sa.Date, nullable=True),
        sa.Column("moq", sa.Integer, nullable=False, server_default="1"),
        sa.Column("warranty_years", sa.Integer, nullable=False, server_default="2"),
        sa.Column("supplier_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("last_inquiry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_inquiry_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_devices_code", "devices", ["code"], unique=True)
    op.create_index("ix_devices_name", "devices", ["name"])
    op.create_index("ix_devices_brand", "devices", ["brand"])
    op.create_index("ix_devices_model", "devices", ["model"])
    op.create_index("ix_devices_category", "devices", ["category"])
    op.create_index("ix_devices_status", "devices", ["status"])

    # ----- labor_rates -----
    op.create_table(
        "labor_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("region", sa.String(32), nullable=False, server_default="ningbo"),
        sa.Column("labor_class", labor_class, nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("price_per_day", sa.Numeric(10, 2), nullable=False),
        sa.Column("source", sa.String(64), nullable=False, server_default="nbzj-info"),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("notes", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_labor_rates_region", "labor_rates", ["region"])
    op.create_index("ix_labor_rates_labor_class", "labor_rates", ["labor_class"])
    op.create_index("ix_labor_rates_effective_date", "labor_rates", ["effective_date"])

    # ----- quota_items -----
    op.create_table(
        "quota_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("category", quota_category, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False, server_default="100m"),
        sa.Column("labor_a_day", sa.Numeric(10, 3), nullable=False, server_default="0"),
        sa.Column("labor_b_day", sa.Numeric(10, 3), nullable=False, server_default="0"),
        sa.Column("labor_c_day", sa.Numeric(10, 3), nullable=False, server_default="0"),
        sa.Column("base_price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("shielded_coef", sa.Numeric(4, 2), nullable=False, server_default="1.20"),
        sa.Column("cat5e_coef", sa.Numeric(4, 2), nullable=False, server_default="1.10"),
        sa.Column("ceiling_coef", sa.Numeric(4, 2), nullable=False, server_default="1.20"),
        sa.Column("notes", sa.String(1024), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_quota_items_code", "quota_items", ["code"], unique=True)
    op.create_index("ix_quota_items_category", "quota_items", ["category"])

    # ----- quotes -----
    op.create_table(
        "quotes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", quote_status, nullable=False, server_default="draft"),
        sa.Column("customer_name", sa.String(255), nullable=True),
        sa.Column("customer_contact", sa.String(64), nullable=True),
        sa.Column("customer_phone", sa.String(32), nullable=True),
        sa.Column("tax_method", sa.String(32), nullable=False, server_default="general_9pct"),
        sa.Column("tax_rate", sa.Numeric(5, 4), nullable=False, server_default="0.09"),
        sa.Column("fee_base", fee_base, nullable=False, server_default="device_aux_install"),
        sa.Column("auxiliary_base", auxiliary_base, nullable=False, server_default="device"),
        sa.Column("auxiliary_rate", sa.Numeric(5, 4), nullable=False, server_default="0.06"),
        sa.Column("design_rate", sa.Numeric(5, 4), nullable=False, server_default="0.02"),
        sa.Column("warranty_rate", sa.Numeric(5, 4), nullable=False, server_default="0.03"),
        sa.Column("training_fee", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("mgmt_profit_rate", sa.Numeric(5, 4), nullable=False, server_default="0.08"),
        sa.Column("labor_float_rate", sa.Numeric(5, 4), nullable=False, server_default="0"),
        # ⚠ 冒号必须写成 \: 。
        # sa.text() 会把 `:name` 当成绑定参数：JSON 里的 `"class_a":0` 被解析成
        # 参数 `:0`，编译出来的 DDL 就变成 `'{"class_a"NULL,...}'::jsonb`，
        # PostgreSQL 直接报 `invalid input syntax for type json / Token "NULL" is invalid`，
        # 迁移在第一条建表语句上就失败（而且是事务性 DDL，前面建的枚举也一并回滚）。
        # `\:` 是 SQLAlchemy text() 文档里的转义写法。
        sa.Column("labor_float_per_class", postgresql.JSONB, nullable=False,
                  server_default=sa.text("'{\"class_a\"\\:0,\"class_b\"\\:0,\"class_c\"\\:0}'::jsonb")),
        sa.Column("equipment_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("install_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("auxiliary_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("design_fee", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("warranty_fee", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("subtotal_before_tax", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("mgmt_profit", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("total_with_tax", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("quota_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("quota_total_with_tax", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("cost_total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("gross_profit", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("gross_profit_rate", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("notes", sa.String(2048), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_quotes_code", "quotes", ["code"], unique=True)
    op.create_index("ix_quotes_project_id", "quotes", ["project_id"])
    op.create_index("ix_quotes_status", "quotes", ["status"])

    # ----- quote_items -----
    op.create_table(
        "quote_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subsystem_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("subsystems.id", ondelete="SET NULL"), nullable=True),
        sa.Column("device_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("devices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("brand", sa.String(64), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("unit", sa.String(16), nullable=False, server_default="台"),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("unit_price", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("equipment_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("install_unit_price", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("install_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("cost_price", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("is_owner_supplied", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("source", item_source, nullable=False, server_default="library"),
        sa.Column("is_replaced", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("quote_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("notes", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_quote_items_quote_id", "quote_items", ["quote_id"])
    op.create_index("ix_quote_items_subsystem_id", "quote_items", ["subsystem_id"])
    op.create_index("ix_quote_items_device_id", "quote_items", ["device_id"])

    # ----- quote_audit_logs -----
    op.create_table(
        "quote_audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("quote_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", audit_action, nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("note", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_quote_audit_logs_quote_id", "quote_audit_logs", ["quote_id"])
    op.create_index("ix_quote_audit_logs_action", "quote_audit_logs", ["action"])


def downgrade() -> None:
    op.drop_table("quote_audit_logs")
    op.drop_table("quote_items")
    op.drop_table("quotes")
    op.drop_table("quota_items")
    op.drop_table("labor_rates")
    op.drop_table("devices")
    op.drop_table("subsystems")
    op.drop_table("projects")
    op.drop_table("suppliers")
    op.drop_table("users")

    # Drop enums
    op.execute("DROP TYPE IF EXISTS audit_action")
    op.execute("DROP TYPE IF EXISTS quota_category")
    op.execute("DROP TYPE IF EXISTS labor_class")
    op.execute("DROP TYPE IF EXISTS item_source")
    op.execute("DROP TYPE IF EXISTS auxiliary_base")
    op.execute("DROP TYPE IF EXISTS fee_base")
    op.execute("DROP TYPE IF EXISTS quote_status")
    op.execute("DROP TYPE IF EXISTS device_status")
    op.execute("DROP TYPE IF EXISTS device_category")
    op.execute("DROP TYPE IF EXISTS subsystem_type")
    op.execute("DROP TYPE IF EXISTS project_status")
    op.execute("DROP TYPE IF EXISTS project_type")
    op.execute("DROP TYPE IF EXISTS user_status")
    op.execute("DROP TYPE IF EXISTS user_role")
