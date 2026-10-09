<script setup lang="ts">
import { http } from '@/api/client'
/**
 * 报价编制工作台 —— 按原型第 1 页版式重做（2026-10-03）。
 *
 * 版式依据：`原型第1页_版式蓝图.md` / `原型页面/proto-1.png`：
 *   ② 项目信息条   ③ 工具栏（7 个操作 + 右侧统计）
 *   ④ 左栏 子系统|小计       ④ 右栏 当前子系统的明细表
 *   ⑤ 左栏下半 项目合计 + 对内成本毛利视图
 *   ⑥ 底部常驻 费用汇总（含各费率与税金，报价合计在最右）
 *
 * 与原型「融合」的部分（原型没有、但必须保留的新功能）：
 *   · 报价单状态机（提交审核 / 开始审核 / 批准 / 驳回 / 退回草稿）—— 原型是 9/28 做的，早于审批链
 *   · 操作留痕抽屉（admin/finance）
 *   · 成本/毛利仅对有成本权限的角色显示（后端也会二次把关）
 *   · 已批准/已归档 = 锁定（编辑入口禁用）
 * 原型有而后端未实现的操作（点位模板自动带安装单价、批量调价、复制到系统、套用模板）
 * 一律**置灰 + 鼠标说明**，不做假按钮。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AddDeviceDialog from '@/components/AddDeviceDialog.vue'
import QuotaPickerDialog from '@/components/QuotaPickerDialog.vue'
import ApplyPointTemplateDialog from '@/components/ApplyPointTemplateDialog.vue'
import InquiryImportDialog from '@/components/InquiryImportDialog.vue'
import QuoteItemDialog from '@/components/QuoteItemDialog.vue'
import { deviceApi, projectApi, quoteApi, laborApi, settingApi } from '@/api'
import type { Letterhead } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type {
  AuditLog,
  Device,
  Project,
  QuoteCost,
  QuoteDetail,
  QuoteItem,
  Subsystem,
  SubsystemSummary,
  QuoteRevision,
  RevisionDiff,
  ApprovalChain,
} from '@/api/types'
import { dateTime, dateTimeSec, money, percentInputToRatio, ratioToPercentInput, toNum } from '@/utils/format'
import { computeSavedMessage } from '@/utils/computeMessage'
import {
  effectiveValidDays,
  formatLocalDate,
  validityHint,
  validityInfo,
  validityTagType,
} from '@/utils/quoteValidity'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const projectId = computed(() => String(route.params.projectId))
const quoteId = computed(() => String(route.params.quoteId))

const loading = ref(false)
const project = ref<Project | null>(null)
const quote = ref<QuoteDetail | null>(null)
const subsystems = ref<Subsystem[]>([])
const summary = ref<SubsystemSummary | null>(null)
const cost = ref<QuoteCost | null>(null)
const deviceTotal = ref(0)
const warnings = ref<string[]>([])
const computing = ref(false)
const ratesDirty = ref(false)

/** 当前选中的子系统（null = 全部）—— 原型左栏点击切换右栏明细。 */
const currentSubsystemId = ref<string | null>(null)
const selected = ref<QuoteItem[]>([])
/** 右栏搜索框（原型 ④ 右栏「搜索名称 / 品牌 / 型号」）。 */
const itemKeyword = ref('')
const pickerVisible = ref(false)
const itemDialogVisible = ref(false)
/** 原型第 7 页：导入甲方询价清单（4 步向导）。 */
const inquiryVisible = ref(false)
/** 原型第 1 页：套用点位模板 / 批量添加点位。 */
const applyTemplateVisible = ref(false)
const editingItem = ref<QuoteItem | null>(null)
const auditVisible = ref(false)
const auditLogs = ref<AuditLog[]>([])

// ----- 费率（原型 ⑥ 区：各费率就在汇总条上，可逐单覆盖）-----
const rates = reactive({
  tax_rate_pct: 9,
  fee_base: 'device_aux_install',
  auxiliary_base: 'device',
  auxiliary_rate_pct: 6,
  design_rate_pct: 2,
  warranty_rate_pct: 3,
  training_fee: 0,
  mgmt_profit_rate_pct: 8,
  labor_float_rate_pct: 0,
  float_a_pct: 0,
  float_b_pct: 0,
  float_c_pct: 0,
})

function ratePayload(): Record<string, unknown> {
  return {
    version: quote.value?.version,
    tax_rate: percentInputToRatio(rates.tax_rate_pct),
    fee_base: rates.fee_base,
    auxiliary_base: rates.auxiliary_base,
    // auxiliary_rate 创建报价时确定、之后不可改 ✗（决策④ ✓）→ 不再发送
    // auxiliary_rate: percentInputToRatio(rates.auxiliary_rate_pct),
    design_rate: percentInputToRatio(rates.design_rate_pct),
    warranty_rate: percentInputToRatio(rates.warranty_rate_pct),
    training_fee: rates.training_fee,
    // mgmt_profit_rate 创建报价时确定、之后不可改 ✗（决策④ ✓）→ 不再发送
    // mgmt_profit_rate: percentInputToRatio(rates.mgmt_profit_rate_pct),
    // 整体浮动 + 分类浮动（相加，见 AUDIT_REPORT §14）
    labor_float_rate: percentInputToRatio(rates.labor_float_rate_pct),
    labor_float_per_class: {
      class_a: percentInputToRatio(rates.float_a_pct),
      class_b: percentInputToRatio(rates.float_b_pct),
      class_c: percentInputToRatio(rates.float_c_pct),
    },
  }
}

function syncRatesFromQuote(q: QuoteDetail): void {
  const per = (q.labor_float_per_class ?? {}) as Record<string, number>
  Object.assign(rates, {
    tax_rate_pct: ratioToPercentInput(q.tax_rate),
    fee_base: q.fee_base,
    auxiliary_base: q.auxiliary_base,
    auxiliary_rate_pct: ratioToPercentInput(q.auxiliary_rate),
    design_rate_pct: ratioToPercentInput(q.design_rate),
    warranty_rate_pct: ratioToPercentInput(q.warranty_rate),
    training_fee: toNum(q.training_fee),
    mgmt_profit_rate_pct: ratioToPercentInput(q.mgmt_profit_rate),
    labor_float_rate_pct: ratioToPercentInput(q.labor_float_rate),
    float_a_pct: ratioToPercentInput(per.class_a ?? 0),
    float_b_pct: ratioToPercentInput(per.class_b ?? 0),
    float_c_pct: ratioToPercentInput(per.class_c ?? 0),
  })
  ratesDirty.value = false
}

// ----- 权限与状态机（沿用旧编辑器的口径）-----
const quoteStatus = computed(() => String(quote.value?.status ?? 'draft'))
const locked = computed(() => ['approved', 'archived'].includes(quoteStatus.value))
/** 已归档（2026-10-08 用户反馈 ✓）：归档是**终态**，界面要整单只读 ✓
 *  （后端已经做到了：归档单改内容 → 409 ✓、改状态 → 422 ✓，缺的是界面只读 ✗）。
 *  approved 不走这一支 ✗ —— 批准单允许有权限的人退回草稿，那是既有审批流程 ✓。 */
const archived = computed(() => quoteStatus.value === 'archived')
const canEdit = computed(() => auth.canWrite && !locked.value)
const canApprove = computed(() => auth.can('quote', 'approve'))
const canSeeAudit = computed(() => auth.can('audit_log', 'read'))
const waitingClaim = computed(() => quoteStatus.value === 'submitted')
const inReview = computed(() => quoteStatus.value === 'reviewing')
const canSubmit = computed(
  () => auth.canWrite && ['draft', 'rejected'].includes(quoteStatus.value),
)
const canStartReview = computed(() => canApprove.value && waitingClaim.value)
const canDecide = computed(() => canApprove.value && inReview.value)
const statusChanging = ref(false)

async function changeStatus(next: string, label: string, confirmText?: string): Promise<void> {
  if (!quote.value) return
  // 归档是终态 ✓（后端改状态 422 ✓）：界面不再给入口 ✓，这里再兜一层 ✓
  if (archived.value) {
    ElMessage.warning('本单已归档，不可修改；如需增项请新建关联本单的报价单')
    return
  }
  if (confirmText) {
    const go = await ElMessageBox.confirm(confirmText, label, {
      confirmButtonText: label,
      cancelButtonText: '取消',
      type: 'warning',
    })
      .then(() => true)
      .catch(() => false)
    if (!go) return
  }
  statusChanging.value = true
  try {
    await quoteApi.update(quote.value.id, { status: next, version: quote.value.version })
    ElMessage.success(`已${label}`)
    await reload()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    statusChanging.value = false
  }
}

function onStatusCommand(cmd: string): void {
  if (cmd === 'submitted') {
    void changeStatus('submitted', '提交审核', '提交后进入待审批状态，确认提交？')
  } else if (cmd === 'reviewing') {
    void changeStatus('reviewing', '开始审核', '接手后由你批准或驳回（这是批准的前置条件）。确认接手？')
  } else if (cmd === 'draft') {
    void changeStatus('draft', '退回草稿', '退回草稿后这张单可以继续修改。确认退回？')
  }
}

// ----- 本单有效期（2026-10-08 用户反馈：进单后看不到「本单有效期」✗）-----
//  天数来源（按优先级 ✓）：
//    ① 后端 `GET /quotes/{id}/export-clauses` 的 valid_days ✓ —— 与客户版预览/PDF **完全同源** ✓，
//       且不依赖 `setting:read` 权限 ✓（销售/采购角色也拿得到 ✓）；
//    ② 拿不到就退回前端取值链 ✓（复用 utils/quoteValidity 的纯函数 ✗ 不另写一套 ✓）：
//         terms_override.valid_days → valid_days_override → 系统设置 quote.valid_days → 30
//  ⚠ 系统设置那一档要 `setting:read`（销售/采购没有 ✗）：先判权限再发请求 ✓，
//    拿不到就落到兜底值 ✓ —— 不会因为"读不到设置"就让页面报错，也不会平白弹 403 警告 ✓。
const validDaysFromApi = ref<number | null>(null)
const settingsValidDays = ref<number | null>(null)

async function loadValidDays(): Promise<void> {
  try {
    const d = await quoteApi.exportClauses(quoteId.value)
    validDaysFromApi.value = d.valid_days ?? null
  } catch {
    validDaysFromApi.value = null
  }
}

async function loadSettingsValidDays(): Promise<void> {
  if (!auth.can('setting', 'read')) return
  try {
    const s = await settingApi.get()
    settingsValidDays.value = s.quote?.valid_days ?? null
  } catch {
    settingsValidDays.value = null   // 可接受降级：退回 terms_override / 30 天 ✓
  }
}

/**
 * 报价日期（有效期起点）→ 交给纯函数时用的**本地** ISO 串。
 *
 * 为什么不用 `new Date(created_at)` 直接算：`created_at` 带 Z（UTC）✓，而
 *   后端 export_data._quote_date 走的是 `created_at.astimezone()` —— **服务器本地**日期 ✓，
 *   本部署服务器 TZ = UTC（实测 `timedatectl`：UTC）✗，浏览器却在 +08:00 ✗
 *   → 对「本地凌晨建的单」会与导出件/客户版差一天 ✗
 *   （实测探针 Q01：本地 10-08 vs 导出 10-07 ✗）。
 * 所以这里取 created_at 那一瞬在 **UTC 日历**上的日期 ✓（与后端在 UTC 服务器上的结果一致 ✓），
 *   再当成本地 00:00 交给 validityInfo ✓；「剩余天数」仍按浏览器本地今天算 ✓。
 * ⚠ 只转这一次 ✗ —— 不要对结果再 getTimezoneOffset 一次（那才是"双重换算" ✗）。
 */
function quoteStartIso(createdAt: string | null | undefined): string | null {
  if (!createdAt) return null
  const d = new Date(createdAt)
  if (Number.isNaN(d.getTime())) return null
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T00:00:00`
}

const validity = computed(() => {
  const q = quote.value
  if (!q) return null
  // ① 后端权威口径（与 PDF/预览同源 ✓）→ ② 前端取值链兜底 ✓
  const days = validDaysFromApi.value ?? effectiveValidDays(q, settingsValidDays.value)
  return validityInfo(quoteStartIso(q.created_at), days)
})

/** 悬浮算式（报价日期 + 天数 = 有效期至）—— 便于和导出件/客户核对 ✓ */
const validityTip = computed(() => {
  const q = quote.value
  return validity.value && q ? validityHint(quoteStartIso(q.created_at), validity.value) : ''
})

// ----- 数据加载 -----
async function reload(): Promise<void> {
  void loadQuotaInfo()
  loading.value = true
  try {
    const [p, q, s, sum] = await Promise.all([
      projectApi.get(projectId.value),
      quoteApi.get(quoteId.value),
      projectApi.subsystems(projectId.value),
      quoteApi.subsystemSummary(quoteId.value).catch(() => null),
    ])
    project.value = p
    quote.value = q
    // 报价模式对齐（2026-10-07 修正 ✓）：必须在本单数据 q **拿到之后**再读 install_mode ✓，
    //   旧写法在本函数开头读 quote.value ✗ —— 那时它还是**上一张单/空值** ✗，
    //   首屏必然落到 'manual' 兜底 ✗ → 定额单被显示成「手工人工费模式」✗
    //   （连带 AI 预套定额按钮、定额列也被误藏 ✗）。
    //   切换模式期间仍不许被并发 reload 覆盖 ✓（见 onModeChange 的说明 ✓）。
    if (!modeSaving) {
      installMode.value = String((q as { install_mode?: string } | null)?.install_mode ?? 'manual')
    }
    subsystems.value = s
    summary.value = sum
    syncRatesFromQuote(q)
    if (auth.canSeeCost) {
      // 无成本权限的角色这里会 403 —— 静默降级成"不显示"，不是错误
      cost.value = await quoteApi.cost(quoteId.value).catch(() => null)
    } else {
      cost.value = null
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

async function loadDeviceTotal(): Promise<void> {
  try {
    const page = await deviceApi.list({ page: 1, page_size: 1 })
    deviceTotal.value = page.total
  } catch {
    deviceTotal.value = 0
  }
}

// ---------- 版本历史与对比（2026-10-04）----------
const revDrawer = ref(false)
const revLoading = ref(false)
const revisions = ref<QuoteRevision[]>([])
const revFrom = ref<number | null>(null)
const revTo = ref<number | null>(null)
const revDiff = ref<RevisionDiff | null>(null)

const REASON_LABEL: Record<string, string> = {
  submitted: '提交审核',
  approved: '批准',
  manual: '手动存档',
  // 2026-10-08 新增：人点「保存并重算」落的那一版 ✓
  saved: '保存并重算',
}

async function openRevisions(): Promise<void> {
  revDrawer.value = true
  revDiff.value = null
  await loadRevisions()
}

async function loadRevisions(): Promise<void> {
  revLoading.value = true
  try {
    revisions.value = await quoteApi.revisions(quoteId.value)
    // 顺手把「业务版本」同步到本地 quote（2026-10-08）：
    //   revisions 是权威来源 ✓，刚点过「保存并重算」/「保存当前版本」时
    //   GET /quotes 还没重拉 ✓，不同步的话本地那份会停在旧值 ✗。
    if (quote.value) {
      quote.value.revision_count = revisions.value.length
      quote.value.latest_revision_seq = revisions.value.length
        ? Math.max(...revisions.value.map((r) => r.seq))
        : 0
      // 展示口径是**时间标记**：取最新一版的 created_at（列表已按 seq 倒序 ✓）
      quote.value.latest_revision_at = revisions.value.length
        ? revisions.value[0].created_at
        : null
    }
    // 默认比"最近两版"，用户最常看的就是这个
    if (revisions.value.length >= 2) {
      revTo.value = revisions.value[0].seq
      revFrom.value = revisions.value[1].seq
      await runDiff()
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    revLoading.value = false
  }
}

async function runDiff(): Promise<void> {
  if (revFrom.value === null || revTo.value === null) return
  if (revFrom.value === revTo.value) {
    ElMessage.warning('请选择两个不同的版本')
    return
  }
  try {
    revDiff.value = await quoteApi.diffRevisions(quoteId.value, revFrom.value, revTo.value)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

async function saveRevisionNow(): Promise<void> {
  try {
    const { value } = await ElMessageBox.prompt('给这一版起个名字（选填）：', '保存当前版本', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputPlaceholder: '如：甲方砍价后',
    })
    const r = await quoteApi.saveRevision(quoteId.value, value || undefined)
    // 提示里也用**时间标记** ✓（原来是「已保存为第 N 版」✗ —— 用户要的是纯时间 ✓）
    ElMessage.success(
      `已保存版本（${dateTimeSec(r.created_at)}）` +
        (value ? `：${value}` : ''),
    )
    await loadRevisions()
  } catch (e) {
    if (e !== 'cancel') ElMessage.error(extractErrorMessage(e))
  }
}

// ---------- 分级审批（2026-10-04）----------
const approval = ref<ApprovalChain | null>(null)

/** 审批链只在"审批中"才需要显示；其余状态不拉这个接口，省一跳。 */
async function loadApproval(): Promise<void> {
  const st = quote.value?.status
  if (st !== 'submitted' && st !== 'reviewing') {
    approval.value = null
    return
  }
  try {
    approval.value = await quoteApi.approvals(quoteId.value)
  } catch {
    approval.value = null
  }
}

async function decide(decision: 'approved' | 'rejected'): Promise<void> {
  const isReject = decision === 'rejected'
  let comment: string | undefined
  try {
    const { value } = await ElMessageBox.prompt(
      isReject
        ? '驳回必须写明意见（改什么、为什么），否则对方不知道从哪改：'
        : '审批意见（选填）：',
      isReject ? '驳回报价单' : '通过本级审批',
      {
        confirmButtonText: isReject ? '驳回' : '通过',
        cancelButtonText: '取消',
        inputPlaceholder: isReject ? '如：单价低于底价，请重新核价' : '如：造价复核通过',
        inputValidator: (v: string) =>
          !isReject || (v && v.trim().length >= 2) || '驳回必须写意见',
      },
    )
    comment = value?.trim() || undefined
  } catch {
    return
  }
  try {
    const r = await quoteApi.decideApproval(quoteId.value, decision, comment)
    if (r.finalized) ElMessage.success('审批已全部通过，报价单已批准')
    else if (decision === 'rejected') ElMessage.warning('已驳回，单据退回')
    else ElMessage.success(`第 ${r.level} 级已通过，继续等待后续审批`)
    await reload()
    await loadApproval()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

onMounted(async () => {
  await Promise.all([meta.load(), reload(), loadDeviceTotal(), loadValidDays(), loadSettingsValidDays()])
  await loadApproval()
})

// ----- 明细 -----
const items = computed<QuoteItem[]>(() => quote.value?.items ?? [])

const visibleItems = computed<QuoteItem[]>(() => {
  let list = currentSubsystemId.value
    ? items.value.filter((i) => i.subsystem_id === currentSubsystemId.value)
    : items.value
  const kw = itemKeyword.value.trim().toLowerCase()
  if (kw) {
    list = list.filter((i) =>
      [i.name, i.brand ?? '', i.model ?? '', i.device_code ?? '']
        .some((f) => String(f).toLowerCase().includes(kw)),
    )
  }
  return list
})

/** 原型 ④ 右栏的快捷筛选 chips（取前 8 个子系统，点一下就切换筛选）。 */
const quickChips = computed(() => subsystemRows.value.slice(0, 8))

const subsystemName = computed(() => {
  const m = new Map<string, string>()
  for (const s of subsystems.value) {
    m.set(s.id, s.custom_name || meta.label('subsystem_type', s.subsystem_type))
  }
  return m
})

const subsystemOptions = computed(() =>
  subsystems.value.map((s) => ({
    id: s.id,
    name: s.custom_name || meta.label('subsystem_type', s.subsystem_type),
  })),
)

/** 左栏：子系统 + 小计（原型 ④ 左栏；没有明细的子系统也列出来，便于补齐）。 */
const subsystemRows = computed(() => {
  const rows = (summary.value?.subsystems ?? []).map((r) => ({
    id: r.subsystem_id,
    name: r.name || subsystemName.value.get(r.subsystem_id) || '未命名子系统',
    amount: r.amount,
    point_count: r.point_count,
  }))
  const known = new Set(rows.map((r) => r.id))
  for (const s of subsystems.value) {
    if (!known.has(s.id)) {
      rows.push({
        id: s.id,
        name: s.custom_name || meta.label('subsystem_type', s.subsystem_type),
        amount: 0,
        point_count: 0,
      })
    }
  }
  return rows
})

const stats = computed(() => ({
  subsystems: subsystemRows.value.length,
  items: items.value.length,
  points: subsystemRows.value.reduce((sum, r) => sum + (r.point_count || 0), 0),
}))

const currentRows = computed(() =>
  currentSubsystemId.value
    ? subsystemRows.value.filter((r) => r.id === currentSubsystemId.value)
    : [],
)
const currentEquipment = computed(() =>
  currentRows.value.reduce((s, r) => s + r.amount, 0),
)

const discontinuedCount = computed(
  () => items.value.filter((i) => i.device_status && meta.isDiscontinued(i.device_status)).length,
)

function selectSubsystem(id: string | null): void {
  currentSubsystemId.value = id
}

function onSelectionChange(rows: QuoteItem[]): void {
  // AI 预套：把勾选的行记下来（批量确认用 ✓，不影响原有逻辑 ✓）
  aiSelected.value = (rows ?? []).map((r: { id: unknown }) => String(r.id))
  selected.value = rows
}

async function onQuantityChange(row: QuoteItem, value: number | undefined): Promise<void> {
  if (value === undefined || value === null) return
  try {
    await quoteApi.updateItem(quoteId.value, row.id, { quantity: value })
    await reload()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
    await reload()
  }
}

function openItemDialog(item: QuoteItem | null): void {
  editingItem.value = item
  itemDialogVisible.value = true
}

/**
 * 低于底价时的放行交互（2026-10-04）。
 *
 * 后端会拦"低于设备底价"的报价：只有经理/管理员 + 写明理由才放行。
 * 这里把那次 422 变成"问一句理由"而不是一句干巴巴的报错 ——
 * 否则这条控制从界面上根本走不通，用户只会以为系统坏了。
 */

/** 辅材单价（按工程量口径）：改完要重算，否则表格里的辅材费不动。 */
async function onAuxUnitChange(row: QuoteItem, value: number | undefined): Promise<void> {
  const v = value ?? 0
  if (toNum(row.auxiliary_unit_price) === v) return
  try {
    await quoteApi.updateItem(quoteId.value, row.id, { auxiliary_unit_price: v })
    await reload()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

async function confirmBelowFloor(detail: string): Promise<string | null> {
  try {
    const { value } = await ElMessageBox.prompt(
      `${detail}\n\n请填写放行理由（会记入操作留痕）：`,
      '低于底价，需要授权',
      {
        confirmButtonText: '放行',
        cancelButtonText: '取消',
        inputPlaceholder: '如：甲方年度框架协议价，已口头批准',
        inputValidator: (v: string) => (v && v.trim().length >= 2) || '理由不能为空',
      },
    )
    return value?.trim() ?? null
  } catch {
    return null
  }
}

function isFloorError(e: unknown): boolean {
  const msg = extractErrorMessage(e)
  return msg.includes('底价')
}

// ==================== 附件配单（2026-10-05 用户反馈 ✓）====================
// 买防火墙/交换机常要带授权/配件 ✓，以前加设备**没有任何提示** ✗。
// 数据与判断都在后端（GET /quotes/{id}/accessory-check ✓），前端只负责问与落库 ✓。
const accPickVisible = ref(false)
const accLoading = ref(false)
const accMissing = ref<
  Array<{
    item_id: string
    item_name: string
    quantity: string | number
    group_label: string
    is_required: boolean
    select_mode: 'one' | 'many'
    quantity_mode: 'per_unit' | 'fixed'
    default_quantity: string | number
    options: Array<{
      accessory_id: string
      code: string
      name: string
      brand?: string | null
      model?: string | null
      unit?: string | null
      reference_price?: string | number | null
      is_default?: boolean
    }>
  }>
>([])
/** 勾选状态：key = 明细行id + '|' + 组名 → 选中的 accessory_id 列表 ✓ */
const accChosen = ref<Record<string, string[]>>({})
/** 每行的数量（默认取主机行数量 × 组默认数量 ✓） */
const accQty = ref<Record<string, number>>({})

function accKey(itemId: string, groupLabel: string): string {
  return `${itemId}|${groupLabel}`
}

function accIsChosen(itemId: string, groupLabel: string, accessoryId: string): boolean {
  return (accChosen.value[accKey(itemId, groupLabel)] ?? []).includes(accessoryId)
}

function accToggle(
  m: { item_id: string; group_label: string; select_mode: 'one' | 'many' },
  accessoryId: string,
): void {
  const k = accKey(m.item_id, m.group_label)
  const cur = accChosen.value[k] ?? []
  if (m.select_mode === 'one') {
    accChosen.value = { ...accChosen.value, [k]: cur.includes(accessoryId) ? [] : [accessoryId] }
    return
  }
  accChosen.value = {
    ...accChosen.value,
    [k]: cur.includes(accessoryId) ? cur.filter((x) => x !== accessoryId) : [...cur, accessoryId],
  }
}

/** 拉一次配单检查 ✓（缺附件就弹窗 ✓；不缺就静默 ✓） */
async function openAccessoryPicker(force = false): Promise<void> {
  if (!quoteId.value) return
  accLoading.value = true
  try {
    const res = await quoteApi.accessoryCheck(quoteId.value)
    const missing = (res?.missing ?? []) as typeof accMissing.value
    if (!missing.length) {
      if (force) ElMessage.success('配单检查通过：没有缺附件的主机 ✓')
      return
    }
    accMissing.value = missing
    // 预选：后端给的 chosen ✓ + 组里 is_default 的 ✓
    const chosen: Record<string, string[]> = {}
    const qty: Record<string, number> = {}
    for (const m of missing) {
      const k = accKey(m.item_id, m.group_label)
      const presets = m.options.filter((o) => o.is_default).map((o) => o.accessory_id)
      chosen[k] = presets.slice(0, m.select_mode === 'one' ? 1 : presets.length)
      const per = Number(m.default_quantity ?? 1)
      qty[k] = m.quantity_mode === 'per_unit'
        ? Math.max(1, Math.round(Number(m.quantity ?? 1) * (per || 1)))
        : Math.max(1, Math.round(per || 1))
    }
    accChosen.value = chosen
    accQty.value = qty
    accPickVisible.value = true
    if (force) ElMessage.info(`有 ${missing.length} 处需要配附件 ✓`)
  } catch (e) {
    if (force) ElMessage.error(extractErrorMessage(e))
  } finally {
    accLoading.value = false
  }
}

/** 确认配单：把选中的附件作为**子行**挂到主机行下 ✓ */
async function confirmAccessories(): Promise<void> {
  if (!quoteId.value) return
  accLoading.value = true
  let added = 0
  try {
    for (const m of accMissing.value) {
      const k = accKey(m.item_id, m.group_label)
      const picked = accChosen.value[k] ?? []
      if (!picked.length) {
        if (m.is_required) {
          ElMessage.warning(`「${m.group_label}」是必选，请为 ${m.item_name} 选一个 ✓`)
          return
        }
        continue
      }
      for (const id of picked) {
        const opt = m.options.find((o) => o.accessory_id === id)
        if (!opt) continue
        await quoteApi.addItem(quoteId.value, {
          // ★ 必须写 device_id ✓（2026-10-05 实测）：后端的"配单检查"是按**设备 id**
          //   判断某个附件"已选" ✓ —— 只写名字/型号的话加完仍报"缺附件" ✗。
          //   候选里的 accessory_id 就是库里那台附件设备的 id ✓。
          device_id: opt.accessory_id,
          name: opt.name,
          brand: opt.brand ?? '',
          model: opt.model ?? '',
          unit: opt.unit || '套',
          quantity: accQty.value[k] ?? 1,
          unit_price: Number(opt.reference_price ?? 0),
          install_unit_price: 0,
          notes: `附件：${m.group_label}（主机 ${m.item_name}）`,
          parent_item_id: m.item_id,       // ★ 作为主机行的子行 ✓（树形一致 ✓）
        } as never)
        added += 1
      }
    }
    accPickVisible.value = false
    if (added) {
      ElMessage.success(`已加入 ${added} 行附件 ✓`)
      // 明细写完 → 后端已重算 ✓（见 confirmOne 的说明）→ 拉最新数据 ✓
      await reload()
    } else {
      ElMessage.info('没有勾选附件 ✓')
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    accLoading.value = false
  }
}

async function onPick(payload: { devices: Device[]; subsystemId: string | null }): Promise<void> {
  let added = 0
  let failed = 0
  for (const device of payload.devices) {
    try {
      await quoteApi.addItem(quoteId.value, {
        device_id: device.id,
        subsystem_id: payload.subsystemId,
        name: device.name,
        brand: device.brand,
        model: device.model,
        unit: device.unit,
        quantity: 1,
        unit_price: device.reference_price,
        install_unit_price: device.install_unit_price,
      })
      added += 1
    } catch {
      failed += 1
    }
  }
  if (added) ElMessage.success(`已加入 ${added} 行`)
  if (failed) ElMessage.warning(`${failed} 行加入失败（可能已停产或数据不完整）`)
  await reload()
  // 加完立刻查"缺不缺附件" ✓（用户反馈：买防火墙要带授权，以前毫无提示 ✗）
  void openAccessoryPicker()
}

/** 原型第 2 页第三个页签：手动录入的设备 / 材料，直接作为一行加入本报价单。 */
async function onManualAdd(payload: Record<string, unknown>): Promise<void> {
  const body: Record<string, unknown> = {
    ...payload,
    subsystem_id: payload.subsystem_id ?? currentSubsystemId.value,
  }
  try {
    await quoteApi.addItem(quoteId.value, body)
    ElMessage.success(`已加入：${String(payload.name ?? '')}`)
    await reload()
  } catch (e) {
    // 低于底价 → 问一句理由再试一次（经理/管理员才放得过去，后端把关）
    if (!isFloorError(e)) {
      ElMessage.error(extractErrorMessage(e))
      return
    }
    const reason = await confirmBelowFloor(extractErrorMessage(e))
    if (!reason) return
    try {
      await quoteApi.addItem(quoteId.value, { ...body, below_floor_reason: reason })
      ElMessage.success(`已按授权价加入：${String(payload.name ?? '')}`)
      await reload()
    } catch (e2) {
      ElMessage.error(extractErrorMessage(e2))
    }
  }
}

/**
 * 原型报价页的「复制到系统」：把勾选的行**收进企业设备库**。
 * 库里本来就有（device_id 非空）或同型号已存在的行会被后端跳过并说明原因。
 */
async function promoteToLibrary(): Promise<void> {
  if (!selected.value.length) return
  const go = await ElMessageBox.confirm(
    `把勾选的 ${selected.value.length} 行收进企业设备库？\\n\\n` +
      '· 已经来自设备库的行会被跳过；\\n' +
      '· 型号已存在的行也会跳过（避免库里出现重复型号）；\\n' +
      '· 新建设备会按分类自动编号。',
    '复制到设备库',
    { confirmButtonText: '收进设备库', cancelButtonText: '取消', type: 'info' },
  )
    .then(() => true)
    .catch(() => false)
  if (!go) return
  try {
    const res = await deviceApi.fromQuoteItems(selected.value.map((r) => r.id))
    if (res.created_count) {
      ElMessage.success(
        `已收进设备库 ${res.created_count} 个：` +
          res.created.map((c) => `${c.code} ${c.name}`).join('、'),
      )
    }
    if (res.skipped_count) {
      ElMessageBox.alert(
        res.skipped.map((s) => `${s.name}：${s.reason}`).join('\\n'),
        `跳过 ${res.skipped_count} 行`,
        { confirmButtonText: '知道了', type: 'warning' },
      ).catch(() => undefined)
    }
    if (!res.created_count && !res.skipped_count) {
      ElMessage.info('没有可收进设备库的行')
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

async function removeSelected(): Promise<void> {
  if (!selected.value.length) return
  const go = await ElMessageBox.confirm(
    `确认删除选中的 ${selected.value.length} 行？`,
    '删除明细',
    { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
  )
    .then(() => true)
    .catch(() => false)
  if (!go) return
  for (const row of selected.value) {
    await quoteApi.removeItem(quoteId.value, row.id).catch(() => undefined)
  }
  ElMessage.success('已删除')
  await reload()
}

async function removeRow(row: QuoteItem): Promise<void> {
  const go = await ElMessageBox.confirm(`删除「${row.name}」这一行？`, '删除明细', {
    confirmButtonText: '删除',
    cancelButtonText: '取消',
    type: 'warning',
  })
    .then(() => true)
    .catch(() => false)
  if (!go) return
  await quoteApi.removeItem(quoteId.value, row.id)
  await reload()
}

/** 进「客户版预览」页（原型第 3 页）—— 送客户前先在这里确认。 */
function goPreview(): void {
  void router.push({
    name: 'quote-preview',
    params: { projectId: projectId.value, quoteId: quoteId.value },
  })
}

/** 进「人工费设置」页（原型第 4 页）—— 报价单级的人工费浮动都在那里改。 */
function goLaborSetting(): void {
  void router.push({
    name: 'quote-labor',
    params: { projectId: projectId.value, quoteId: quoteId.value },
  })
}

async function goSubsystems(): Promise<void> {
  await router.push({ name: 'project-subsystems', params: { projectId: projectId.value } })
}

// ----- 费率与重算 -----
function onRateChange(): void {
  ratesDirty.value = true
}

async function saveAndCompute(): Promise<void> {
  if (!canEdit.value) return
  computing.value = true
  try {
    const res = await quoteApi.compute(quoteId.value, ratePayload())
    warnings.value = res.warnings ?? []
    // 提示要说清"这次到底存没存版本"（2026-10-08：compute 现在会落一版 ✓；
    // 内容没变时按去重口径跳过 ✓ —— 见 utils/computeMessage.ts）
    ElMessage.success(computeSavedMessage(res, '已按当前费率重算'))
    await reload()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
    // 失败也要把界面拉回服务端真实状态 ✓：比如"写完明细立刻 compute"会因乐观锁
    // 报 409（我们自己把 version 顶掉了 ✗），这时**不能**让界面停在旧数据上 ✗
    // —— 用户看到的就是"子目行不出现 / 主行安装单价还是 0" ✗。
    await reload()
  } finally {
    computing.value = false
  }
}


// ----- 报价说明条款（2026-10-05）-----
// 默认用「系统设置」里那份；也可以**只给这一张单**自定义条款与有效期，
// 不污染其它报价单（后端 quotes.clauses_override / valid_days_override）。
const clausesData = ref<{
  clauses: string[]
  clause_template: string[]
  valid_days: number
  variables: Record<string, string | number>
  available_variables: Record<string, string>
  clauses_overridden: boolean
  valid_days_overridden: boolean
} | null>(null)
const clausesDraft = ref<string[]>([])
const validDaysDraft = ref<number>(30)
const useOwnClauses = ref(false)
const clausesSaving = ref(false)

function moveClause(idx: number, delta: number): void {
  const target = idx + delta
  if (target < 0 || target >= clausesDraft.value.length) return
  const [row] = clausesDraft.value.splice(idx, 1)
  clausesDraft.value.splice(target, 0, row)
}

void loadStandardClauses()   // 后台标准条款（勾选清单 ✓，2026-10-05）

async function loadClauses(): Promise<void> {
  if (!quoteId.value) return
  try {
    const d = await quoteApi.exportClauses(quoteId.value)
    clausesData.value = d as never
    // ⚠ 用 **clause_template（原始模板 ✓）** 而不是 clauses（渲染后的文字 ✗）：
    //   · 渲染后的文字里 {valid_days} 已被换成 30 ✓ → 与标准条款标签永远比不相等 ✗
    //     （用户看到的现象就是"点了标签没反应" ✓）
    //   · 更关键：保存时会把变量**固化**成死数字 ✗ → 以后改数字条款不跟着变 ✗
    clausesDraft.value = [...(d.clause_template ?? d.clauses ?? [])]
    validDaysDraft.value = d.valid_days ?? 30
    useOwnClauses.value = !!(d.clauses_overridden || d.valid_days_overridden)
    // 每单数字：用**生效值**回填 ✓（本单覆盖过就是本单值 ✓，否则是系统设置值 ✓）
    const vars = (d.variables ?? {}) as Record<string, unknown>
    for (const k of Object.keys(termsDraft) as Array<keyof typeof termsDraft>) {
      const v = vars[k]
      if (v !== undefined && v !== null && !Number.isNaN(Number(v))) {
        termsDraft[k] = Number(v)
      }
    }
    // 策略三项：从报价单本体读 ✓
    const q = quote.value as unknown as {
      tax_method?: string
      tax_rate?: number | string
      labor_float_rate?: number | string
      company_name?: string | null
      company_address?: string | null
      company_phone?: string | null
      company_email?: string | null
    } | null
    if (q) {
      // 公司抬头 ✓：本单存的就是当时选定那一项的**值本身** ✓（没存 = 用系统设置 ✓）。
      // ⚠ 四项**各自**读本单对应的列 ✓ —— 不要"拿名字回查抬头库" ✗：
      //   那样一改抬头库就会**追溯改写这张单** ✗，快照就白做了 ✗。
      letterheadDraft.value = (q.company_name ?? '') || ''
      letterheadContact.address = (q.company_address ?? '') || ''
      letterheadContact.phone = (q.company_phone ?? '') || ''
      letterheadContact.email = (q.company_email ?? '') || ''
      if (q.tax_method) taxMethodDraft.value = q.tax_method
      if (q.tax_rate !== undefined && q.tax_rate !== null) {
        taxRateDraft.value = Number(q.tax_rate) * 100        // 存的是比率 ✓ 显示百分数 ✓
      }
      if (q.labor_float_rate !== undefined && q.labor_float_rate !== null) {
        laborFloatDraft.value = Number(q.labor_float_rate) * 100
      }
    }
  } catch {
    clausesData.value = null
  }
}

// ---- 每单条款数字（2026-10-05）----
// 6 个变量的本单取值 ✓（对应后端 quotes.terms_override ✓）。
// 文字里仍然写 {变量} ✓ —— 数字改了，条款那句话自动跟着变 ✓（用户决策 ③A ✓）。
const termsDraft = reactive({
  valid_days: 30,
  prepay_ratio: 30,
  accept_ratio: 60,
  warranty_ratio: 10,
  warranty_months: 12,
  build_days: 45,
})

/** 后台维护的标准条款（可勾选 ✓） */
const standardClauses = ref<string[]>([])

/** 公司抬头库（2026-10-09）：系统设置里维护的**候选**抬头 ✓
 *  （名称 + 地址/电话/邮箱 ✓，3~5 个 ✓）。 */
const letterheads = ref<Letterhead[]>([])
/** 本单选用的公司抬头**名称** ✓。`''` = 「默认（系统设置）」= 用系统设置那整套 ✓
 *  （当前是「XX科技有限公司」，也就是加这个功能之前的行为 ✓）。 */
const letterheadDraft = ref<string>('')
/** 本单抬头快照的**另外三项** ✓（2026-10-09 用户补充「地址和联系电话和邮箱」✓）。
 *  用户不必手填 ✓ —— 选抬头时从库里**一并快照**进来 ✓；
 *  ⚠ 回填的是**本单已存的值** ✓（不是按名字回查抬头库 ✗，那样改库会追溯改写历史 ✗）。 */
const letterheadContact = reactive({ address: '', phone: '', email: '' })

/** 下拉选项：第一项「默认（系统设置）」= 空值（模板里写死 ✓）；
 *  抬头库里没有、但**本单已经存着**的名字也要列出来 ✓ ——
 *  否则用户一打开弹窗，下拉里找不到当前值会显示成空 ✗，
 *  再点一次「保存」就把这张单的抬头**悄悄改成默认**了 ✗
 *  （等于追溯改写了历史单据 ✗，正是本功能最要避免的那件事）。 */
const letterheadOptions = computed(() => {
  const out = letterheads.value.map((lh) => String(lh.name ?? '').trim()).filter(Boolean)
  const cur = letterheadDraft.value.trim()
  if (cur && !out.includes(cur)) out.push(cur)
  return out
})

/** 选中抬头 → 把它那三项**一并快照**进本单草稿 ✓（用户不必手填 ✓）。
 *  选「默认（系统设置）」→ 清空本单四项 ✓（保存时写 null → 整组回落系统设置 ✓）。 */
function onLetterheadChange(name: string): void {
  const key = String(name ?? '').trim()
  const hit = letterheads.value.find((lh) => String(lh.name ?? '').trim() === key)
  letterheadContact.address = (hit?.address ?? '').trim()
  letterheadContact.phone = (hit?.phone ?? '').trim()
  letterheadContact.email = (hit?.email ?? '').trim()
}

async function loadStandardClauses(): Promise<void> {
  try {
    const { data } = await http.get('/settings')
    const d = (data ?? {}) as Record<string, any>
    const q = (d.quote ?? {}) as Record<string, unknown>
    standardClauses.value = ((q.clauses ?? []) as unknown[]).map((c) => String(c))
    // 公司抬头库 ✓（和标准条款同一次请求拿回来 ✓，不用再打一次 /settings ✓）
    // 后端已归一成对象 ✓；这里再兜一层旧格式（字符串数组 ✓）保证不崩 ✗。
    const co = (d.company ?? {}) as Record<string, unknown>
    letterheads.value = ((co.letterheads ?? []) as Array<Letterhead | string>)
      .map((lh) =>
        typeof lh === 'string'
          ? { name: String(lh).trim(), address: '', phone: '', email: '' }
          : {
              name: String(lh?.name ?? '').trim(),
              address: String(lh?.address ?? '').trim(),
              phone: String(lh?.phone ?? '').trim(),
              email: String(lh?.email ?? '').trim(),
            },
      )
      .filter((lh) => !!lh.name)
  } catch {
    standardClauses.value = []      // 拿不到就不显示勾选清单 ✓，手写条款照常可用 ✓
    letterheads.value = []          // 抬头库拿不到 → 下拉只剩「默认（系统设置）」✓ 不崩 ✓
  }
}

/** 这条标准条款是否已在本单里 ✓ */
function hasStandard(text: string): boolean {
  return clausesDraft.value.some((c) => c.trim() === text.trim())
}

/** 勾选/取消一条标准条款 ✓（勾上=追加到本单 ✓；取消=从本单移除 ✓） */
function toggleStandard(text: string): void {
  const on = hasStandard(text)
  if (on) {
    clausesDraft.value = clausesDraft.value.filter((c) => c.trim() !== text.trim())
    ElMessage.info('已从本单移除该条款 ✓')
  } else {
    clausesDraft.value = [...clausesDraft.value, text]
    // 点了必须**看得见效果** ✓（2026-10-05）：
    //   以前点完还在"继承系统设置"模式 ✓，界面上什么也不变 ✗ → 用户以为点不动 ✓。
    //   现在自动切到"本单自选条款" ✓，并明确提示 ✓。
    if (!useOwnClauses.value) useOwnClauses.value = true
    ElMessage.success('已加入本单条款 ✓（记得点下方「保存条款设置」✓）')
  }
}

// ---- 报价策略（用户决策 ④：只开放这四项 ✓）----
const taxMethodDraft = ref<string>('general_9pct')
const taxRateDraft = ref<number>(9)
const laborFloatDraft = ref<number>(0)

/** 客户版预览（2026-10-05 用户反馈 ✓）：
 *  把条款草稿里的 {变量} 换成本单当前填的数字 ✓ —— 用户要的是"所见即所得" ✓，
 *  不用再对着 {valid_days} 这种变量原文猜它会变成什么 ✓。
 *  整数不带 .0（50% 而不是 50.0% ✓，与后端 render_clauses 的格式一致 ✓）。 */
const clausesPreview = computed(() =>
  clausesDraft.value.map((c) =>
    String(c).replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (whole, name: string) => {
      const v = (termsDraft as unknown as Record<string, unknown>)[name]
      if (v === undefined || v === null || v === '') return whole
      const num = Number(v)
      if (Number.isFinite(num)) return String(num)
      return String(v)
    }),
  ),
)

async function saveClauses(): Promise<void> {
  if (!quoteId.value) return
  // 归档 = 终态 ✓（后端 409 ✓）：条款弹窗里的保存也要挡住 ✓（不只是置灰按钮 ✓）
  if (!canEdit.value) {
    ElMessage.warning('本单已锁定（已批准/已归档），不能改条款')
    return
  }
  clausesSaving.value = true
  try {
    // 只把**改动过的**数字写成本单覆盖 ✓（没改的继承系统设置 ✓，避免把默认值固化成死数 ✓）
    const termsOverride: Record<string, number> = {}
    for (const [k, v] of Object.entries(termsDraft)) {
      const base = Number((clausesData.value?.variables ?? {})[k] ?? v)
      if (Math.abs(Number(v) - base) > 1e-9) termsOverride[k] = Number(v)
    }
    const payload = {
      clauses_override: useOwnClauses.value
        ? clausesDraft.value.map((c) => c.trim()).filter(Boolean)
        : null,
      valid_days_override: useOwnClauses.value ? validDaysDraft.value : null,
      // 每单数字 ✓：勾了"本单自选"才写 ✓（否则清空覆盖 ✓，回到系统设置 ✓）
      terms_override: useOwnClauses.value ? termsOverride : null,
      // 公司抬头（2026-10-09）✓：存**值本身** ✓（不是抬头库的下标/id ✓）
      //   —— 这样以后改/删抬头库，已开出的单**不会**被追溯改写 ✓（天然快照 ✓）。
      //   空 → null ✓ = 回落「系统设置 → 公司信息」✓（老单 behavior 不变 ✓）。
      company_name: letterheadDraft.value.trim() || null,
      // 抬头的另外三项（2026-10-09 用户补充 ✓）：与名称一起**快照**进本单 ✓。
      //   ⚠ **逐字段**独立 ✓：某一项为空 → 这一项回落系统设置 ✓（不是整组回落 ✗）。
      company_address: letterheadContact.address.trim() || null,
      company_phone: letterheadContact.phone.trim() || null,
      company_email: letterheadContact.email.trim() || null,
      // 策略三项（用户决策 ④ ✓）：一直可改 ✓
      tax_method: taxMethodDraft.value,
      tax_rate: Number(taxRateDraft.value) / 100,
      labor_float_rate: Number(laborFloatDraft.value) / 100,
    }
    await quoteApi.update(quoteId.value, payload as never)
    clausesVisible.value = false      // 保存后自动关闭 ✓（落到打印/预览 ✓）
    ElMessage.success(
      useOwnClauses.value
        ? `本单已用自定义条款${Object.keys(termsOverride).length ? `（${Object.keys(termsOverride).length} 个数字本单生效 ✓）` : ''}`
        : '已恢复为系统设置里的条款',
    )
    // PATCH 本身就会重算费用瀑布 ✓（quotes.py update_quote → _recompute ✓），
    // 所以这里只 reload() ✓；再调 compute 会因为 version 过期吃 409 ✗（见 confirmOne）。
    await reload()
    await loadClauses()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '保存失败')
  } finally {
    clausesSaving.value = false
  }
}

// 导出 / 打印函数随入口一起取消（2026-10-05 ✓）：这些动作统一在「客户版预览」页做 ✓

// ----- 留痕 -----
async function openAudit(): Promise<void> {
  auditVisible.value = true
  try {
    auditLogs.value = await quoteApi.auditLogs(quoteId.value, 100)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

const feeBaseLabel = computed(
  () => meta.options('fee_base').find((o) => o.value === rates.fee_base)?.label ?? rates.fee_base,
)

// ==================== 定额套用 / 安装单价就地编辑（2026-10-05）====================
const QUOTA_MARK = 'quota:'

/** 从备注里取定额编号（口径与后端 parse_quota_code 一致 ✓） */
function quotaOf(row: { notes?: string | null }): string {
  const notes = row?.notes || ''
  const i = notes.indexOf(QUOTA_MARK)
  if (i < 0) return ''
  return notes.slice(i + QUOTA_MARK.length).trim().split(/\s+/)[0] || ''
}

/** 把定额写回备注：保留其它说明 ✓、替换旧的 quota:xxx ✓ */
function withQuota(notes: string | null | undefined, code: string): string {
  const kept = (notes || '')
    .split(/\s+/)
    .filter((w) => w && !w.startsWith(QUOTA_MARK))
    .join(' ')
  return [kept, code ? `${QUOTA_MARK}${code}` : ''].filter(Boolean).join(' ')
}

const quotaInfo = ref<Record<string, { base_price?: string; labor_b_day?: string }>>({})

/** 定额索引（编号 → 基价/工日）✓：套定额后要能直接看到基价 ✓ */
async function loadQuotaInfo(): Promise<void> {
  if (Object.keys(quotaInfo.value).length) return
  try {
    const rows = (await laborApi.quotaItems()) as unknown as Array<Record<string, unknown>>
    const map: Record<string, { base_price?: string; labor_b_day?: string }> = {}
    for (const r of rows) {
      map[String(r.code)] = {
        base_price: String(r.base_price ?? ''),
        labor_b_day: String(r.labor_b_day ?? ''),
      }
    }
    quotaInfo.value = map
  } catch {
    /* 拉不到就不显示基价，不影响套定额 ✓ */
  }
}

const quotaPickVisible = ref(false)
const quotaTarget = ref<{ id: string; notes?: string | null } | null>(null)

function openQuotaPicker(row: { id: string; notes?: string | null }): void {
  quotaTarget.value = row
  quotaPickVisible.value = true
}

/** 选中定额 → 写备注 → 收尾（定额人工费就是靠这个备注触发的 ✓） */
async function onQuotaPicked(q: { code: string; name?: string }): Promise<void> {
  if (!canEdit.value) return
  const row = quotaTarget.value
  if (!row) return
  const parentId = (row as { parent_item_id?: string | null }).parent_item_id
  // 定额模式 + 点的是**主设备行** → 建一条定额子目行 ✓（广联达式 ✓）
  //   子目的钱走**既有** install_unit_price（= 定额基价 ✓）→ 后端零改动 ✓
  //   ⚠ 已经有子目行时**改那一条** ✗ 不再加一条（否则安装费翻倍 ✗）
  if (installMode.value === 'quota' && !parentId) {
    const base = Number((q as { base_price?: string | number }).base_price ?? 0)
    const parent = visibleTree.value.find((r) => String(r.id) === String(row.id)) as
      | { quantity?: unknown }
      | undefined
    const qty = Number(parent?.quantity ?? 1)
    const payload = {
      name: q.name ? `${q.code} ${q.name}` : q.code,
      unit: (q as { unit?: string }).unit || '项',
      quantity: qty,
      install_unit_price: base,
      notes: `quota:${q.code}`,
    }
    const exist = quotaChildren(row)
    if (exist.length) {
      await quoteApi.updateItem(quoteId.value, exist[0].id, payload as never)
      ElMessage.success(`已改定额子目为 ${q.code}（安装单价 ${base} ✓）`)
    } else {
      await quoteApi.addItem(quoteId.value, {
        ...payload,
        unit_price: 0,
        parent_item_id: String(row.id),
      } as never)
      ElMessage.success(`已加定额子目 ${q.code}（工程量默认 ${qty} ✓ 可单独改 ✓）`)
    }
  } else {
    await quoteApi.updateItem(quoteId.value, row.id, installMode.value === 'quota'
      ? { notes: withQuota(row.notes, q.code),
          install_unit_price: Number((q as { base_price?: string | number }).base_price ?? 0) }
      : { notes: withQuota(row.notes, q.code) })
    ElMessage.success(`已套定额 ${q.code}${q.name ? '（' + q.name + '）' : ''} ✓`)
  }
  // 明细写完 → 后端已重算 ✓ → 拉最新数据即可 ✓（旧 version 去 compute 会 409 ✗，见 confirmOne）
  await reload()
}

/** 设备单价（销售价）就地改（2026-10-05 用户要求 ✓）：
 *  以前这一列**只能看不能改** ✗ —— 设备库里那个价只是"建议零售价" ✓，
 *  实际卖多少由报价人自己填 ✓，所以必须可改 ✓。改完自动重算合计 ✓。 */
async function onDevicePriceChange(row: { id: string }, v: number | undefined): Promise<void> {
  await quoteApi.updateItem(quoteId.value, row.id, { unit_price: v ?? 0 })
  // 同 confirmOne：明细写完后端已重算 ✓；这里若调 compute 会因为 version 过期吃 409 ✗，
  // 结果是"价格改了、界面却还显示旧值" ✗。
  await reload()
}

/** 安装单价就地改（用户 ②：以前只能在编辑弹窗改 ✗）
 *  定额模式的主行：钱在子目行上 ✓ → 这一格改的是**那条唯一的子目行** ✓
 *  （主行自己也记一遍 → 安装费翻倍 ✗，见 installDisplay 的说明 ✓）。 */
async function onInstallPriceChange(row: QuoteItem, v: number | undefined): Promise<void> {
  if (!canEdit.value) return
  const kids = installMode.value === 'quota' && !isQuotaChild(row) ? quotaChildren(row) : []
  const target = kids.length === 1 ? kids[0] : row
  await quoteApi.updateItem(quoteId.value, target.id, { install_unit_price: v ?? 0 })
  await reload()
}

// ==================== 安装单价方式（2026-10-05 用户设计）====================
//   manual：手动填安装单价（不显示定额列 ✓）
//   quota ：套定额模式（套定额后**基价自动填进安装单价** ✓，且不再叠加定额人工费 ✓）
const installMode = ref<string>('manual')

// 切换模式期间的标志（2026-10-05）：
//   页面里有多处会触发 reload ✓ —— 若一个"早于 PATCH 发出"的 reload 后回来，
//   它会把 installMode 按**旧数据**重新赋值 ✗ → 表现为"切了又弹回去、不流畅" ✓。
//   所以切换期间不许 reload 覆盖它 ✗。
let modeSaving = false

async function onModeChange(v: string): Promise<void> {
  const prev = installMode.value
  installMode.value = v          // ① 先就地切：界面立刻响应，不等网络 ✓
  modeSaving = true
  try {
    await quoteApi.update(quoteId.value, { install_mode: v } as never)
    ElMessage.success(v === 'quota' ? '已切到「套定额」模式 ✓' : '已切到「手动填」模式 ✓')
  } catch (e) {
    installMode.value = prev     // ② 失败回滚 ✓
    ElMessage.error(extractErrorMessage(e))
  } finally {
    modeSaving = false
    await reload()               // ③ 保存成功后才同步一次（此时拿到的一定是新值 ✓）
  }
}

// 2026-10-07：界面上的模式选择控件已按用户要求换成**只读彩色标识** ✓，
//   本函数因此暂无模板调用方 ✗ —— 但**不删除、不改动** ✓（后端已 422 ✗，逻辑留作历史/后续备用）。
//   为满足 tsconfig 的 noUnusedLocals ✓，这里保留一次显式引用（不影响运行 ✓）。
void onModeChange

// ==================== 广联达式树形明细（2026-10-05 阶段1）====================
// 主设备行 = 有设备费的普通行 ✓；定额子目行 = parent_item_id 指向主设备 ✓
//   · 子目的 money 走**既有** install_unit_price（= 定额基价）✓ → 后端零改动 ✓
//   · 子目的 quantity = 工程量（默认跟随主设备 ✓，可单独改 ✓）
const visibleTree = computed<any[]>(() => {
  const rows = visibleItems.value as unknown as Array<Record<string, unknown>>
  const byParent = new Map<string, Array<Record<string, unknown>>>()
  for (const r of rows) {
    const pid = r.parent_item_id ? String(r.parent_item_id) : ''
    if (pid) {
      const arr = byParent.get(pid) ?? []
      arr.push(r)
      byParent.set(pid, arr)
    }
  }
  return rows
    .filter((r) => !r.parent_item_id)                 // 顶层只留主设备 ✓
    .map((r) => ({ ...r, children: byParent.get(String(r.id)) ?? [] }))
})

/** 是不是定额子目行 ✓ */
function isQuotaChild(row: { parent_item_id?: string | null }): boolean {
  return Boolean(row?.parent_item_id)
}

/** item_id（主行）→ 它下面的定额子目行 ✓ */
const quotaChildrenMap = computed(() => {
  const map = new Map<string, QuoteItem[]>()
  for (const it of items.value) {
    const pid = it.parent_item_id ? String(it.parent_item_id) : ''
    if (!pid) continue
    const arr = map.get(pid) ?? []
    arr.push(it)
    map.set(pid, arr)
  }
  return map
})

function quotaChildren(row: { id?: unknown } | null | undefined): QuoteItem[] {
  return row?.id ? (quotaChildrenMap.value.get(String(row.id)) ?? []) : []
}

/**
 * 主设备行的「安装单价 / 安装费 / 小计」展示值（2026-10-08 用户反馈 ✓）。
 *
 * 用户原话：「套定额模式，点击 AI 预套定额，然后批量直接确认以后定额价格不会填充到安装单价」
 * —— 指的是**主设备行这一格** ✓。事实：定额模式下钱落在**定额子目行**上
 *   （子目行的 install_unit_price = 定额基价 ✓，工程量默认跟随主行 ✓），
 *   主行自己的 install_unit_price 一直是 0 ✗ → 界面上就是"灰的 0" ✗。
 *
 * 为什么不把基价**也写进主行** ✗：安装费 = Σ(数量 × 安装单价) 对**每一行**都算
 *   （quote_compute.py:292 ✓），主行再记一遍 → **整单安装费翻倍** ✗。
 * 所以这里只做**展示汇总**（广联达"主子行"的口径 ✓）：主行显示它下面子目行的合计 ✓，
 *   落库金额一分不动 ✓ → 整单安装费与后端计算始终一致 ✓。
 */
function installDisplay(row: QuoteItem): { unit: number; amount: number; kids: number } {
  const own = { unit: toNum(row.install_unit_price), amount: toNum(row.install_amount), kids: 0 }
  if (installMode.value !== 'quota') return own
  const kids = quotaChildren(row)
  if (!kids.length) return own
  return {
    unit: kids.reduce((sum, k) => sum + toNum(k.install_unit_price), 0),
    amount: kids.reduce((sum, k) => sum + toNum(k.install_amount), 0),
    kids: kids.length,
  }
}

/**
 * 这一格的安装单价能不能就地改 ✓。
 *   手动模式 → 跟原来一样，可改（含子目行 ✓）；
 *   定额模式 → 主行自己没有钱 ✓，改了就**多一笔** ✗ → 只有"恰好一条子目行"时
 *     才允许改（改的是那条子目行 ✓ 金额口径不变 ✓）；0 条或多条 → 置灰 ✗，
 *     请走「＋定额」（0 条时建子目 ✓）或直接改子目行 ✓。
 *   !canEdit（归档/批准/无权限）一律置灰 ✓。
 */
function installCellEditable(row: QuoteItem): boolean {
  if (!canEdit.value) return false
  if (installMode.value !== 'quota') return true
  if (isQuotaChild(row)) return true
  return quotaChildren(row).length === 1
}

/** 主行的定额编号：自己备注里没有就取子目行 ✓（AI 预套把 quota:xxx 写在子目行上 ✓） */
function quotaCodeOf(row: { id?: unknown; notes?: string | null }): string {
  const own = quotaOf(row)
  if (own) return own
  for (const k of quotaChildren(row)) {
    const c = quotaOf(k)
    if (c) return c
  }
  return ''
}

// ==================== AI 预套定额（2026-10-05 搬到主界面）====================
//  规则先匹配（产品分类 + 名称）✓ → AI 只在候选里挑 ✓ → **人工确认后**才落库 ✓
//  约定：定额模式 + 主设备行 → 建定额子目行 ✓；手动模式 → 写备注 quota:编号 ✓
const aiSuggesting = ref(false)
const aiSuggest = ref<Record<string, any>>({})    // item_id → 建议
const aiPicked = ref<Record<string, string>>({})  // item_id → 选中的编号
const aiSelected = ref<string[]>([])              // 勾选的行（批量确认用）

const aiPickedCount = computed(
  () => aiSelected.value.filter((id) => aiPicked.value[id]).length,
)

async function runAiSuggest(): Promise<void> {
  if (!canEdit.value) return
  aiSuggesting.value = true
  try {
    const { data } = await http.post(`/quotes/${quoteId.value}/quota-suggest`)
    const rows = ((data as { items?: unknown[] })?.items ?? []) as Array<Record<string, any>>
    const map: Record<string, any> = {}
    const sel: Record<string, string> = {}
    for (const r of rows) {
      const id = String(r.item_id)
      map[id] = r
      const code = r.ai_code || r.suggestions?.[0]?.code || ''
      if (code) sel[id] = code
    }
    aiSuggest.value = map
    aiPicked.value = sel
    aiSelected.value = rows.map((r) => String(r.item_id))
    ElMessage.success(
      `已给出 ${rows.length} 行建议（默认全勾选）—— 可逐行确认，或点「批量直接确认」✓`,
    )
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    aiSuggesting.value = false
  }
}

/** 把一行建议落库 ✓（定额模式建子目 ✓ / 手动模式写备注 ✓） */
async function applySuggestion(itemId: string): Promise<boolean> {
  const code = aiPicked.value[itemId]
  if (!code) return false
  const row = visibleItems.value.find((r) => String(r.id) === String(itemId)) as
    | { id: string; quantity?: unknown; notes?: string | null; parent_item_id?: string | null }
    | undefined
  if (!row) return false
  const cand = ((aiSuggest.value[itemId]?.suggestions ?? []) as Array<Record<string, any>>)
    .find((c) => String(c.code) === code)
  // 候选查不到 = 这一行刚被单独确认过 ✓（confirmOne 只删 aiSuggest ✗ 留了 aiPicked ✗）
  //   → 绝不能拿 undefined 去当"基价 0"落库 ✗（那会多出一条 0 元子目行 ✗，
  //     批量确认时必然发生 ✗ —— 实测探针里就留下了这样一条 ✓）。
  if (!cand) return false
  if (installMode.value === 'quota' && !row.parent_item_id) {
    // 定额模式 + 主设备行 → 定额子目行 ✓（主行只做**汇总展示** ✓ 见 installDisplay ✓）
    //  ⚠ 同一条主行再确认一次不许**再加一条**子目行 ✗（安装费会翻倍 ✗）：
    //     已经有一条 → 改那一条 ✓（幂等 ✓）。
    const payload = {
      name: cand?.name ? `${code} ${cand.name}` : code,
      unit: String(cand?.unit ?? '项'),
      quantity: Number(row.quantity ?? 1),
      install_unit_price: Number(cand?.base_price ?? 0),
      notes: `quota:${code}`,
    }
    const exist = quotaChildren(row)
    if (exist.length) {
      await quoteApi.updateItem(quoteId.value, exist[0].id, payload as never)
    } else {
      await quoteApi.addItem(quoteId.value, {
        ...payload,
        unit_price: 0,
        parent_item_id: String(row.id),
      } as never)
    }
  } else {
    await quoteApi.updateItem(quoteId.value, row.id, {
      notes: withQuota(row.notes, code),
      ...(installMode.value === 'quota'
        ? { install_unit_price: Number(cand?.base_price ?? 0) }
        : {}),
    })
  }
  return true
}

async function confirmOne(itemId: string): Promise<void> {
  if (!canEdit.value) return
  if (await applySuggestion(itemId)) {
    const next = { ...aiSuggest.value }
    delete next[itemId]
    aiSuggest.value = next
    ElMessage.success('已确认该行定额 ✓')
    // ⚠ 这里只能 reload() ✗ 不能 saveAndCompute()：
    //   明细增删改**后端自己就会重算**（quotes.py add_item/update_item → _recompute ✓），
    //   而 compute 要带 version ✗ —— 刚写完明细时本地 version 已经过期 ✗
    //   → 乐观锁 409 → 原来的代码在 catch 里**不刷新** ✗
    //   → 界面停在旧数据上：子目行不出现 ✓、主行安装单价还是 0 ✓（就是用户报的现象 ✓）。
    await reload()
  }
}

async function confirmBatch(): Promise<void> {
  if (!canEdit.value) return
  let n = 0
  for (const id of [...aiSelected.value]) {
    if (await applySuggestion(id)) n += 1
  }
  aiSuggest.value = {}
  aiPicked.value = {}
  aiSelected.value = []
  ElMessage.success(`批量直接确认 ${n} 行 ✓`)
  // 同 confirmOne：明细写完后端已重算 ✓，这里只拉最新数据 ✓（带旧 version 去 compute 会 409 ✗）
  await reload()
}


/** 滚到「报价说明条款」卡片（2026-10-05）✓
 *  它原先只出现在页面最下方 ✓（明细表 → 版本快照 → 才轮到它 ✗），
 *  用户找不到 ✓ → 加一个直达入口 ✓。 */
// 「报价条款」现在是**弹窗**（2026-10-05 ✓）：不再滚动到页面底部 ✓
const clausesVisible = ref(false)
</script>

<template>
  <div v-loading="loading" class="page wb">
    <!-- ② 项目信息条 -->
    <div class="card wb-info">
      <div class="wi-left">
        <div class="wi-title">{{ project?.name ?? '—' }}</div>
        <div class="wi-sub">{{ project?.address || project?.customer_name || '—' }}</div>
      </div>
      <div class="wi-grid">
        <div><span class="k">客户名称</span><span class="v">{{ project?.customer_name || '—' }}</span></div>
        <div><span class="k">联系人</span><span class="v">{{ project?.customer_contact || '—' }} {{ project?.customer_phone || '' }}</span></div>
        <div><span class="k">报价方式</span><span class="v">{{ feeBaseLabel }}</span></div>
        <div><span class="k">计税方式</span><span class="v">{{ rates.tax_rate_pct }}%</span></div>
        <div><span class="k">编制人 / 日期</span><span class="v">{{ auth.displayName }} · {{ quote?.created_at ? dateTime(quote?.created_at).slice(0, 10) : '—' }}</span></div>
        <!-- 本单有效期（2026-10-08 用户反馈 ✓）：进单后要能一眼看到 ✓，
             取值链与后端同源（terms_override → valid_days_override → 系统设置 → 30 ✓），
             悬浮给出算式 ✓，过期/临期用颜色区分 ✓（红 = 已过期 ✓、橙 = ≤7 天 ✓）。 -->
        <div class="wi-valid">
          <span class="k">本单有效期</span>
          <span class="v">
            <template v-if="validity">
              <el-tooltip :content="validityTip" placement="bottom">
                <el-tag size="small" effect="plain" :type="validityTagType(validity.level)">
                  有效期至 {{ formatLocalDate(validity.validUntil) }}（{{ validity.text }}）
                </el-tag>
              </el-tooltip>
              <span class="text-muted"> 有效天数 {{ validity.days }} 天</span>
            </template>
            <span v-else class="text-muted">—</span>
          </span>
        </div>
        <div class="wi-no">
          <span class="k">报价单号</span>
          <span class="v mono">{{ quote?.code }}</span>
          <el-tag size="small" effect="plain" :type="locked ? 'success' : 'info'">
            {{ meta.label('quote_status', quote?.status) }}
          </el-tag>
        </div>
      </div>
      <div class="wi-actions no-print">
        <!-- 状态机：原型没有，但审批链是新需求，保留 -->
            <!-- 提交审核（2026-10-05 用户反馈 ✓）：原先藏在下拉里 ✗，现提成直接按钮 ✓，
                 用与「从设备库添加」同款样式 type=primary（蓝色 ✓）✓ -->
            <el-button
              v-if="canSubmit"
              type="primary"
              size="small"
              :disabled="!canEdit || statusChanging"
              :loading="statusChanging"
              @click="changeStatus('submitted', quoteStatus === 'rejected' ? '重新提交' : '提交审核', '提交后进入待审批状态，确认提交？')"
            >
              {{ quoteStatus === 'rejected' ? '重新提交审核' : '提交审核' }}
            </el-button>
        <el-dropdown v-if="canSubmit || canStartReview || (canApprove && quoteStatus === 'approved')" @command="onStatusCommand">
          <el-button size="small" :loading="statusChanging">
            状态操作<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="canStartReview" command="reviewing">开始审核（接手）</el-dropdown-item>
              <el-dropdown-item v-if="canApprove && locked" command="draft">退回草稿（解除锁定）</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-button
          v-if="canDecide"
          size="small"
          type="success"
          :loading="statusChanging"
          @click="changeStatus('approved', '批准', '批准后内容将被锁定，如需修改必须先退回草稿。确认批准？')"
        >
          批准
        </el-button>
        <el-button
          v-if="canDecide"
          size="small"
          type="danger"
          plain
          :loading="statusChanging"
          @click="changeStatus('rejected', '驳回', '驳回后需要修改并重新提交，确认驳回？')"
        >
          驳回
        </el-button>
        <el-button v-if="canSeeAudit" size="small" link @click="openAudit">
          <el-icon><Clock /></el-icon>&nbsp;操作记录
        </el-button>
        <el-button @click="openRevisions">
          <el-icon><Clock /></el-icon>&nbsp;版本历史
        </el-button>
        <el-button size="small" link class="no-print" @click="router.push({ name: 'projects' })">
          <el-icon><ArrowLeft /></el-icon>&nbsp;返回项目
        </el-button>
      </div>
    </div>

    <!-- 归档提示（2026-10-08 用户反馈 ✓）：归档是终态（后端改内容 409 ✓、改状态 422 ✓），
         界面必须一眼看出"这单不能再改" ✓ —— approved 不走这里 ✗（那是既有审批流程 ✓）。 -->
    <el-alert
      v-if="archived"
      class="mb no-print"
      type="warning"
      show-icon
      :closable="false"
      title="本单已归档，不可修改；如需增项请新建关联本单的报价单"
    />

    <!-- 停产提醒（保留既有功能；打印不要） -->
    <el-alert
      v-if="discontinuedCount > 0"
      class="mb no-print"
      type="error"
      show-icon
      :closable="false"
      :title="`明细里有 ${discontinuedCount} 行是已停产/退市型号 —— 出货前需要换型`"
    />

    <!-- ③ 工具栏 -->
    <div class="card wb-toolbar no-print">
      <el-button :disabled="!canEdit" @click="goSubsystems">
        <el-icon><Plus /></el-icon>&nbsp;新增子系统
      </el-button>
      <el-button type="primary" :disabled="!canEdit" @click="pickerVisible = true">
        <el-icon><Box /></el-icon>&nbsp;从设备库添加
      </el-button>
      <el-button :disabled="!canEdit" @click="openItemDialog(null)">手工新增行</el-button>
      <el-button :disabled="!canEdit" @click="inquiryVisible = true">
        <el-icon><Upload /></el-icon>&nbsp;导入甲方清单
      </el-button>
      <el-button :disabled="!canEdit" @click="applyTemplateVisible = true">
        <el-icon><Grid /></el-icon>&nbsp;套用点位模板
      </el-button>
      <el-button :disabled="!canEdit" @click="applyTemplateVisible = true">批量添加点位</el-button>
      <el-button disabled title="原型已设计，后端未实现">批量调价</el-button>
      <el-button :disabled="!canEdit || !selected.length" @click="removeSelected">
        删除行{{ selected.length ? `（${selected.length}）` : '' }}
      </el-button>
      <el-button
        :disabled="!selected.length || !auth.can('device_library', 'create')"
        :title="selected.length ? `把勾选的 ${selected.length} 行收进企业设备库（手工录入的行才有意义）` : '先在明细里勾选行'"
        @click="promoteToLibrary"
      >
        复制到设备库{{ selected.length ? `（${selected.length}）` : '' }}
      </el-button>

      <div class="spacer" />
      <span class="text-muted toolbar-stat">
        共 {{ stats.subsystems }} 个子系统 · {{ stats.items }} 项设备 · {{ stats.points }} 个点位
      </span>
          <el-button :loading="accLoading" :disabled="!canEdit" @click="openAccessoryPicker(true)">
            <el-icon><Paperclip /></el-icon>&nbsp;配单检查
          </el-button>
          <el-button type="primary" plain @click="clausesVisible = true">
            <el-icon><Document /></el-icon>&nbsp;报价条款
          </el-button>
          <!-- 归档/锁定后不能让 AI 预套定额继续改单 ✓（后端本来也会 409 ✓，界面先挡住 ✓） -->
          <el-button
            v-if="installMode === 'quota'"
            type="primary"
            :loading="aiSuggesting"
            :disabled="!canEdit"
            @click="runAiSuggest"
          >
            <el-icon><MagicStick /></el-icon>&nbsp;AI 预套定额
          </el-button>
          <el-button v-if="aiPickedCount" :disabled="!canEdit" @click="confirmBatch">
            批量直接确认（{{ aiPickedCount }}）
          </el-button>
      <!-- 报价模式：只读彩色标识（2026-10-05 用户要求 ✓）
           模式在建单时确定、之后不可切换（后端对 PATCH 直接 422 ✗），
           所以原来的下拉控件已经**没有选择意义** ✗ → 改成彩色只读标识用于提醒 ✓。
           注：installMode 变量与加载逻辑保留 ✓（AI 预套定额 v-if、安装单价列禁用判断等仍在用）。 -->
      <el-tooltip content="报价模式在建单时确定，之后不可切换；如需更换请新建报价单" placement="bottom">
        <span class="mode-badge-wrap">
          <el-tag v-if="installMode === 'quota'" class="mode-badge" type="warning" effect="dark" size="large">
            套定额模式
          </el-tag>
          <el-tag v-else class="mode-badge" type="success" effect="dark" size="large">
            手工人工费模式
          </el-tag>
        </span>
      </el-tooltip>
      <!-- 打印入口已取消（2026-10-05 用户要求 ✓）：打印统一在「客户版预览」里做 ✓ -->
    </div>

    <!-- ④ 两栏 + ⑤ 左栏下半 -->
    <div class="wb-body">
      <aside class="card wb-left">
        <div class="wl-head">
          <span>子系统</span>
          <span>小计（元）</span>
        </div>
        <div
          class="wl-row"
          :class="{ active: currentSubsystemId === null }"
          @click="selectSubsystem(null)"
        >
          <span>全部</span>
          <span class="num">{{ money(summary?.grand_total ?? quote?.total_with_tax) }}</span>
        </div>
        <div
          v-for="r in subsystemRows"
          :key="r.id"
          class="wl-row"
          :class="{ active: currentSubsystemId === r.id }"
          @click="selectSubsystem(r.id)"
        >
          <span class="wl-name">{{ r.name }}</span>
          <span class="num">{{ money(r.amount) }}</span>
        </div>
        <el-empty v-if="!subsystemRows.length" description="还没有子系统" :image-size="50" />

        <!-- ⑤ 项目合计 + 对内视图 -->
        <el-divider class="wl-div" content-position="left">项目合计</el-divider>
        <div class="wl-kv"><span>设备费</span><span class="num">{{ money(quote?.equipment_total) }}</span></div>
        <div class="wl-kv"><span>安装调试费</span><span class="num">{{ money(quote?.install_total) }}</span></div>
        <div class="wl-kv"><span>辅材费</span><span class="num">{{ money(quote?.auxiliary_total) }}</span></div>
        <div class="wl-kv"><span>点位总数</span><span class="num">{{ stats.points }} 点</span></div>
        <div class="wl-total">
          <div class="wl-total-label">报价合计（含税）</div>
          <div class="wl-total-value">¥ {{ money(quote?.total_with_tax) }}</div>
          <div class="wl-total-sub">
            不含税 {{ money(quote?.subtotal_before_tax) }} · 税额 {{ money(quote?.tax_amount) }}
          </div>
        </div>
        <template v-if="cost">
          <el-divider class="wl-div" content-position="left">成本与毛利（对内视图）</el-divider>
          <div class="wl-kv"><span>成本</span><span class="num">{{ money(cost.cost_total) }}</span></div>
          <div class="wl-kv">
            <span>毛利 · {{ (toNum(cost.gross_profit_rate) * 100).toFixed(2) }}%</span>
            <span class="num ok-text">{{ money(cost.gross_profit) }}</span>
          </div>
        </template>
      </aside>

      <section class="card wb-right">
        <div class="wr-head">
          <span class="wr-title">
            设备库：企业库 {{ deviceTotal }} 条
          </span>
          <el-tag v-if="currentSubsystemId" size="small" type="info" effect="plain">
            {{ subsystemName.get(currentSubsystemId) }}
          </el-tag>
          <div class="spacer" />
          <el-input
            v-model="itemKeyword"
            size="small"
            placeholder="搜索名称 / 品牌 / 型号"
            clearable
            style="width: 220px"
          >
            <template #prefix><el-icon><Search /></el-icon></template>
          </el-input>
          <el-button size="small" disabled title="原型已设计，后端未实现">套用点位模板</el-button>
          <el-button size="small" type="primary" :disabled="!canEdit" @click="pickerVisible = true">
            + 添加设备
          </el-button>
        </div>

        <!-- 原型 ④ 右栏的快捷 chips：直接切子系统筛选 -->
        <div class="wr-chips no-print">
          <button
            class="chip"
            :class="{ active: currentSubsystemId === null }"
            @click="selectSubsystem(null)"
          >
            全部
          </button>
          <button
            v-for="r in quickChips"
            :key="r.id"
            class="chip"
            :class="{ active: currentSubsystemId === r.id }"
            @click="selectSubsystem(r.id)"
          >
            {{ r.name }}
          </button>
        </div>

        <el-table
          :data="visibleTree"
          border
          stripe
          size="small"
          row-key="id"
          :tree-props="{ children: 'children' }"
          default-expand-all
          @selection-change="onSelectionChange"
        >
          <el-table-column type="selection" width="38" />
          <el-table-column label="序号" type="index" width="52" />
          <el-table-column label="设备 / 材料名称" min-width="180">
            <template #default="{ row }">
              <div>{{ row.name }}</div>
              <div class="row-tags">
                <el-tag v-if="row.device_status && meta.isDiscontinued(row.device_status)" size="small" type="danger" effect="plain">
                  已停产
                </el-tag>
                <el-tag v-else-if="row.device_status === 'near_eol'" size="small" type="warning" effect="plain">
                  临近停产
                </el-tag>
                <el-tag v-if="row.is_owner_supplied" size="small" type="info" effect="plain">甲供</el-tag>
                <el-tag v-if="row.source === 'library'" size="small" effect="plain">库</el-tag>
                <span v-else-if="row.source === 'imported'" class="text-muted">导入</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="品牌" width="110">
            <template #default="{ row }">{{ row.brand || '—' }}</template>
          </el-table-column>
          <el-table-column label="型号规格" min-width="150">
            <template #default="{ row }">
              <div>{{ row.model || '—' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="单位" width="60" prop="unit" />
          <el-table-column label="数量" width="104">
            <template #default="{ row }">
              <el-input-number
                :model-value="toNum(row.quantity)"
                :min="0"
                :precision="2"
                :controls="false"
                size="small"
                :disabled="!canEdit"
                style="width: 100%"
                @change="(v: number | undefined) => onQuantityChange(row, v)"
              />
            </template>
          </el-table-column>
          <el-table-column label="设备单价（可改）" width="128" align="right">
            <template #default="{ row }">
              <el-input-number
                :model-value="toNum(row.unit_price)"
                :min="0"
                :precision="2"
                :controls="false"
                size="small"
                :disabled="!canEdit"
                style="width: 100%"
                @change="(v: number | undefined) => onDevicePriceChange(row, v)"
              />
            </template>
          </el-table-column>
          <!-- 安装单价：**就地可编辑**（2026-10-05 用户要求）——
               以前只有点开编辑弹窗才能改 ✗；设备库也不再维护这个值 ✓
               2026-10-08 修正 ✓：定额模式下主设备行**直接显示定额基价** ✓
               （钱落在子目行上 ✓，这里只做汇总展示 ✗ 不重复计费 ✓，见 installDisplay ✓） -->
          <el-table-column label="安装单价" width="128" align="right">
            <template #default="{ row }">
              <el-input-number
                :model-value="installDisplay(row).unit"
                :disabled="!installCellEditable(row)"
                :min="0"
                :precision="2"
                :controls="false"
                size="small"
                style="width: 100%"
                @change="(v: number | undefined) => onInstallPriceChange(row, v)"
              />
              <div v-if="installDisplay(row).kids" class="text-muted cell-note">
                定额子目 {{ installDisplay(row).kids }} 条合计
              </div>
            </template>
          </el-table-column>
          <!-- 定额：显示本行套的定额编号（备注里的 quota:xxx ✓），点击可换 ✓ -->
          <!-- AI 预套建议（2026-10-05 搬到主界面 ✓）：可改下拉 + 逐行确认 ✓ -->
          <el-table-column v-if="Object.keys(aiSuggest).length" label="AI 建议" min-width="220">
            <template #default="{ row }">
              <el-select
                v-if="aiSuggest[String(row.id)]"
                v-model="aiPicked[String(row.id)]"
                size="small"
                clearable
                style="width: 100%"
              >
                <el-option
                  v-for="c in aiSuggest[String(row.id)].suggestions"
                  :key="c.code"
                  :label="`${c.code} ${c.name}`"
                  :value="c.code"
                />
              </el-select>
              <span v-else class="text-muted">—</span>
            </template>
          </el-table-column>
          <el-table-column v-if="Object.keys(aiSuggest).length" label="确认" width="92" align="center">
            <template #default="{ row }">
              <el-button
                v-if="aiSuggest[String(row.id)]"
                link
                type="primary"
                @click.stop="confirmOne(String(row.id))"
              >
                确认
              </el-button>
            </template>
          </el-table-column>
          <el-table-column v-if="installMode === 'quota'" label="定额" width="132" align="center">
            <template #default="{ row }">
              <el-button
                v-if="installMode === 'quota' && !isQuotaChild(row)"
                link
                type="primary"
                :disabled="!canEdit"
                @click.stop="openQuotaPicker(row)"
              >
                ＋定额
              </el-button>
              <el-button link type="primary" :disabled="!canEdit" @click.stop="openQuotaPicker(row)">
                <template v-if="quotaCodeOf(row)">
                  {{ quotaCodeOf(row) }}
                  <span v-if="quotaInfo[quotaCodeOf(row)]" class="text-muted">
                    · 基价 {{ quotaInfo[quotaCodeOf(row)].base_price }}
                  </span>
                </template>
                <template v-else>未套定额</template>
              </el-button>
            </template>
          </el-table-column>
          <!-- 辅材「按工程量」口径才需要这一列（2026-10-04）：
               辅材费 = Σ(数量 × 单位辅材费)，所以必须让用户能填这个单价 -->
          <el-table-column
            v-if="rates.auxiliary_base === 'by_quantity'"
            label="辅材单价"
            width="112"
            align="right"
          >
            <template #default="{ row }">
              <el-input-number
                :model-value="toNum(row.auxiliary_unit_price)"
                :min="0"
                :precision="4"
                :controls="false"
                size="small"
                :disabled="!canEdit"
                style="width: 100%"
                @change="(v: number | undefined) => onAuxUnitChange(row, v)"
              />
            </template>
          </el-table-column>
          <el-table-column label="安装费" width="106" align="right">
            <template #default="{ row }">
              <span class="num">{{ money(installDisplay(row).amount) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="小计" width="118" align="right">
            <template #default="{ row }">
              <span class="num strong">{{ money(toNum(row.equipment_amount) + installDisplay(row).amount) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="104" fixed="right" class-name="col-no-print" label-class-name="col-no-print">
            <template #default="{ row }">
              <el-button link size="small" :disabled="!canEdit" @click="openItemDialog(row)">编辑</el-button>
              <el-button link size="small" type="danger" :disabled="!canEdit" @click="removeRow(row)">删</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="这个子系统还没有明细，点「从设备库添加」开始" :image-size="70" />
          </template>
        </el-table>

        <div class="wr-foot">
          <span v-if="currentSubsystemId">
            本系统小计 <b class="num">{{ money(currentEquipment) }}</b>
          </span>
          <span v-else>
            全部小计 <b class="num">{{ money(summary?.grand_total ?? quote?.subtotal_before_tax) }}</b>
          </span>
          <span class="text-muted">
            安装费按「安装单价 × 数量 × 工艺系数」计；甲供行只计安装费。
          </span>
        </div>
      </section>
    </div>

    <!-- ⑥ 费用汇总（常驻底部；原型把费率摊在这里） -->
    <div class="card wb-fees">
      <div class="fee">
        <div class="fee-label">设备费</div>
        <div class="fee-value num">{{ money(quote?.equipment_total) }}</div>
      </div>
      <div class="fee">
        <div class="fee-label">安装调试费</div>
        <div class="fee-value num">{{ money(quote?.install_total) }}</div>
      </div>
      <div class="fee">
        <div class="fee-label">
          辅材费 ·
          <el-input-number
            v-model="rates.auxiliary_rate_pct"
            :min="0" :max="100" :precision="2" :controls="false" size="small"
            class="fee-input" :disabled="true" @change="onRateChange"
          />%
        </div>
        <div class="fee-value num">{{ money(quote?.auxiliary_total) }}</div>
      </div>
      <div class="fee">
        <div class="fee-label">
          设计费 ·
          <el-input-number
            v-model="rates.design_rate_pct"
            :min="0" :max="100" :precision="2" :controls="false" size="small"
            class="fee-input" :disabled="!canEdit" @change="onRateChange"
          />%
        </div>
        <div class="fee-value num">{{ money(quote?.design_fee) }}</div>
      </div>
      <div class="fee">
        <div class="fee-label">
          维保费 ·
          <el-input-number
            v-model="rates.warranty_rate_pct"
            :min="0" :max="100" :precision="2" :controls="false" size="small"
            class="fee-input" :disabled="!canEdit" @change="onRateChange"
          />%
        </div>
        <div class="fee-value num">{{ money(quote?.warranty_fee) }}</div>
      </div>
      <div class="fee">
        <div class="fee-label">
          培训费 ·
          <el-input-number
            v-model="rates.training_fee"
            :min="0" :precision="2" :controls="false" size="small"
            class="fee-input fee-input-wide" :disabled="!canEdit" @change="onRateChange"
          />元
        </div>
        <div class="fee-value num">{{ money(quote?.training_fee) }}</div>
      </div>
      <div class="fee">
        <div class="fee-label">
          管理费及利润 ·
          <el-input-number
            v-model="rates.mgmt_profit_rate_pct"
            :min="0" :max="100" :precision="2" :controls="false" size="small"
            class="fee-input" :disabled="true" @change="onRateChange"
          />%
        </div>
        <div class="fee-value num">{{ money(quote?.mgmt_profit) }}</div>
      </div>
      <div class="fee">
        <div class="fee-label">
          税金 ·
          <el-input-number
            v-model="rates.tax_rate_pct"
            :min="0" :max="100" :precision="2" :controls="false" size="small"
            class="fee-input" :disabled="!canEdit" @change="onRateChange"
          />%
        </div>
        <div class="fee-value num">{{ money(quote?.tax_amount) }}</div>
      </div>
      <div class="fee fee-grand">
        <div class="fee-label">报价合计（含税）</div>
        <div class="fee-value num grand">¥ {{ money(quote?.total_with_tax) }}</div>
      </div>

      <div class="fee-actions no-print">
        <el-button link type="primary" @click="goPreview">
          <el-icon><Document /></el-icon>&nbsp;客户版预览
        </el-button>
        <el-button link type="primary" @click="goLaborSetting">
          <el-icon><Coin /></el-icon>&nbsp;人工费设置
        </el-button>
        <el-button
          v-if="canEdit"
          type="primary"
          :loading="computing"
          :disabled="!ratesDirty"
          @click="saveAndCompute"
        >
          保存并重算{{ ratesDirty ? ' *' : '' }}
        </el-button>
        <!-- 导出入口已取消（2026-10-05 ✓）：Excel/PDF 统一在「客户版预览」里导出 ✓ -->
      </div>
    </div>

    <div class="wb-foot-hint text-muted">
      辅材费 = 基数 × {{ rates.auxiliary_rate_pct }}% ｜ 设计费 = 基数 × {{ rates.design_rate_pct }}% ｜
      维保费 = 设备费 × {{ rates.warranty_rate_pct }}% ｜ 管理费及利润 = 小计 × {{ rates.mgmt_profit_rate_pct }}% ｜
      税金 = 税前 × {{ rates.tax_rate_pct }}%~ ｜
      {{ auth.canSeeCost ? '成本口径 = 设备采购价 + 施工人工 + 辅材（仅主管与财务可见）' : '当前账号看不到成本视图' }}
      <span v-if="quote?.updated_at"> · 已自动保存 {{ dateTime(quote?.updated_at) }}</span>
    </div>

    <el-alert
      v-for="(w, i) in warnings"
      :key="i"
      class="mb"
      type="warning"
      show-icon
      :closable="false"
      :title="w"
    />

    <!-- 弹窗 -->
    <AddDeviceDialog
      v-model="pickerVisible"
      :subsystems="subsystemOptions"
      :default-subsystem-id="currentSubsystemId"
      @pick="onPick"
      @manual="onManualAdd"
    />

        <!-- 附件配单弹窗（2026-10-05 ✓）：只问"缺的那些" ✓，数据来自后端配单检查 ✓ -->
        <el-dialog v-model="accPickVisible" title="选择附件（主机下单常带授权/配件）" width="760px">
          <div v-if="!accMissing.length" class="text-muted">没有需要配的附件 ✓</div>
          <div v-for="(m, mi) in accMissing" :key="mi" class="acc-block">
            <div class="acc-head">
              <b>{{ m.item_name }}</b>
              <span class="text-muted">（主机数量 {{ m.quantity }}）</span>
              <el-tag v-if="m.is_required" type="danger" size="small" effect="plain">必选</el-tag>
              <span class="text-muted">{{ m.group_label }}</span>
            </div>
            <div>
              <el-checkbox
                v-for="o in m.options"
                :key="o.accessory_id"
                :model-value="accIsChosen(m.item_id, m.group_label, o.accessory_id)"
                @change="accToggle(m, o.accessory_id)"
              >
                {{ o.name }}
                <span class="text-muted">（{{ o.model || o.code }} · 参考价 {{ o.reference_price ?? '—' }}）</span>
              </el-checkbox>
            </div>
            <div style="margin-top: 6px">
              数量：
              <el-input-number
                v-model="accQty[accKey(m.item_id, m.group_label)]"
                :min="1"
                :precision="0"
                size="small"
              />
              <span class="text-muted" style="margin-left: 8px">默认按主机数量算 ✓，可改 ✓</span>
            </div>
          </div>
          <template #footer>
            <el-button @click="accPickVisible = false">稍后再说</el-button>
            <el-button type="primary" :loading="accLoading" :disabled="!canEdit" @click="confirmAccessories">
              确认加入附件
            </el-button>
          </template>
        </el-dialog>

      <!-- 报价条款弹窗（2026-10-05 用户反馈 ✓）：以前摊在页面底部**太长** ✗，
           低分辨率不友好 ✓ → 改成点按钮弹窗 ✓；主报价界面不再显示 ✓。
           不选条款 = 用系统默认条款 ✓（弹窗里选"继承系统设置条款"即可 ✓）。 -->
      <el-dialog
        v-model="clausesVisible"
        title="报价条款（客户版）"
        width="min(1320px, 94vw)"
        top="3vh"
        append-to-body
        class="clauses-dialog"
      >
        <div style="max-height: 78vh; overflow: auto; padding-right: 6px">
      <div class="card" style="margin-top: 12px">
        <div class="card-title">
          报价说明条款（客户版）
          <span class="text-muted">
            · 默认用「系统设置」里那份；也可以只给这一张单自定义
          </span>
        </div>

            <div v-if="standardClauses.length" class="mb">
              <div class="text-muted" style="margin-bottom: 4px">
                后台标准条款（**点一下即加入本单** ✓ / 再点移除 ✓；加入后会自动切到「本单自选条款」✓）：
              </div>
              <div>
                <el-tag
                  v-for="(c, i) in standardClauses"
                  :key="i"
                  :type="hasStandard(c) ? 'success' : 'info'"
                  :effect="hasStandard(c) ? 'dark' : 'plain'"
                  size="small"
                  style="cursor: pointer; margin: 0 6px 6px 0"
                  @click="toggleStandard(c)"
                >
                  {{ hasStandard(c) ? '✓ ' : '＋ ' }}{{ c.length > 34 ? c.slice(0, 34) + '…' : c }}
                </el-tag>
              </div>
            </div>
        <el-radio-group v-model="useOwnClauses" class="mb">
          <el-radio :value="false">用系统设置（所有单共用）</el-radio>
          <el-radio :value="true">本单自定义</el-radio>
        </el-radio-group>

        <template v-if="useOwnClauses">
          <el-form label-width="110px" class="mb">
            <el-form-item label="本单有效期">
              <div class="st-terms">
                <div class="st-term">
                  <span>有效期（天）</span>
                  <el-input-number v-model="termsDraft.valid_days" :min="1" :max="3650" :precision="0" size="small" />
                </div>
                <div class="st-term">
                  <span>预付比例（%）</span>
                  <el-input-number v-model="termsDraft.prepay_ratio" :min="0" :max="100" :precision="0" size="small" />
                </div>
                <div class="st-term">
                  <span>验收后付款（%）</span>
                  <el-input-number v-model="termsDraft.accept_ratio" :min="0" :max="100" :precision="0" size="small" />
                </div>
                <div class="st-term">
                  <span>质保金（%）</span>
                  <el-input-number v-model="termsDraft.warranty_ratio" :min="0" :max="100" :precision="0" size="small" />
                </div>
                <div class="st-term">
                  <span>质保期（月）</span>
                  <el-input-number v-model="termsDraft.warranty_months" :min="0" :max="120" :precision="0" size="small" />
                </div>
                <div class="st-term">
                  <span>工期（日历天）</span>
                  <el-input-number v-model="termsDraft.build_days" :min="1" :max="3650" :precision="0" size="small" />
                </div>
                <div class="text-muted" style="grid-column: 1 / -1; font-size: 12px">
                  ↑ 这些数字改动**只对本单生效** ✓；条款文字里仍写 <code>{valid_days}</code> 这样的变量 ✓，
                  导出时会自动替换成上面的值 ✓。
                </div>
              </div>
              <span class="text-muted ml">天</span>
            </el-form-item>
          </el-form>

          <div v-for="(_c, idx) in clausesDraft" :key="idx" class="st-clause">
            <span class="st-no">{{ idx + 1 }}.</span>
            <el-input v-model="clausesDraft[idx]" placeholder="条款文本，数字可用 {变量}" />
            <el-button link :disabled="idx === 0" @click="moveClause(idx, -1)">↑</el-button>
            <el-button link :disabled="idx === clausesDraft.length - 1"
              @click="moveClause(idx, 1)">↓</el-button>
            <el-button link type="danger" @click="clausesDraft.splice(idx, 1)">删</el-button>
          </div>
          <el-button size="small" @click="clausesDraft.push('')">+ 加一条</el-button>
          <div class="text-muted" style="margin-top: 6px">
            可用变量：
            <code v-for="(label, name) in clausesData?.available_variables ?? {}" :key="name"
              style="margin-right: 8px">{{ '{' + name + '}' }}={{ label }}</code>
            —— 变量会在生成 PDF/预览时替换成本单实际数字
          </div>
        </template>

        <div v-else-if="clausesData" class="text-muted">
          当前生效：有效期 {{ clausesData.valid_days }} 天，
          条款 {{ clausesData.clauses.length }} 条（在「系统设置 → 报价设置」里改）
        </div>

        <div v-if="clausesData" style="margin-top: 10px">
          <b>客户版预览（替换变量后的样子）</b>
          <ol class="clause-preview">
            <li v-for="(c, i) in clausesData.clauses" :key="i">{{ c }}</li>
          </ol>
        </div>

            <div class="mb" style="border-top: 1px dashed var(--el-border-color); padding-top: 8px">
              <div class="text-muted" style="margin-bottom: 4px">
                报价策略（本单生效 ✓，与条款一起在「保存条款设置」时提交 ✓）：
              </div>
              <div class="st-terms">
                <div class="st-term">
                  <span>计税方式</span>
                  <el-select v-model="taxMethodDraft" size="small" style="width: 150px">
                    <el-option label="一般计税 9%" value="general_9pct" />
                    <el-option label="简易计税 3%" value="simple_3pct" />
                  </el-select>
                </div>
                <div class="st-term">
                  <span>税率（%）</span>
                  <el-input-number v-model="taxRateDraft" :min="0" :max="100" :precision="0" size="small" />
                </div>
                <div class="st-term">
                  <span>人工费浮动（%）</span>
                  <el-input-number v-model="laborFloatDraft" :min="-50" :max="200" :precision="0" size="small" />
                </div>
              </div>
            </div>
            <!-- 公司抬头（2026-10-09）：有时要用**别家公司抬头**去报价 ✓。
                 一开始只做「公司名称」，用户随即补充「地址和联系电话和邮箱还是要修改一下的」
                 ⇒ 现在连同**地址/电话/邮箱**一起快照进本单 ✓（从抬头库里带过来，用户不必手填 ✓）。
                 税号 / Logo / 盖章位仍然不做 ✓。 -->
            <div class="mb" style="border-top: 1px dashed var(--el-border-color); padding-top: 8px">
              <div class="text-muted" style="margin-bottom: 4px">
                公司抬头（本单生效 ✓，与条款一起在「保存条款设置」时提交 ✓）：
              </div>
              <div class="st-terms">
                <div class="st-term">
                  <span>公司抬头</span>
                  <el-select
                    v-model="letterheadDraft"
                    size="small"
                    style="width: 300px"
                    :disabled="!canEdit"
                    placeholder="默认（系统设置）"
                    @change="onLetterheadChange"
                  >
                    <el-option label="默认（系统设置）" value="" />
                    <el-option v-for="n in letterheadOptions" :key="n" :label="n" :value="n" />
                  </el-select>
                </div>
                <!-- 选中抬头后**一并快照**进来的三项 ✓（用户不必手填 ✓）。
                     为空 = 这一项用「系统设置」的那一项 ✓（**逐字段**回落 ✓ 不是整组 ✗）。 -->
                <div class="st-term">
                  <span>地址 · 电话 · 邮箱</span>
                  <span class="text-muted" style="font-size: 12px">
                    {{ [letterheadContact.address, letterheadContact.phone, letterheadContact.email]
                        .filter(Boolean).join('　') || '（未填 → 用系统设置那一套）' }}
                  </span>
                </div>
                <div class="text-muted" style="grid-column: 1 / -1; font-size: 12px">
                  客户版 PDF / 内部版 PDF / Excel / 客户预览页的抬头**都取这一处** ✓。
                  本单记的是**当时选定那一项的名称/地址/电话/邮箱本身** ✓ —— 以后改/删抬头库，这张单
                  **不会**被追溯改写 ✓（历史单据绝不跟着变 ✓）。
                  <template v-if="!letterheads.length">
                    <br />抬头库还是空的：去「系统设置 → 公司信息」里加几个可选抬头 ✓。
                  </template>
                </div>
              </div>
            </div>
        <el-button type="primary" :loading="clausesSaving" :disabled="!canEdit" @click="saveClauses">保存条款设置</el-button>
        <el-button :disabled="!canEdit" @click="useOwnClauses = false; saveClauses()">恢复用系统设置</el-button>
        <div v-if="clausesData?.clauses_overridden || clausesData?.valid_days_overridden"
          class="text-muted" style="margin-top: 6px">
          本单已自定义（不影响其它报价单）
        </div>
      </div>
        
          <div class="clauses-preview">
            <div class="text-muted" style="margin-bottom: 4px">
              预览 —— 这张单发给客户 / 打印出来就是下面这几句 ✓（数字已按上面填的替换 ✓）：
            </div>
            <ol style="margin: 0; padding-left: 20px">
              <li v-for="(c, i) in clausesPreview" :key="i" style="margin-bottom: 4px">{{ c }}</li>
            </ol>
            <div v-if="!clausesPreview.length" class="text-muted">
              还没有条款 ✓ —— 可在上方点标准条款标签加入 ✓，或选「继承系统设置条款」用默认 ✓。
            </div>
          </div>
        </div>
        <template #footer>
          <el-button @click="clausesVisible = false">取消</el-button>
          <el-button type="primary" :loading="clausesSaving" :disabled="!canEdit" @click="saveClauses">
            保存（落到打印 / 预览 ✓）
          </el-button>
        </template>
      </el-dialog>

    <ApplyPointTemplateDialog
      v-model="applyTemplateVisible"
      :quote-id="quoteId"
      :subsystems="subsystemOptions"
      :default-subsystem-id="currentSubsystemId"
      @applied="reload"
    />
    <InquiryImportDialog
      v-model="inquiryVisible"
      :quote-id="quoteId"
      :subsystems="subsystemOptions"
      @done="reload"
    />
    <QuoteItemDialog
      v-model="itemDialogVisible"
      :quote-id="quoteId"
      :item="editingItem"
      :subsystems="subsystemOptions"
      :readonly="!canEdit"
      @saved="reload"
    />
    <el-drawer v-model="auditVisible" title="操作记录" size="520px">
      <el-table :data="auditLogs" size="small" border>
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ dateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="动作" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.action }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="150">
          <template #default="{ row }">
            <div class="mono">{{ row.ip || '—' }}</div>
            <div class="text-muted mono audit-ua">{{ row.user_agent || '' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="内容" min-width="200">
          <template #default="{ row }">
            <span class="mono">{{ JSON.stringify(row.payload) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
    <!-- 版本历史与对比（2026-10-04）：提交/批准各落一版，可比"这版和上版差在哪" -->
    <el-drawer v-model="revDrawer" title="版本历史" size="70%">
      <el-form inline class="mb" @submit.prevent>
        <el-form-item>
          <el-button type="primary" plain :disabled="!canEdit" @click="saveRevisionNow">
            <el-icon><Plus /></el-icon>&nbsp;保存当前版本
          </el-button>
        </el-form-item>
        <el-form-item label="对比">
          <!-- 版本就按**时间标记**称呼 ✓（2026-10-08 用户要求：不再出现「第 N 版」✗）。
               seq 仍作为下拉的 value 传给 diff 接口 ✓（内部排序/追溯用 ✓）。 -->
          <el-select v-model="revFrom" style="width: 210px" placeholder="较早的版本">
            <el-option
              v-for="r in revisions"
              :key="`f-${r.seq}`"
              :value="r.seq"
              :label="`${dateTimeSec(r.created_at)} · ${REASON_LABEL[r.reason] ?? r.reason}`"
            />
          </el-select>
          <span style="margin: 0 6px">→</span>
          <el-select v-model="revTo" style="width: 210px" placeholder="较晚的版本">
            <el-option
              v-for="r in revisions"
              :key="`t-${r.seq}`"
              :value="r.seq"
              :label="`${dateTimeSec(r.created_at)} · ${REASON_LABEL[r.reason] ?? r.reason}`"
            />
          </el-select>
          <el-button type="primary" style="margin-left: 8px" @click="runDiff">对比</el-button>
        </el-form-item>
      </el-form>

      <el-table v-loading="revLoading" :data="revisions" border size="small" max-height="240">
        <!-- 「版本」列原来显示「第 N 版」✗ —— 用户要的是**纯时间标记** ✓，
             而时间这一列本来就有 ✓，所以这里整列去掉（seq 仍存在于数据里 ✓，
             下拉选择器与两版对比都用它 ✓）。 -->
        <el-table-column label="来源" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">
              {{ REASON_LABEL[row.reason] ?? row.reason }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" />
        <el-table-column prop="item_count" label="行数" width="70" />
        <el-table-column label="含税合计" width="130" align="right">
          <template #default="{ row }">
            <span class="num">{{ money(row.totals?.total_with_tax) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="140" show-overflow-tooltip />
        <el-table-column prop="created_by_name" label="操作人" width="100" />
        <!-- 版本标记精确到秒 ✓：同一分钟内保存两次也要能分清哪版是哪版 ✓ -->
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ row.created_at ? dateTimeSec(row.created_at) : '—' }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有版本快照（提交或批准时会自动保存）" :image-size="60" />
        </template>
      </el-table>

      <template v-if="revDiff">
        <!-- 对比标题也用**时间标记**称呼这两版 ✓（不再写「第 N 版」✗） -->
        <div class="card-title" style="margin-top: 14px">
          {{ dateTimeSec(revDiff.from.created_at) }}
          <span class="text-muted">
            （{{ REASON_LABEL[revDiff.from.reason] ?? revDiff.from.reason }}）</span>
          → {{ dateTimeSec(revDiff.to.created_at) }}
          <span class="text-muted">
            （{{ REASON_LABEL[revDiff.to.reason] ?? revDiff.to.reason }}）</span>
        </div>

        <el-alert
          v-if="!revDiff.has_changes && !revDiff.totals.length"
          type="success"
          :closable="false"
          show-icon
          title="两版内容完全一致"
        />

        <template v-if="revDiff.totals.length">
          <div class="mb"><b>金额变化</b></div>
          <el-table :data="revDiff.totals" border size="small">
            <el-table-column prop="field" label="项目" width="180" />
            <el-table-column label="旧" width="130" align="right">
              <template #default="{ row }"><span class="num">{{ money(row.from) }}</span></template>
            </el-table-column>
            <el-table-column label="新" width="130" align="right">
              <template #default="{ row }"><span class="num">{{ money(row.to) }}</span></template>
            </el-table-column>
            <el-table-column label="差" align="right">
              <template #default="{ row }">
                <span class="num" :class="Number(row.delta) >= 0 ? 'ok-text' : 'danger-text'">
                  {{ Number(row.delta) >= 0 ? '+' : '' }}{{ money(row.delta) }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </template>

        <template v-if="revDiff.items_added.length || revDiff.items_removed.length">
          <div class="mb" style="margin-top: 12px"><b>明细增删</b></div>
          <el-table
            :data="[
              ...revDiff.items_added.map((x) => ({ ...x, kind: '新增' })),
              ...revDiff.items_removed.map((x) => ({ ...x, kind: '删除' })),
            ]"
            border
            size="small"
          >
            <el-table-column prop="kind" label="动作" width="80" />
            <el-table-column prop="name" label="名称" min-width="180" />
            <el-table-column prop="model" label="型号" min-width="140" />
            <el-table-column prop="quantity" label="数量" width="90" align="right" />
            <el-table-column label="单价" width="110" align="right">
              <template #default="{ row }"><span class="num">{{ money(row.unit_price) }}</span></template>
            </el-table-column>
          </el-table>
        </template>

        <template v-if="revDiff.items_changed.length">
          <div class="mb" style="margin-top: 12px"><b>明细改动</b></div>
          <el-table :data="revDiff.items_changed" border size="small">
            <el-table-column prop="name" label="名称" min-width="180" />
            <el-table-column prop="model" label="型号" min-width="130" />
            <el-table-column label="变化" min-width="260">
              <template #default="{ row }">
                <div v-for="f in row.fields" :key="f.field" class="mono">
                  {{ f.field }}：{{ f.from }} → {{ f.to }}
                </div>
              </template>
            </el-table-column>
          </el-table>
        </template>

        <template v-if="revDiff.header.length">
          
      <!-- 报价说明条款（2026-10-05）：客户版 PDF 与预览印的就是这些 -->

<div class="mb" style="margin-top: 12px"><b>费率/口径改动</b></div>
          <el-table :data="revDiff.header" border size="small">
            <el-table-column prop="field" label="字段" width="200" />
            <el-table-column prop="from" label="旧" />
            <el-table-column prop="to" label="新" />
          </el-table>
        </template>
      </template>
    </el-drawer>


    <!-- 分级审批（2026-10-04）：开启后"批准"要走完 N 级；这里显示进度与意见 -->
    <el-card
      v-if="approval && approval.enabled && canApprove"
      class="card no-print"
      shadow="never"
    >
      <div class="card-title">
        审批链 · 已批 {{ approval.approved_levels }}/{{ approval.required_levels }} 级
      </div>
      <el-table :data="approval.records" size="small" border max-height="180">
        <el-table-column prop="level" label="级" width="60" />
        <el-table-column label="结果" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="row.decision === 'approved' ? 'success' : 'danger'">
              {{ row.decision === 'approved' ? '通过' : '驳回' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="comment" label="意见" min-width="200" show-overflow-tooltip />
        <el-table-column prop="approver_name" label="审批人" width="110" />
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ row.created_at ? dateTime(row.created_at) : '—' }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有审批记录" :image-size="50" />
        </template>
      </el-table>
      <div class="mt">
        <el-button type="success" @click="decide('approved')">
          <el-icon><Select /></el-icon>&nbsp;通过本级
        </el-button>
        <el-button type="danger" plain @click="decide('rejected')">
          <el-icon><CloseBold /></el-icon>&nbsp;驳回
        </el-button>
        <span class="text-muted" style="margin-left: 8px">
          含税合计需 {{ approval.required_levels }} 级；批满后自动置为「已批准」
        </span>
      </div>
    </el-card>


    <!-- 定额选择器（2026-10-05）：点明细行的「定额」列打开 ✓ -->
    <QuotaPickerDialog v-model="quotaPickVisible" @pick="onQuotaPicked" />
  </template>

<style scoped>
/* 条款预览（2026-10-05）：浅底 + 左边条，一眼区分"编辑区"与"将会打印的样子" ✓ */
.clauses-preview { margin-top: 12px; padding: 10px 12px; border-left: 3px solid var(--el-color-primary);
  background: var(--el-fill-color-lighter); border-radius: 4px; font-size: 13px; }

/* 报价条款弹窗（2026-10-05）：内容再长也不撑页面 ✓ */
/* 1440×900 下给足宽度 ✓：条款文本单行铺满，不再是一条窄缝 ✓ */
.clauses-dialog .st-clause { gap: 8px; }

.clauses-dialog .el-dialog__body { padding-top: 8px; }

.acc-block { border: 1px solid var(--el-border-color-lighter); border-radius: 6px;
  padding: 8px 10px; margin-bottom: 10px; }
.acc-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }

.st-terms { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px 12px; }
.st-term { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.st-term > span:first-child { width: 96px; color: var(--el-text-color-secondary); }

.wb {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* ② 项目信息条 */
.wb-info {
  display: flex;
  align-items: flex-start;
  gap: 18px;
  flex-wrap: wrap;
}

.wi-left {
  min-width: 240px;
}

.wi-title {
  font-size: 16px;
  font-weight: 700;
}

.wi-sub {
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
  margin-top: 2px;
}

.wi-grid {
  flex: 1 1 620px;
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 4px 18px;
}

.wi-grid .k {
  display: block;
  color: var(--el-text-color-secondary);
  font-size: 11.5px;
}

.wi-grid .v {
  display: inline-block;
  font-size: 13px;
}

.wi-no .v {
  margin-right: 6px;
}

.wi-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

/* ③ 工具栏 */
.wb-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 12px;
}

.toolbar-stat {
  font-size: 12.5px;
  margin-right: 4px;
}

/* 报价模式只读彩色标识（提醒用 ✓） */
.mode-badge-wrap {
  display: inline-flex;
  align-items: center;
  cursor: help;
}

.mode-badge {
  font-weight: 600;
  letter-spacing: 0.5px;
}

/* ④ 两栏 */
.wb-body {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.wb-left {
  width: 268px;
  flex: 0 0 268px;
  padding: 10px 12px;
}

.wl-head {
  display: flex;
  justify-content: space-between;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.wl-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 6px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}

.wl-row:hover {
  background: var(--el-fill-color-light);
}

.wl-row.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.wl-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wl-div {
  margin: 10px 0 6px;
}

.wl-kv {
  display: flex;
  justify-content: space-between;
  font-size: 12.5px;
  padding: 3px 0;
}

.wl-total {
  margin-top: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--el-color-primary-light-9);
}

.wl-total-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.wl-total-value {
  font-size: 19px;
  font-weight: 700;
  color: var(--el-color-primary);
}

.wl-total-sub {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}

/* ④ 右栏 */
.wb-right {
  flex: 1 1 auto;
  min-width: 0;
  padding: 10px 12px;
}

.wr-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.wr-title {
  font-weight: 600;
  font-size: 13.5px;
}

.wr-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  font-size: 12.5px;
}

.wr-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.chip {
  padding: 3px 10px;
  border: 1px solid var(--el-border-color);
  border-radius: 12px;
  background: #fff;
  color: var(--el-text-color-regular);
  font-size: 12.5px;
  cursor: pointer;
}

.chip:hover {
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
}

.chip.active {
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
  font-weight: 600;
}

.row-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 2px;
}

/* ⑥ 费用汇总 */
.wb-fees {
  display: flex;
  align-items: stretch;
  gap: 0;
  flex-wrap: wrap;
  padding: 10px 12px;
}

.fee {
  flex: 1 1 118px;
  min-width: 118px;
  padding: 0 10px;
  border-right: 1px dashed var(--el-border-color-lighter);
}

.fee:last-of-type {
  border-right: none;
}

.fee-label {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  display: flex;
  align-items: center;
  gap: 2px;
}

.fee-value {
  font-size: 14.5px;
  font-weight: 600;
  margin-top: 3px;
}

.fee-input {
  width: 54px;
}

.fee-input-wide {
  width: 74px;
}

.fee-grand .fee-value.grand {
  font-size: 18px;
  color: var(--el-color-primary);
}

.fee-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  padding-left: 12px;
}

.audit-ua {
  font-size: 10.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 150px;
}

.wb-foot-hint {
  font-size: 11.5px;
  line-height: 1.7;
  padding: 0 2px 6px;
}

/* 主行安装单价格子下面那行小字（定额子目 N 条合计 ✓）—— 说明这个数的出处 ✓ */
.cell-note {
  font-size: 10.5px;
  line-height: 1.4;
  text-align: right;
}

.clause-preview {
  margin: 6px 0 10px 18px;
  color: #555;
  font-size: 13px;
  line-height: 1.7;
}
.st-clause {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 6px;
}
.st-no {
  color: #909399;
  min-width: 18px;
}
</style>









