/** 全部后端端点封装。页面只 import 这里，不直接碰 axios。 */
import { filenameFromDisposition } from '@/utils/download'
import { TOKEN_KEY, http } from './client'
import type {
  AdjustSimulation,
  AuditLogEntry,
  AuditLog,
  BulkPriceResult,
  ComputeResult,
  CurrentLaborRates,
  Device,
  DeviceCategoryNode,
  DeviceCategoryTree,
  Department,
  DepartmentTree,
  DeviceImportPreview,
  DeviceImportResult,
  AiConfig,
  AiParseJobRow,
  AiParseResult,
  AiUsage,
  AppNotification,
  ApprovalChain,
  ProfitDashboard,
  QuoteRevision,
  RevisionDiff,
  PriceTier,
  PriceTierInput,
  DeviceStats,
  DuplicatePolicy,
  InquiryCommitLine,
  InquiryCommitResult,
  InquiryPreview,
  LaborRate,
  Customer,
  ImportBatch,
  ImportBatchDetail,
  LoginLogEntry,
  MatchResult,
  MetaEnums,
  MetaLabels,
  Page,
  ReportOverview,
  PointTemplate,
  PointTemplateApplyResult,
  PermissionsResponse,
  Project,
  Quote,
  QuoteCost,
  QuoteDetail,
  QuoteItem,
  QuotaItem,
  ReplacementSuggestions,
  Subsystem,
  SubsystemSummary,
  Supplier,
  TemplateInfo,
  TokenResponse,
  User,
  AccessoryCheck,
  AccessoryGroup,
} from './types'

// ---------------------------------------------------------------------------
// 认证 / 用户
// ---------------------------------------------------------------------------

export const authApi = {
  /** 改自己的口令（2026-10-05 ✓）：成功后会清掉"必须改口令"标记 ✓ */
  async changePassword(oldPassword: string, newPassword: string): Promise<void> {
    await http.post('/auth/change-password', {
      old_password: oldPassword,
      new_password: newPassword,
    })
  },

  /** 登出：让后端记一条会话留痕（拿不到令牌也无所谓，本地照常清态） */
  async logout(): Promise<void> {
    try {
      await http.post('/auth/logout')
    } catch {
      // 留痕失败不该挡住用户登出
    }
  },

  /** 登录尝试留痕（仅管理员；H-10/H-8 的审计入口） */
  async loginLogs(params: {
    username?: string
    only_failed?: boolean
    limit?: number
  } = {}): Promise<LoginLogEntry[]> {
    const { data } = await http.get<LoginLogEntry[]>('/auth/login-logs', { params })
    return data
  },

  /** 用当前令牌换一张新令牌（H-8 令牌刷新；client.ts 的 401 拦截器也会用它） */
  async refresh(): Promise<string | null> {
    try {
      const { data } = await http.post<{ access_token: string }>('/auth/refresh')
      return data.access_token
    } catch {
      return null
    }
  },
  async login(username: string, password: string): Promise<TokenResponse> {
    const { data } = await http.post<TokenResponse>('/auth/login/json', { username, password })
    return data
  },
  async me(): Promise<User> {
    const { data } = await http.get<User>('/auth/me')
    return data
  },
  /**
   * 后端把 «资源 -> 允许的动作» 直接算好返回，前端据此隐藏/禁用按钮。
   * 这样权限规则只有 app/core/permissions.py 一个事实源。
   */
  async permissions(): Promise<PermissionsResponse> {
    const { data } = await http.get<PermissionsResponse>('/auth/me/permissions')
    return data
  },
  /** 注意：/users 是分页接口，返回 Page[User] 而不是裸数组。 */
  async listUsers(
    params: {
      page?: number
      page_size?: number
      keyword?: string
      /** 组织架构节点：按部门筛选时后端默认包含下级部门的人 */
      department_id?: string
      include_descendants?: boolean
      unassigned?: boolean
    } = {},
  ): Promise<Page<User>> {
    const { data } = await http.get<Page<User>>('/users', { params })
    return data
  },
  /** 组织架构树（含每个部门的人数）。用户管理页左侧那棵树就是它。 */
  async departmentTree(): Promise<DepartmentTree> {
    const { data } = await http.get<DepartmentTree>('/users/departments/tree')
    return data
  },
  async createDepartment(payload: {
    code: string
    name: string
    parent_id?: string | null
    sort_order?: number
  }): Promise<Department> {
    const { data } = await http.post<Department>('/users/departments', payload)
    return data
  },
  async updateDepartment(id: string, payload: Record<string, unknown>): Promise<Department> {
    const { data } = await http.patch<Department>(`/users/departments/${id}`, payload)
    return data
  },
  async removeDepartment(id: string): Promise<void> {
    await http.delete(`/users/departments/${id}`)
  },
  async createUser(payload: {
    username: string
    email: string
    password: string
    full_name?: string | null
    phone?: string | null
    department?: string | null
    department_id?: string | null
    role?: string
    status?: string
  }): Promise<User> {
    const { data } = await http.post<User>('/users', payload)
    return data
  },
  async updateUser(id: string, payload: Record<string, unknown>): Promise<User> {
    const { data } = await http.patch<User>(`/users/${id}`, payload)
    return data
  },
  /** 可选角色清单（内置 + 自定义 ✓）。
   *
   * 2026-10-05 修「用户管理页角色下拉选不了」时接上 ✓：
   *  下拉原先取自 meta.options('role') ✗ —— 那是静态枚举，认不出**自定义角色** ✓，
   *  缺分组时还会整个空掉 ✓（表现为"下拉打开了却没有可选项" ✗）。
   *  后端这个接口本来就为下拉准备 ✓（返回 key ✓ 中文 name ✓ builtin ✓）。
   */
  async roleOptions(): Promise<Array<{ key: string; name: string; builtin: boolean }>> {
    const { data } = await http.get<Array<{ key: string; name: string; builtin: boolean }>>(
      '/users/roles',
    )
    return data
  },

  /** 后端 DELETE /users/{id} 是软删除（置为停用），不是物理删除。 */
  async deleteUser(id: string): Promise<void> {
    await http.delete(`/users/${id}`)
  },
}

// ---------------------------------------------------------------------------
// 项目 / 子系统
// ---------------------------------------------------------------------------

export interface ProjectQuery {
  keyword?: string
  status?: string
  project_type?: string
  page?: number
  page_size?: number
}

export const projectApi = {
  async list(params: ProjectQuery = {}): Promise<Page<Project>> {
    const { data } = await http.get<Page<Project>>('/projects', { params })
    return data
  },
  async get(id: string): Promise<Project> {
    const { data } = await http.get<Project>(`/projects/${id}`)
    return data
  },
  async create(payload: Record<string, unknown>): Promise<Project> {
    const { data } = await http.post<Project>('/projects', payload)
    return data
  },
  /** 生成下一个项目编号（与设备库 nextCode 对齐）。 */
  async nextCode(): Promise<string> {
    const { data } = await http.post<{ code: string }>('/projects/auto-code')
    return data.code
  },
  async update(id: string, payload: Record<string, unknown>): Promise<Project> {
    const { data } = await http.patch<Project>(`/projects/${id}`, payload)
    return data
  },
  async remove(id: string): Promise<void> {
    await http.delete(`/projects/${id}`)
  },
  /**
   * 状态机全表：{当前状态: [可改成的状态…]}。
   * 下拉选项一律来自后端 —— 前端抄一份流转表迟早漂移成"下拉里有、点下去 409"。
   */
  async statusOptions(): Promise<Record<string, string[]>> {
    const { data } = await http.get<Record<string, string[]>>('/projects/status-options')
    return data
  },
  /** 项目操作留痕（谁能看审计谁能看：管理员 · 财务）。 */
  async auditLogs(id: string, limit = 50): Promise<AuditLogEntry[]> {
    const { data } = await http.get<AuditLogEntry[]>(`/projects/${id}/audit-logs`, {
      params: { limit },
    })
    return data
  },
  async subsystems(projectId: string): Promise<Subsystem[]> {
    const { data } = await http.get<Subsystem[]>(`/projects/${projectId}/subsystems`)
    return data
  },
  async createSubsystem(payload: {
    project_id: string
    subsystem_type: string
    custom_name?: string | null
    notes?: string | null
    sort_order?: number
    /** 供货商：可选（UUID 字符串）。校验不通过后端会回 400。 */
    supplier_id?: string | null
  }): Promise<Subsystem> {
    const { data } = await http.post<Subsystem>('/subsystems', payload)
    return data
  },
  async updateSubsystem(id: string, payload: Record<string, unknown>): Promise<Subsystem> {
    const { data } = await http.patch<Subsystem>(`/subsystems/${id}`, payload)
    return data
  },
  async removeSubsystem(id: string): Promise<void> {
    await http.delete(`/subsystems/${id}`)
  },
}

// ---------------------------------------------------------------------------
// 设备库
// ---------------------------------------------------------------------------

export interface DeviceQuery {
  keyword?: string
  /** 一级分类（历史参数，兼容保留） */
  category?: string
  /** 分类树节点：按节点筛选时后端**默认包含整棵子树** */
  category_id?: string
  include_descendants?: boolean
  uncategorized?: boolean
  status?: string
  brand?: string
  only_common?: boolean
  /** 只看甲供设备（原型第 5 页「仅看甲供设备」） */
  is_owner_supplied?: boolean
  /** 只看已配替代型号的（原型第 5 页「仅看停产有替代」） */
  has_replacement?: boolean
  page?: number
  page_size?: number
}

export const deviceApi = {
  /** 读设备的配件槽与候选（含参考价）—— 2026-10-05 */
  async accessories(id: string): Promise<AccessoryGroup[]> {
    const { data } = await http.get<AccessoryGroup[]>(`/devices/${id}/accessories`)
    return data
  },

  /** 整体替换设备的配件槽（界面一个「保存配件」按钮） */
  async saveAccessories(id: string, groups: AccessoryGroup[]): Promise<AccessoryGroup[]> {
    const { data } = await http.put<AccessoryGroup[]>(`/devices/${id}/accessories`, {
      groups,
    })
    return data
  },

  /** 设备的阶梯价（2026-10-04） */
  async priceTiers(id: string): Promise<PriceTier[]> {
    const { data } = await http.get<PriceTier[]>(`/devices/${id}/price-tiers`)
    return data
  },

  /** 整体替换阶梯价（档位是一组有顺序的数据，逐条改容易重叠） */
  async replacePriceTiers(id: string, tiers: PriceTierInput[]): Promise<PriceTier[]> {
    const { data } = await http.put<PriceTier[]>(`/devices/${id}/price-tiers`, tiers)
    return data
  },

  async list(params: DeviceQuery = {}): Promise<Page<Device>> {
    const { data } = await http.get<Page<Device>>('/devices', { params })
    return data
  },
  async get(id: string): Promise<Device> {
    const { data } = await http.get<Device>(`/devices/${id}`)
    return data
  },
  /**
   * 批量调价（原型第 5 页工具栏）。`dry_run=true` 只算不改 —— 价格是钱，先看再改。
   */
  async bulkPrice(body: {
    device_ids: string[]
    field?: 'reference_price' | 'cost_price' | 'install_unit_price'
    mode?: 'percent' | 'amount'
    value: number | string
    round_yuan?: boolean
    dry_run?: boolean
  }): Promise<BulkPriceResult> {
    const { data } = await http.post<BulkPriceResult>('/devices/bulk-price', body)
    return data
  },
  /** 报价行 → 设备库（原型「复制到系统」）。已关联或同型号已存在的行会被跳过。 */
  async fromQuoteItems(
    itemIds: string[],
    categoryId?: string | null,
  ): Promise<{
    created: { item_id: string; device_id: string; code: string; name: string; reference_price: number }[]
    skipped: { item_id: string; name: string; reason: string }[]
    missing: string[]
    created_count: number
    skipped_count: number
  }> {
    const { data } = await http.post('/devices/from-quote-items', {
      item_ids: itemIds,
      category_id: categoryId ?? undefined,
    })
    return data
  },
  /** 导出整份设备库（xlsx，列与导入模板一致 —— 导出的文件能直接再导回去）。 */
  async exportXlsx(): Promise<{ blob: Blob; filename: string }> {
    const resp = await http.get('/devices/export.xlsx', { responseType: 'blob' })
    return {
      blob: resp.data as Blob,
      filename: filenameFromDisposition(
        resp.headers['content-disposition'] as string | undefined,
        '设备库.xlsx',
      ),
    }
  },
  async stats(): Promise<DeviceStats> {
    const { data } = await http.get<DeviceStats>('/devices/stats')
    return data
  },
  /** 分类树（含每级设备数）。设备库页面左侧的树就是它。 */
  async categoryTree(): Promise<DeviceCategoryTree> {
    const { data } = await http.get<DeviceCategoryTree>('/devices/category-tree')
    return data
  },
  async createCategory(payload: Record<string, unknown>): Promise<DeviceCategoryNode> {
    const { data } = await http.post<DeviceCategoryNode>('/devices/categories', payload)
    return data
  },
  async updateCategory(id: string, payload: Record<string, unknown>): Promise<DeviceCategoryNode> {
    const { data } = await http.patch<DeviceCategoryNode>(`/devices/categories/${id}`, payload)
    return data
  },
  async removeCategory(id: string): Promise<void> {
    await http.delete(`/devices/categories/${id}`)
  },
  async create(payload: Record<string, unknown>): Promise<Device> {
    const { data } = await http.post<Device>('/devices', payload)
    return data
  },
  async update(id: string, payload: Record<string, unknown>): Promise<Device> {
    const { data } = await http.patch<Device>(`/devices/${id}`, payload)
    return data
  },
  async remove(id: string): Promise<void> {
    await http.delete(`/devices/${id}`)
  },
  /** 下一个设备编号；带 category_id 时用分类节点的前缀。 */
  async nextCode(category: string, categoryId?: string | null): Promise<string> {
    const params: Record<string, string> = { category }
    if (categoryId) params.category_id = categoryId
    const { data } = await http.post<{ code: string }>('/devices/auto-code', null, {
      params,
    })
    return data.code
  },
  /** 场景二核心：型号精确/模糊匹配。 */
  async match(params: {
    name: string
    model?: string | null
    brand?: string | null
    threshold?: number
    limit?: number
  }): Promise<MatchResult> {
    const { data } = await http.post<MatchResult>('/devices/match', null, { params })
    return data
  },
  async replacements(id: string, limit = 5): Promise<ReplacementSuggestions> {
    const { data } = await http.get<ReplacementSuggestions>(`/devices/${id}/replacements`, {
      params: { limit },
    })
    return data
  },
}

// ---------------------------------------------------------------------------
// 报价单
// ---------------------------------------------------------------------------

export interface QuoteQuery {
  project_id?: string
  status?: string
  keyword?: string
  page?: number
  page_size?: number
}

export const quoteApi = {
  /** 配单检查：哪些必配配件还没选（2026-10-05） */
  async accessoryCheck(quoteId: string): Promise<AccessoryCheck> {
    const { data } = await http.get<AccessoryCheck>(
      `/quotes/${quoteId}/accessory-check`,
    )
    return data
  },

  /** 审批链状态（2026-10-04） */
  async approvals(quoteId: string): Promise<ApprovalChain> {
    const { data } = await http.get<ApprovalChain>(`/quotes/${quoteId}/approvals`)
    return data
  },

  /** 审批一次：通过/驳回（驳回需要意见） */
  async decideApproval(
    quoteId: string,
    decision: 'approved' | 'rejected',
    comment?: string,
  ): Promise<{ level: number; status: string; finalized: boolean }> {
    const { data } = await http.post<{ level: number; status: string; finalized: boolean }>(
      `/quotes/${quoteId}/approvals`,
      { decision, comment },
    )
    return data
  },

  /** 版本历史（2026-10-04）：提交/批准/手动存档 */
  async revisions(quoteId: string): Promise<QuoteRevision[]> {
    const { data } = await http.get<QuoteRevision[]>(`/quotes/${quoteId}/revisions`)
    return data
  },

  /** 手动存一版（谈判过程打点） */
  async saveRevision(
    quoteId: string,
    note?: string,
  ): Promise<{ seq: number; created_at: string | null }> {
    const { data } = await http.post<{ seq: number; created_at: string | null }>(
      `/quotes/${quoteId}/revisions`,
      {},
      { params: { note } },
    )
    return data
  },

  /** 两版差异 */
  async diffRevisions(quoteId: string, fromSeq: number, toSeq: number): Promise<RevisionDiff> {
    const { data } = await http.get<RevisionDiff>(`/quotes/${quoteId}/revisions/diff`, {
      params: { from_seq: fromSeq, to_seq: toSeq },
    })
    return data
  },

  async list(params: QuoteQuery = {}): Promise<Page<Quote>> {
    const { data } = await http.get<Page<Quote>>('/quotes', { params })
    return data
  },
  async get(id: string): Promise<QuoteDetail> {
    const { data } = await http.get<QuoteDetail>(`/quotes/${id}`)
    return data
  },
  async create(payload: Record<string, unknown>): Promise<Quote> {
    const { data } = await http.post<Quote>('/quotes', payload)
    return data
  },
  /**
   * 保存报价单（2026-10-05 用户反馈改造）。
   *
   * 以前：只要别人碰过这张单，保存一律 409 → 只能手动刷新页面重来 ✗
   *   （哪怕两人改的是完全不同的字段）。
   * 现在：
   *   ① 双方改的字段**不重叠** → 自动带上最新版本重试一次 ✓（用户无感）
   *   ② 字段**重叠** → 抛错并挂上 conflict 详情 ✓，界面可据此让用户选择保留哪一份
   *      （错误文案也由后端带上"谁在什么时候改了哪些"，不再只有一句"请刷新"）
   * 判定字段名的一致性：前端 PATCH 的键 = 后端审计 payload 的键（都是字段名）。
   */
  async update(id: string, payload: Record<string, unknown>): Promise<Quote> {
    const send = (body: Record<string, unknown>) => http.patch<Quote>(`/quotes/${id}`, body)
    try {
      const { data } = await send(payload)
      return data
    } catch (e) {
      const ax = e as { response?: { status?: number; data?: { detail?: unknown } } }
      const det = ax.response?.data?.detail as
        | { error?: string; current_version?: number; remote_changed?: string[]; message?: string }
        | undefined
      if (ax.response?.status === 409 && det?.error === 'version_conflict') {
        const mine = new Set(Object.keys(payload).filter((k) => k !== 'version'))
        const theirs = new Set(det.remote_changed ?? [])
        const overlap = [...mine].filter((k) => theirs.has(k))
        if (overlap.length === 0 && typeof det.current_version === 'number') {
          const { data } = await send({ ...payload, version: det.current_version })
          return data
        }
        ;(e as { conflict?: unknown }).conflict = { ...det, overlap }
      }
      throw e
    }
  },
  async remove(id: string): Promise<void> {
    await http.delete(`/quotes/${id}`)
  },
  /** 重算；payload 可覆盖费率与人工浮动。 */
  async compute(id: string, payload: Record<string, unknown> = {}): Promise<ComputeResult> {
    const { data } = await http.post<ComputeResult>(`/quotes/${id}/compute`, payload)
    return data
  },
  async items(quoteId: string): Promise<QuoteItem[]> {
    const { data } = await http.get<QuoteItem[]>(`/quotes/${quoteId}/items`)
    return data
  },
  async addItem(quoteId: string, payload: Record<string, unknown>): Promise<QuoteItem> {
    const { data } = await http.post<QuoteItem>(`/quotes/${quoteId}/items`, payload)
    return data
  },
  async updateItem(quoteId: string, itemId: string, payload: Record<string, unknown>): Promise<QuoteItem> {
    const { data } = await http.patch<QuoteItem>(`/quotes/${quoteId}/items/${itemId}`, payload)
    return data
  },
  async removeItem(quoteId: string, itemId: string): Promise<void> {
    await http.delete(`/quotes/${quoteId}/items/${itemId}`)
  },
  /** 停产型号换替代型号（替代型号自身停产会被后端 400）。 */
  async replaceItemDevice(quoteId: string, itemId: string, newDeviceId: string): Promise<QuoteItem> {
    const { data } = await http.post<QuoteItem>(`/quotes/${quoteId}/items/${itemId}/replace`, null, {
      params: { new_device_id: newDeviceId },
    })
    return data
  },
  async subsystemSummary(quoteId: string): Promise<SubsystemSummary> {
    const { data } = await http.get<SubsystemSummary>(`/quotes/${quoteId}/subsystems`)
    return data
  },
  /** 客户版报价说明条款（与 PDF 导出同一出处，保证"预览 = 导出"）。 */
  async exportClauses(quoteId: string): Promise<{
    clauses: string[]
    /** 原始模板（含 {变量}）：用来说明"这些数字是从哪来的" */
    clause_template: string[]
    variables: Record<string, string | number>
    available_variables: Record<string, string>
    clauses_overridden: boolean
    valid_days_overridden: boolean
    valid_days: number
    company?: {
      name: string
      address: string
      phone: string
      email: string
      letterheads?: string[]
    } | null
  }> {
    const { data } = await http.get<{
      clauses: string[]
      clause_template: string[]
      variables: Record<string, string | number>
      available_variables: Record<string, string>
      clauses_overridden: boolean
      valid_days_overridden: boolean
      valid_days: number
      company?: {
        name: string
        address: string
        phone: string
        email: string
        letterheads?: string[]
      } | null
    }>(
      `/quotes/${quoteId}/export-clauses`,
    )
    return data
  },
  async cost(quoteId: string): Promise<QuoteCost> {
    const { data } = await http.get<QuoteCost>(`/quotes/${quoteId}/cost`)
    return data
  },
  async auditLogs(quoteId: string, limit = 50): Promise<AuditLog[]> {
    const { data } = await http.get<AuditLog[]>(`/quotes/${quoteId}/audit-logs`, {
      params: { limit },
    })
    return data
  },
  /**
   * 导出报价单（Excel / PDF）。
   *
   * `internal` 为 true 时导出**内部版**（含成本与毛利）——后端会按
   * 成本查看权限校验，无权限时返回 403，这里不要自己吞掉那个错误。
   * 默认 false = 客户版，可以发给甲方。
   */
  async exportFile(
    id: string,
    format: 'xlsx' | 'pdf',
    internal = false,
  ): Promise<{ blob: Blob; filename: string }> {
    const resp = await http.get(`/quotes/${id}/export.${format}`, {
      params: internal ? { internal: true } : {},
      responseType: 'blob',
    })
    const filename = filenameFromDisposition(
      resp.headers['content-disposition'] as string | undefined,
      `报价单.${format}`,
    )
    return { blob: resp.data as Blob, filename }
  },
}

// ---------------------------------------------------------------------------
// 点位模板（2026-10-04）
// ---------------------------------------------------------------------------

export interface PointTemplateItemPayload {
  device_id?: string | null
  quantity?: number | string
  is_primary?: boolean
  notes?: string | null
  sort_order?: number
}

export interface PointTemplatePayload {
  code: string
  name: string
  subsystem_type?: string | null
  install_unit_price?: number | string
  notes?: string | null
  is_active?: boolean
  sort_order?: number
  items: PointTemplateItemPayload[]
}

export const pointTemplateApi = {
  async list(params: {
    keyword?: string
    subsystem_type?: string
    include_inactive?: boolean
    page?: number
    page_size?: number
  } = {}): Promise<Page<PointTemplate>> {
    const { data } = await http.get<Page<PointTemplate>>('/point-templates', { params })
    return data
  },

  async get(id: string): Promise<PointTemplate> {
    const { data } = await http.get<PointTemplate>(`/point-templates/${id}`)
    return data
  },

  async create(body: PointTemplatePayload): Promise<PointTemplate> {
    const { data } = await http.post<PointTemplate>('/point-templates', body)
    return data
  },

  async update(id: string, body: Partial<PointTemplatePayload>): Promise<PointTemplate> {
    const { data } = await http.patch<PointTemplate>(`/point-templates/${id}`, body)
    return data
  },

  async remove(id: string): Promise<void> {
    await http.delete(`/point-templates/${id}`)
  },

  /**
   * 套用到报价单。`dry_run=true` 只算不写（界面先预览再确认）。
   * 安装单价（元/点）只落在模板的主设备行上，其余行安装费为 0。
   */
  async apply(
    id: string,
    body: {
      quote_id: string
      subsystem_id?: string | null
      point_count: number | string
      dry_run?: boolean
    },
  ): Promise<PointTemplateApplyResult> {
    const { data } = await http.post<PointTemplateApplyResult>(
      `/point-templates/${id}/apply`,
      body,
    )
    return data
  },
}

// ---------------------------------------------------------------------------
// 报表中心（2026-10-04）
// ---------------------------------------------------------------------------

export interface AppSettings {
  company: {
    name: string
    address: string
    phone: string
    email: string
    /**
     * 公司抬头库（2026-10-09）：报价单可选用的**其它公司名称**，3~5 个。
     * 这是**候选名单**，不是"当前生效的抬头"；单据上存的是当时选定的字符串本身。
     * 空 = 只有 `name`，行为与加这个功能之前完全一致。
     */
    letterheads?: string[]
  }
  /**
   * 数据可见性（2026-10-05）：按角色配"能否看成本/利润"。
   * 后端 core/visibility 判定，保存后立即生效。
   */
  visibility?: {
    cost_roles: string[]
    profit_roles: string[]
  }
  quote: {
    valid_days: number
    // 条款里用 {变量} 引用的结构化数字（2026-10-05）：改这里一处，
    // 所有报价单的条款文本自动跟着变，不会出现"字段 45 天、条款写 30 天"
    prepay_ratio: number
    accept_ratio: number
    warranty_ratio: number
    warranty_months: number
    build_days: number
    clauses: string[]
  }
  /** 自定义角色（内置角色在 core/roles.py，不在这里） */
  roles?: {
    custom: { key: string; name: string }[]
  }
  /** 条款里可用的 {变量}（后端定义，前端只显示） */
  clause_variables?: Record<string, string>
}

/** 配置档（导出/导入用；不含密钥） */
export interface SettingsDocument {
  schema_version: number
  exported_at?: string
  exported_by?: string
  app_version?: string
  note?: string
  settings: AppSettings
}

/** 导入预演/应用的结果 */
export interface SettingsImportResult {
  dry_run: boolean
  group_count: number
  diff: { group: string; changes: { field: string; from: string; to: string }[] }[]
  note?: string
  settings?: AppSettings
}

export const settingApi = {
  /** 读系统设置（缺项由后端用默认值补齐） */
  async get(): Promise<AppSettings> {
    const { data } = await http.get<AppSettings>('/settings')
    return data
  },
  /** 保存（只覆盖传入的键） */
  async patch(body: Partial<AppSettings>): Promise<AppSettings> {
    const { data } = await http.patch<AppSettings>('/settings', body)
    return data
  },
  /** 配置/权限变更留痕（最近 N 条；不含密钥） */
  async auditLogs(limit = 50): Promise<{
    total: number
    items: {
      id: string
      created_at: string | null
      username: string | null
      action: string
      action_label: string
      summary: string | null
      diff: { group: string; changes: { field: string; from: string; to: string }[] }[]
      ip: string | null
    }[]
  }> {
    const { data } = await http.get('/settings/audit-logs', { params: { limit } })
    return data
  },
  /** 导出配置（换机器/重装时用来还原口径；**不含密钥**） */
  async exportConfig(): Promise<SettingsDocument> {
    const { data } = await http.get<SettingsDocument>('/settings/export')
    return data
  },
  /** 导入配置：默认只预演差异（dry_run=true），确认后才落库 */
  async importConfig(
    document: SettingsDocument,
    dryRun = true,
  ): Promise<SettingsImportResult> {
    const { data } = await http.post<SettingsImportResult>('/settings/import', {
      document,
      dry_run: dryRun,
    })
    return data
  },
}

/** 权限矩阵（2026-10-05）：资源 × 动作 × 角色，可在设置里调 */
export interface PermissionMatrix {
  editable_roles: string[]
  /** 资源 → 该资源支持的动作（决定每行几个勾选框） */
  catalog: Record<string, string[]>
  /** 代码里的默认矩阵（「恢复默认」用） */
  defaults: Record<string, Record<string, string[]>>
  /** 当前实际生效值（默认 + 覆盖） */
  effective: Record<string, Record<string, string[]>>
  overrides: Record<string, Record<string, string[]>>
  admin_note: string
}

export const permissionApi = {
  async matrix(): Promise<PermissionMatrix> {
    const { data } = await http.get<PermissionMatrix>('/settings/permission-matrix')
    return data
  },
}

/** 利润看板（2026-10-04；2026-10-05 起按"利润可见性"判定，可配置） */

export const reportApi = {
  /** 利润看板（2026-10-04，仅 admin/经理/财务） */
  async profitDashboard(months = 6): Promise<ProfitDashboard> {
    const { data } = await http.get<ProfitDashboard>('/reports/profit-dashboard', {
      params: { months },
    })
    return data
  },

  /** 一屏总览：报价/项目状态、设备库健康度、停产影响、人工信息价。 */
  async overview(): Promise<ReportOverview> {
    const { data } = await http.get<ReportOverview>('/reports/overview')
    return data
  },
}

// ---------------------------------------------------------------------------
// 人工信息价 / 定额
// ---------------------------------------------------------------------------

export const laborApi = {
  async list(onlyCurrent = false): Promise<LaborRate[]> {
    const { data } = await http.get<LaborRate[]>('/labor-rates', {
      params: { only_current: onlyCurrent },
    })
    return data
  },
  async current(): Promise<CurrentLaborRates> {
    const { data } = await http.get<CurrentLaborRates>('/labor-rates/current')
    return data
  },
  async create(payload: Record<string, unknown>): Promise<LaborRate> {
    const { data } = await http.post<LaborRate>('/labor-rates', payload)
    return data
  },
  /** Page 4「调价测算」按钮。 */
  async simulate(payload: {
    labor_days: Record<string, number>
    new_rates?: Record<string, number>
  }): Promise<AdjustSimulation> {
    const { data } = await http.post<AdjustSimulation>('/labor-rates/adjust-simulate', payload)
    return data
  },
  async quotaItems(params: { category?: string; keyword?: string } = {}): Promise<QuotaItem[]> {
    const { data } = await http.get<QuotaItem[]>('/quota-items', { params })
    return data
  },
}

// ---------------------------------------------------------------------------
// Excel 批量导入
// ---------------------------------------------------------------------------

/** 上传 multipart 并复用统一错误处理。 */
function formOf(file: File, extra: Record<string, string> = {}): FormData {
  const fd = new FormData()
  fd.append('file', file)
  for (const [k, v] of Object.entries(extra)) fd.append(k, v)
  return fd
}

export const importApi = {
  /** 导入批次列表（原型 #IM-xxx）：谁、什么时候、导了什么、成败多少 */
  async batches(params: { kind?: string; status_filter?: string; limit?: number } = {}) {
    const { data } = await http.get<{ items: ImportBatch[] }>('/imports/batches', { params })
    return data.items
  },

  /** 单批明细（含失败行号与原因） */
  async batchDetail(id: string): Promise<ImportBatchDetail> {
    const { data } = await http.get<ImportBatchDetail>(`/imports/batches/${id}`)
    return data
  },

  /** 下载设备库导入模板（**真 xlsx** —— 填完能直接导回；M-19 修复） */
  async downloadDeviceTemplate(): Promise<{ blob: Blob; filename: string }> {
    const resp = await http.get('/imports/device-template.xlsx', { responseType: 'blob' })
    return {
      blob: resp.data as Blob,
      filename: filenameFromDisposition(
        resp.headers['content-disposition'] as string | undefined,
        '设备库导入模板.xlsx',
      ),
    }
  },

  /** 下载甲方清单导入模板（真 xlsx） */
  async downloadInquiryTemplate(): Promise<{ blob: Blob; filename: string }> {
    const resp = await http.get('/imports/inquiry-template.xlsx', { responseType: 'blob' })
    return {
      blob: resp.data as Blob,
      filename: filenameFromDisposition(
        resp.headers['content-disposition'] as string | undefined,
        '甲方清单导入模板.xlsx',
      ),
    }
  },

  async deviceTemplate(): Promise<TemplateInfo> {
    const { data } = await http.get<TemplateInfo>('/imports/device-template')
    return data
  },
  async inquiryTemplate(): Promise<TemplateInfo> {
    const { data } = await http.get<TemplateInfo>('/imports/inquiry-template')
    return data
  },
  /** 场景一：设备库导入预览（不落库）。 */
  async previewDevices(
    file: File,
    policy: DuplicatePolicy = 'skip',
    maxRows = 100,
  ): Promise<DeviceImportPreview> {
    const { data } = await http.post<DeviceImportPreview>(
      '/imports/devices/preview',
      formOf(file, { duplicate_policy: policy, max_rows: String(maxRows) }),
    )
    return data
  },
  /** 场景一：确认导入。 */
  async commitDevices(
    file: File,
    policy: DuplicatePolicy = 'skip',
    skipErrors = true,
  ): Promise<DeviceImportResult> {
    const { data } = await http.post<DeviceImportResult>(
      '/imports/devices/commit',
      formOf(file, { duplicate_policy: policy, skip_errors: String(skipErrors) }),
    )
    return data
  },
  /**
   * 场景二：甲方询价清单逐行匹配预览。
   *
   * ⚠ 这里**不再传 limit**。以前前端硬编码 `limit = 50`，而后端默认是 100，
   * 于是 200 行的甲方清单只匹配前 50 行、提交时也只写入那 50 行，界面还提示
   * 「已写入 50 行」—— 剩下 150 行静默丢失。上限现在只由后端决定，
   * 前端用响应里的 `truncated` 判断并**阻断提交**。
   */
  async previewInquiry(file: File, threshold = 75): Promise<InquiryPreview> {
    const { data } = await http.post<InquiryPreview>(
      '/imports/inquiry/preview',
      formOf(file, { threshold: String(threshold) }),
    )
    return data
  },
  /** 场景二：把确认的匹配行写入报价单。 */
  async commitInquiry(quoteId: string, lines: InquiryCommitLine[]): Promise<InquiryCommitResult> {
    const fd = new FormData()
    fd.append('quote_id', quoteId)
    fd.append('payload', JSON.stringify(lines))
    const { data } = await http.post<InquiryCommitResult>('/imports/inquiry/commit', fd)
    return data
  },
}

// ---------------------------------------------------------------------------
// 元数据
// ---------------------------------------------------------------------------

export const metaApi = {
  async enums(): Promise<MetaEnums> {
    const { data } = await http.get<MetaEnums>('/meta/enums')
    return data
  },
  async labels(): Promise<MetaLabels> {
    const { data } = await http.get<MetaLabels>('/meta/labels')
    return data
  },
  async health(): Promise<{ status: string; app: string; version: string }> {
    const { data } = await http.get<{ status: string; app: string; version: string }>(
      '/health',
      { baseURL: '/api' },
    )
    return data
  },
}

// ---------------------------------------------------------------------------
// 供货商（supplier）
// ---------------------------------------------------------------------------
//
// 后端 supplier CRUD（2026-09-29 后端 CRUD 小弟交付）：
//   GET    /suppliers               分页 + 关键字搜索 + 默认只看启用中
//   GET    /suppliers/{id}          详情（soft-deleted 也是 200，前端按 is_active 判断）
//   POST   /suppliers               新增；需要 admin/estimator（sales/viewer = 403）
//   PATCH  /suppliers/{id}          改 name/联系人等（code 不允许改）
//   DELETE /suppliers/{id}          软删（204，admin 才能）
//
// 子系统编辑 UI 已经接 `supplier_id` 下拉；报价员或管理员选好后下次再编辑即可改。


export interface SupplierCreate {
  code: string
  name: string
  contact_name?: string | null
  contact_phone?: string | null
  contact_email?: string | null
  address?: string | null
  /** 后端 SupplierCreate 里就有（默认 True）；停用/启用走 PATCH 的 is_active */
  is_active?: boolean
}

export type SupplierUpdate = Partial<SupplierCreate>

export const supplierApi = {
  /**
   * 列表拉一页。关键字同时匹配 code / name / contact_name。
   *
   * 状态参数有两个，别搞混（2026-10-04 修）：
   *   · `is_active=false` → **只看停用的**（不是"含停用"）
   *   · `include_inactive=true` → 在用与停用**都要**（供应商库页的「显示已停用」用这个）
   */
  async list(params: {
    page?: number
    page_size?: number
    keyword?: string
    is_active?: boolean
    include_inactive?: boolean
  } = {}): Promise<Page<Supplier>> {
    const { data } = await http.get<Page<Supplier>>('/suppliers', { params })
    return data
  },

  /** 下拉选择场景的"全部启用中"，不分页，最多 500 条。 */
  async listActive(): Promise<Supplier[]> {
    const first = await supplierApi.list({ page: 1, page_size: 500 })
    return first.items
  },

  async get(id: string): Promise<Supplier> {
    const { data } = await http.get<Supplier>(`/suppliers/${id}`)
    return data
  },

  async create(body: SupplierCreate): Promise<Supplier> {
    const { data } = await http.post<Supplier>('/suppliers', body)
    return data
  },

  async update(id: string, body: SupplierUpdate): Promise<Supplier> {
    const { data } = await http.patch<Supplier>(`/suppliers/${id}`, body)
    return data
  },

  async remove(id: string): Promise<void> {
    await http.delete(`/suppliers/${id}`)
  },
}

















/** 客户档案（2026-10-04）：报价/项目的甲方主数据 */
export const customerApi = {
  async list(params: {
    keyword?: string
    include_inactive?: boolean
    page?: number
    page_size?: number
  } = {}): Promise<Page<Customer>> {
    const { data } = await http.get<Page<Customer>>('/customers', { params })
    return data
  },

  /** 下拉用：只要启用的，取前 N 条 */
  async options(keyword?: string): Promise<Customer[]> {
    const { data } = await http.get<Page<Customer>>('/customers', {
      params: { keyword, page_size: 100 },
    })
    return data.items
  },

  async create(payload: Partial<Customer>): Promise<Customer> {
    const { data } = await http.post<Customer>('/customers', payload)
    return data
  },

  async update(id: string, payload: Partial<Customer>): Promise<Customer> {
    const { data } = await http.patch<Customer>(`/customers/${id}`, payload)
    return data
  },

  /** 停用（后端不物理删除：历史项目还引用着它） */
  async deactivate(id: string): Promise<void> {
    await http.delete(`/customers/${id}`)
  },
}

/** 站内通知（2026-10-04）：待审批提醒、审批结果通知 */
export const notificationApi = {
  async list(unreadOnly = true): Promise<{ unread: number; items: AppNotification[] }> {
    const { data } = await http.get<{ unread: number; items: AppNotification[] }>(
      '/notifications',
      { params: { unread_only: unreadOnly } },
    )
    return data
  },

  async unreadCount(): Promise<number> {
    const { data } = await http.get<{ unread: number }>('/notifications/unread-count')
    return data.unread
  },

  async read(id: string): Promise<void> {
    await http.post(`/notifications/${id}/read`)
  },

  async readAll(): Promise<number> {
    const { data } = await http.post<{ marked: number }>('/notifications/read-all')
    return data.marked
  },
}

/** AI 助理（2026-10-04）：配置只给 admin，解析与确认给一线角色 */
export const aiApi = {
  async config(): Promise<AiConfig> {
    const { data } = await http.get<AiConfig>('/ai/config')
    return data
  },

  async saveConfig(payload: Record<string, unknown>): Promise<AiConfig> {
    const { data } = await http.put<AiConfig>('/ai/config', payload)
    return data
  },

  async testConnection(): Promise<{ ok: boolean; duration_ms: number; model: string }> {
    const { data } = await http.post<{ ok: boolean; duration_ms: number; model: string }>(
      '/ai/test-connection',
    )
    return data
  },

  /** 拉取服务商可用模型列表（填模型名时用） */
  async models(): Promise<{ count: number; models: string[] }> {
    const { data } = await http.get<{ count: number; models: string[] }>('/ai/models')
    return data
  },

  async usage(limit = 50): Promise<AiUsage> {
    const { data } = await http.get<AiUsage>('/ai/usage', { params: { limit } })
    return data
  },

  /** 解析供应商报价（不落库）：文件或粘贴文本二选一 */
  async parseSupplierQuote(input: {
    file?: File
    text?: string
    supplierId?: string
    projectId?: string
    forceAi?: boolean
    /** 解析方式（2026-10-05）：auto=先规则后模型 / rule=只用规则 / llm=强制大模型 */
    engineMode?: 'auto' | 'rule' | 'llm'
  }): Promise<AiParseResult> {
    const form = new FormData()
    if (input.file) form.append('file', input.file)
    if (input.text) form.append('text', input.text)
    if (input.supplierId) form.append('supplier_id', input.supplierId)
    if (input.projectId) form.append('project_id', input.projectId)
    form.append('force_ai', String(input.forceAi ?? false))
    form.append('engine_mode', input.engineMode ?? 'auto')
    const { data } = await http.post<AiParseResult>('/ai/supplier-quote-parse', form, {
      timeout: 180000, // 大模型慢，别用默认超时把请求掐掉
    })
    return data
  },

  async parseJobs(limit = 20): Promise<{ items: AiParseJobRow[] }> {
    const { data } = await http.get<{ items: AiParseJobRow[] }>('/ai/parse-jobs', {
      params: { limit },
    })
    return data
  },

  async parseJob(id: string): Promise<{
    id: string
    /** pending（后台解析中）/ ready / failed / committed */
    status: string
    filename: string | null
    result: AiParseResult | Record<string, never>
    commit_info: Record<string, unknown>
  }> {
    const { data } = await http.get(`/ai/parse-jobs/${id}`)
    return data
  },

  /** 人工确认后落库（更新设备库进价 + #IM 批次） */
  async commitParse(
    jobId: string,
    payload: { rows: Record<string, unknown>[]; supplier_id?: string | null },
  ): Promise<{
    batch_no: string
    updated: number
    skipped: number
    skipped_detail: { row: number | null; name: string; message: string }[]
    changes: string[]
  }> {
    const { data } = await http.post(`/ai/parse-jobs/${jobId}/commit`, payload)
    return data
  },
}

/**
 * 流式对话（SSE）。逐事件回调，事件类型见后端 ai_chat.run_agent_turn：
 *   conversation / user_saved / delta / tool_start / tool_result / done / error
 *
 * 用 fetch 而不是 axios —— 只有 fetch 能拿到增量流。
 */
export async function streamChat(
  payload: {
    message: string
    conversation_id?: string | null
    attachments?: Record<string, unknown>[]
  },
  onEvent: (ev: Record<string, unknown>) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = localStorage.getItem(TOKEN_KEY)
  const resp = await fetch('/api/v1/ai/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(payload),
    signal,
  })

  if (!resp.ok) {
    let detail = `请求失败（HTTP ${resp.status}）`
    try {
      const body = await resp.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      // 后端不是 JSON（例如 nginx 502），保留默认文案
    }
    throw new Error(detail)
  }

  const reader = resp.body?.getReader()
  if (!reader) throw new Error('当前浏览器不支持流式响应')
  const decoder = new TextDecoder()
  let buffer = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE 以空行分隔事件
    let idx = buffer.indexOf('\n\n')
    while (idx !== -1) {
      const chunk = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      for (const line of chunk.split('\n')) {
        if (!line.startsWith('data:')) continue
        const raw = line.slice(5).trim()
        if (!raw) continue
        try {
          onEvent(JSON.parse(raw))
        } catch {
          // 半个事件（理论上不会发生，SSE 按行发）——忽略而不是中断整段对话
        }
      }
      idx = buffer.indexOf('\n\n')
    }
  }
}

/** 对话与消息（回看历史用） */
export const chatApi = {
  async conversations(): Promise<{
    items: { id: string; title: string; message_count: number; last_message_at: string | null }[]
  }> {
    const { data } = await http.get('/ai/conversations')
    return data
  },

  async messages(conversationId: string): Promise<{
    id: string
    title: string
    messages: {
      id: string
      role: string
      content: string | null
      tool_calls: { name?: string; arguments?: Record<string, unknown> }[]
      tool_name: string | null
      attachments: Record<string, unknown>[]
      meta: Record<string, unknown>
      created_at: string | null
    }[]
  }> {
    const { data } = await http.get(`/ai/conversations/${conversationId}`)
    return data
  },

  async remove(conversationId: string): Promise<void> {
    await http.delete(`/ai/conversations/${conversationId}`)
  },
}

/** 恢复出厂设置（2026-10-05）：高危，仅 admin；先备份、可预演 */
export const factoryResetApi = {
  async preview(scope: string): Promise<{
    scope: string
    total: number
    items: { table: string; label: string; rows: number }[]
  }> {
    const { data } = await http.get('/settings/factory-reset/preview', { params: { scope } })
    return data
  },

  async run(payload: {
    scope: string
    confirm_text: string
    password: string
    dry_run?: boolean
  }): Promise<{
    scope: string
    dry_run: boolean
    backup: { filename: string; size_mb: number } | null
    deleted: { table: string; label: string; deleted: number; error?: string }[]
    total_deleted: number
    note: string
  }> {
    const { data } = await http.post('/settings/factory-reset', payload)
    return data
  },
}
