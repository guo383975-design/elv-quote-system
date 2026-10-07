/**
 * 与后端 Pydantic schema 一一对应的 TS 类型。
 *
 * 重要：所有金额字段后端都是 Decimal，序列化后可能是 **字符串**
 * （如 "58480.00"）也可能是数字，取决于 pydantic 的 mode。因此这里统一
 * 声明为 `Decimalish`，前端一律通过 utils/format 里的 money()/toNum()
 * 归一化后再展示或计算 —— 绝不要直接 `a + b`。
 */
export type Decimalish = string | number

/** 后端分页信封。 */
export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

// ----- 认证 / 用户 -----

/**
 * 角色键（2026-10-05 起**可自定义**，所以不再是固定联合类型）。
 * 可选项来自 `/meta/enums` 的 role 组（内置 + 自定义角色），
 * 校验与权限判定都在后端 core/roles.py + core/permissions.py。
 */
export type Role = string

export interface LoginPayload {
  username: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
}

export interface User {
  id: string
  username: string
  email: string
  full_name: string | null
  phone: string | null
  department: string | null
  /** 组织架构节点 id（实际归属） */
  department_id: string | null
  /** 「公司 / 设计部 / 智能化设计组」——由后端算好带出来 */
  department_path: string
  role: Role
  status: string
  is_superuser: boolean
  created_at: string
  last_login_at: string | null
}

/**
 * 组织架构树节点（2026-10-02：用户管理改成"左树右列表"）。
 *
 * `user_count` 是本节点**自身**的人数，`total_count` 是含全部下级的人数 ——
 * 树上显示后者（点父部门能看到整棵子树），前者用来判断这一格自己有没有人。
 */
export interface Department {
  id: string
  parent_id: string | null
  code: string
  name: string
  sort_order: number
  is_active: boolean
  notes: string | null
  path: string
  user_count: number
  total_count: number
  children: Department[]
}

export interface DepartmentTree {
  nodes: Department[]
  /** 没有归属部门的用户数 */
  unassigned: number
  total: number
}

/** 操作留痕（项目 / 报价单同构）。 */
export interface AuditLogEntry {
  id: string
  user_id: string | null
  action: string
  payload: Record<string, unknown>
  note: string | null
  created_at: string | null
}

/** GET /auth/me/permissions —— 后端已算好的 «资源 -> 允许动作» 矩阵。 */
export interface PermissionsResponse {
  role: Role
  is_superuser: boolean
  resources: string[]
  actions: Record<string, string[]>
  /**
   * 字段级可见性（2026-10-05）：与后端 `core/visibility` 同一判定。
   * 前端用它隐藏成本/利润的入口与列，不自己硬编码角色名单
   * （以前硬编码过两份，与新口径打架）。
   */
  can_see_cost: boolean
  can_see_profit: boolean
}

// ----- 项目 / 子系统 -----

export type ProjectType = 'commercial' | 'public' | 'residential' | 'data_center' | 'other'
export type ProjectStatus = 'draft' | 'quoting' | 'quoted' | 'won' | 'lost' | 'archived'

export interface Project {
  id: string
  code: string
  name: string
  /** 客户档案 id（可空）：老项目只有下面的文本字段 */
  customer_id?: string | null
  customer_name: string
  project_type: ProjectType
  status: ProjectStatus
  /** 这一单有几张报价单 / 其中几张在等我审批（列表徽标用） */
  quote_count: number
  pending_quote_count: number
  customer_contact: string | null
  customer_phone: string | null
  address: string | null
  expected_start: string | null
  expected_duration_days: number | null
  total_area_sqm: Decimalish | null
  notes: string | null
  created_at: string
  updated_at: string | null
  /** 项目级费率：建报价单时抄进报价单作为快照（2026-10-03 起可在界面上配置） */
  tax_rate: Decimalish
  fee_base: FeeBaseType
  auxiliary_base: AuxiliaryBaseType
  auxiliary_rate: Decimalish
  design_rate: Decimalish
  warranty_rate: Decimalish
  training_fee: Decimalish
  mgmt_profit_rate: Decimalish
}

export type SubsystemType =
  | 'video_surveillance'
  | 'access_control'
  | 'intrusion'
  | 'structured_cabling'
  | 'network'
  | 'broadcast'
  | 'conference'
  | 'server_room'
  | 'parking'
  | 'info_publish'
  | 'other'

export interface Subsystem {
  id: string
  project_id: string
  subsystem_type: SubsystemType
  custom_name: string | null
  sort_order: number
  point_count: number
  equipment_subtotal: Decimalish
  install_subtotal: Decimalish
  auxiliary_subtotal: Decimalish
  /**
   * 备注字段。**当前后端 schema 还没挂这个字段**（TODO(后端 CRUD)），
   * POST 时附带上去会被 FastAPI 当作 extra 丢弃；这里先声明成可选，
   * 等后端补齐后就会真的回。
   */
  notes?: string | null
  /** 子系统所挂的供货商（2026-09-29 后端已上 FK）。可选：选「自定义」时可以不挂。 */
  supplier_id: string | null
}

// ----- 设备库 -----

export type DeviceCategory =
  | 'video'
  | 'access'
  | 'intrusion'
  | 'cabling'
  | 'network'
  | 'broadcast'
  | 'conference'
  | 'server_room'
  | 'parking'
  | 'info_publish'
  | 'power'
  | 'accessory'

export type DeviceStatus = 'on_sale' | 'near_eol' | 'eol' | 'discontinued'

export interface Device {
  id: string
  code: string
  name: string
  brand: string | null
  model: string | null
  spec: string | null
  /** 设备描述 / 性能参数（2026-10-05）：选产品时悬浮显示，也带进报价单的设备参数栏 */
  description?: string | null
  unit: string
  category: DeviceCategory
  /** 分类树节点 id（设备的实际归属）；为空表示未分类 */
  category_id: string | null
  /** 「视频监控 / 摄像机」——由后端从分类树算好带出来 */
  category_path: string
  subsystem_default: string | null
  reference_price: Decimalish
  cost_price: Decimalish
  install_unit_price: Decimalish
  status: DeviceStatus
  is_owner_supplied: boolean
  is_active: boolean
  is_common: boolean
  replacement_id: string | null
  eol_at: string | null
  moq: number
  warranty_years: number
  supplier_id: string | null
  /** 最后询价（原型第 2 页「最近询价」列）：日期 + 价格，2026-10-03 起由 DeviceOut 暴露 */
  last_inquiry_at: string | null
  last_inquiry_price: Decimalish | null  /** 价格底线：低于它报价需要经理授权 + 理由 */
  min_price?: string | null
  price_tiers?: PriceTier[]
  /** 单位工程量辅材费（元/单位）：随设备带进报价单 */
  auxiliary_unit_price?: string | number

}

/** 批量调价结果（dry_run=true 时 changes 只是预览，不落库）。 */
export interface BulkPriceResult {
  dry_run: boolean
  field: string
  mode: string
  value: number
  round_yuan: boolean
  count: number
  missing: string[]
  total_before: number
  total_after: number
  changes: {
    device_id: string
    code: string
    name: string
    before: number
    after: number
    delta: number
    clamped: boolean
  }[]
}
export interface DeviceStats {
  total: number
  by_status: Record<string, number>
  by_category: Record<string, number>
  with_replacement: number
  /** 甲供设备数（原型第 5 页统计条要显示「甲供 N」） */
  owner_supplied: number
}

/**
 * 设备库分类树节点（2026-09-30 起设备库不再是一层扁平分类）。
 *
 * `device_count` 是本节点**自身**挂的设备数，`total_count` 是含全部子节点的合计
 * —— 树上显示后者（点父节点能看到整棵子树），前者用来判断这一格自己有没有东西。
 */
export interface DeviceCategoryNode {
  id: string
  parent_id: string | null
  code: string
  name: string
  sort_order: number
  is_active: boolean
  legacy_enum: string | null
  code_prefix: string | null
  notes: string | null
  path: string
  device_count: number
  total_count: number
  children: DeviceCategoryNode[]
}

export interface DeviceCategoryTree {
  nodes: DeviceCategoryNode[]
  /** 没挂到分类树上的在售设备数 */
  unassigned: number
  total: number
  /** 枚举里有、树上还没有一级节点的分类（界面提示用） */
  missing_roots: string[]
}

/** 型号匹配候选（来自 services/device_match.py 的 MatchCandidate.to_dict()）。 */
export interface MatchCandidate {
  device_id: string
  code: string
  name: string
  brand: string | null
  model: string | null
  unit: string
  reference_price: number
  install_unit_price: number
  status: DeviceStatus
  score: number
  score_detail: { name: number; model: number; brand: number }
  /** 仅 /replacements 会带：configured 或 similarity。 */
  source?: 'configured' | 'similarity'
  is_discontinued?: boolean
  replacement_id?: string | null
  price_delta?: number
}

export interface MatchResult {
  query: Record<string, string | null>
  match_type: 'exact' | 'fuzzy' | 'none'
  threshold: number
  best: MatchCandidate | null
  candidates: MatchCandidate[]
  discontinued_count: number
}

export interface ReplacementSuggestions {
  device: Device
  source: 'configured' | 'similarity'
  candidates: MatchCandidate[]
}

// ----- 供应商 -----

export interface Supplier {
  id: string
  code: string
  name: string
  contact_name: string | null
  contact_phone: string | null
  contact_email: string | null
  address: string | null
  is_active: boolean
  /** 后端 SupplierOut 一直有这两个时间戳，前端类型此前漏了 */
  created_at?: string | null
  updated_at?: string | null
}

// ----- 报表中心（2026-10-04）-----

export interface ReportOverview {
  quotes: {
    total: number
    amount: number
    pending_count: number
    pending_amount: number
    by_status: { status: string; label: string; count: number; amount: number; pending: boolean }[]
  }
  projects: {
    total: number
    by_status: { status: string; label: string; count: number }[]
  }
  devices: {
    total: number
    by_status: Record<string, number>
    on_sale: number
    near_eol: number
    eol: number
    with_replacement: number
    owner_supplied: number
  }
  discontinued_impact: {
    quote_count: number
    item_count: number
    quotes: { code: string; status: string; status_label: string; item_count: number }[]
  }
  labor_current: {
    labor_class: string
    label: string
    price_per_day: number
    region: string
    effective_date: string | null
  }[]
}

// ----- 点位模板（2026-10-04）-----

export interface PointTemplateItem {
  id: string
  device_id: string | null
  quantity: Decimalish
  is_primary: boolean
  notes: string | null
  sort_order: number
  device_code: string | null
  device_name: string | null
  device_model: string | null
  device_unit: string | null
  device_reference_price: Decimalish | null
  device_status: string | null
}

export interface PointTemplate {
  id: string
  code: string
  name: string
  subsystem_type: string | null
  install_unit_price: Decimalish
  notes: string | null
  is_active: boolean
  sort_order: number
  created_at?: string | null
  updated_at?: string | null
  items: PointTemplateItem[]
}

export interface PointTemplateApplyResult {
  dry_run: boolean
  template_code: string
  template_name: string
  point_count: number
  install_unit_price: number
  primary_device_code: string | null
  primary_fallback: boolean
  created: number
  rows: {
    device_id: string
    device_code: string
    name: string
    brand: string | null
    model: string | null
    unit: string
    quantity: number
    unit_price: number
    install_unit_price: number
    equipment_amount: number
    install_amount: number
    is_primary: boolean
    sort_order: number
    device_status: string
  }[]
  skipped: { sort_order: number; reason: string; notes: string | null }[]
  equipment_total: number
  install_total: number
}

// ----- 报价单 -----

export type QuoteStatus = 'draft' | 'reviewing' | 'submitted' | 'approved' | 'rejected' | 'archived'
export type FeeBaseType = 'device_only' | 'device_aux_install' | 'direct_fee'
export type AuxiliaryBaseType = 'device' | 'device_install' | 'by_quantity'
export type ItemSource = 'library' | 'manual' | 'imported'

export interface Quote {
  id: string
  project_id: string
  code: string
  version: number
  title: string
  status: QuoteStatus
  /** 本单自定义的说明条款 / 有效期（null = 用系统设置里那份） */
  clauses_override?: string[] | null
  valid_days_override?: number | null
  customer_name: string | null
  customer_contact: string | null
  customer_phone: string | null

  tax_method: string
  tax_rate: Decimalish
  fee_base: FeeBaseType
  auxiliary_base: AuxiliaryBaseType
  auxiliary_rate: Decimalish
  design_rate: Decimalish
  warranty_rate: Decimalish
  training_fee: Decimalish
  mgmt_profit_rate: Decimalish
  labor_float_rate: Decimalish
  labor_float_per_class: Record<string, number>

  equipment_total: Decimalish
  install_total: Decimalish
  auxiliary_total: Decimalish
  design_fee: Decimalish
  warranty_fee: Decimalish
  subtotal_before_tax: Decimalish
  mgmt_profit: Decimalish
  tax_amount: Decimalish
  total_with_tax: Decimalish
  quota_total: Decimalish
  quota_total_with_tax: Decimalish
  // 注意：通用报价 DTO 里**没有** cost_total / gross_profit / gross_profit_rate。
  // 这三个是对内敏感字段，需另外调 GET /quotes/{id}/cost（viewer/sales 会 403）。

  notes: string | null
  created_at: string
  updated_at: string
}

export interface QuoteItem {
  id: string
  quote_id: string
  // 定额子目挂在哪台主设备下（2026-10-05 阶段1/2 ✓）：
  // 有值 = 子目行 ✓（导出/打印会缩进显示 ✓）
  parent_item_id?: string | null
  subsystem_id: string | null
  device_id: string | null
  name: string
  brand: string | null
  model: string | null
  /** 设备参数（2026-10-05）：来自设备描述的快照，可在单里改 */
  spec_text?: string | null
  unit: string
  quantity: Decimalish
  unit_price: Decimalish
  equipment_amount: Decimalish
  install_unit_price: Decimalish
  install_amount: Decimalish
  source: ItemSource
  is_owner_supplied: boolean
  is_replaced: boolean
  replaced_by_id: string | null
  sort_order: number
  notes: string | null
  /** 设备状态投影，用于画「已停产」徽章；单行写入类接口可能返回 null。 */
  device_code: string | null
  device_status: DeviceStatus | null

  // ---- 工艺系数（定额口径用） ----
  // 后端会在 QuoteItem schema 上加这三个 boolean 字段；当前 TS 类型先声明，
  // 等后端迁移（TODO(后端核心)）完成后才真正往返。**前端 UI 已经接上**，但
  // 不要在「未联调」之前拿这些字段当业务判据。
  /** 屏蔽线缆：定额单价 × 1.20。 */
  is_shielded?: boolean
  /** 六类线：定额单价 × 1.10。 */
  is_cat5e?: boolean
  /** 吊顶敷设：定额单价 × 1.20。 */
  is_ceiling?: boolean  /** 单位工程量辅材费（元/单位）：按工程量口径下 辅材费=Σ(数量×本列) */
  auxiliary_unit_price?: string | number

}

export interface QuoteDetail extends Quote {
  items: QuoteItem[]
}

export interface ComputeResult {
  equipment_total: Decimalish
  install_total: Decimalish
  auxiliary_total: Decimalish
  design_fee: Decimalish
  warranty_fee: Decimalish
  subtotal_before_tax: Decimalish
  mgmt_profit: Decimalish
  tax_amount: Decimalish
  total_with_tax: Decimalish
  quota_total: Decimalish
  quota_total_with_tax: Decimalish
  /** 仅当当前角色有成本视图权限时才有值，否则为 null。 */
  cost_total: Decimalish | null
  gross_profit: Decimalish | null
  gross_profit_rate: Decimalish | null
  /** 计价降级提示，例如「定额库未加载，定额口径已退化为安装费」。 */
  warnings: string[]
}

export interface QuoteCost {
  quote_id: string
  quote_code: string
  total_with_tax: number
  cost_total: number
  gross_profit: number
  gross_profit_rate: number
  equipment_total: number
  install_total: number
  auxiliary_total: number
}

export interface SubsystemSummaryRow {
  subsystem_id: string
  subsystem_type: string
  name: string
  point_count: number
  equipment_subtotal: number
  install_subtotal: number
  amount: number
  share: number
}

export interface SubsystemSummary {
  quote_id: string
  quote_code: string
  subsystems: SubsystemSummaryRow[]
  grand_total: number
}

/** 利润看板（2026-10-04）：只统计已批准报价，每项目取最新一版 */
export interface ProfitProjectRow {
  project_code: string
  project_name: string
  customer_name: string
  quote_code: string
  total_with_tax: number
  cost_total: number
  gross_profit: number
  gross_profit_rate: number
  approved_at: string | null
}

export interface ProfitTrendRow {
  month: string
  quotes: number
  amount: number
  cost: number
  profit: number
  rate: number
}

export interface ProfitDashboard {
  summary: { projects: number; amount: number; cost: number; profit: number; rate: number }
  by_project: ProfitProjectRow[]
  trend: ProfitTrendRow[]
  months: number
}

/** 站内通知（2026-10-04） */
export interface AppNotification {
  id: string
  kind: string
  title: string
  body: string | null
  link: string | null
  is_read: boolean
  created_at: string | null
}

/** 分级审批链状态（2026-10-04） */
export interface ApprovalRecord {
  level: number
  decision: string
  comment: string | null
  approver_name: string | null
  approver_role: string | null
  required_levels: number
  created_at: string | null
}

export interface ApprovalChain {
  enabled: boolean
  required_levels: number
  approved_levels: number
  can_finalize: boolean
  thresholds: { min_amount: number; levels: number }[]
  records: ApprovalRecord[]
}

/** 报价单版本快照（2026-10-04）：提交/批准/手动存档各落一版 */
export interface QuoteRevision {
  id: string
  seq: number
  version: number
  reason: string
  status: string
  title: string
  totals: Record<string, string>
  note: string | null
  created_by_name: string | null
  created_at: string | null
  item_count: number
}

/** 两版之间的差异 */
export interface RevisionDiff {
  header: { field: string; from: string; to: string }[]
  items_added: { name: string; model: string | null; quantity: string; unit_price: string }[]
  items_removed: { name: string; model: string | null; quantity: string; unit_price: string }[]
  items_changed: {
    name: string
    model: string | null
    fields: { field: string; from: string; to: string }[]
  }[]
  totals: { field: string; from: string; to: string; delta: string }[]
  has_changes: boolean
  from: { seq: number; reason: string; created_at: string | null; created_by_name: string | null }
  to: { seq: number; reason: string; created_at: string | null; created_by_name: string | null }
}

/** 设备阶梯价（2026-10-04）：按数量分档 */
export interface PriceTier {
  id: string
  min_qty: string
  max_qty: string | null
  unit_price: string
  note: string | null
}

/** 写入用的档位（没有 id） */
export interface PriceTierInput {
  min_qty: string
  max_qty: string | null
  unit_price: string
  note?: string | null
}

/** 客户档案（甲方主数据） */
export interface Customer {
  id: string
  code: string
  name: string
  short_name: string | null
  contact_name: string | null
  contact_phone: string | null
  contact_email: string | null
  address: string | null
  tax_no: string | null
  bank_name: string | null
  bank_account: string | null
  industry: string | null
  note: string | null
  is_active: boolean
}

/** 导入批次留档（原型 #IM-xxx）：每次导入的文件、行数与成败 */
export interface ImportBatch {
  id: string
  batch_no: string
  kind: 'device' | 'inquiry'
  filename: string | null
  file_size: number
  total_rows: number
  created_rows: number
  updated_rows: number
  skipped_rows: number
  error_rows: number
  status: 'success' | 'partial' | 'failed'
  summary: Record<string, unknown>
  created_by_name: string | null
  created_at: string | null
}

export interface ImportBatchError {
  row: number
  message: string
}

export interface ImportBatchDetail extends ImportBatch {
  errors: ImportBatchError[]
}

/** 登录尝试留痕（H-10）：谁、从哪个 IP、成功还是失败 */
export interface LoginLogEntry {
  username: string
  success: boolean
  reason: string | null
  ip: string | null
  user_agent: string | null
  created_at: string | null
}

export interface AuditLog {
  id: string
  user_id: string | null
  action: string
  payload: Record<string, unknown>
  note: string | null
  /** 留痕来源（H-10）：IP / User-Agent / 单号快照 */
  ip?: string | null
  user_agent?: string | null
  subject_code?: string | null
  created_at: string | null
}

// ----- 人工信息价 / 定额 -----

export interface LaborRate {
  id: string
  region: string
  labor_class: 'class_a' | 'class_b' | 'class_c'
  effective_date: string
  price_per_day: Decimalish
  source: string
  is_current: boolean
}

export interface CurrentLaborRates {
  region: string
  effective_date: string
  rates: Record<string, { price_per_day: number; source: string }>
  stale_warning: string | null
}

export interface AdjustSimulation {
  labor_days: Record<string, number>
  old_rates: Record<string, number>
  new_rates: Record<string, number>
  old_total: number
  new_total: number
  delta: number
  delta_pct: number
  rate_source: string
}

export interface QuotaItem {
  id: string
  code: string
  category: string
  name: string
  unit: string
  labor_a_day: Decimalish
  labor_b_day: Decimalish
  labor_c_day: Decimalish
  base_price: Decimalish
  shielded_coef: Decimalish
  cat5e_coef: Decimalish
  ceiling_coef: Decimalish
  notes: string | null
  is_active: boolean
}

// ----- Excel 导入 -----

export type ImportLevel = 'ok' | 'warn' | 'error'
export type DuplicatePolicy = 'skip' | 'overwrite' | 'new'

export interface DeviceImportPreviewRow {
  row: number
  level: ImportLevel
  /** 后端只回 existing_code / level，不回 code / action（以下两个是派生用的可选字段）。 */
  action?: string
  code?: string | null
  name: string | null
  brand: string | null
  model: string | null
  category: DeviceCategory | null
  unit: string | null
  reference_price: Decimalish | null
  existing_code?: string | null
  status_in_db?: DeviceStatus | null
  issues: string[]
}

export interface DeviceImportPreview {
  preview: DeviceImportPreviewRow[]
  stat: Record<string, number>
  sheet_rows: number
  /** 后端现在会回 unmapped（来自 excel_io.describe_headers）。 */
  unmapped: string[]
  file_name?: string | null
  preview_count?: number
  duplicate_policy?: DuplicatePolicy
  /**
   * 字段映射（原型第 6 页左侧那栏）：Excel 列 → 系统字段 → 示例值。
   * 后端 2026-10-03 起把 excel_io.detect_columns 的结果回上来（此前该函数无人调用）。
   */
  mapping?: { excel_index: number; excel_header: string; system_field: string; sample: string }[]
  /** 表头所在行（1 基） */
  header_row?: number
}

export interface DeviceImportResult {
  sheet_rows: number
  created: number
  updated: number
  skipped: number
  errors: number
  created_codes?: string[]
}

/**
 * 场景二逐行结果 = device_match.match_device() 的返回 + 后端补充的 row/quantity/level/issues。
 * 注意 `quantity` 在**顶层**（不在 query 里）—— match_device 的 query 只是查询条件的回显。
 */
export interface InquiryPreviewRow {
  row: number
  query: { name: string; model: string | null; brand: string | null }
  quantity: number
  match_type: 'exact' | 'fuzzy' | 'none'
  best: MatchCandidate | null
  candidates: MatchCandidate[]
  discontinued_count?: number
  raw_name?: string
  raw_model?: string | null
  /** 甲方清单里的单位（原型第 7 页左栏「原文」表有这一列） */
  unit?: string | null
  level: ImportLevel
  issues: string[]
}

export interface InquiryPreview {
  results: InquiryPreviewRow[]
  stat: Record<string, number>
  /** 数据行总数（**不是**表头行号 —— 旧代码把它标成了"表头行数"） */
  sheet_rows: number
  /** 同上，语义化的名字；判断有没有丢行用下面这三个字段 */
  total_rows: number
  previewed_rows: number
  /** true = 只匹配了前 N 行，剩下的**没有**返回 → 禁止提交，必须拆分文件 */
  truncated: boolean
  threshold: number
  file_name?: string | null
}

export interface InquiryCommitLine {
  row: number
  device_id?: string | null
  name: string
  model?: string | null
  brand?: string | null
  unit?: string | null
  quantity?: Decimalish
  unit_price?: Decimalish | null
  subsystem_id?: string | null
}

export interface InquiryCommitResult {
  added: number
  pending_inquiry: number
  auto_replaced: number
  total_with_tax: number
}

/** /imports/*-template 返回的是**表头字符串数组**（excel_io.DEVICE_TEMPLATE_COLUMNS），
 *  不是 {field, header} 对象数组 —— 以前按对象读 c.header，下载下来的模板表头全是 undefined。 */
export interface TemplateInfo {
  columns: string[]
  sheet_name?: string
}

// ----- 元数据 -----

export interface EnumOption {
  value: string
  label: string
}

export interface MetaEnums {
  project_type: EnumOption[]
  project_status: EnumOption[]
  subsystem_type: EnumOption[]
  device_category: EnumOption[]
  device_status: EnumOption[]
  quote_status: EnumOption[]
  fee_base: EnumOption[]
  auxiliary_base: EnumOption[]
  item_source: EnumOption[]
  labor_class: EnumOption[]
  quota_category: EnumOption[]
  role: EnumOption[]
}

export type MetaLabels = Record<string, Record<string, string>>












// ---------------------------------------------------------------------------
// AI 助理（2026-10-04）
// ---------------------------------------------------------------------------

export interface AiConfig {
  enabled: boolean
  provider: string
  base_url: string
  model: string
  vision_model: string
  temperature: number
  timeout_seconds: number
  redact_customer: boolean
  /** 是否已配置 Key（明文/密文都不会下发到前端） */
  api_key_set: boolean
  /** 掩码，如 sk-****cdef */
  api_key_hint: string
  last_test_ok: boolean | null
  last_test_at: string | null
  last_test_error: string | null
}

export interface AiUsageItem {
  id: string
  username: string | null
  task: string
  model: string | null
  total_tokens: number
  duration_ms: number
  ok: boolean
  error: string | null
  created_at: string | null
}

export interface AiUsage {
  totals: { calls: number; total_tokens: number; duration_ms: number; failed: number }
  items: AiUsageItem[]
}

/** 一行候选（AI/规则解析结果，**未经确认不入库**） */
export interface AiParseItem {
  row: number | null
  name: string | null
  brand: string | null
  model: string | null
  spec: string | null
  unit: string | null
  quantity: string | number | null
  unit_price: string | number | null
  /** 对外价（京东控价/零售价）：与进价分开，价目表类文件会带出来 */
  reference_price?: string | number | null
  amount: string | number | null
  remark: string | null
  source: string
  /** 逐字回查结果：型号/单价是否真的出现在原文里 */
  verified: Record<string, boolean>
  needs_review: boolean
  warnings: string[]
  match: {
    device_id: string | null
    code: string | null
    name: string | null
    model: string | null
    unit: string | null
    reference_price: number | null
    cost_price: number | null
    last_inquiry_price: number | null
    supplier_id: string | null
    score: number | null
    match_type: string
    needs_review: boolean
  } | null
  match_alternatives: {
    device_id: string | null
    code: string | null
    name: string | null
    model: string | null
    score: number | null
  }[]
  price_vs_reference?: { reference_price: number; delta: number; delta_rate: number | null }
}

export interface AiParseResult {
  job_id: string
  /** pending = 后台解析中（前端轮询取结果）；ready / failed 为终态 */
  status?: string
  engine: string
  items: AiParseItem[]
  warnings: string[]
  supplier: { name?: string | null; contact?: string | null }
  notes: string[]
  filename: string
  extract_engine: string
  preview: string
}

export interface AiParseJobRow {
  id: string
  status: string
  filename: string | null
  engine: string | null
  item_count: number
  commit_info: Record<string, unknown>
  created_by_name: string | null
  created_at: string | null
}

// ---------------------------------------------------------------------------
// 配件 / 授权（2026-10-05）
// ---------------------------------------------------------------------------
export interface AccessoryOption {
  accessory_id: string
  code: string
  name: string
  brand?: string | null
  model?: string | null
  unit: string
  reference_price?: string | number | null
  is_default: boolean
  note?: string | null
  sort_order?: number
}

/** 配件槽：例如防火墙的「授权」；槽里的 options 是不同档的候选 */
export interface AccessoryGroup {
  id?: string
  label: string
  is_required: boolean
  select_mode: 'one' | 'many'
  quantity_mode: 'per_unit' | 'fixed'
  default_quantity: number | string
  note?: string | null
  sort_order?: number
  is_active?: boolean
  options: AccessoryOption[]
}

/** 配单检查里的一条"还没配"提示 */
export interface MissingAccessory {
  item_id: string
  item_name: string
  device_id?: string | null
  device_code?: string | null
  quantity: string | number
  group_label: string
  is_required: boolean
  select_mode: 'one' | 'many'
  quantity_mode: 'per_unit' | 'fixed'
  default_quantity: string | number
  chosen: string[]
  options: AccessoryOption[]
}

export interface AccessoryCheck {
  quote_id: string
  missing: MissingAccessory[]
  summary: string
}
