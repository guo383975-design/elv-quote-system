"""Common Pydantic schemas (DTOs)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, field_validator

# 枚举字段直接复用 ORM 的枚举类型，不要再写成 str。
# 原因：这些值最终落进 pg 的 enum 列，写 str 的话
#   - PostgreSQL 会由服务端拒绝 → DBAPIError → 500（本该是 422）
#   - SQLite（测试）不建枚举约束 → 非法值被**静默存进库**，只在下拉框里显示成空
# 用枚举类型还能让 OpenAPI 自动带上可选值列表。
from app.models.device import DeviceCategory, DeviceStatus
from app.models.project import ProjectStatus, ProjectType
from app.models.quote import AuxiliaryBaseType, FeeBaseType, QuoteStatus
from app.models.quote_item import ItemSource
from app.models.subsystem import SubsystemType
from app.models.user import UserStatus

# 取值依据：报价单上的费率都是"比例"，合理上界远小于 1（税率 13% 已是最高档），
# 留 le=1 给极端场景（如 100% 的维保费率用于特殊合同）。
RATE_MIN = Decimal("0")
RATE_MAX = Decimal("1")
# 人工浮动可以为负（下浮），给一个宽但有限的区间：-50% ~ +200%
LABOR_FLOAT_MIN = Decimal("-0.5")
LABOR_FLOAT_MAX = Decimal("2")
# 金额类：非负。负数量/负单价会产出负金额，且能绕过"按金额"的一切校验。
MONEY_MIN = Decimal("0")
# 单价上限：Numeric(12,4) 理论上限约 1e8
UNIT_PRICE_MAX = Decimal("99999999")

LABOR_CLASSES = ("class_a", "class_b", "class_c")


def _bounded_labor_floats(
    value: dict[str, Decimal] | None,
) -> dict[str, Decimal] | None:
    """分人工种浮动：只认三类工日，且每个值都要落在 LABOR_FLOAT_* 区间内。

    以前这个字典**没有任何校验** —— 键名打错（`class_A`）会被静默忽略、
    值可以传 -100（算出来是负人工费）。整体浮动有 `ge/le`、分类浮动却没有，
    两边严格程度不一致本身就会让人误判。
    """
    if not value:
        return value
    out: dict[str, Decimal] = {}
    for key, raw in value.items():
        if key not in LABOR_CLASSES:
            raise ValueError(
                f"未知的工种类别 {key!r}（只接受 {'/'.join(LABOR_CLASSES)}）"
            )
        num = Decimal(str(raw))
        if num < LABOR_FLOAT_MIN or num > LABOR_FLOAT_MAX:
            raise ValueError(
                f"{key} 浮动 {num} 超出允许范围 "
                f"{LABOR_FLOAT_MIN} ~ {LABOR_FLOAT_MAX}（-50% ~ +200%）"
            )
        out[key] = num
    return out


# 分人工种浮动字段类型（三个入参模型共用）
LaborFloatPerClass = Annotated[
    dict[str, Decimal] | None, AfterValidator(_bounded_labor_floats)
]

T = TypeVar("T")


class ORMBase(BaseModel):
    """Base for schemas that mirror ORM models."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ----- Pagination -----

class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    page_size: int = 20


# ----- Auth -----

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=72)


class TokenResponse(BaseModel):
    # 初始/重置口令必须强制修改（2026-10-05 ✓）：前端据此弹阻塞对话框 ✓
    must_change_password: bool = False
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)
    full_name: str | None = None
    department: str | None = None
    # 引导令牌：必须与后端 .env 里的 BOOTSTRAP_TOKEN 一致，且只在空库上有效。
    # 默认空 = 自助注册关闭（见 api/v1/auth.py: register 的说明）。
    bootstrap_token: str = ""


# ----- User -----

class UserOut(ORMBase):
    id: UUID
    username: str
    email: EmailStr
    full_name: str | None
    phone: str | None
    department: str | None
    # 组织架构：department_id 是实际归属；department_path 是「公司 / 设计部 / 智能化设计组」
    department_id: UUID | None = None
    department_path: str = ""
    role: str
    status: str
    is_superuser: bool
    created_at: datetime
    last_login_at: datetime | None


def _validate_role(value: str) -> str:
    """用户角色校验：内置 + 自定义（core/roles.py 的清单）。

    以前这里限定 Role 枚举，于是自定义角色一分给人就 422 ——
    而"塞一个不存在的角色"更糟：那个人登录后一片空白，还没人知道为什么。
    """
    from app.core.roles import known_role_keys

    key = str(value)
    if key not in known_role_keys():
        raise ValueError(f"未知角色：{key}")
    return key


class UserCreate(BaseModel):
    """管理员建号。

    刻意**不收** `is_superuser`：它是绕过 require_roles 与 can_see_cost 的开关，
    不该由请求体决定（以前收裸 dict，body 里塞 is_superuser 就生效）。

    角色（2026-10-05 起）：`str` + **显式校验**（内置 + 自定义角色清单）。
    以前用枚举是为了"非法值在 PG 上变成 500、在 SQLite 上被静默存进去"，
    现在角色清单是动态的（可自定义），所以校验改由 `_validate_role` 承担 ——
    非法值一律 422，两个数据库行为一致。
    """

    username: str = Field(..., min_length=3, max_length=64)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=72)
    full_name: str | None = Field(None, max_length=128)
    phone: str | None = Field(None, max_length=32)
    # 组织架构节点（可选）：传了就以它为准，自由文本的 department 由节点名反推
    department_id: UUID | None = None
    department: str | None = Field(None, max_length=64)
    role: str = "sales"
    status: UserStatus = UserStatus.ACTIVE

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        return _validate_role(v)


class UserUpdate(BaseModel):
    """管理员改号。同样不收 is_superuser。"""

    email: EmailStr | None = None
    password: str | None = Field(None, min_length=8, max_length=72)
    full_name: str | None = Field(None, max_length=128)
    phone: str | None = Field(None, max_length=32)
    department_id: UUID | None = None
    department: str | None = Field(None, max_length=64)
    role: str | None = None
    status: UserStatus | None = None

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str | None) -> str | None:
        return None if v is None else _validate_role(v)


# ----- 组织架构树（用户管理页左侧的那棵树）-----

class DepartmentIn(BaseModel):
    """新增部门节点。`parent_id` 为空即顶级部门（公司/中心）。"""

    code: str = Field(..., min_length=1, max_length=64, description="稳定代号，如 dept.design")
    name: str = Field(..., min_length=1, max_length=64)
    parent_id: UUID | None = None
    sort_order: int = 0
    is_active: bool = True
    notes: str | None = Field(None, max_length=255)


class DepartmentUpdate(BaseModel):
    """改部门。`code` 不允许改（回填与外部对照按它对齐），换层级用 parent_id。"""

    name: str | None = Field(None, min_length=1, max_length=64)
    parent_id: UUID | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    notes: str | None = Field(None, max_length=255)


class DepartmentOut(ORMBase):
    id: UUID
    parent_id: UUID | None
    code: str
    name: str
    sort_order: int
    is_active: bool
    notes: str | None = None
    # 「公司 / 设计部 / 智能化设计组」
    path: str = ""
    # 本节点自身的人数 / 含全部子节点的人数
    user_count: int = 0
    total_count: int = 0
    children: list[DepartmentOut] = []


class DepartmentTreeOut(BaseModel):
    nodes: list[DepartmentOut] = []
    # 没有归属部门的用户数（新库 / 没填部门的用户）
    unassigned: int = 0
    total: int = 0


# ----- Project -----

class ProjectCreate(BaseModel):
    # 新建时是否按「系统设置 → 项目默认子系统」自动建专业（默认建）
    # 客户主数据引用（可空）：给了它就用档案里的名称/联系人/电话兜底
    customer_id: UUID | None = None
    create_default_subsystems: bool = True
    code: str = Field(..., min_length=2, max_length=32)
    name: str = Field(..., min_length=1, max_length=255)
    # 客户名与客户档案**二选一**：选了档案就用档案里的名称（后端校验至少给一个）。
    # 直接必填会挡住"从客户档案里挑一家"的正常流程。
    customer_name: str | None = None
    customer_contact: str | None = None
    customer_phone: str | None = None
    address: str | None = None
    project_type: ProjectType = ProjectType.COMMERCIAL
    # 新建表单里就有「项目状态」下拉，不收这个字段的话控件是假的：
    # 用户选了「报价中」，保存后仍然是 draft。
    status: ProjectStatus = ProjectStatus.DRAFT
    expected_start: datetime | None = None
    expected_duration_days: int | None = None
    total_area_sqm: Decimal | None = None
    notes: str | None = None
    # 项目级费率：建报价单时**抄进报价单**作为快照（见 api/v1/quotes.py 的 create_quote）。
    # 以前这 8 列模型里有、接口与界面都没有入口 —— 既配不了也读不到，
    # 是彻底的死列（审计 H-12）。现在补齐"配置 → 快照"这条链。
    tax_rate: Decimal = Field(Decimal("0.09"), ge=RATE_MIN, le=RATE_MAX)
    fee_base: FeeBaseType = FeeBaseType.DEVICE_AUX_INSTALL
    auxiliary_base: AuxiliaryBaseType = AuxiliaryBaseType.DEVICE
    auxiliary_rate: Decimal = Field(Decimal("0.06"), ge=RATE_MIN, le=RATE_MAX)
    design_rate: Decimal = Field(Decimal("0.02"), ge=RATE_MIN, le=RATE_MAX)
    warranty_rate: Decimal = Field(Decimal("0.03"), ge=RATE_MIN, le=RATE_MAX)
    training_fee: Decimal = Field(Decimal("0"), ge=MONEY_MIN)
    mgmt_profit_rate: Decimal = Field(Decimal("0.08"), ge=RATE_MIN, le=RATE_MAX)


class ProjectUpdate(BaseModel):
    # 客户主数据引用（可空）：给了它就用档案里的名称/联系人/电话兜底
    customer_id: UUID | None = None
    name: str | None = None
    customer_name: str | None = None
    customer_contact: str | None = None
    customer_phone: str | None = None
    address: str | None = None
    project_type: ProjectType | None = None
    status: ProjectStatus | None = None
    expected_start: datetime | None = None
    expected_duration_days: int | None = None
    total_area_sqm: Decimal | None = None
    notes: str | None = None
    # 改项目费率**不影响已建报价单**（快照语义），只影响之后新建的。
    tax_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    fee_base: FeeBaseType | None = None
    auxiliary_base: AuxiliaryBaseType | None = None
    auxiliary_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    design_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    warranty_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    training_fee: Decimal | None = Field(None, ge=MONEY_MIN)
    mgmt_profit_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)


class ProjectOut(ORMBase):
    id: UUID
    code: str
    name: str
    customer_name: str
    project_type: ProjectType
    status: ProjectStatus
    # 列表页用：这一单有几张报价单 / 其中几张在等我审批
    quote_count: int = 0
    pending_quote_count: int = 0
    # 详情字段：GET /projects/{id} 是详情端点，只回 6 个字段会逼前端再发一次请求。
    customer_contact: str | None = None
    customer_phone: str | None = None
    address: str | None = None
    expected_start: date | None = None
    expected_duration_days: int | None = None
    total_area_sqm: Decimal | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    # 项目级费率（新建报价单时抄进报价单；见 ProjectCreate 的说明）
    tax_rate: Decimal
    fee_base: FeeBaseType
    auxiliary_base: AuxiliaryBaseType
    auxiliary_rate: Decimal
    design_rate: Decimal
    warranty_rate: Decimal
    training_fee: Decimal
    mgmt_profit_rate: Decimal


# ----- Subsystem -----

class SubsystemCreate(BaseModel):
    project_id: UUID
    subsystem_type: SubsystemType
    custom_name: str | None = None
    sort_order: int = 0
    notes: str | None = None
    # 供货商：可选。子系统的工程实务里"供货商"是项目级的事，但作为报价归口的属性，
    # 子系统级别也能挂（同一项目里不同专业用不同供货商很常见）。
    supplier_id: UUID | None = None


class SubsystemOut(ORMBase):
    id: UUID
    project_id: UUID
    subsystem_type: SubsystemType
    custom_name: str | None
    sort_order: int
    point_count: int
    equipment_subtotal: Decimal
    install_subtotal: Decimal
    auxiliary_subtotal: Decimal
    notes: str | None = None
    supplier_id: UUID | None = None


# ----- Device (library) -----

# ---------------------------------------------------------------------------
# 阶梯价 / 底价（2026-10-04）
# ---------------------------------------------------------------------------

class PriceTierIn(BaseModel):
    min_qty: Decimal = Field(..., ge=0)
    # 空 = 该档以上（如"50 台以上"）
    max_qty: Decimal | None = Field(None, ge=0)
    unit_price: Decimal = Field(..., ge=0)
    note: str | None = Field(None, max_length=128)


class PriceTierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    min_qty: Decimal
    max_qty: Decimal | None = None
    unit_price: Decimal
    note: str | None = None


class DeviceCreate(BaseModel):
    auxiliary_unit_price: Decimal = Decimal(0)
    # 价格底线：低于它报价需要经理授权 + 理由
    min_price: Decimal | None = Field(None, ge=0)
    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=255)
    brand: str | None = None
    model: str | None = None
    spec: str | None = None
    # 设备描述 / 性能参数（2026-10-05）：多行文本，写"吞吐 10Gbps；并发 200 万"这类基础信息
    description: str | None = Field(None, max_length=4000)
    unit: str = "台"
    category: DeviceCategory
    # 分类树节点（可选）：给了就以它为准，并从它所属的一级分类反推 category
    category_id: UUID | None = None
    subsystem_default: str | None = None
    reference_price: Decimal = Decimal("0")
    cost_price: Decimal = Decimal("0")
    install_unit_price: Decimal = Decimal("0")
    status: DeviceStatus = DeviceStatus.ON_SALE
    is_owner_supplied: bool = False
    is_common: bool = False
    replacement_id: UUID | None = None
    moq: int = 1
    warranty_years: int = 2
    supplier_id: UUID | None = None
    eol_at: date | None = None


class DeviceUpdate(BaseModel):
    # 设备描述 / 性能参数（2026-10-05）
    auxiliary_unit_price: Decimal = Decimal(0)
    # 价格底线：低于它报价需要经理授权 + 理由
    min_price: Decimal | None = Field(None, ge=0)
    # 编辑表单能改的字段必须都在这里：pydantic v2 默认 extra='ignore'，
    # 漏掉一个字段不会报错，只会静默丢弃 —— 用户点了「保存」、看到成功提示，
    # 刷新后改动没了。所以每次给编辑表单加字段，都要同步加到这里。
    name: str | None = None
    brand: str | None = None
    model: str | None = None
    spec: str | None = None
    # 设备描述 / 性能参数（2026-10-05）：编辑表单能改的字段必须在这里，
    # 否则 pydantic 默认 extra='ignore' 会静默丢弃（点了保存却看不到变化）
    description: str | None = Field(None, max_length=4000)
    unit: str | None = None
    category: DeviceCategory | None = None
    category_id: UUID | None = None
    subsystem_default: str | None = None
    reference_price: Decimal | None = None
    cost_price: Decimal | None = None
    install_unit_price: Decimal | None = None
    status: DeviceStatus | None = None
    is_active: bool | None = None
    is_owner_supplied: bool | None = None
    is_common: bool | None = None
    replacement_id: UUID | None = None
    moq: int | None = None
    warranty_years: int | None = None
    eol_at: date | None = None


class DeviceOut(ORMBase):
    auxiliary_unit_price: Decimal = Decimal(0)
    # 价格底线与阶梯价（2026-10-04）：明细取材时前端要显示「这一档多少钱」
    min_price: Decimal | None = None
    price_tiers: list[PriceTierOut] = Field(default_factory=list)
    id: UUID
    code: str
    name: str
    brand: str | None
    model: str | None
    spec: str | None
    # 设备描述 / 性能参数（2026-10-05）：选产品时悬浮显示，也带进报价单的「设备参数」栏
    description: str | None = None
    unit: str
    category: DeviceCategory
    # 分类树：category_id 是实际归属；category_path 是给人看的「视频监控 / 摄像机」
    # （由接口从分类树算好带出来，不在 ORM 上顺着 relationship 懒加载 —— 那会 MissingGreenlet）
    category_id: UUID | None = None
    category_path: str = ""
    subsystem_default: str | None = None
    reference_price: Decimal
    cost_price: Decimal
    install_unit_price: Decimal
    status: DeviceStatus
    is_owner_supplied: bool
    is_active: bool
    replacement_id: UUID | None
    # 以下字段 ORM 里有、前端编辑表单也在用，之前没放进 DeviceOut，
    # 于是「常用」永远是未勾选、质保年限/起订量永远显示默认值。
    is_common: bool
    moq: int
    warranty_years: int
    eol_at: date | None = None
    supplier_id: UUID | None = None
    # 最后询价：原型第 2 页「最近询价」列要显示"日期 + 供应商"，模型里早就有这两列，
    # 但一直没在 DeviceOut 里暴露，前端拿不到。
    last_inquiry_at: datetime | None = None
    last_inquiry_price: Decimal | None = None


# ----- 设备库分类树（2026-09-30 起：设备库不再是一层扁平分类）-----

class DeviceCategoryNodeIn(BaseModel):
    """新增分类节点。层级不限：`parent_id` 为空即一级分类。"""

    code: str = Field(..., min_length=1, max_length=64, description="稳定代号，如 video.camera")
    name: str = Field(..., min_length=1, max_length=64)
    parent_id: UUID | None = None
    sort_order: int = 0
    is_active: bool = True
    # 一级分类必填（取 DeviceCategory 的值）；子节点必须留空，由父链推导
    legacy_enum: str | None = None
    # 设备编号前缀（V/A/I/…）；子节点留空表示继承父节点
    code_prefix: str | None = Field(None, max_length=4)
    notes: str | None = Field(None, max_length=255)


class DeviceCategoryNodeUpdate(BaseModel):
    """改分类节点。`code` 不允许改（导入映射按它对齐），换位置用 parent_id。"""

    name: str | None = Field(None, min_length=1, max_length=64)
    parent_id: UUID | None = None
    sort_order: int | None = None
    is_active: bool | None = None
    code_prefix: str | None = Field(None, max_length=4)
    notes: str | None = Field(None, max_length=255)


class DeviceCategoryNodeOut(ORMBase):
    id: UUID
    parent_id: UUID | None
    code: str
    name: str
    sort_order: int
    is_active: bool
    legacy_enum: str | None = None
    code_prefix: str | None = None
    notes: str | None = None
    # 「视频监控 / 摄像机」
    path: str = ""
    # 本节点自身的在售设备数 / 含全部子节点的设备数
    device_count: int = 0
    total_count: int = 0
    children: list[DeviceCategoryNodeOut] = []


class DeviceCategoryTreeOut(BaseModel):
    nodes: list[DeviceCategoryNodeOut] = []
    # 没挂到分类树上的在售设备数（手工 SQL 插入的行可能出现）
    unassigned: int = 0
    total: int = 0
    # 枚举里有、树上却没有一级节点的分类 —— 显式提示，避免出现"看不见的分类"
    missing_roots: list[str] = []


# ----- Quote -----

# ---------------------------------------------------------------------------
# 费率 / 金额的边界
# ---------------------------------------------------------------------------
# 以前这些字段只声明了 Decimal，没有任何上下界。后果实测：
#   tax_rate=5.0（=500%）、mgmt_profit_rate=99.0、auxiliary_rate=-0.5 全部 200 通过，
#   一行 -5 × -100 的明细算出了 159,000.00 的含税合计并直接落库、可直接导出给甲方。
# 另外这些列是 Numeric(5,4)（|x| < 10）：在 SQLite（测试）上 99 会被静默接受，
# 到 PostgreSQL 才 numeric field overflow → 500。加界之后两边都是 422。
#


class QuoteCreate(BaseModel):
    project_id: UUID
    # code/title 允许不传：前端在「编制报价」时只想给一个 project_id，
    # 让用户先答一道「报价单编号叫什么」是没必要的门槛。create_quote 会按项目
    # 编号 + 版本号自动生成 code，title 默认取项目名。
    code: str | None = Field(None, min_length=2, max_length=32)
    title: str | None = None
    customer_name: str | None = None
    customer_contact: str | None = None
    customer_phone: str | None = None
    # tax_method 在库里是普通 String(32) 列（pg 没有对应 enum），保持 str。
    tax_method: str = "general_9pct"
    tax_rate: Decimal = Field(Decimal("0.09"), ge=RATE_MIN, le=RATE_MAX)
    fee_base: FeeBaseType = FeeBaseType.DEVICE_AUX_INSTALL
    # 报价模式（2026-10-05 用户要求）：**创建时确定、之后不可切换** ✓
    #   manual = 手工设置人工费（默认，兼容老调用方 / 脚本）
    #   quota  = 套定额（人工费按定额库取）
    install_mode: Literal["manual", "quota"] = "manual"
    # 增项来源（2026-10-05）：本单从哪张报价单增项而来 ✓
    #   带上它 → 自动复制原单的费率/条款与明细 ✓（归档单尤其需要这样增项 ✓）
    based_on_quote_id: UUID | None = None
    auxiliary_base: AuxiliaryBaseType = AuxiliaryBaseType.DEVICE
    auxiliary_rate: Decimal = Field(Decimal("0.06"), ge=RATE_MIN, le=RATE_MAX)
    design_rate: Decimal = Field(Decimal("0.02"), ge=RATE_MIN, le=RATE_MAX)
    warranty_rate: Decimal = Field(Decimal("0.03"), ge=RATE_MIN, le=RATE_MAX)
    training_fee: Decimal = Field(Decimal("0"), ge=MONEY_MIN)
    mgmt_profit_rate: Decimal = Field(Decimal("0.08"), ge=RATE_MIN, le=RATE_MAX)
    labor_float_rate: Decimal = Field(
        Decimal("0"), ge=LABOR_FLOAT_MIN, le=LABOR_FLOAT_MAX
    )
    labor_float_per_class: LaborFloatPerClass = None
    notes: str | None = None


# ---- 每单条款数字（2026-10-05）----
# 6 个变量的取值范围 ✓：比例 0~100% ✓、天数/月数有上限 ✓。
# 越界直接 422 ✓（宁可在入口拦住 ✗ —— 条款是印给客户看的东西 ✓）。
_TERM_RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "valid_days": (Decimal("1"), Decimal("3650")),
    "prepay_ratio": (Decimal("0"), Decimal("100")),
    "accept_ratio": (Decimal("0"), Decimal("100")),
    "warranty_ratio": (Decimal("0"), Decimal("100")),
    "warranty_months": (Decimal("0"), Decimal("120")),
    "build_days": (Decimal("1"), Decimal("3650")),
}


def _bounded_terms(value: dict[str, Decimal] | None) -> dict[str, Decimal] | None:
    """每单条款数字：只认 6 个已知变量 ✓，且落在合理区间内 ✓。"""
    if value is None:
        return None
    out: dict[str, Decimal] = {}
    for name, raw in value.items():
        if name not in _TERM_RANGES:
            raise ValueError(f"条款变量不存在：{name}（可用：{'、'.join(_TERM_RANGES)}）")
        num = Decimal(str(raw))
        lo, hi = _TERM_RANGES[name]
        if num < lo or num > hi:
            raise ValueError(f"{name} 超出范围：应在 {lo} ~ {hi} 之间")
        out[name] = num
    return out


class QuoteUpdate(BaseModel):
    # 单据级条款覆盖（2026-10-05）：传 null = 恢复用系统设置；
    # 支持 {valid_days} 之类变量，渲染时按本单生效值替换。
    # 安装单价方式（2026-10-05 用户设计）：manual=手填；quota=套定额（基价自动填）
    install_mode: str | None = Field(None, pattern="^(manual|quota)$")
    clauses_override: list[str] | None = None
    valid_days_override: int | None = Field(None, ge=1, le=365)
    # 乐观锁：把读到的 version 原样带回来。对不上后端返回 409，
    # 避免后提交的人静默覆盖前一个人的修改。不传则跳过比对（向后兼容）。
    version: int | None = None
    title: str | None = None
    status: QuoteStatus | None = None
    tax_method: str | None = None
    tax_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    fee_base: FeeBaseType | None = None
    auxiliary_base: AuxiliaryBaseType | None = None
    auxiliary_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    design_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    warranty_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    training_fee: Decimal | None = Field(None, ge=MONEY_MIN)
    mgmt_profit_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    labor_float_rate: Decimal | None = Field(
        None, ge=LABOR_FLOAT_MIN, le=LABOR_FLOAT_MAX
    )
    labor_float_per_class: LaborFloatPerClass = None
    notes: str | None = None


    # 每单条款数字（2026-10-05）：6 个变量的本单取值 ✓
    #   为空 = 继承系统设置 ✓（行为与以前一致 ✓）
    terms_override: Annotated[
        dict[str, Decimal] | None, AfterValidator(_bounded_terms)
    ] = None

class QuoteOut(ORMBase):
    # 安装单价方式（2026-10-05）：缺省 manual ✓（老单子不受影响 ✓）
    install_mode: str = "manual"
    # 增项来源（2026-10-05）：前端据此显示「增项自 <原单号>」✓
    based_on_quote_id: UUID | None = None
    id: UUID
    project_id: UUID
    code: str
    # ⚠ version 是**乐观锁计数器**（每次写库 +1），不是给用户看的版本号 ✗。
    version: int
    # 业务版本（2026-10-08 用户反馈「只选了一台录像机就 V9」）：
    #   「版本」= 这张单**被人保存为第几版**（人点「保存并重算」/提交/批准/手动存档）✓。
    #   展示用**时间标记** ✓（2026-10-08 用户补充：「版本号以时间为标记」✓）：
    #   `latest_revision_at` = 最新一版快照的 created_at（UTC，前端按浏览器本地时区显示）✓。
    #   没保存过 → None / 0 → 界面显示「—」✓，不显示数字 ✓。
    #   seq 仍保留 ✓：版本排序、两版对比、追溯都要用它 ✓（不该拿去当"版本号"展示 ✗）。
    #   由 services.revisions.attach_revision_stats 挂上（瞬态属性，一次 group by ✓）。
    revision_count: int = 0
    latest_revision_seq: int = 0
    latest_revision_at: datetime | None = None
    title: str
    status: QuoteStatus
    # 本单自定义的说明条款 / 有效期（null = 用系统设置里那份）
    clauses_override: list[str] | None = None
    valid_days_override: int | None = None
    customer_name: str | None = None
    customer_contact: str | None = None
    customer_phone: str | None = None
    # 费率面板：双行并列与基数三选一都靠这几个字段回显
    tax_method: str
    tax_rate: Decimal
    fee_base: FeeBaseType
    auxiliary_base: AuxiliaryBaseType
    auxiliary_rate: Decimal
    design_rate: Decimal
    warranty_rate: Decimal
    training_fee: Decimal
    mgmt_profit_rate: Decimal
    labor_float_rate: Decimal
    labor_float_per_class: dict[str, Any] = {}
    # 费用瀑布结果
    equipment_total: Decimal
    install_total: Decimal
    auxiliary_total: Decimal
    design_fee: Decimal
    warranty_fee: Decimal
    subtotal_before_tax: Decimal
    mgmt_profit: Decimal
    tax_amount: Decimal
    total_with_tax: Decimal
    quota_total: Decimal
    quota_total_with_tax: Decimal
    # 刻意不含 cost_total / gross_profit / gross_profit_rate：
    # 这三个是对内敏感字段，只由 GET /quotes/{id}/cost 按权限下发。
    # 放在通用 DTO 里会让 viewer 也能从列表/详情接口读到毛利。
    notes: str | None = None
    created_at: datetime
    updated_at: datetime


    # 每单条款数字 ✓（为空表示继承系统设置 ✓）
    terms_override: dict[str, Any] | None = None

class QuoteDetailOut(QuoteOut):
    """Quote with items inline."""

    items: list[QuoteItemOut] = []


# ----- Quote item -----

class QuoteItemCreate(BaseModel):
    # 单位工程量辅材费（2026-10-04）：「按工程量」口径下 辅材费=Σ(数量×本列)
    auxiliary_unit_price: Decimal = Field(Decimal(0), ge=0)
    # 低于设备底价时的放行理由（只有经理/管理员能放行，且必须非空）
    below_floor_reason: str | None = Field(None, max_length=255)
    subsystem_id: UUID | None = None
    device_id: UUID | None = None
    # 配件行指向主设备行（2026-10-05）：由"配单时补授权"的流程带上
    parent_item_id: UUID | None = None
    name: str
    brand: str | None = None
    model: str | None = None
    unit: str = "台"
    # 设备参数（2026-10-05）：不传则从设备的 description 自动带过来
    spec_text: str | None = Field(None, max_length=512)
    # 非负：负数量或负单价会产出负金额，进而让"按金额"的一切校验与汇总失真。
    # 实测以前 {"quantity": "-5", "unit_price": "-100"} 会 201 入库。
    quantity: Decimal = Field(Decimal("0"), ge=MONEY_MIN)
    unit_price: Decimal = Field(Decimal("0"), ge=MONEY_MIN, le=UNIT_PRICE_MAX)
    install_unit_price: Decimal = Field(
        Decimal("0"), ge=MONEY_MIN, le=UNIT_PRICE_MAX
    )
    cost_price: Decimal | None = Field(None, ge=MONEY_MIN)
    is_owner_supplied: bool = False
    source: ItemSource = ItemSource.LIBRARY
    sort_order: int = 0
    notes: str | None = None
    # 工艺系数（结构化字段，默认 False —— 全 False 时安装单价 ×1.0）
    is_shielded: bool = False
    is_cat5e: bool = False
    is_ceiling: bool = False


class QuoteItemUpdate(BaseModel):
    # 单位工程量辅材费（2026-10-04）：「按工程量」口径下 辅材费=Σ(数量×本列)
    auxiliary_unit_price: Decimal = Field(Decimal(0), ge=0)
    # 低于设备底价时的放行理由（只有经理/管理员能放行，且必须非空）
    below_floor_reason: str | None = Field(None, max_length=255)
    # 编辑明细行时能改的字段都要在这里（pydantic 会静默忽略多余的键）。
    # name/brand/model/unit/subsystem_id 以前缺失，于是「编辑」弹窗里改了名称、
    # 型号或所属子系统，提示成功但刷新后原样不动。
    name: str | None = None
    brand: str | None = None
    model: str | None = None
    unit: str | None = None
    subsystem_id: UUID | None = None
    quantity: Decimal | None = Field(None, ge=MONEY_MIN)
    unit_price: Decimal | None = Field(None, ge=MONEY_MIN, le=UNIT_PRICE_MAX)
    install_unit_price: Decimal | None = Field(None, ge=MONEY_MIN, le=UNIT_PRICE_MAX)
    is_owner_supplied: bool | None = None
    sort_order: int | None = None
    notes: str | None = None
    # 工艺系数 — 编辑行也可以勾上/取消
    is_shielded: bool | None = None
    is_cat5e: bool | None = None
    is_ceiling: bool | None = None


class QuoteItemOut(ORMBase):
    # 定额子目挂在哪台主设备下（2026-10-05 阶段1 ✓）：
    # 前端靠它把子目行挂到主设备行下 ✓；缺了它树就展不开 ✗
    parent_item_id: UUID | None = None
    auxiliary_unit_price: Decimal = Decimal(0)
    id: UUID
    quote_id: UUID
    subsystem_id: UUID | None
    device_id: UUID | None
    name: str
    brand: str | None
    model: str | None
    unit: str
    spec_text: str | None = None
    quantity: Decimal
    unit_price: Decimal
    equipment_amount: Decimal
    install_unit_price: Decimal
    install_amount: Decimal
    source: ItemSource
    is_owner_supplied: bool
    # 停产闭环：界面要显示「已替换」徽章与「quota:<code>」等约定标记。
    # 注意：cost_price 故意不在此暴露 —— 成本仅为 admin/estimator/finance 可见
    # （见 GET /quotes/{id}/cost）。
    is_replaced: bool = False
    replaced_by_id: UUID | None = None
    sort_order: int = 0
    notes: str | None = None
    # 工艺系数（结构化字段）
    is_shielded: bool = False
    is_cat5e: bool = False
    is_ceiling: bool = False
    # 设备状态投影，供界面画「已停产」徽章。
    # 仅在设备被 eager load 时有值（见 quotes.py 的 _load_quote）；
    # 单行写入类接口返回 None，前端改完会整单重取。
    device_code: str | None = None
    device_status: DeviceStatus | None = None


# ----- Supplier -----

class DeviceFromQuoteItemsRequest(BaseModel):
    """把报价单里的行「收进企业设备库」（原型报价页的「复制到系统」）。

    典型场景：报价时库里没有这个型号，先手工录入了一行；成交后希望它变成
    标准设备，下次直接选。已经关联设备库的行会被跳过，同型号已存在的也会跳过
    并告诉你命中了哪个编码 —— 宁可少建，也不要在库里造重复型号。
    """

    item_ids: list[UUID] = Field(..., min_length=1, max_length=200)
    # 指定挂到分类树的哪个节点（不传则按报价行所属子系统落到对应一级分类）
    category_id: UUID | None = None


class DeviceBulkPriceRequest(BaseModel):
    """批量调价（原型第 5 页工具栏「批量调价」）。

    两种口径：
      · `percent` —— `value=5` 表示 +5%；`value=-3.5` 表示 -3.5%
      · `amount`  —— 在现价上直接加/减 `value` 元
    `dry_run=True` 只算不改（先给用户看变更明细），这是默认值：
    价格是钱，宁可多一步确认。
    """

    device_ids: list[UUID] = Field(..., min_length=1, max_length=500)
    field: Literal["reference_price", "cost_price", "install_unit_price"] = "reference_price"
    mode: Literal["percent", "amount"] = "percent"
    value: Decimal = Field(..., description="percent: 百分数（5 = +5%）；amount: 元")
    round_yuan: bool = True
    dry_run: bool = True


# ----- 点位模板（2026-10-04）-----


class PointTemplateItemIn(BaseModel):
    device_id: UUID | None = None
    quantity: Decimal = Field(Decimal("1"), gt=0)
    is_primary: bool = False
    notes: str | None = Field(None, max_length=512)
    sort_order: int = 0


class PointTemplateCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=255)
    subsystem_type: SubsystemType | None = None
    # 元/点。套用时只落到「主设备」那一行，避免一个点位里多组件重复计安装费。
    install_unit_price: Decimal = Field(Decimal("0"), ge=0)
    notes: str | None = Field(None, max_length=2048)
    is_active: bool = True
    sort_order: int = 0
    items: list[PointTemplateItemIn] = Field(default_factory=list)


class PointTemplateUpdate(BaseModel):
    """编辑：`items` 传了就整表替换（不传则不动明细）。"""

    name: str | None = Field(None, min_length=1, max_length=255)
    subsystem_type: SubsystemType | None = None
    install_unit_price: Decimal | None = Field(None, ge=0)
    notes: str | None = Field(None, max_length=2048)
    is_active: bool | None = None
    sort_order: int | None = None
    items: list[PointTemplateItemIn] | None = None


class PointTemplateItemOut(ORMBase):
    id: UUID
    device_id: UUID | None
    quantity: Decimal
    is_primary: bool
    notes: str | None
    sort_order: int
    # 设备投影（模板详情要显示"球型摄像机 ×1 2,450.00"）
    device_code: str | None = None
    device_name: str | None = None
    device_model: str | None = None
    device_unit: str | None = None
    device_reference_price: Decimal | None = None
    device_status: str | None = None


class PointTemplateOut(ORMBase):
    id: UUID
    code: str
    name: str
    subsystem_type: SubsystemType | None
    install_unit_price: Decimal
    notes: str | None
    is_active: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime
    items: list[PointTemplateItemOut] = Field(default_factory=list)


class PointTemplateApplyRequest(BaseModel):
    """把模板套到某张报价单：点位数 N → 每行数量 = 模板数量 × N。"""

    quote_id: UUID
    subsystem_id: UUID | None = None
    point_count: Decimal = Field(..., gt=0)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# 客户档案（2026-10-04）
# ---------------------------------------------------------------------------

class CustomerCreate(BaseModel):
    # code 可空：不传就由后端按 CUS-0001 递增分配（销售不该被"编号"卡住）
    code: str | None = Field(None, max_length=32)
    name: str = Field(..., min_length=1, max_length=255)
    short_name: str | None = Field(None, max_length=64)
    contact_name: str | None = Field(None, max_length=64)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: str | None = Field(None, max_length=128)
    address: str | None = Field(None, max_length=255)
    tax_no: str | None = Field(None, max_length=64)
    bank_name: str | None = Field(None, max_length=128)
    bank_account: str | None = Field(None, max_length=64)
    industry: str | None = Field(None, max_length=64)
    note: str | None = None


class CustomerUpdate(BaseModel):
    # 编辑表单能改的都要在这：pydantic v2 默认 extra='ignore'，
    # 漏一个字段不会报错，只会被静默丢弃。
    name: str | None = Field(None, min_length=1, max_length=255)
    short_name: str | None = Field(None, max_length=64)
    contact_name: str | None = Field(None, max_length=64)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: str | None = Field(None, max_length=128)
    address: str | None = Field(None, max_length=255)
    tax_no: str | None = Field(None, max_length=64)
    bank_name: str | None = Field(None, max_length=128)
    bank_account: str | None = Field(None, max_length=64)
    industry: str | None = Field(None, max_length=64)
    note: str | None = None
    is_active: bool | None = None


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    short_name: str | None = None
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    address: str | None = None
    tax_no: str | None = None
    bank_name: str | None = None
    bank_account: str | None = None
    industry: str | None = None
    note: str | None = None
    is_active: bool = True


class SupplierCreate(BaseModel):
    # 业务编码（如 SUP-001）必须传、唯一。重名重码都会 409。
    # `name` 不做 max_length=255 之外的额外约束；供应商名称本来就有长有短。
    code: str = Field(..., min_length=1, max_length=32)
    name: str = Field(..., min_length=1, max_length=255)
    contact_name: str | None = Field(None, max_length=64)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: str | None = Field(None, max_length=255)
    address: str | None = Field(None, max_length=512)
    # 仅 admin/admin 可写（看路由层 require_permission）。
    # 前端表单不会带 `is_active`，缺省 True 即可。
    is_active: bool = True


class SupplierUpdate(BaseModel):
    # 编辑表单能改的字段都要在这里：pydantic v2 默认 extra='ignore'，
    # 漏掉一个字段不会报错，只会被静默丢弃。
    name: str | None = Field(None, min_length=1, max_length=255)
    contact_name: str | None = Field(None, max_length=64)
    contact_phone: str | None = Field(None, max_length=32)
    contact_email: str | None = Field(None, max_length=255)
    address: str | None = Field(None, max_length=512)
    is_active: bool | None = None
    # code 不允许修改：code 已经被 quote_items / devices 用作外键/业务键，
    # 一改上下游全得改。这条限制在这里写死，路由层也会再守一道。


class SupplierOut(ORMBase):
    id: UUID
    code: str
    name: str
    contact_name: str | None
    contact_phone: str | None
    contact_email: str | None
    address: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ----- Labor / quota -----

class LaborRateOut(ORMBase):
    id: UUID
    region: str
    labor_class: str
    effective_date: date
    price_per_day: Decimal
    source: str
    is_current: bool


class QuotaItemOut(ORMBase):
    id: UUID
    code: str
    category: str
    name: str
    unit: str
    labor_a_day: Decimal
    labor_b_day: Decimal
    labor_c_day: Decimal
    base_price: Decimal
    # 2022.7.13 智能化纪要工艺系数 —— 前端「定额核对」页要展示可比对
    # 定额三费（2026-10-05 阶段 0 ✓）：广联达模式显示「定额人工/辅材/机械」用
    labor_fee: Decimal = Decimal("0")
    material_fee: Decimal = Decimal("0")
    machine_fee: Decimal = Decimal("0")
    shielded_coef: Decimal
    cat5e_coef: Decimal
    ceiling_coef: Decimal
    notes: str | None = None
    is_active: bool = True


# ----- Compute (DTO) -----

class ComputeRequest(BaseModel):
    """触发重算时可选地覆盖费率。

    必须与 QuoteUpdate 的费率字段保持一致：pydantic v2 默认 extra='ignore'，
    少写一个字段不会报错，只会被丢掉 —— 界面上改了税率、点了「重算」、
    收到成功提示，实际却按旧费率算，钱错了还看不出来。
    """

    # 乐观锁：与 QuoteUpdate 同理。这里少写一个字段同样会被 pydantic 静默丢掉，
    # 结果就是「点重算」这个口子绕过版本比对，把别人的修改盖掉。
    version: int | None = None
    tax_method: str | None = None
    tax_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    fee_base: FeeBaseType | None = None
    auxiliary_base: AuxiliaryBaseType | None = None
    auxiliary_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    design_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    warranty_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    training_fee: Decimal | None = Field(None, ge=MONEY_MIN)
    mgmt_profit_rate: Decimal | None = Field(None, ge=RATE_MIN, le=RATE_MAX)
    labor_float_rate: Decimal | None = Field(
        None, ge=LABOR_FLOAT_MIN, le=LABOR_FLOAT_MAX
    )
    labor_float_per_class: LaborFloatPerClass = None
    notes: str | None = None


class ComputeResult(BaseModel):
    equipment_total: Decimal
    install_total: Decimal
    auxiliary_total: Decimal
    design_fee: Decimal
    warranty_fee: Decimal
    subtotal_before_tax: Decimal
    mgmt_profit: Decimal
    tax_amount: Decimal
    total_with_tax: Decimal
    quota_total: Decimal
    quota_total_with_tax: Decimal
    # 仅在调用者具备成本视图权限时才有值，否则为 None（见 api/v1/quotes.py 的 _can_see_cost）
    cost_total: Decimal | None = None
    gross_profit: Decimal | None = None
    gross_profit_rate: Decimal | None = None
    # 成本口径的可识别性：有多少行的成本是"按设备价 ×75% 估算"出来的。
    # 不把这些行标出来，报表里的毛利率就分不清真实与估算。
    estimated_cost_rows: int = 0
    estimated_cost_amount: Decimal | None = None
    # 计价过程中的降级提示，例如「定额库未加载，定额口径已退化为安装费」
    warnings: list[str] = []
    # ----- 本次「保存并重算」有没有落一版快照（2026-10-08 用户要求）-----
    # 语义变了提示就得跟上 ✓：`POST /quotes/{id}/compute` 现在**会落一版**
    # （reason=saved，见 api/v1/quotes.py），界面必须能说清"刚存了一版（时间）"✓，
    # 而不是只回一句「已按当前费率重算」✗ —— 那样用户根本不知道存了版本 ✗。
    # `revision_saved=False` = 内容与最新一版完全一致、按去重口径**跳过**了 ✓，
    # 界面据此提示「内容未变化，未新增版本」✓，不许假装存了一版 ✗。
    revision_saved: bool = False
    revision_created_at: datetime | None = None
    revision_count: int = 0


# Resolve forward refs
QuoteDetailOut.model_rebuild()
# 分类树节点自引用（children 指回自己），也要显式 rebuild
DeviceCategoryNodeOut.model_rebuild()
# 部门树同理
DepartmentOut.model_rebuild()








# ---------------------------------------------------------------------------
# 分级审批（2026-10-04）
# ---------------------------------------------------------------------------

class ApprovalDecision(BaseModel):
    """一次审批：通过或驳回。驳回按设置可能需要写意见。"""

    decision: Literal["approved", "rejected"]
    comment: str | None = Field(None, max_length=500)


# ---------------------------------------------------------------------------
# 配件 / 授权（2026-10-05）
# ---------------------------------------------------------------------------
# 只说"能配什么"：槽（授权/电源模块）+ 槽里的候选（不同档的授权）。
# 候选的价格取**参考价**；进价不在这里暴露 —— 字段级可见性由权限决定，
# 需要成本的地方走 /quotes/{id}/cost 那条已经带门禁的路。
class AccessoryOptionIn(BaseModel):
    accessory_id: UUID
    is_default: bool = False
    note: str | None = Field(None, max_length=255)
    sort_order: int = 0


class AccessoryGroupIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    is_required: bool = True
    select_mode: Literal["one", "many"] = "one"
    quantity_mode: Literal["per_unit", "fixed"] = "per_unit"
    default_quantity: Decimal = Field(Decimal(1), ge=0)
    note: str | None = Field(None, max_length=255)
    sort_order: int = 0
    is_active: bool = True
    options: list[AccessoryOptionIn] = Field(default_factory=list)


class AccessorySaveIn(BaseModel):
    """整体替换：界面上编辑完点保存，一次性提交。"""

    groups: list[AccessoryGroupIn] = Field(default_factory=list)


class AccessoryOptionOut(BaseModel):
    accessory_id: UUID
    code: str
    name: str
    brand: str | None = None
    model: str | None = None
    unit: str = "台"
    reference_price: Decimal | None = None
    is_default: bool = False
    note: str | None = None
    sort_order: int = 0


class AccessoryGroupOut(BaseModel):
    id: UUID
    label: str
    is_required: bool
    select_mode: str
    quantity_mode: str
    default_quantity: Decimal
    note: str | None = None
    sort_order: int = 0
    is_active: bool = True
    options: list[AccessoryOptionOut] = Field(default_factory=list)


class MissingAccessoryOut(BaseModel):
    """配单检查里的一条"还没配"提示。"""

    item_id: UUID
    item_name: str
    device_id: UUID | None = None
    device_code: str | None = None
    quantity: Decimal
    group_label: str
    is_required: bool
    select_mode: str
    quantity_mode: str
    default_quantity: Decimal
    # 已经配上的配件名（用于显示"已选：高级授权"），空 = 一个都没配
    chosen: list[str] = Field(default_factory=list)
    options: list[AccessoryOptionOut] = Field(default_factory=list)


class AccessoryCheckOut(BaseModel):
    quote_id: UUID
    missing: list[MissingAccessoryOut] = Field(default_factory=list)
    # 人话总结，界面直接显示
    summary: str = ""
