-- =====================================================================
-- 智能化弱电报价系统 · 数据库结构（v2.11）
-- 版权：© 2026 XX科技有限公司
-- 生成日期：2026-10-03
--
-- ⚠ 阅读须知（重要）：
--   本文件由 SQLAlchemy 元数据**自动生成**（app/db/base.py 的 Base.metadata），
--   与 apps/api/app/models/*.py 完全一致，并与迁移链
--   0001 → 0002 → 0003 → 0004 → 0005 → 0006 → 0007 → 0008 → 0009 → 0010 的结构对齐。
--   建库的唯一权威仍然是 `apps/api/alembic/versions/`；
--   本文件用于：①评审结构 ②给 DBA 做参考 ③比对线上库。
--
--   与上一版（v2.0，2026-09-29）相比的变化：
--     1. 新表 device_categories（设备库分类树，自引用 parent_id）
--     2. devices.category_id（→ device_categories.id, ON DELETE SET NULL）
--     3. 枚举 user_role 新增 purchaser / manager（共 7 个角色）
--     4. suppliers 补 code 唯一索引与 contact_name / contact_email
--     5. quote_items 补 is_shielded / is_cat5e / is_ceiling
--     6. subsystems 补 supplier_id + 索引
--
--   本文件**不含** alembic_version 表，也不含 seed 演示数据。
-- =====================================================================
-- 14 �� PG ö�����ͣ��� Python ö����ֵһ�£�
CREATE TYPE user_role AS ENUM (
    'admin',
    'sales',
    'estimator',
    'finance',
    'viewer',
    'purchaser',
    'manager'
);

CREATE TYPE user_status AS ENUM (
    'active',
    'disabled',
    'pending'
);

CREATE TYPE project_type AS ENUM (
    'commercial',
    'public',
    'residential',
    'data_center',
    'other'
);

CREATE TYPE project_status AS ENUM (
    'draft',
    'quoting',
    'quoted',
    'won',
    'lost',
    'archived'
);

CREATE TYPE subsystem_type AS ENUM (
    'video_surveillance',
    'access_control',
    'intrusion',
    'structured_cabling',
    'network',
    'broadcast',
    'conference',
    'server_room',
    'parking',
    'info_publish',
    'other'
);

CREATE TYPE device_category AS ENUM (
    'video',
    'access',
    'intrusion',
    'cabling',
    'network',
    'broadcast',
    'conference',
    'server_room',
    'parking',
    'info_publish',
    'power',
    'accessory'
);

CREATE TYPE device_status AS ENUM (
    'on_sale',
    'near_eol',
    'eol',
    'discontinued'
);

CREATE TYPE quote_status AS ENUM (
    'draft',
    'reviewing',
    'submitted',
    'approved',
    'rejected',
    'archived'
);

CREATE TYPE fee_base AS ENUM (
    'device_only',
    'device_aux_install',
    'direct_fee'
);

CREATE TYPE auxiliary_base AS ENUM (
    'device',
    'device_install',
    'by_quantity'
);

CREATE TYPE item_source AS ENUM (
    'library',
    'manual',
    'imported'
);

CREATE TYPE labor_class AS ENUM (
    'class_a',
    'class_b',
    'class_c'
);

CREATE TYPE quota_category AS ENUM (
    '5-1',
    '5-3',
    '5-5',
    '5-6',
    '5-7',
    'other'
);

CREATE TYPE audit_action AS ENUM (
    'create',
    'update',
    'delete',
    'compute',
    'submit',
    'approve',
    'reject',
    'export',
    'import',
    'replace_device'
);


CREATE TABLE users (
	username VARCHAR(64) NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	hashed_password VARCHAR(255) NOT NULL, 
	full_name VARCHAR(128), 
	phone VARCHAR(32), 
	department VARCHAR(64), 
	role user_role NOT NULL, 
	status user_status NOT NULL, 
	is_superuser BOOLEAN NOT NULL, 
	last_login_at TIMESTAMP WITH TIME ZONE, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_users_username ON users (username);

CREATE INDEX ix_users_role ON users (role);

CREATE UNIQUE INDEX ix_users_email ON users (email);

CREATE TABLE projects (
	code VARCHAR(32) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	customer_name VARCHAR(255) NOT NULL, 
	customer_contact VARCHAR(64), 
	customer_phone VARCHAR(32), 
	address VARCHAR(512), 
	project_type project_type NOT NULL, 
	status project_status NOT NULL, 
	expected_start DATE, 
	expected_duration_days INTEGER, 
	total_area_sqm NUMERIC(12, 2), 
	notes VARCHAR(2048), 
	auxiliary_rate NUMERIC(5, 4) NOT NULL, 
	design_rate NUMERIC(5, 4) NOT NULL, 
	warranty_rate NUMERIC(5, 4) NOT NULL, 
	training_fee NUMERIC(12, 2) NOT NULL, 
	mgmt_profit_rate NUMERIC(5, 4) NOT NULL, 
	tax_rate NUMERIC(5, 4) NOT NULL, 
	auxiliary_base VARCHAR(32) NOT NULL, 
	fee_base VARCHAR(32) NOT NULL, 
	owner_id UUID, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_projects_name ON projects (name);

CREATE INDEX ix_projects_customer_name ON projects (customer_name);

CREATE UNIQUE INDEX ix_projects_code ON projects (code);

CREATE INDEX ix_projects_project_type ON projects (project_type);

CREATE INDEX ix_projects_status ON projects (status);

CREATE TABLE subsystems (
	project_id UUID NOT NULL, 
	subsystem_type subsystem_type NOT NULL, 
	custom_name VARCHAR(128), 
	sort_order INTEGER NOT NULL, 
	point_count INTEGER NOT NULL, 
	equipment_subtotal NUMERIC(14, 2) NOT NULL, 
	install_subtotal NUMERIC(14, 2) NOT NULL, 
	auxiliary_subtotal NUMERIC(14, 2) NOT NULL, 
	notes VARCHAR(1024), 
	supplier_id UUID, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(supplier_id) REFERENCES suppliers (id) ON DELETE SET NULL
);

CREATE INDEX ix_subsystems_subsystem_type ON subsystems (subsystem_type);

CREATE INDEX ix_subsystems_project_id ON subsystems (project_id);

CREATE INDEX ix_subsystems_supplier_id ON subsystems (supplier_id);

CREATE TABLE suppliers (
	code VARCHAR(32) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	contact_name VARCHAR(64), 
	contact_phone VARCHAR(32), 
	contact_email VARCHAR(255), 
	address VARCHAR(512), 
	bank_account VARCHAR(64), 
	notes VARCHAR(2048), 
	is_active BOOLEAN NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_suppliers_code ON suppliers (code);

CREATE UNIQUE INDEX ix_suppliers_name ON suppliers (name);

CREATE TABLE devices (
	code VARCHAR(32) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	brand VARCHAR(64), 
	model VARCHAR(128), 
	spec VARCHAR(512), 
	unit VARCHAR(16) NOT NULL, 
	category device_category NOT NULL, 
	category_id UUID, 
	subsystem_default VARCHAR(64), 
	reference_price NUMERIC(12, 2) NOT NULL, 
	cost_price NUMERIC(12, 2) NOT NULL, 
	install_unit_price NUMERIC(12, 2) NOT NULL, 
	status device_status NOT NULL, 
	is_owner_supplied BOOLEAN NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	is_common BOOLEAN NOT NULL, 
	replacement_id UUID, 
	eol_at DATE, 
	moq INTEGER NOT NULL, 
	warranty_years INTEGER NOT NULL, 
	supplier_id UUID, 
	last_inquiry_at TIMESTAMP WITH TIME ZONE, 
	last_inquiry_price NUMERIC(12, 2), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(category_id) REFERENCES device_categories (id) ON DELETE SET NULL, 
	FOREIGN KEY(replacement_id) REFERENCES devices (id) ON DELETE SET NULL, 
	FOREIGN KEY(supplier_id) REFERENCES suppliers (id) ON DELETE SET NULL
);

CREATE INDEX ix_devices_model ON devices (model);

CREATE INDEX ix_devices_status ON devices (status);

CREATE INDEX ix_devices_brand ON devices (brand);

CREATE INDEX ix_devices_category ON devices (category);

CREATE INDEX ix_devices_category_id ON devices (category_id);

CREATE INDEX ix_devices_name ON devices (name);

CREATE UNIQUE INDEX ix_devices_code ON devices (code);

CREATE TABLE labor_rates (
	region VARCHAR(32) NOT NULL, 
	labor_class labor_class NOT NULL, 
	effective_date DATE NOT NULL, 
	price_per_day NUMERIC(10, 2) NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	is_current BOOLEAN NOT NULL, 
	notes VARCHAR(256), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_labor_rates_labor_class ON labor_rates (labor_class);

CREATE INDEX ix_labor_rates_effective_date ON labor_rates (effective_date);

CREATE INDEX ix_labor_rates_region ON labor_rates (region);

CREATE TABLE quota_items (
	code VARCHAR(32) NOT NULL, 
	category quota_category NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	unit VARCHAR(16) NOT NULL, 
	labor_a_day NUMERIC(10, 3) NOT NULL, 
	labor_b_day NUMERIC(10, 3) NOT NULL, 
	labor_c_day NUMERIC(10, 3) NOT NULL, 
	base_price NUMERIC(12, 2) NOT NULL, 
	shielded_coef NUMERIC(4, 2) NOT NULL, 
	cat5e_coef NUMERIC(4, 2) NOT NULL, 
	ceiling_coef NUMERIC(4, 2) NOT NULL, 
	notes VARCHAR(1024), 
	is_active BOOLEAN NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_quota_items_code ON quota_items (code);

CREATE INDEX ix_quota_items_category ON quota_items (category);

CREATE TABLE quotes (
	project_id UUID NOT NULL, 
	code VARCHAR(32) NOT NULL, 
	version INTEGER NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	status quote_status NOT NULL, 
	customer_name VARCHAR(255), 
	customer_contact VARCHAR(64), 
	customer_phone VARCHAR(32), 
	tax_method VARCHAR(32) NOT NULL, 
	tax_rate NUMERIC(5, 4) NOT NULL, 
	fee_base fee_base NOT NULL, 
	auxiliary_base auxiliary_base NOT NULL, 
	auxiliary_rate NUMERIC(5, 4) NOT NULL, 
	design_rate NUMERIC(5, 4) NOT NULL, 
	warranty_rate NUMERIC(5, 4) NOT NULL, 
	training_fee NUMERIC(12, 2) NOT NULL, 
	mgmt_profit_rate NUMERIC(5, 4) NOT NULL, 
	labor_float_rate NUMERIC(5, 4) NOT NULL, 
	labor_float_per_class JSONB NOT NULL, 
	equipment_total NUMERIC(14, 2) NOT NULL, 
	install_total NUMERIC(14, 2) NOT NULL, 
	auxiliary_total NUMERIC(14, 2) NOT NULL, 
	design_fee NUMERIC(14, 2) NOT NULL, 
	warranty_fee NUMERIC(14, 2) NOT NULL, 
	subtotal_before_tax NUMERIC(14, 2) NOT NULL, 
	mgmt_profit NUMERIC(14, 2) NOT NULL, 
	tax_amount NUMERIC(14, 2) NOT NULL, 
	total_with_tax NUMERIC(14, 2) NOT NULL, 
	quota_total NUMERIC(14, 2) NOT NULL, 
	quota_total_with_tax NUMERIC(14, 2) NOT NULL, 
	cost_total NUMERIC(14, 2) NOT NULL, 
	gross_profit NUMERIC(14, 2) NOT NULL, 
	gross_profit_rate NUMERIC(5, 4) NOT NULL, 
	notes VARCHAR(2048), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE INDEX ix_quotes_status ON quotes (status);

CREATE INDEX ix_quotes_project_id ON quotes (project_id);

CREATE UNIQUE INDEX ix_quotes_code ON quotes (code);

CREATE TABLE quote_items (
	quote_id UUID NOT NULL, 
	subsystem_id UUID, 
	device_id UUID, 
	name VARCHAR(255) NOT NULL, 
	brand VARCHAR(64), 
	model VARCHAR(128), 
	unit VARCHAR(16) NOT NULL, 
	quantity NUMERIC(12, 3) NOT NULL, 
	unit_price NUMERIC(12, 4) NOT NULL, 
	equipment_amount NUMERIC(14, 2) NOT NULL, 
	install_unit_price NUMERIC(12, 4) NOT NULL, 
	install_amount NUMERIC(14, 2) NOT NULL, 
	cost_price NUMERIC(12, 4) NOT NULL, 
	is_owner_supplied BOOLEAN NOT NULL, 
	source item_source NOT NULL, 
	is_replaced BOOLEAN NOT NULL, 
	replaced_by_id UUID, 
	sort_order INTEGER NOT NULL, 
	notes VARCHAR(512), 
	is_shielded BOOLEAN NOT NULL, 
	is_cat5e BOOLEAN NOT NULL, 
	is_ceiling BOOLEAN NOT NULL, 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(quote_id) REFERENCES quotes (id) ON DELETE CASCADE, 
	FOREIGN KEY(subsystem_id) REFERENCES subsystems (id) ON DELETE SET NULL, 
	FOREIGN KEY(device_id) REFERENCES devices (id) ON DELETE SET NULL, 
	FOREIGN KEY(replaced_by_id) REFERENCES quote_items (id) ON DELETE SET NULL
);

CREATE INDEX ix_quote_items_quote_id ON quote_items (quote_id);

CREATE INDEX ix_quote_items_device_id ON quote_items (device_id);

CREATE INDEX ix_quote_items_subsystem_id ON quote_items (subsystem_id);

CREATE TABLE quote_audit_logs (
	quote_id UUID NOT NULL, 
	user_id UUID, 
	action audit_action NOT NULL, 
	payload JSONB NOT NULL, 
	note VARCHAR(512), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	id UUID NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(quote_id) REFERENCES quotes (id) ON DELETE CASCADE, 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL
);

CREATE INDEX ix_quote_audit_logs_quote_id ON quote_audit_logs (quote_id);

CREATE INDEX ix_quote_audit_logs_action ON quote_audit_logs (action);

CREATE TABLE device_categories (
	parent_id UUID, 
	code VARCHAR(64) NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	sort_order INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	legacy_enum VARCHAR(32), 
	code_prefix VARCHAR(4), 
	notes VARCHAR(255), 
	id UUID NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(parent_id) REFERENCES device_categories (id) ON DELETE SET NULL
);

CREATE INDEX ix_device_categories_parent_id ON device_categories (parent_id);

CREATE INDEX ix_device_categories_legacy_enum ON device_categories (legacy_enum);

CREATE UNIQUE INDEX ix_device_categories_code ON device_categories (code);


-- =====================================================================
-- 与迁移的差异说明（为何本文件不能直接当 alembic 用）
--   1. 迁移里的 server_default（如 created_at 的 now()）在本文件中以
--      DEFAULT now() 形式呈现，二者语义一致。
--   2. 枚举类型在迁移里用 postgresql.ENUM(create_type=False) + 显式
--      CREATE TYPE 建，本文件直接给出 CREATE TYPE，效果相同。
--   3. gen_random_uuid() 需要 PostgreSQL 13+（项目要求 18，满足）。
-- =====================================================================



-- ===========================================================================
-- 点位模板（迁移 0009）
-- ===========================================================================
-- 一个「点位」的物料构成 + 该点位的安装单价（元/点）。
-- 套用：点位数 N → 每行数量 = 模板数量 × N；安装单价只写在 is_primary 那一行，
-- 否则一个点位里的支架/电源会各收一次安装费。
CREATE TABLE point_templates (
    id                 UUID PRIMARY KEY,
    code               VARCHAR(32)  NOT NULL UNIQUE,
    name               VARCHAR(255) NOT NULL,
    subsystem_type     VARCHAR(32),
    install_unit_price NUMERIC(12, 2) NOT NULL DEFAULT 0,
    notes              VARCHAR(2048),
    is_active          BOOLEAN      NOT NULL DEFAULT TRUE,
    sort_order         INTEGER      NOT NULL DEFAULT 0,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE point_template_items (
    id          UUID PRIMARY KEY,
    template_id UUID           NOT NULL REFERENCES point_templates (id) ON DELETE CASCADE,
    device_id   UUID           REFERENCES devices (id) ON DELETE SET NULL,
    quantity    NUMERIC(12, 2) NOT NULL DEFAULT 1,
    is_primary  BOOLEAN        NOT NULL DEFAULT FALSE,
    notes       VARCHAR(512),
    sort_order  INTEGER        NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ    NOT NULL DEFAULT now()
);
CREATE INDEX ix_point_template_items_template_id ON point_template_items (template_id);
CREATE INDEX ix_point_template_items_device_id   ON point_template_items (device_id);

-- ===========================================================================
-- 系统设置（迁移 0010）
-- ===========================================================================
-- 键值 + JSONB：company（公司抬头）/ quote（报价有效期天数 + 报价说明条款）。
-- 缺行时用 app/services/settings.py 的 DEFAULTS，所以不写数据也能跑。
CREATE TABLE app_settings (
    id         UUID PRIMARY KEY,
    key        VARCHAR(64) NOT NULL UNIQUE,
    value      JSONB       NOT NULL,
    updated_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
