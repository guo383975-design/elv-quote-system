<script setup lang="ts">
/**
 * 系统设置（2026-10-04，原型顶栏导航项）。
 *
 * 只放**会影响输出**的配置 —— 改一次，所有客户版 PDF 与预览的抬头、有效期、条款都变：
 *   · 公司信息（抬头/地址/电话/邮箱）
 *   · 报价有效期天数
 *   · 报价说明条款（可增删改）
 *
 * 写权限只给 admin（后端 `setting:update`）；管理岗/造价能看但不能改 ——
 * 这些值影响面比单张报价单大得多。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { authApi, settingApi , aiApi , factoryResetApi , permissionApi } from '@/api'
import type { Letterhead, PermissionMatrix, SettingsDocument } from '@/api'
import type { AiConfig, AiUsage } from '@/api/types'
import type { LoginLogEntry } from '@/api/types'
import { dateTime } from '@/utils/format'
import { extractErrorMessage } from '@/api/client'
import UserManagementView from '@/views/UserManagementView.vue'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'
import { useRoute, useRouter } from 'vue-router'

const auth = useAuthStore()
const meta = useMetaStore()
const route = useRoute()
const router = useRouter()

/**
 * 用户管理与组织架构已收进本页（2026-10-04）：顶栏不再单列「用户管理」，
 * `/users` 旧链接重定向到 `/settings?tab=users`，页签状态与 URL 双向同步，
 * 直接分享链接也能落在同一个页签上。
 */
const canManageUsers = computed(() => auth.hasResource('user'))
/** 登录日志只有管理员能看（后端也是这么卡的） */
/** AI 配置只给 admin（后端也是 ai.configure = ADMIN） */
const canConfigureAi = computed(() => auth.can('ai', 'configure'))

const canViewLoginLogs = computed(
  () => !!auth.user?.is_superuser || auth.user?.role === 'admin',
)
// ⚠ 页签与 URL 查询参数是**双向同步**的（下面两个 watch）。
// 因此**每加一个页签，必须同时加进 TabKey 与 resolveTab** ——
// 否则点它会立刻被解析回 basic，表现为"这个标签点不了"
// （2026-10-05 加「角色管理与权限矩阵」「配置变更记录」时就踩了）。
type TabKey = 'basic' | 'perms' | 'cfglog' | 'users' | 'logins' | 'ai'
const tab = ref<TabKey>(resolveTab(route.query.tab))

function resolveTab(value: unknown): TabKey {
  if (value === 'users' && auth.hasResource('user')) return 'users'
  if (value === 'logins' && (auth.user?.is_superuser || auth.user?.role === 'admin')) {
    return 'logins'
  }
  if (value === 'ai' && canConfigureAi.value) return 'ai'
  // 权限相关（与页签自身的 v-if="canUpdate" 一致）
  if (value === 'perms' && canUpdate.value) return 'perms'
  if (value === 'cfglog' && canUpdate.value) return 'cfglog'
  return 'basic'
}

watch(tab, (value) => {
  void router.replace({
    name: 'settings',
    query: value === 'basic' ? {} : { tab: value },
  })
  // "切到哪个页签要加载什么"——收拢在这里，加页签时一眼能看到该补哪一行
  if (value === 'logins') void loadLoginLogs()
  if (value === 'ai') {
    void loadAi()
    void loadFrPreview()
  }
  if (value === 'perms' || value === 'basic') {
    void loadPermMatrix()   // 角色清单：可见性卡片也要用
  }
  if (value === 'cfglog' || value === 'basic') void loadAuditLogs()
})

watch(
  () => route.query.tab,
  (value) => {
    const want = resolveTab(value)
    if (want !== tab.value) tab.value = want
  },
)

// ---------- AI 助理（2026-10-04，仅 admin）----------
const aiConfig = ref<AiConfig | null>(null)
const aiLoading = ref(false)
const aiSaving = ref(false)
const aiTesting = ref(false)
const aiKeyInput = ref('')
const aiUsage = ref<AiUsage | null>(null)
const aiForm = reactive({
  enabled: false,
  base_url: '',
  model: '',
  vision_model: '',
  temperature: 0.1,
  timeout_seconds: 120,
  redact_customer: false,
})

function fillAiForm(cfg: AiConfig): void {
  Object.assign(aiForm, {
    enabled: cfg.enabled,
    base_url: cfg.base_url,
    model: cfg.model,
    vision_model: cfg.vision_model,
    temperature: cfg.temperature,
    timeout_seconds: cfg.timeout_seconds,
    redact_customer: cfg.redact_customer,
  })
}

async function loadAi(): Promise<void> {
  if (!canConfigureAi.value) return
  aiLoading.value = true
  try {
    aiConfig.value = await aiApi.config()
    fillAiForm(aiConfig.value)
    aiUsage.value = await aiApi.usage(20)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    aiLoading.value = false
  }
}

async function saveAi(): Promise<void> {
  aiSaving.value = true
  try {
    const payload: Record<string, unknown> = { ...aiForm }
    // 只在真的填了新 key 时才发；留空 = 不修改（表单里显示的是掩码）
    if (aiKeyInput.value.trim()) payload.api_key = aiKeyInput.value.trim()
    aiConfig.value = await aiApi.saveConfig(payload)
    fillAiForm(aiConfig.value)
    aiKeyInput.value = ''
    ElMessage.success('AI 配置已保存')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    aiSaving.value = false
  }
}

async function clearAiKey(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '清空 API Key？清空后 AI 助理将不可用（配置项保留）。',
      '清空密钥',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    aiConfig.value = await aiApi.saveConfig({ api_key: '__CLEAR__' })
    fillAiForm(aiConfig.value)
    ElMessage.success('API Key 已清空')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

async function testAi(): Promise<void> {
  aiTesting.value = true
  try {
    const r = await aiApi.testConnection()
    ElMessage.success(`连接成功：${r.model}，耗时 ${r.duration_ms} ms`)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    aiTesting.value = false
    await loadAi()
  }
}

// 可用模型列表（拉取后可直接选，避免手打错名字）
const aiModels = ref<string[]>([])
const aiModelsLoading = ref(false)

async function fetchAiModels(): Promise<void> {
  aiModelsLoading.value = true
  try {
    const r = await aiApi.models()
    aiModels.value = r.models
    ElMessage.success(`拉到 ${r.count} 个模型，可直接在下面选择`)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    aiModelsLoading.value = false
  }
}

// ---------- 恢复出厂设置（2026-10-05，仅 admin）----------
// 高危操作：三道门（仅 admin / 确认短语 / 当前密码）+ 先自动备份，另支持预演。
const canReset = computed(
  () => auth.can('setting', 'reset') || !!auth.user?.is_superuser,
)
const frScope = ref<'business' | 'business_settings' | 'all'>('business')
const frPreview = ref<{ total: number; items: { table: string; label: string; rows: number }[] } | null>(null)
const frLoading = ref(false)
const frRunning = ref(false)
const frConfirm = ref('')
const frPassword = ref('')
const frResult = ref<{
  dry_run: boolean
  backup: { filename: string; size_mb: number } | null
  deleted: { table: string; label: string; deleted: number; error?: string }[]
  total_deleted: number
  note: string
} | null>(null)

const FR_SCOPES = [
  { value: 'business', label: '只清业务数据', desc: '项目 / 报价 / 设备库 / 供应商 / 客户 / 定额 / 人工费 / 点位模板 / 批次 / 留痕 / 通知 / AI 会话；**保留账号与系统设置**' },
  { value: 'business_settings', label: '业务数据 + 系统设置', desc: '再加公司抬头、费率、条款、审批阈值、AI 配置 —— 相当于"除了账号，全部回到默认"' },
  { value: 'all', label: '全部（含账号）', desc: '再加组织架构与**除当前账号外**的所有用户；相当于刚装好、只剩你这个管理员' },
] as const

async function loadFrPreview(): Promise<void> {
  if (!canReset.value) return
  frLoading.value = true
  try {
    frPreview.value = await factoryResetApi.preview(frScope.value)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    frLoading.value = false
  }
}

async function runFactoryReset(dryRun: boolean): Promise<void> {
  if (!dryRun) {
    try {
      await ElMessageBox.confirm(
        `确认要恢复出厂设置吗？\n\n范围：${FR_SCOPES.find((s) => s.value === frScope.value)?.label}\n\n` +
          '系统会**先自动备份**再清空。清空后要恢复演示数据，需要在服务器上跑 seed 脚本。',
        '最后确认',
        { type: 'warning', confirmButtonText: '执行', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  frRunning.value = true
  try {
    frResult.value = await factoryResetApi.run({
      scope: frScope.value,
      confirm_text: frConfirm.value,
      password: frPassword.value,
      dry_run: dryRun,
    })
    ElMessage.success(dryRun ? '预演完成（没有删除任何数据）' : `已清空 ${frResult.value.total_deleted} 行`)
    await loadFrPreview()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    frRunning.value = false
  }
}

const TASK_LABEL: Record<string, string> = {
  test: '连通性测试',
  parse_supplier_quote: '供应商报价解析',
  analyze_quote: '报价审查',
}

// ----- 登录日志（仅管理员）-----
const loginLogs = ref<LoginLogEntry[]>([])
const loginsLoading = ref(false)
const loginQuery = reactive({ username: '', only_failed: false, limit: 100 })

async function loadLoginLogs(): Promise<void> {
  loginsLoading.value = true
  try {
    loginLogs.value = await authApi.loginLogs({
      username: loginQuery.username || undefined,
      only_failed: loginQuery.only_failed || undefined,
      limit: loginQuery.limit,
    })
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loginsLoading.value = false
  }
}

const failedCount = computed(() => loginLogs.value.filter((r) => !r.success).length)

const loading = ref(false)
const saving = ref(false)
const canUpdate = computed(() => auth.can('setting', 'update'))

const company = reactive({
  name: '',
  address: '',
  phone: '',
  email: '',
  /** 公司抬头库（2026-10-09）：报价单可选用的**其它公司抬头**（名称 + 地址/电话/邮箱，建议 3~5 个）。
   *  这是**候选名单**，不是"当前生效的抬头" —— 单据上存的是当时选定那一项的**值本身**，
   *  所以在这里改/删**不会**追溯改写已开出的报价单 ✓。
   *  ⚠ `name` 必填；其余三项可空 ✓（空的那些在导出/预览时**逐字段**回落上面那套 ✓）。 */
  letterheads: [] as Letterhead[],
})
const quote = reactive({
  valid_days: 30,
  // 条款里用到的数字（2026-10-05 起结构化）：条款文本用 {变量} 引用它们，
  // 改这里一处，所有报价单的条款自动跟着变。
  prepay_ratio: 30,
  accept_ratio: 60,
  warranty_ratio: 10,
  warranty_months: 12,
  build_days: 45,
  clauses: [] as string[],
})
/** 后端定义的可用变量（来自 GET /quotes/{id}/export-clauses 的 available_variables） */
const clauseVars = ref<Record<string, string>>({})

// ---------- 权限矩阵（2026-10-05）----------
// 只调**现有角色**的权限；admin 恒定全权、不可改（防自锁）。
// 保存时只把"与默认值不同"的角色写成覆盖 —— 没动过的角色继续跟随代码默认值，
// 将来改默认值它们能跟着更新。
const RES_LABEL: Record<string, string> = {
  ai: 'AI 助理', audit_log: '操作留痕', customer: '客户档案',
  device_library: '设备库', labor_rate: '人工费', point_template: '点位模板',
  project: '项目', quota: '定额', quote: '报价单', quote_item: '报价明细',
  setting: '系统设置', subsystem: '子系统', supplier: '供应商', user: '用户管理',
  // 字段级资源（2026-10-05）：进得去模块 ≠ 看得到里面的成本/毛利
  data_cost: '成本/进价（字段级）', data_profit: '利润/毛利（字段级）',
}
const ACTION_LABEL: Record<string, string> = {
  read: '查看', create: '新建', update: '修改', delete: '删除',
  export: '导出', approve: '审批', compute: '重算', configure: '配置',
  use: '使用', reset: '恢复出厂',
}

const permMatrix = ref<PermissionMatrix | null>(null)
const permRole = ref<string>('sales')
/** 编辑中的草稿：角色 → 资源 → 动作[] */
const permDraft = ref<Record<string, Record<string, string[]>>>({})
const permLoading = ref(false)
const permSaving = ref(false)

// ---------- 配置变更记录（2026-10-05）----------
// 谁在什么时候把设置/权限/角色改成了什么。改错了要能查出来，
// 而不是只能翻日志或问"是不是你改的"。
const cfgLogs = ref<
  {
    id: string
    created_at: string | null
    username: string | null
    action_label: string
    summary: string | null
    diff: { group: string; changes: { field: string; from: string; to: string }[] }[]
    ip: string | null
  }[]
>([])
const cfgLogsLoading = ref(false)

async function loadAuditLogs(): Promise<void> {
  cfgLogsLoading.value = true
  try {
    const res = await settingApi.auditLogs(50)
    cfgLogs.value = res.items
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    cfgLogsLoading.value = false
  }
}

// ---------- 配置导出 / 导入（2026-10-05）----------
// 换服务器、重装、或"改乱了想回到某个口径"时用。
// 导出文件**不含密钥**；导入默认先预演差异，确认后才落库。
const cfgDoc = ref<SettingsDocument | null>(null)
const cfgImporting = ref(false)
const cfgFileName = ref('')
/** 预演出来的差异（后端算，这里只缓存给表格渲染） */
const cfgDiff = ref<
  { group: string; changes: { field: string; from: string; to: string }[] }[]
>([])

async function exportConfig(): Promise<void> {
  try {
    const doc = await settingApi.exportConfig()
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
    const blob = new Blob([JSON.stringify(doc, null, 2)], {
      type: 'application/json;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `报价系统配置_${stamp}.json`
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success('已导出（不含密钥，AI Key 需要在新环境重新填写）')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

async function onPickConfig(file: File): Promise<void> {
  cfgImporting.value = true
  cfgFileName.value = file.name
  try {
    const text = await file.text()
    const doc = JSON.parse(text) as SettingsDocument
    cfgDoc.value = doc
    const preview = await settingApi.importConfig(doc, true)
    if (preview.group_count === 0) {
      ElMessage.info('这份配置与当前设置一致，无需导入')
    } else {
      ElMessage.info(`预演完成：会改动 ${preview.group_count} 个设置组，确认后再点「应用」`)
    }
  } catch (e) {
    cfgDoc.value = null
    ElMessage.error(
      e instanceof Error ? `文件读不了或格式不对：${e.message}` : '文件读不了',
    )
  } finally {
    cfgImporting.value = false
  }
}

/** 清掉预演状态（模板里写多行内联表达式是非法的，收成方法） */
function clearConfigImport(): void {
  cfgDoc.value = null
  cfgFileName.value = ''
  cfgDiff.value = []
}

/** el-upload 的 change 回调（拿 raw File，再交给 onPickConfig） */
function onConfigFileChange(f: { raw?: File }): void {
  if (f.raw) void onPickConfig(f.raw)
}

async function applyConfigImport(): Promise<void> {
  if (!cfgDoc.value) return
  try {
    await ElMessageBox.confirm(
      '确定要应用这份配置吗？会覆盖当前的设置、权限矩阵与自定义角色。\n' +
        '导出文件里不含 AI Key，应用后需要重新填写。',
      '确认导入配置',
      { type: 'warning', confirmButtonText: '应用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  cfgImporting.value = true
  try {
    const res = await settingApi.importConfig(cfgDoc.value, false)
    ElMessage.success(`已应用（${res.group_count} 个设置组）`)
    await load()
    await loadPermMatrix()
    await meta.load(true)
    cfgDoc.value = null
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    cfgImporting.value = false
  }
}

// ---------- 角色管理（2026-10-05）----------
// 内置角色不可增删改（代码里到处引用）；自定义角色存在 settings.roles.custom，
// 保存后立即生效：新角色马上能在用户管理里选、在权限矩阵里配（含成本/利润两行）。
const customRoles = ref<{ key: string; name: string }[]>([])
const rolesSaving = ref(false)

function addCustomRole(): void {
  customRoles.value.push({ key: '', name: '' })
}

async function saveCustomRoles(): Promise<void> {
  rolesSaving.value = true
  try {
    const cleaned = customRoles.value
      .map((r) => ({ key: r.key.trim().toLowerCase(), name: r.name.trim() }))
      .filter((r) => r.key || r.name)
    await settingApi.patch({ roles: { custom: cleaned } } as never)
    ElMessage.success('角色已保存并立即生效')
    await load()
    await loadPermMatrix()
    await meta.load(true) // 用户管理下拉与各处角色标签跟着更新
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    rolesSaving.value = false
  }
}

function roleLabel(key: string): string {
  return meta.label('role', key)
}

async function loadPermMatrix(): Promise<void> {
  if (!canManageUsers.value && !canUpdate.value) return
  permLoading.value = true
  try {
    const m = await permissionApi.matrix()
    permMatrix.value = m
    // 草稿从"当前生效值"起步
    const draft: Record<string, Record<string, string[]>> = {}
    for (const role of m.editable_roles) {
      draft[role] = {}
      for (const res of Object.keys(m.catalog)) {
        draft[role][res] = [...(m.effective[role]?.[res] ?? [])]
      }
    }
    permDraft.value = draft
    if (!m.editable_roles.includes(permRole.value)) {
      permRole.value = m.editable_roles[0] ?? ''
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    permLoading.value = false
  }
}

function permToggle(res: string, action: string): void {
  const cur = permDraft.value[permRole.value]?.[res] ?? []
  const next = cur.includes(action)
    ? cur.filter((a) => a !== action)
    : [...cur, action]
  permDraft.value[permRole.value] = { ...permDraft.value[permRole.value], [res]: next }
}

function permSetAll(res: string, on: boolean): void {
  const all = permMatrix.value?.catalog[res] ?? []
  permDraft.value[permRole.value] = {
    ...permDraft.value[permRole.value],
    [res]: on ? [...all] : [],
  }
}

/** 当前角色是否已被改动（与生效值比） */
function permDirty(role: string): boolean {
  const eff = permMatrix.value?.effective[role] ?? {}
  const draft = permDraft.value[role] ?? {}
  for (const res of Object.keys(permMatrix.value?.catalog ?? {})) {
    const a = [...(eff[res] ?? [])].sort().join(',')
    const b = [...(draft[res] ?? [])].sort().join(',')
    if (a !== b) return true
  }
  return false
}

function permResetRole(): void {
  const d = permMatrix.value?.defaults[permRole.value] ?? {}
  const next: Record<string, string[]> = {}
  for (const res of Object.keys(permMatrix.value?.catalog ?? {})) {
    next[res] = [...(d[res] ?? [])]
  }
  permDraft.value[permRole.value] = next
  ElMessage.info(`「${permRole.value}」已恢复为默认值（别忘点保存）`)
}

async function savePermMatrix(): Promise<void> {
  permSaving.value = true
  try {
    // 只提交"与默认不同"的角色：没动过的继续跟随代码默认值
    const overrides: Record<string, Record<string, string[]>> = {}
    for (const role of permMatrix.value?.editable_roles ?? []) {
      const def = permMatrix.value?.defaults[role] ?? {}
      const draft = permDraft.value[role] ?? {}
      let differs = false
      for (const res of Object.keys(permMatrix.value?.catalog ?? {})) {
        const a = [...(def[res] ?? [])].sort().join(',')
        const b = [...(draft[res] ?? [])].sort().join(',')
        if (a !== b) differs = true
      }
      if (differs) overrides[role] = draft
    }
    await settingApi.patch({ permissions: { overrides } } as never)
    ElMessage.success('权限已保存并立即生效')
    await loadPermMatrix()
    // 若改到自己所在的角色，菜单要跟着变
    await auth.loadMe()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    permSaving.value = false
  }
}

/** 原值快照：用来判断"有没有改过"，避免无意义的保存 */
const snapshot = ref('')

const dirty = computed(() => JSON.stringify({ company, quote }) !== snapshot.value)

async function load(): Promise<void> {
  loading.value = true
  try {
    const s = await settingApi.get()
    Object.assign(company, {
      name: s.company.name ?? '',
      address: s.company.address ?? '',
      phone: s.company.phone ?? '',
      email: s.company.email ?? '',
      // 抬头库：后端已在**唯一读取出口**归一成 `{name,address,phone,email}` ✓；
      // 这里再兜一层旧格式（字符串数组 ✓，例如导入的旧配置档 ✓），保证界面不崩 ✗。
      letterheads: ((s.company.letterheads ?? []) as Array<Letterhead | string>).map((lh) =>
        typeof lh === 'string'
          ? { name: lh, address: '', phone: '', email: '' }
          : {
              name: lh?.name ?? '',
              address: lh?.address ?? '',
              phone: lh?.phone ?? '',
              email: lh?.email ?? '',
            },
      ),
    })
    quote.valid_days = s.quote.valid_days ?? 30
  quote.prepay_ratio = s.quote.prepay_ratio ?? 30
  quote.accept_ratio = s.quote.accept_ratio ?? 60
  quote.warranty_ratio = s.quote.warranty_ratio ?? 10
  quote.warranty_months = s.quote.warranty_months ?? 12
  quote.build_days = s.quote.build_days ?? 45
    quote.clauses = [...(s.quote.clauses ?? [])]
  clauseVars.value = s.clause_variables ?? {}
  customRoles.value = [...(s.roles?.custom ?? [])]
    snapshot.value = JSON.stringify({ company, quote })
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

onMounted(load)

function addClause(): void {
  quote.clauses.push('')
}

function removeClause(idx: number): void {
  quote.clauses.splice(idx, 1)
}

function moveClause(idx: number, delta: number): void {
  const target = idx + delta
  if (target < 0 || target >= quote.clauses.length) return
  const [row] = quote.clauses.splice(idx, 1)
  quote.clauses.splice(target, 0, row)
}

/** 公司抬头库（2026-10-09）：逐字段去空白、按名称去重（与后端同一套规则 ✓）——
 *  前端先拦住是为了给个**当场看得懂**的提示 ✓，而不是保存后吃 422 ✓。
 *  `name` 为空的条目在这里被丢掉 ✓（保存前另有专门提示，见 save()）。 */
const cleanedLetterheads = computed<Letterhead[]>(() => {
  const out: Letterhead[] = []
  const seen = new Set<string>()
  for (const lh of company.letterheads) {
    const name = String(lh?.name ?? '').trim()
    if (!name || seen.has(name)) continue
    seen.add(name)
    out.push({
      name,
      address: String(lh?.address ?? '').trim(),
      phone: String(lh?.phone ?? '').trim(),
      email: String(lh?.email ?? '').trim(),
    })
  }
  return out
})

/** 那一行填了内容却**没填公司名称** ✗（后端会当"没填完的一行"丢掉，
 *  但那样用户会以为存上了 ✓ → 这里明确拦住并说清是哪一行 ✓）。 */
const letterheadMissingName = computed(() => {
  const i = company.letterheads.findIndex(
    (lh) =>
      !String(lh?.name ?? '').trim() &&
      !!(String(lh?.address ?? '').trim() ||
        String(lh?.phone ?? '').trim() ||
        String(lh?.email ?? '').trim()),
  )
  return i < 0 ? null : i + 1
})

async function save(): Promise<void> {
  if (!company.name.trim()) {
    ElMessage.warning('公司名称不能为空 —— 它是客户版 PDF 的抬头')
    return
  }
  // 抬头库（2026-10-09）：上限与后端 services/settings.py 的 MAX_LETTERHEADS /
  // LETTERHEAD_MAXLEN 保持一致（后端仍会再校验一次 ✓，前端这层只是提示更友好 ✓）。
  const missing = letterheadMissingName.value
  if (missing !== null) {
    ElMessage.warning(`抬头库第 ${missing} 行填了地址/电话/邮箱，但「公司名称」是必填的`)
    return
  }
  const heads = cleanedLetterheads.value
  if (heads.length > 20) {
    ElMessage.warning('公司抬头最多 20 个')
    return
  }
  for (const h of heads) {
    if (h.name.length > 255) {
      ElMessage.warning(`抬头名称过长（最多 255 字）：${h.name.slice(0, 20)}…`)
      return
    }
    if ((h.address ?? '').length > 255) {
      ElMessage.warning(`抬头「${h.name}」的地址过长（最多 255 字）`)
      return
    }
    if ((h.phone ?? '').length > 64) {
      ElMessage.warning(`抬头「${h.name}」的电话过长（最多 64 字）`)
      return
    }
    if ((h.email ?? '').length > 255) {
      ElMessage.warning(`抬头「${h.name}」的邮箱过长（最多 255 字）`)
      return
    }
  }
  if (!quote.valid_days || quote.valid_days < 1) {
    ElMessage.warning('报价有效期要大于 0 天')
    return
  }
  const clauses = quote.clauses.map((c) => c.trim()).filter(Boolean)
  if (!clauses.length) {
    ElMessage.warning('至少保留一条报价说明')
    return
  }
  saving.value = true
  try {
    const s = await settingApi.patch({
      company: {
        name: company.name.trim(),
        address: company.address.trim(),
        phone: company.phone.trim(),
        email: company.email.trim(),
        // 公司抬头库（2026-10-09）：**净化后**再提交 ✓（去空去重 ✓）
        letterheads: heads,
      },
      quote: {
        valid_days: quote.valid_days,
        prepay_ratio: quote.prepay_ratio,
        accept_ratio: quote.accept_ratio,
        warranty_ratio: quote.warranty_ratio,
        warranty_months: quote.warranty_months,
        build_days: quote.build_days,
        clauses,
      },
    })
    quote.clauses = [...(s.quote.clauses ?? [])]
    snapshot.value = JSON.stringify({ company, quote })
    ElMessage.success('已保存 —— 之后导出的 PDF 与客户版预览都会用这套抬头与条款')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    saving.value = false
  }
}

function reset(): void {
  void load()
}
</script>

<template>
  <div v-loading="loading" class="page st">
    <div class="page-head">
      <div>
        <h2 class="page-title">系统设置</h2>
        <p class="page-sub">
          影响输出的配置：公司抬头 / 报价有效期 / 报价说明条款 —— 客户版 PDF 与预览页共用同一份
        </p>
      </div>
      <div>
        <el-tag v-if="!canUpdate" size="small" type="info" effect="plain">
          当前角色只读（改设置需要管理员）
        </el-tag>
      </div>
    </div>

    <el-tabs v-model="tab" class="st-tabs">
      <el-tab-pane label="系统设置" name="basic">
        <el-alert
          type="warning"
          show-icon
          :closable="false"
          class="mb"
          title="这些值是全局的：改一次，之后所有导出的客户版 PDF、Excel 抬头与报价说明条款都会跟着变。"
        />

    <div class="card st-card">
      <div class="st-head">公司信息（客户版抬头）</div>
      <el-form label-width="110px" :disabled="!canUpdate">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="公司名称" required>
              <el-input v-model="company.name" maxlength="255" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="电话">
              <el-input v-model="company.phone" maxlength="64" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="地址">
              <el-input v-model="company.address" maxlength="255" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="邮箱">
              <el-input v-model="company.email" maxlength="255" />
            </el-form-item>
          </el-col>
        </el-row>

        <!-- 公司抬头库（2026-10-09）：有时要用**别家公司抬头**去报价 ✓。
             一开始只存公司名称；用户随即补充「地址和联系电话和邮箱还是要修改一下的」
             ⇒ 每条现在存**名称 + 地址 + 电话 + 邮箱**（名称必填、其余可空 ✓）。
             税号 / Logo / 盖章位仍然不做 ✓。
             这里是"候选名单"：改/删它**不会**追溯改写已开出的报价单 ✓。 -->
        <el-form-item label="公司抬头库">
          <div style="width: 100%">
            <div
              v-for="(_n, i) in company.letterheads"
              :key="i"
              style="display: flex; flex-direction: column; gap: 4px;
                     margin-bottom: 10px; padding-bottom: 8px;
                     border-bottom: 1px dashed var(--el-border-color-lighter)"
            >
              <div style="display: flex; gap: 8px; align-items: center">
                <el-input
                  v-model="company.letterheads[i].name"
                  maxlength="255"
                  placeholder="公司名称（必填），例如：浙江某某科技有限公司"
                  style="max-width: 420px"
                />
                <el-button link type="danger" @click="company.letterheads.splice(i, 1)">删</el-button>
              </div>
              <!-- 地址 / 电话 / 邮箱（2026-10-09 用户补充 ✓）：可空 ✓ ——
                   空的那几项在导出/预览时**逐字段**回落到上面「公司信息」那套 ✓。 -->
              <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
                <el-input
                  v-model="company.letterheads[i].address"
                  maxlength="255"
                  placeholder="地址（可空 → 用上面「公司信息」的地址）"
                  style="max-width: 300px"
                />
                <el-input
                  v-model="company.letterheads[i].phone"
                  maxlength="64"
                  placeholder="电话（可空）"
                  style="max-width: 180px"
                />
                <el-input
                  v-model="company.letterheads[i].email"
                  maxlength="255"
                  placeholder="邮箱（可空）"
                  style="max-width: 220px"
                />
              </div>
            </div>
            <el-button
              size="small"
              :disabled="company.letterheads.length >= 20"
              @click="company.letterheads.push({ name: '', address: '', phone: '', email: '' })"
            >
              + 加一个抬头
            </el-button>
            <div class="text-muted" style="margin-top: 6px; font-size: 12px">
              报价时可在「报价条款」弹窗里选用（建议 3~5 个 ✓）。
              每张报价单记的是**当时选定那一项的名称/地址/电话/邮箱本身** ✓ ——
              所以在这里改 / 删，**已开出的报价单不会被追溯改写** ✓。
              只填名称也能用 ✓：地址/电话/邮箱会**各自**沿用上面「公司信息」那一套 ✓
              （不选 = 整组用上面的 ✓）。
              <span v-if="company.letterheads.length">　当前 {{ company.letterheads.length }} 个</span>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <div class="text-muted st-preview">
        预览效果：<b>{{ company.name || '（未填）' }}</b>
        <span v-if="company.address">　{{ company.address }}</span>
        <span v-if="company.phone">　{{ company.phone }}</span>
        <span v-if="company.email">　{{ company.email }}</span>
      </div>
    </div>

    <div class="card st-card">
      <div class="st-head">
        报价策略
        <span class="text-muted">有效期天数会用来算客户版 PDF 的「有效期至」</span>
      </div>
      <el-form label-width="110px" :disabled="!canUpdate">
        <el-form-item label="报价有效期">
          <el-input-number v-model="quote.valid_days" :min="1" :max="365" :precision="0" />
          <span class="text-muted ml">天（条款里写 <code>{valid_days}</code> 就会印这个数）</span>
        </el-form-item>
        <el-form-item label="付款比例">
          <span class="text-muted">预付</span>
          <el-input-number v-model="quote.prepay_ratio" :min="0" :max="100" :precision="0" />
          <span class="text-muted">%　验收合格付</span>
          <el-input-number v-model="quote.accept_ratio" :min="0" :max="100" :precision="0" />
          <span class="text-muted">%　质保金</span>
          <el-input-number v-model="quote.warranty_ratio" :min="0" :max="100" :precision="0" />
          <span class="text-muted">%</span>
          <div class="text-muted">
            条款里写 <code>{prepay_ratio}</code> / <code>{accept_ratio}</code> /
            <code>{warranty_ratio}</code>
          </div>
        </el-form-item>
        <el-form-item label="质保期 / 工期">
          <el-input-number v-model="quote.warranty_months" :min="1" :max="120" :precision="0" />
          <span class="text-muted">个月　工期</span>
          <el-input-number v-model="quote.build_days" :min="1" :max="999" :precision="0" />
          <span class="text-muted">
            个日历天（<code>{warranty_months}</code> / <code>{build_days}</code>）
          </span>
        </el-form-item>
      </el-form>
    </div>

    <div class="card st-card">
      <div class="st-head">
        报价说明条款
        <span class="text-muted">
          按顺序印在客户版 PDF 与预览页上。**写数字请用变量**，否则改了上面的数字、
          这里的文字不会跟着变（会自相矛盾）
        </span>
        <div class="text-muted" style="margin-top: 4px">
          可用变量：<code v-for="(label, name) in clauseVars" :key="name"
            style="margin-right: 8px">{{ '{' + name + '}' }}={{ label }}</code>
        </div>
      </div>
      <div v-for="(_c, idx) in quote.clauses" :key="idx" class="st-clause">
        <span class="st-no">{{ idx + 1 }}.</span>
        <el-input
          v-model="quote.clauses[idx]"
          type="textarea"
          :rows="1"
          :autosize="{ minRows: 1, maxRows: 3 }"
          :disabled="!canUpdate"
        />
        <el-button-group v-if="canUpdate">
          <el-button size="small" :disabled="idx === 0" @click="moveClause(idx, -1)">
            <el-icon><Top /></el-icon>
          </el-button>
          <el-button
            size="small"
            :disabled="idx === quote.clauses.length - 1"
            @click="moveClause(idx, 1)"
          >
            <el-icon><Bottom /></el-icon>
          </el-button>
          <el-button size="small" type="danger" plain @click="removeClause(idx)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </el-button-group>
      </div>
      <el-empty v-if="!quote.clauses.length" description="还没有条款" :image-size="50" />
      <el-button v-if="canUpdate" class="mt" @click="addClause">
        <el-icon><Plus /></el-icon>&nbsp;加一条
      </el-button>
    </div>

        <div v-if="canUpdate" class="st-actions">
          <el-button :disabled="!dirty" @click="reset">放弃修改</el-button>
          <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">
            保存设置{{ dirty ? ' *' : '' }}
          </el-button>
        </div>

    <div class="card st-card">
      <div class="st-head">
        配置导出 / 导入
        <span class="text-muted">
          换服务器、重装、或"改乱了想回到某个口径"时用；导出文件**不含密钥**
        </span>
      </div>
      <div class="mb">
        <el-button :disabled="!canUpdate" @click="exportConfig">导出配置（JSON）</el-button>
        <el-upload
          style="display: inline-block; margin-left: 8px"
          :auto-upload="false"
          :show-file-list="false"
          accept=".json,application/json"
          :on-change="onConfigFileChange"
        >
          <el-button :loading="cfgImporting" :disabled="!canUpdate">选择文件并预演</el-button>
        </el-upload>
        <span v-if="cfgFileName" class="text-muted" style="margin-left: 8px">
          已选：{{ cfgFileName }}
        </span>
      </div>

      <template v-if="cfgDoc">
        <el-alert
          type="warning"
          :closable="false"
          show-icon
          class="mb"
          title="下面是会改动的内容（预演）。确认无误再点「应用」—— 会覆盖设置、权限矩阵与自定义角色。"
        />
        <el-table
          v-if="cfgDiff.length"
          :data="cfgDiff"
          size="small"
          border
          max-height="280"
          row-key="group"
        >
          <el-table-column prop="group" label="设置组" width="130" />
          <el-table-column label="改动">
            <template #default="{ row }">
              <div v-for="c in row.changes" :key="c.field" style="font-size: 12px">
                <b>{{ c.field }}</b>：<span class="text-muted">{{ c.from }}</span>
                → <span>{{ c.to }}</span>
              </div>
            </template>
          </el-table-column>
        </el-table>
        <el-button type="danger" :loading="cfgImporting" @click="applyConfigImport">
          应用这份配置
        </el-button>
        <el-button @click="clearConfigImport">取消</el-button>
      </template>
    </div>




        <div v-if="canReset" class="fr-card">
          <el-divider />
          <div class="card-title danger-text">危险操作 · 恢复出厂设置</div>
          <el-alert
            type="error"
            show-icon
            :closable="false"
            class="mb"
            title="此操作不可撤销。系统会先自动用 pg_dump 备份到服务器 ~/quote-backups/，备份失败则中止（不会在没有备份的情况下清库）。"
          />

          <el-form label-width="110px" style="max-width: 760px">
            <el-form-item label="清空范围">
              <el-radio-group v-model="frScope" @change="loadFrPreview">
                <el-radio v-for="s in FR_SCOPES" :key="s.value" :value="s.value">
                  {{ s.label }}
                </el-radio>
              </el-radio-group>
              <div class="text-muted" style="margin-top: 4px">
                {{ FR_SCOPES.find((s) => s.value === frScope)?.desc }}
              </div>
            </el-form-item>

            <el-form-item label="将删除">
              <div v-loading="frLoading" style="width: 100%">
                <span v-if="frPreview" class="danger-text">
                  共 {{ frPreview.total }} 行
                </span>
                <el-table
                  v-if="frPreview"
                  :data="frPreview.items.filter((x) => x.rows > 0)"
                  size="small"
                  border
                  max-height="200"
                  style="margin-top: 6px"
                >
                  <el-table-column prop="label" label="数据" min-width="180" />
                  <el-table-column prop="table" label="表" width="170">
                    <template #default="{ row }"><span class="mono">{{ row.table }}</span></template>
                  </el-table-column>
                  <el-table-column prop="rows" label="行数" width="90" align="right" />
                  <template #empty>
                    <el-empty description="这些表已经是空的" :image-size="50" />
                  </template>
                </el-table>
              </div>
            </el-form-item>

            <el-form-item label="确认短语">
              <el-input v-model="frConfirm" placeholder="原样输入：恢复出厂设置" />
            </el-form-item>
            <el-form-item label="当前密码">
              <el-input v-model="frPassword" type="password" show-password placeholder="当前登录账号的密码" />
            </el-form-item>
            <el-form-item>
              <el-button :loading="frRunning" @click="runFactoryReset(true)">预演（不删数据）</el-button>
              <el-button
                type="danger"
                :loading="frRunning"
                :disabled="frConfirm.trim() !== '恢复出厂设置' || !frPassword"
                @click="runFactoryReset(false)"
              >
                执行恢复出厂设置
              </el-button>
            </el-form-item>
          </el-form>

          <template v-if="frResult">
            <el-alert
              :type="frResult.dry_run ? 'info' : 'success'"
              :closable="false"
              show-icon
              class="mb"
              :title="frResult.dry_run ? '预演结果（未删除任何数据）' : `已执行：清空 ${frResult.total_deleted} 行`"
            />
            <el-alert
              v-if="frResult.backup"
              type="success"
              :closable="false"
              show-icon
              class="mb"
              :title="`备份文件：${frResult.backup.filename}（${frResult.backup.size_mb} MB，在服务器 ~/quote-backups/）`"
            />
            <el-alert v-else type="warning" :closable="false" show-icon class="mb" title="本次没有生成备份" />
            <div v-if="!frResult.dry_run" class="text-muted">{{ frResult.note }}</div>
            <el-table
              v-if="!frResult.dry_run && frResult.deleted.length"
              :data="frResult.deleted.filter((x) => x.deleted > 0)"
              size="small"
              border
              max-height="220"
            >
              <el-table-column prop="label" label="数据" min-width="180" />
              <el-table-column prop="deleted" label="删除行数" width="100" align="right" />
              <el-table-column prop="error" label="备注" min-width="120" />
            </el-table>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane v-if="canUpdate" label="角色管理与权限矩阵" name="perms" lazy>
    <div class="card st-card">
      <div class="st-head">
        角色管理（自定义角色）
        <span class="text-muted">
          内置角色不可改；这里新增的角色会立刻出现在用户管理下拉与权限矩阵里
        </span>
      </div>
      <el-alert type="info" :closable="false" show-icon class="mb">
        <template #title>
          标识（key）只能用小写字母/数字/下划线、字母开头、2~24 位 ——
          它是系统内部用的，建好之后不建议再改。名称是给人看的。
        </template>
      </el-alert>

      <el-table :data="customRoles" size="small" border>
        <el-table-column label="标识 key" width="200">
          <template #default="{ row }">
            <el-input v-model="row.key" placeholder="engineering" :disabled="!canUpdate" />
          </template>
        </el-table-column>
        <el-table-column label="显示名称" width="200">
          <template #default="{ row }">
            <el-input v-model="row.name" placeholder="工程部" :disabled="!canUpdate" />
          </template>
        </el-table-column>
        <el-table-column label="权限">
          <template #default>
            <span class="text-muted">
              新角色**默认没有任何权限** —— 保存后到下面的「权限矩阵」里勾
            </span>
          </template>
        </el-table-column>
        <el-table-column width="80">
          <template #default="{ $index }">
            <el-button link type="danger" :disabled="!canUpdate"
              @click="customRoles.splice($index, 1)">删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有自定义角色" :image-size="50" />
        </template>
      </el-table>

      <div style="margin-top: 10px">
        <el-button :disabled="!canUpdate" @click="addCustomRole">+ 新增角色</el-button>
        <el-button type="primary" :loading="rolesSaving" :disabled="!canUpdate"
          @click="saveCustomRoles">保存角色</el-button>
        <span class="text-muted" style="margin-left: 8px">
          删除还有人在用的角色会被拒绝（会告诉你还有几个人）
        </span>
      </div>
    </div>
    <div class="card st-card">

      <div class="st-head">

        权限矩阵（按角色）

        <span class="text-muted">

          谁能进哪个模块、能做什么。保存后立即生效；改动只影响你勾过的角色

        </span>

      </div>

      <el-alert type="warning" :closable="false" show-icon class="mb">

        <template #title>

          管理员恒定拥有全部权限、不可修改（这是防止把自己锁在门外的底线）。

          改动会影响该角色**所有人的菜单与按钮**，请谨慎。

        </template>

      </el-alert>



      <div v-loading="permLoading">

        <el-radio-group v-model="permRole" class="mb">

          <el-radio-button v-for="r in permMatrix?.editable_roles ?? []" :key="r" :value="r">

            {{ roleLabel(r) }}

            <span v-if="permDirty(r)" class="danger-text">•</span>

          </el-radio-button>

        </el-radio-group>



        <el-table

          v-if="permMatrix"

          :data="Object.keys(permMatrix.catalog).map((res) => ({ res }))"

          size="small"

          border

          max-height="460"

        >

          <el-table-column label="模块" width="150">

            <template #default="{ row }">

              {{ RES_LABEL[row.res] ?? row.res }}

              <div class="text-muted mono" style="font-size: 11px">{{ row.res }}</div>

            </template>

          </el-table-column>

          <el-table-column label="动作（勾选 = 允许）">

            <template #default="{ row }">

              <el-checkbox

                v-for="a in permMatrix.catalog[row.res]"

                :key="a"

                :model-value="(permDraft[permRole]?.[row.res] ?? []).includes(a)"

                :label="ACTION_LABEL[a] ?? a"

                @change="permToggle(row.res, a)"

              />

              <el-button link size="small" @click="permSetAll(row.res, true)">全选</el-button>

              <el-button link size="small" @click="permSetAll(row.res, false)">清空</el-button>

            </template>

          </el-table-column>

        </el-table>



        <div style="margin-top: 10px">

          <el-button type="primary" :loading="permSaving" @click="savePermMatrix">

            保存权限

          </el-button>

          <el-button @click="permResetRole">把当前角色恢复默认</el-button>

          <span class="text-muted" style="margin-left: 8px">

            带 • 的角色表示已改动过

          </span>

        </div>

      </div>

    </div>

      </el-tab-pane>


      <!-- 用户管理与组织架构（原顶栏独立项，2026-10-04 收进系统设置） -->
      <el-tab-pane v-if="canManageUsers" label="用户管理" name="users" lazy>
        <UserManagementView />
      </el-tab-pane>


      <!-- AI 助理（2026-10-04）：只有 admin 能配（后端 ai.configure 也只给 admin） -->
      <el-tab-pane v-if="canConfigureAi" label="AI 助理" name="ai" lazy>
        <el-alert
          type="warning"
          show-icon
          :closable="false"
          class="mb"
          title="API Key 只会加密保存在服务器上，界面只显示掩码；请用云厂商控制台为它设置额度与告警。"
        />
        <el-form v-loading="aiLoading" label-width="130px" style="max-width: 720px">
          <el-form-item label="启用 AI 助理">
            <el-switch v-model="aiForm.enabled" />
            <span class="text-muted" style="margin-left: 10px">
              关闭后所有 AI 入口隐藏，行为与未集成时一致
            </span>
          </el-form-item>
          <el-form-item label="接口地址">
            <el-input v-model="aiForm.base_url" placeholder="https://api.deepseek.com/v1" />
            <div class="text-muted">OpenAI 兼容地址；DeepSeek / 通义 / Moonshot / 本地 vLLM 都填这个格式</div>
          </el-form-item>
          <el-form-item label="对话/解析模型">
            <div style="display: flex; gap: 8px; width: 100%">
              <el-select
                v-if="aiModels.length"
                v-model="aiForm.model"
                filterable
                allow-create
                default-first-option
                placeholder="从拉取到的列表里选"
                style="flex: 1"
              >
                <el-option v-for="m in aiModels" :key="m" :label="m" :value="m" />
              </el-select>
              <el-input v-else v-model="aiForm.model" placeholder="deepseek-chat" style="flex: 1" />
              <el-button :loading="aiModelsLoading" @click="fetchAiModels">拉取模型列表</el-button>
            </div>
            <div class="text-muted">
              填错模型名会报「模型在服务端不存在」；不知道叫什么就点右边拉取
            </div>
          </el-form-item>
          <el-form-item label="多模态模型">
            <el-select
              v-if="aiModels.length"
              v-model="aiForm.vision_model"
              filterable
              allow-create
              clearable
              default-first-option
              placeholder="留空 = 用上面的模型"
              style="width: 100%"
            >
              <el-option v-for="m in aiModels" :key="m" :label="m" :value="m" />
            </el-select>
            <el-input
              v-else
              v-model="aiForm.vision_model"
              placeholder="留空 = 用上面的模型（图片/扫描件要用支持视觉的模型）"
            />
          </el-form-item>
          <el-form-item label="API Key">
            <el-input
              v-model="aiKeyInput"
              type="password"
              show-password
              :placeholder="aiConfig?.api_key_set ? `已配置 ${aiConfig.api_key_hint}（留空不修改）` : '尚未配置'"
            />
            <el-button v-if="aiConfig?.api_key_set" link type="danger" @click="clearAiKey">
              清空 Key
            </el-button>
          </el-form-item>
          <el-form-item label="温度">
            <el-input-number v-model="aiForm.temperature" :min="0" :max="2" :step="0.1" :precision="2" />
            <span class="text-muted" style="margin-left: 10px">解析类任务建议 0~0.2</span>
          </el-form-item>
          <el-form-item label="超时（秒）">
            <el-input-number v-model="aiForm.timeout_seconds" :min="5" :max="600" />
          </el-form-item>
          <el-form-item label="发送前脱敏">
            <el-checkbox v-model="aiForm.redact_customer">
              隐去客户名称与联系方式（合规要求时打开）
            </el-checkbox>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="aiSaving" @click="saveAi">保存</el-button>
            <el-button :loading="aiTesting" @click="testAi">测试连接</el-button>
            <span v-if="aiConfig?.last_test_at" class="text-muted" style="margin-left: 12px">
              上次测试：{{ dateTime(aiConfig.last_test_at) }}
              {{ aiConfig.last_test_ok ? '成功' : '失败' }}
            </span>
          </el-form-item>
        </el-form>

        <el-alert
          v-if="aiConfig?.last_test_error"
          type="error"
          show-icon
          :closable="false"
          class="mb"
          :title="aiConfig.last_test_error"
        />

        <div class="card-title" style="margin-top: 16px">调用台账</div>
        <el-row v-if="aiUsage" :gutter="12" class="mb">
          <el-col :span="6"><b>调用次数</b> {{ aiUsage.totals.calls }}</el-col>
          <el-col :span="6"><b>累计 token</b> {{ aiUsage.totals.total_tokens }}</el-col>
          <el-col :span="6"><b>失败次数</b> {{ aiUsage.totals.failed }}</el-col>
          <el-col :span="6">
            <b>累计耗时</b> {{ (aiUsage.totals.duration_ms / 1000).toFixed(1) }} 秒
          </el-col>
        </el-row>
        <el-table :data="aiUsage?.items ?? []" border size="small" max-height="300">
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ row.created_at ? dateTime(row.created_at) : '—' }}</template>
          </el-table-column>
          <el-table-column prop="username" label="账号" width="110" />
          <el-table-column label="任务" width="140">
            <template #default="{ row }">{{ TASK_LABEL[row.task] ?? row.task }}</template>
          </el-table-column>
          <el-table-column prop="model" label="模型" width="140" />
          <el-table-column prop="total_tokens" label="token" width="90" align="right" />
          <el-table-column label="耗时" width="90" align="right">
            <template #default="{ row }">{{ row.duration_ms }} ms</template>
          </el-table-column>
          <el-table-column label="结果" width="80">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.ok ? 'success' : 'danger'">
                {{ row.ok ? '成功' : '失败' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="error" label="错误" min-width="200" show-overflow-tooltip />
          <template #empty>
            <el-empty description="还没有调用记录" :image-size="60" />
          </template>
        </el-table>
      </el-tab-pane>







        <!-- 恢复出厂设置（2026-10-05）：仅 admin 可见。三道门 + 先备份 -->

      <!-- 登录日志（H-10）：谁、从哪个 IP、成功还是失败。仅管理员可见 -->
      <el-tab-pane v-if="canViewLoginLogs" label="登录日志" name="logins" lazy>
        <el-alert
          type="info"
          show-icon
          :closable="false"
          class="mb"
          title="登录失败 5 次会临时锁定 15 分钟；这里的记录就是锁定的计数依据，也是排查「谁在试口令」的地方。"
        />
        <el-form inline class="mb" @submit.prevent>
          <el-form-item label="用户名">
            <el-input
              v-model="loginQuery.username"
              placeholder="精确匹配，留空看全部"
              clearable
              style="width: 200px"
              @keyup.enter="loadLoginLogs"
              @clear="loadLoginLogs"
            />
          </el-form-item>
          <el-form-item>
            <el-checkbox v-model="loginQuery.only_failed" @change="loadLoginLogs">
              只看失败
            </el-checkbox>
          </el-form-item>
          <el-form-item label="条数">
            <el-select v-model="loginQuery.limit" style="width: 100px" @change="loadLoginLogs">
              <el-option :value="50" label="50" />
              <el-option :value="100" label="100" />
              <el-option :value="300" label="300" />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="loginsLoading" @click="loadLoginLogs">
              <el-icon><Search /></el-icon>&nbsp;查询
            </el-button>
          </el-form-item>
          <el-form-item>
            <span class="text-muted">
              共 {{ loginLogs.length }} 条，其中失败
              <b :class="failedCount ? 'danger-text' : ''">{{ failedCount }}</b> 条
            </span>
          </el-form-item>
        </el-form>

        <el-table v-loading="loginsLoading" :data="loginLogs" border size="small" height="440">
          <el-table-column label="时间" width="160">
            <template #default="{ row }">{{ row.created_at ? dateTime(row.created_at) : '—' }}</template>
          </el-table-column>
          <el-table-column prop="username" label="用户名" width="130" />
          <el-table-column label="结果" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.success ? 'success' : 'danger'" effect="plain">
                {{ row.success ? '成功' : '失败' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="原因" min-width="160" show-overflow-tooltip>
            <template #default="{ row }">{{ row.reason || '—' }}</template>
          </el-table-column>
          <el-table-column label="IP" width="140">
            <template #default="{ row }"><span class="mono">{{ row.ip || '—' }}</span></template>
          </el-table-column>
          <el-table-column label="User-Agent" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="mono text-muted">{{ row.user_agent || '—' }}</span>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="没有登录记录" :image-size="60" />
          </template>
        </el-table>
      </el-tab-pane>

      <el-tab-pane v-if="canUpdate" label="配置变更记录" name="cfglog" lazy>


    <div class="card st-card">
      <div class="st-head">
        配置变更记录
        <span class="text-muted">
          谁在什么时候把设置 / 权限 / 角色改成了什么（最近 50 条；不含密钥）
        </span>
        <el-button link size="small" :loading="cfgLogsLoading" @click="loadAuditLogs">
          刷新
        </el-button>
      </div>
      <el-table
        v-loading="cfgLogsLoading"
        :data="cfgLogs"
        size="small"
        border
        max-height="360"
      >
        <el-table-column label="时间" width="150">
          <template #default="{ row }">
            <span style="font-size: 12px">{{ dateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="username" label="操作人" width="100" />
        <el-table-column prop="action_label" label="动作" width="110" />
        <el-table-column label="改了什么" min-width="300">
          <template #default="{ row }">
            <div style="font-size: 12px">{{ row.summary }}</div>
            <div
              v-for="g in row.diff"
              :key="g.group"
              class="text-muted"
              style="font-size: 11px; margin-top: 2px"
            >
              <b>{{ g.group }}</b>：
              <span v-for="c in g.changes" :key="c.field" style="margin-right: 8px">
                {{ c.field }} {{ c.from }} → {{ c.to }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="ip" label="来源 IP" width="120">
          <template #default="{ row }">
            <span class="text-muted" style="font-size: 12px">{{ row.ip ?? '—' }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有变更记录" :image-size="50" />
        </template>
      </el-table>
    </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.st-card {
  padding: 12px 14px;
  margin-bottom: 10px;
}

.st-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-weight: 600;
  font-size: 13.5px;
  margin-bottom: 10px;
}

.st-head .text-muted {
  font-weight: 400;
  font-size: 11.5px;
}

.st-preview {
  font-size: 12.5px;
  padding: 6px 10px;
  border-radius: 4px;
  background: var(--el-fill-color-lighter);
}

.st-clause {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 8px;
}

.st-no {
  flex: 0 0 22px;
  text-align: right;
  line-height: 30px;
  color: var(--el-text-color-secondary);
}

.st-clause :deep(.el-textarea) {
  flex: 1 1 auto;
}

.st-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-bottom: 12px;
}

.fr-card {
  margin-top: 8px;
}
</style>




