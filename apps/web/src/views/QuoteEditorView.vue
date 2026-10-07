<script setup lang="ts">
/**
 * 报价编制主界面（对应原型 Page 1）。
 *
 * 数据流：所有写操作都收敛到「改完 → reload()」，
 * 因为后端每次增删改都会重算费用瀑布并回写子系统汇总，
 * 前端自己维护增量状态极易与后端不一致（这类不一致在报价场景里就是钱算错）。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { deviceApi, projectApi, quoteApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type {
  AccessoryCheck,
  MissingAccessory,
  AuditLog,
  Device,
  DeviceStatus,
  Project,
  QuoteCost,
  QuoteDetail,
  QuoteItem,
  Subsystem,
  SubsystemSummary,
} from '@/api/types'
import DevicePickerDialog from '@/components/DevicePickerDialog.vue'
import InquiryImportDialog from '@/components/InquiryImportDialog.vue'
import QuoteItemDialog from '@/components/QuoteItemDialog.vue'
import QuoteTotalPanel from '@/components/QuoteTotalPanel.vue'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'
import {
  dateTime,
  money,
  percentInputToRatio,
  ratioToPercentInput,
  toNum,
} from '@/utils/format'
import { downloadBlob } from '@/utils/download'

/** 替代型号推荐行的形状（来自 /devices/{id}/replacements 的候选）。 */
type SuggestRow = {
  id: string
  code: string
  name: string
  brand: string | null
  model: string | null
  unit: string
  reference_price: number
  install_unit_price: number
  status: DeviceStatus
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const projectId = computed(() => String(route.params.projectId))
const quoteId = computed(() => String(route.params.quoteId))

const loading = ref(false)
const savingRates = ref(false)
const computing = ref(false)
const loadError = ref('')

const project = ref<Project | null>(null)
const quote = ref<QuoteDetail | null>(null)
const subsystems = ref<Subsystem[]>([])
const cost = ref<QuoteCost | null>(null)
const warnings = ref<string[]>([])

const activeTab = ref('items')

// ----- 明细行筛选 -----
const itemFilter = reactive({
  keyword: '',
  subsystem: '' as string,
  onlyPending: false,
})

// ----- 费率表单（界面按 % 录入，提交前转回比率）-----
type RateForm = {
  tax_method: string
  tax_rate_pct: number
  fee_base: string
  auxiliary_base: string
  auxiliary_rate_pct: number
  design_rate_pct: number
  warranty_rate_pct: number
  training_fee: number
  mgmt_profit_rate_pct: number
  labor_float_rate_pct: number
  float_a_pct: number
  float_b_pct: number
  float_c_pct: number
  notes: string
}

const rates = reactive<RateForm>({
  tax_method: 'general_9pct',
  tax_rate_pct: 9,
  fee_base: 'device_aux_install',
  auxiliary_base: 'device_install',
  auxiliary_rate_pct: 6,
  design_rate_pct: 2,
  warranty_rate_pct: 3,
  training_fee: 0,
  mgmt_profit_rate_pct: 8,
  labor_float_rate_pct: 0,
  float_a_pct: 0,
  float_b_pct: 0,
  float_c_pct: 0,
  notes: '',
})

const ratePanelOpen = ref(false)

/**
 * 费率面板有没有改过但还没保存。
 *
 * reload() 会在加行/换型/删行/导入/重算等 9 个地方被调用，每次都用服务端值
 * 覆盖 rates。用户改了税率还没点「保存」时，只要顺手加一行，改动就被静默抹掉，
 * 界面上看不出任何异常（数值悄悄变回去了）。所以脏了就不要再覆盖。
 */
const ratesDirty = ref(false)

/** syncRatesFromQuote 正在回填时为 true，用来抑制下面的 watcher。 */
let syncingRates = false

// 用 flush:'sync' 的深度 watcher 统一标记「改过没保存」，
// 比给 15 个输入框逐个挂 @change 更不容易漏（以后加字段自动生效）。
// sync 是必须的：回填时要在 Object.assign 期间同步抑制，不能等下一帧。
watch(
  rates,
  () => {
    if (syncingRates) return
    ratesDirty.value = true
  },
  { deep: true, flush: 'sync' },
)

function syncRatesFromQuote(q: QuoteDetail): void {
  const per = q.labor_float_per_class ?? {}
  syncingRates = true
  Object.assign(rates, {
    tax_method: q.tax_method,
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
    notes: q.notes ?? '',
  })
  syncingRates = false
  // 用服务端值填充 = 面板与服务端一致，脏标记清掉
  ratesDirty.value = false
}

/** 组装 compute/PATCH 的费率载荷。 */
function ratePayload(): Record<string, unknown> {
  return {
    // 乐观锁：带上当前读到的版本号。对不上后端返回 409（别人改过），
    // 由调用处提示用户刷新，而不是把这边的费率静默盖到别人的修改上。
    version: quote.value?.version,
    tax_method: rates.tax_method,
    tax_rate: percentInputToRatio(rates.tax_rate_pct),
    fee_base: rates.fee_base,
    auxiliary_base: rates.auxiliary_base,
    auxiliary_rate: percentInputToRatio(rates.auxiliary_rate_pct),
    design_rate: percentInputToRatio(rates.design_rate_pct),
    warranty_rate: percentInputToRatio(rates.warranty_rate_pct),
    training_fee: rates.training_fee,
    mgmt_profit_rate: percentInputToRatio(rates.mgmt_profit_rate_pct),
    labor_float_rate: percentInputToRatio(rates.labor_float_rate_pct),
    labor_float_per_class: {
      class_a: percentInputToRatio(rates.float_a_pct),
      class_b: percentInputToRatio(rates.float_b_pct),
      class_c: percentInputToRatio(rates.float_c_pct),
    },
    notes: rates.notes || null,
  }
}

// ----- 加载 -----

// ---------------------------------------------------------------------------
// 配件 / 授权提示（2026-10-05）
// ---------------------------------------------------------------------------
// 用户需求："在销售配单的时候也要提示需要添加授权"。
// 做成**常驻检查**而不是"只在加明细时弹一次"：因为可能是先加了防火墙、
// 后来才想起要配授权，或者这张单子是功能上线前建的。
const accCheck = ref<AccessoryCheck | null>(null)
const accDialog = ref(false)
const accTarget = ref<MissingAccessory | null>(null)
/** 弹窗里选中的候选（单选存一个 id，多选存多个） */
const accChoice = ref<string>('')
const accChoiceMany = ref<string[]>([])
const accAdding = ref(false)

async function loadAccessoryCheck(): Promise<void> {
  try {
    accCheck.value = await quoteApi.accessoryCheck(quoteId.value)
  } catch {
    accCheck.value = null      // 检查失败不该影响报价编辑
  }
}

function openAccDialog(m: MissingAccessory): void {
  accTarget.value = m
  const def = m.options.find((o) => o.is_default) ?? m.options[0]
  accChoice.value = def?.accessory_id ?? ''
  accChoiceMany.value = def ? [def.accessory_id] : []
  accDialog.value = true
}

/** 要加的数量：随主设备数量（per_unit）或固定数量 */
function accQuantity(m: MissingAccessory): number {
  const base = Number(m.default_quantity ?? 1) || 1
  if (m.quantity_mode === 'fixed') return base
  return (Number(m.quantity) || 1) * base
}

async function confirmAcc(): Promise<void> {
  const m = accTarget.value
  if (!m) return
  const ids = m.select_mode === 'many' ? accChoiceMany.value : [accChoice.value]
  const picked = m.options.filter((o) => ids.includes(o.accessory_id))
  if (!picked.length) {
    ElMessage.warning('请先选择要添加的配件')
    return
  }
  accAdding.value = true
  try {
    for (const o of picked) {
      await quoteApi.addItem(quoteId.value, {
        device_id: o.accessory_id,
        name: o.name,
        brand: o.brand ?? undefined,
        model: o.model ?? undefined,
        unit: o.unit,
        quantity: accQuantity(m),
        unit_price: o.reference_price ?? 0,
        parent_item_id: m.item_id,      // 记下"这是那台设备的配件"
      })
    }
    ElMessage.success(`已添加 ${picked.length} 个配件`)
    accDialog.value = false
    await reload()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '添加配件失败')
  } finally {
    accAdding.value = false
  }
}

async function reload(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [p, q, subs] = await Promise.all([
      projectApi.get(projectId.value),
      quoteApi.get(quoteId.value),
      projectApi.subsystems(projectId.value),
    ])
    project.value = p
    quote.value = q
    subsystems.value = subs
    // 面板有未保存的改动时不要覆盖：加一行、删一行都会走到这里，
    // 静默丢弃用户刚填的费率是说不清的（数字悄悄变回去）。
    if (!ratesDirty.value) {
      syncRatesFromQuote(q)
    }

    // 成本视图单独取，且失败不报错（sales/viewer 本来就没权限）
    if (auth.canSeeCost) {
      try {
        cost.value = await quoteApi.cost(quoteId.value)
      } catch {
        cost.value = null
      }
    } else {
      cost.value = null
    }
  } catch (err) {
    loadError.value = extractErrorMessage(err)
  } finally {
    loading.value = false
    // 明细变化后重查配件（新增/删除都可能让提示出现或消失）
    void loadAccessoryCheck()
  }
}

onMounted(async () => {
  await Promise.all([meta.load(), reload()])
})

/** 有效费率载荷：后端会校验，前端先做一次非负检查，避免把负数写进库。 */
function validateRates(): boolean {
  const numeric: [string, number][] = [
    ['税率', rates.tax_rate_pct],
    ['辅材费率', rates.auxiliary_rate_pct],
    ['设计费率', rates.design_rate_pct],
    ['质保费率', rates.warranty_rate_pct],
    ['管理费+利润率', rates.mgmt_profit_rate_pct],
    ['培训费', rates.training_fee],
  ]
  for (const [name, v] of numeric) {
    if (v < 0) {
      ElMessage.warning(`${name}不能为负数`)
      return false
    }
  }
  if (rates.tax_rate_pct > 100) {
    ElMessage.warning('税率看起来超过了 100%，请确认填的是百分数')
    return false
  }
  return true
}

/** 保存费率并重算。 */
async function saveAndCompute(): Promise<void> {
  if (!validateRates()) return
  computing.value = true
  try {
    const res = await quoteApi.compute(quoteId.value, ratePayload())
    warnings.value = res.warnings ?? []
    // 重算也把面板值写进了服务端，脏标记清掉
    ratesDirty.value = false
    ElMessage.success('已按当前费率重算')
    await reload()
  } finally {
    computing.value = false
  }
}

/** 只保存费率（不重算）——后端 PATCH 本身也会触发重算，这里保持一致语义。 */
async function saveRatesOnly(): Promise<void> {
  if (!validateRates()) return
  savingRates.value = true
  try {
    await quoteApi.update(quoteId.value, ratePayload())
    // 保存成功 → 面板与服务端一致，先清脏标记，reload 才会用服务端值回填
    ratesDirty.value = false
    ElMessage.success('费率已保存')
    await reload()
  } finally {
    savingRates.value = false
  }
}

// ----- 明细行 -----

const subsystemName = computed(() => {
  const m = new Map<string, string>()
  for (const s of subsystems.value) {
    m.set(s.id, s.custom_name || meta.label('subsystem_type', s.subsystem_type))
  }
  return m
})

const pickerSubsystems = computed(() =>
  subsystems.value.map((s) => ({
    id: s.id,
    name: s.custom_name || meta.label('subsystem_type', s.subsystem_type),
  })),
)

const filteredItems = computed(() => {
  let list = quote.value?.items ?? []
  const kw = itemFilter.keyword.trim().toLowerCase()
  if (kw) {
    list = list.filter((i) =>
      [i.name, i.brand, i.model, i.device_code]
        .filter(Boolean)
        .some((v) => String(v).toLowerCase().includes(kw)),
    )
  }
  if (itemFilter.subsystem === '__none__') {
    list = list.filter((i) => !i.subsystem_id)
  } else if (itemFilter.subsystem) {
    list = list.filter((i) => i.subsystem_id === itemFilter.subsystem)
  }
  if (itemFilter.onlyPending) {
    list = list.filter((i) => toNum(i.unit_price) === 0)
  }
  return list
})

const pendingCount = computed(
  () => (quote.value?.items ?? []).filter((i) => toNum(i.unit_price) === 0).length,
)
const discontinuedCount = computed(
  () => (quote.value?.items ?? []).filter((i) => isItemDiscontinued(i)).length,
)

function isItemDiscontinued(item: QuoteItem): boolean {
  if (item.is_replaced) return false
  return item.device_status ? meta.isDiscontinued(item.device_status) : false
}

function isManual(item: QuoteItem): boolean {
  return item.source === 'manual'
}

function rowClassName({ row }: { row: QuoteItem }): string {
  if (isManual(row)) return 'row-manual'
  if (isItemDiscontinued(row)) return 'row-discontinued'
  return ''
}

// ----- 设备选择 / 手动录入 -----

const pickerVisible = ref(false)
const pickerMode = ref<'add' | 'replace'>('add')
const replacingItem = ref<QuoteItem | null>(null)

function openPicker(): void {
  pickerMode.value = 'add'
  replacingItem.value = null
  pickerVisible.value = true
}

function openReplace(item: QuoteItem): void {
  pickerMode.value = 'replace'
  replacingItem.value = item
  pickerVisible.value = true
}

async function onPick(payload: {
  devices: Device[]
  subsystemId: string | null
}): Promise<void> {
  if (pickerMode.value === 'replace') {
    const item = replacingItem.value
    const device = payload.devices[0]
    if (!item || !device) return
    await quoteApi.replaceItemDevice(quoteId.value, item.id, device.id)
    ElMessage.success(`已替换为 ${device.name}`)
    await reload()
    return
  }

  // add 模式可能一次勾选多个设备：全部加完再 reload 一次。
  // 逐个 add + reload 会并发打架，界面可能停在中间状态。
  let added = 0
  let failed = 0
  try {
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
        // 单个失败不要中断整批（比如某一行被后端拒了），
        // 但也要如实告诉用户加了几行、失败几行，不能只说「成功」。
        failed += 1
      }
    }
  } finally {
    // 无论成功失败都要刷新：否则已经写进去的行在界面上看不到。
    await reload()
  }

  if (failed === 0) {
    ElMessage.success(
      added > 1 ? `已加入 ${added} 个设备` : `已加入 ${payload.devices[0]?.name ?? ''}`,
    )
  } else if (added === 0) {
    ElMessage.error(`${failed} 个设备都未能加入，请检查后重试`)
  } else {
    ElMessage.warning(`已加入 ${added} 个，${failed} 个失败（详见接口返回）`)
  }
}

/** 明细表内直接改数量：PATCH 后整单重算并刷新。 */
async function changeQuantity(row: QuoteItem, value: number | undefined): Promise<void> {
  if (value === undefined || !Number.isFinite(value) || value < 0) return
  try {
    await quoteApi.updateItem(quoteId.value, row.id, { quantity: value })
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    await reload()
  }
}

const itemDialogVisible = ref(false)
const editingItem = ref<QuoteItem | null>(null)

function openAddManual(): void {
  editingItem.value = null
  itemDialogVisible.value = true
}

function openEditItem(item: QuoteItem): void {
  editingItem.value = item
  itemDialogVisible.value = true
}

async function removeItem(item: QuoteItem): Promise<void> {
  await ElMessageBox.confirm(`确认删除明细行「${item.name}」？`, '提示', {
    confirmButtonText: '删除',
    cancelButtonText: '取消',
    type: 'warning',
  })
    .then(async () => {
      await quoteApi.removeItem(quoteId.value, item.id)
      ElMessage.success('已删除')
      await reload()
    })
    .catch(() => undefined)
}

// ----- 客户版汇总 -----

const summary = ref<SubsystemSummary | null>(null)
const summaryLoading = ref(false)

async function loadSummary(): Promise<void> {
  summaryLoading.value = true
  try {
    summary.value = await quoteApi.subsystemSummary(quoteId.value)
  } finally {
    summaryLoading.value = false
  }
}

// ----- 操作记录 -----

const auditLogs = ref<AuditLog[]>([])
const auditLoading = ref(false)

/** 操作记录只对 admin/finance 开放（audit_log.read）；不拦的话其他角色
 *  每次切到这个页签都会吃一个 403 警告弹窗。 */
const canSeeAudit = computed(() => auth.can('audit_log', 'read'))

/**
 * 导出。
 *
 * 注意 `quote.export` 与「能不能看成本」是两件事：sales 有导出权限但**没有**
 * 成本权限，所以给他们的菜单里只有两个客户版选项；后端也会再校验一次，
 * 就算有人手改前端参数去要内部版，也会拿到 403。
 *
 * 后端 `?internal=true` 的默认值是 false（客户版），因此这里只有显式选了
 * 内部版才传 true —— 默认路径永远不泄露成本。
 */
const canExport = computed(() => auth.can('quote', 'export'))
const exporting = ref(false)

// ---------------------------------------------------------------------------
// 报价单状态流转（2026-10-02）
//
// 之前后端有完整的审批规则，但**界面上没有任何入口** —— 状态只能靠接口改，
// 「销售提交 → 经理审批」这条链在浏览器里走不通。这里按权限把它接出来：
//
//   · 提交审核 / 重新提交：需要 quote.update（销售、设计）
//   · 批准 / 驳回 / 退回草稿 / 归档：需要 quote.approve（经理、管理员）
//   · 已批准 / 已归档的单据：内容编辑入口全部禁用（后端也会 409），
//     要改必须先「退回草稿」
//
// 刻意做成**明确的动作按钮**而不是一个自由下拉：状态在业务上是有方向的
// （草稿→提交→批准/驳回），给一个能随便跳的下拉只会让人点出无意义的状态。
// ---------------------------------------------------------------------------

const canApprove = computed(() => auth.can('quote', 'approve'))
const statusChanging = ref(false)

const quoteStatus = computed(() => String(quote.value?.status ?? 'draft'))

/** 已批准/已归档 = 锁定：内容不可改，先退回草稿。 */
const locked = computed(() => ['approved', 'archived'].includes(quoteStatus.value))

/**
 * 能否编辑报价内容 = 有编制权限 且 单据未锁定。
 * 锁定时把入口禁用（而不是让用户点完再吃一个 409）：后端会返回
 * 「已批准不允许直接修改，请先退回草稿」，但让人先看到禁用状态更省事。
 */
const canEdit = computed(() => auth.canWrite && !locked.value)

/**
 * 已提交 = 等人接手；审核中 = 已接手。**只有「审核中」能批准/驳回**
 * （业务要求：「审核中」必须是必经步骤，否则它就是个没强制力的装饰）。
 */
const waitingClaim = computed(() => quoteStatus.value === 'submitted')
const inReview = computed(() => quoteStatus.value === 'reviewing')

/** 可提交（草稿或已驳回，有编制权限就能提交/重新提交）。 */
const canSubmit = computed(
  () => auth.canWrite && ['draft', 'rejected'].includes(quoteStatus.value),
)
/** 可批准/驳回：单据已被接手（审核中），且当前用户有审批权。 */
const canDecide = computed(() => canApprove.value && inReview.value)
/** 可开始审核：已提交且还没人接手 —— 这是批准/驳回的**前置步骤**。 */
const canStartReview = computed(() => canApprove.value && waitingClaim.value)
/**
 * 审批人看着一张草稿/驳回单：不是"没按钮"，而是"还不能批" —— 明确说一句，
 * 免得他以为界面坏了（后端也会 409 并说明要先提交）。
 */
const needsSubmitFirst = computed(
  () => canApprove.value && ['draft', 'rejected'].includes(quoteStatus.value),
)

function onMoreCommand(cmd: string): void {
  if (cmd === 'reviewing') void changeStatus('reviewing', '开始审核')
  else if (cmd === 'draft') void onRevertDraft()
  else if (cmd === 'archive') void onArchive()
}

async function changeStatus(next: string, label: string, confirmText?: string): Promise<void> {
  if (!quote.value) return
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
    // 带上 version：这是一次有业务含义的写入，不该被别人静默覆盖
    await quoteApi.update(quote.value.id, {
      status: next,
      version: quote.value.version,
    })
    ElMessage.success(`已${label}`)
    await reload()
    await loadSummary()
  } finally {
    statusChanging.value = false
  }
}

const onSubmitReview = () =>
  changeStatus('submitted', '提交审核', '提交后进入待审批状态，确认提交？')
const onStartReview = () =>
  changeStatus(
    'reviewing',
    '开始审核',
    '接手后单据进入「审核中」，之后由你批准或驳回（这一步是批准的前置条件）。确认接手？',
  )
const onReject = () =>
  changeStatus('rejected', '驳回', '驳回后销售/设计需要修改并重新提交，确认驳回？')
const onRevertDraft = () =>
  changeStatus('draft', '退回草稿', '退回草稿后这张单可以继续修改（批准状态将被清除），确认退回？')
const onApprove = () =>
  changeStatus(
    'approved',
    '批准',
    '批准后这张报价单的内容将被锁定，如需修改必须先退回草稿。确认批准？',
  )
const onArchive = () =>
  changeStatus('archived', '归档', '归档后单据锁定且不再出现在常规列表里，确认归档？')

async function onExport(command: string): Promise<void> {
  if (!quote.value) return
  const [format, edition] = command.split(':') as ['xlsx' | 'pdf', 'customer' | 'internal']
  exporting.value = true
  try {
    const { blob, filename } = await quoteApi.exportFile(
      quote.value.id,
      format,
      edition === 'internal',
    )
    downloadBlob(blob, filename)
    ElMessage.success(
      edition === 'internal' ? `已导出内部版：${filename}` : `已导出：${filename}`,
    )
  } catch {
    // 失败原因由 axios 拦截器统一弹出（403 会说清是权限问题），
    // 这里不再重复弹，避免一次失败弹两个框。
  } finally {
    exporting.value = false
  }
}

/**
 * 打印。
 *
 * 不调任何额外库，直接 window.print() 触发浏览器原生打印对话框。
 * 视觉由 styles/print.css 里的 @media print 块承担：隐藏侧栏/顶栏/按钮，
 * 强制 el-table 取消高度裁切，表头跨页重复、tr 不跨页断开。
 *
 * 浏览器对 `window.print()` 的语义是「弹打印对话框」，用户在那里选打印机
 * 或「另存为 PDF」。导出 PDF 也走同一条路径，所以没必要再单独搞一个
 * 「导出 PDF」按钮——print.css 已经把 PDF 输出也按 A4 / 12mm 排好了。
 */
function onPrint(): void {
  // 给 body 临时加 print-mode 类：作为保险，将来若 print.css 出现
  // 优先级问题，可以用 .print-mode .xxx 提高特异性。当前 print.css
  // 全是 @media print + !important，不需要这个类；保留 API 便于后续扩展。
  document.body.classList.add('print-mode')
  // 给浏览器一帧时间把样式 settle 进 layout 计算，再触发。
  // 不加这一帧时某些情况下 el-table 的 transform 还没卸载，会出半渲染。
  setTimeout(() => {
    try {
      window.print()
    } finally {
      document.body.classList.remove('print-mode')
    }
  }, 50)
}

async function loadAudit(): Promise<void> {
  if (!canSeeAudit.value) return
  auditLoading.value = true
  try {
    auditLogs.value = await quoteApi.auditLogs(quoteId.value, 100)
  } finally {
    auditLoading.value = false
  }
}

function onTabChange(name: string | number): void {
  // el-tabs 的回调签名是 TabPaneName = string | number，不能只声明 string
  const key = String(name)
  if (key === 'summary' && !summary.value) void loadSummary()
  if (key === 'audit' && !auditLogs.value.length) void loadAudit()
}

// ----- 停产替代建议 -----

const suggestVisible = ref(false)
const suggestLoading = ref(false)
const suggestFor = ref<QuoteItem | null>(null)
const suggestList = ref<SuggestRow[]>([])

async function openSuggest(item: QuoteItem): Promise<void> {
  suggestFor.value = item
  suggestVisible.value = true
  suggestLoading.value = true
  suggestList.value = []
  try {
    if (!item.device_id) {
      ElMessage.warning('该行不是来自设备库，无法推荐替代型号')
      return
    }
    const res = await deviceApi.replacements(item.device_id, 5)
    suggestList.value = res.candidates.map((c) => ({
      id: c.device_id,
      code: c.code,
      name: c.name,
      brand: c.brand,
      model: c.model,
      unit: c.unit,
      reference_price: c.reference_price,
      install_unit_price: c.install_unit_price,
      status: c.status,
    }))
  } finally {
    suggestLoading.value = false
  }
}

async function applySuggestion(device: SuggestRow): Promise<void> {
  const item = suggestFor.value
  if (!item) return
  await quoteApi.replaceItemDevice(quoteId.value, item.id, device.id)
  ElMessage.success(`已替换为 ${device.name}`)
  suggestVisible.value = false
  await reload()
}

function goBack(): void {
  void router.push({ name: 'projects' })
}

// ----- 场景二：甲方清单智能匹配导入 -----

const inquiryVisible = ref(false)

function openInquiryImport(): void {
  inquiryVisible.value = true
}
</script>

<template>
  <div v-loading="loading" class="page print-area">
    <el-alert
      v-if="loadError"
      type="error"
      :closable="false"
      show-icon
      :title="loadError"
      class="mb no-print"
    />

    <!-- 头部：项目与报价单信息。
         .no-print 标在按钮区外层，打印样式只隐藏按钮区；
         项目名 / 报价单号 / 状态这些**印刷正文**会留着。 -->
    <div class="page-head">
      <div>
        <h2 class="page-title">
          <el-button link class="no-print" @click="goBack">
            <el-icon><ArrowLeft /></el-icon>
          </el-button>
          {{ project?.name ?? '报价编制' }}
        </h2>
        <p class="page-sub">
          {{ project?.code }} · 客户 {{ project?.customer_name }}
          <template v-if="quote">
            · 报价单 <b>{{ quote.code }}</b> v{{ quote.version }}
            · <el-tag size="small" :type="locked ? 'success' : 'info'" effect="plain">
              {{ meta.label('quote_status', quote.status) }}
            </el-tag>
            <el-tag v-if="locked" size="small" type="warning" effect="plain" class="ml">
              已锁定：改内容需先退回草稿
            </el-tag>
            <!-- 审批人看到草稿/驳回单：不是没按钮，是"还不能批" -->
            <el-tag v-if="needsSubmitFirst" size="small" type="info" effect="plain" class="ml">
              待提交：先提交审核才能批准
            </el-tag>
            <!-- 已提交但没人接手：说明下一步动作，而不是让批准按钮消失得莫名其妙 -->
            <el-tag
              v-if="waitingClaim && canApprove"
              size="small"
              type="warning"
              effect="plain"
              class="ml"
            >
              待接手：先「开始审核」再批准/驳回
            </el-tag>
          </template>
        </p>
      </div>
      <div class="no-print">
        <!-- 状态流转：按权限与当前状态渲染，不是自由下拉 -->
        <el-button
          v-if="canSubmit"
          :loading="statusChanging"
          @click="onSubmitReview"
        >
          {{ quoteStatus === 'rejected' ? '重新提交' : '提交审核' }}
        </el-button>
        <!-- 「开始审核」是批准/驳回的必经步骤，所以给它独立按钮而不是塞进「更多」 -->
        <el-button
          v-if="canStartReview"
          type="primary"
          plain
          :loading="statusChanging"
          @click="onStartReview"
        >
          <el-icon><View /></el-icon>&nbsp;开始审核
        </el-button>
        <template v-if="canDecide">
          <el-button type="success" :loading="statusChanging" @click="onApprove">
            <el-icon><Select /></el-icon>&nbsp;批准
          </el-button>
          <el-button type="danger" plain :loading="statusChanging" @click="onReject">
            驳回
          </el-button>
        </template>
        <el-dropdown v-if="canApprove && locked" trigger="click" @command="onMoreCommand">
          <el-button :loading="statusChanging">
            更多<el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="draft">
                退回草稿（解除锁定）
              </el-dropdown-item>
              <el-dropdown-item v-if="quoteStatus === 'approved'" command="archive">
                归档
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-dropdown v-if="canExport" trigger="click" @command="onExport">
          <el-button :loading="exporting" :disabled="!quote">
            <el-icon><Download /></el-icon>&nbsp;导出
            <el-icon class="el-icon--right"><ArrowDown /></el-icon>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="xlsx:customer">客户版 Excel（可发甲方）</el-dropdown-item>
              <el-dropdown-item command="pdf:customer">客户版 PDF（可发甲方）</el-dropdown-item>
              <!-- 内销版：只有能看成本的角色才给这项，点了也过不了后端校验 -->
              <el-dropdown-item v-if="auth.canSeeCost" divided command="xlsx:internal">
                内部版 Excel（含成本毛利）
              </el-dropdown-item>
              <el-dropdown-item v-if="auth.canSeeCost" command="pdf:internal">
                内部版 PDF（含成本毛利）
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <!--
          打印按钮：触发 window.print()，由 styles/print.css 接管样式。
          为什么不放在 el-dropdown 里：打印是高频操作，单独按钮更顺手；
          也不需要权限校验（本地预览不需要后端配合）。
        -->
        <el-button
          class="no-print"
          :disabled="!quote"
          @click="onPrint"
        >
          <el-icon><Printer /></el-icon>&nbsp;打印
        </el-button>
        <el-button :disabled="!canEdit" @click="ratePanelOpen = !ratePanelOpen">
          <el-icon><SetUp /></el-icon>&nbsp;费率与人工浮动{{ ratesDirty ? ' *' : '' }}
        </el-button>
        <!-- 重算与"改费率"分开判权：审批人（manager）只读不能改费率，
             但必须能重算对齐数字，否则只能退回让别人算。 -->
        <el-button
          type="primary"
          :loading="computing"
          :disabled="!auth.canWrite && !auth.can('quote', 'compute')"
          @click="saveAndCompute"
        >
          <el-icon><Refresh /></el-icon>&nbsp;重算
        </el-button>
      </div>
    </div>

    <!-- 停产 / 待询价 提醒条：屏幕上的提醒，打印不要。 -->
    <el-alert
      v-if="discontinuedCount > 0"
      type="error"
      show-icon
      :closable="false"
      class="mb no-print"
      :title="`有 ${discontinuedCount} 行使用了已停产型号，请在提交前替换`"
    >
      <template #default>
        已停产的型号继续留在报价里，会在供货阶段变成废单。请在明细行上点「换型号」。
      </template>
    </el-alert>
    <el-alert
      v-if="pendingCount > 0"
      type="warning"
      show-icon
      :closable="false"
      class="mb no-print"
      :title="`有 ${pendingCount} 行单价为 0（待询价）`"
    >
      <template #default>这些行不影响合计口径，但会导致总价偏低，提交前请补齐单价。</template>
    </el-alert>

    <el-tabs v-model="activeTab" class="tabs" @tab-change="onTabChange">
      <!-- ---------------- 报价明细 ---------------- -->
      <el-tab-pane label="报价明细" name="items">
        <!-- 费率面板 -->
        <el-collapse-transition>
          <div v-show="ratePanelOpen" class="card">
            <h3 class="card-title">
              <el-icon><SetUp /></el-icon>费率与人工浮动
              <el-tag size="small" type="info" effect="plain">界面按百分数录入</el-tag>
            </h3>

            <el-form label-width="120px" :disabled="!canEdit">
              <div class="grid-3">
                <el-form-item label="计税方式">
                  <el-select v-model="rates.tax_method" style="width: 100%">
                    <el-option label="一般计税 9%" value="general_9pct" />
                    <el-option label="简易计税 3%" value="simple_3pct" />
                  </el-select>
                </el-form-item>
                <el-form-item label="税率 %">
                  <el-input-number
                    v-model="rates.tax_rate_pct"
                    :min="0"
                    :max="100"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item label="管理费+利润 %">
                  <el-input-number
                    v-model="rates.mgmt_profit_rate_pct"
                    :min="0"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
              </div>

              <div class="grid-3">
                <el-form-item label="附加费基数">
                  <el-select v-model="rates.fee_base" style="width: 100%">
                    <el-option
                      v-for="o in meta.options('fee_base')"
                      :key="o.value"
                      :label="o.label"
                      :value="o.value"
                    />
                  </el-select>
                </el-form-item>
                <el-form-item label="辅材基数">
                  <el-select v-model="rates.auxiliary_base" style="width: 100%">
                    <el-option
                      v-for="o in meta.options('auxiliary_base')"
                      :key="o.value"
                      :label="o.label"
                      :value="o.value"
                    />
                  </el-select>
                </el-form-item>
                <el-form-item label="辅材费率 %">
                  <el-input-number
                    v-model="rates.auxiliary_rate_pct"
                    :min="0"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
              </div>

              <div class="grid-3">
                <el-form-item label="设计费 %">
                  <el-input-number
                    v-model="rates.design_rate_pct"
                    :min="0"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item label="质保/维保 %">
                  <el-input-number
                    v-model="rates.warranty_rate_pct"
                    :min="0"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item label="培训费（元）">
                  <el-input-number
                    v-model="rates.training_fee"
                    :min="0"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
              </div>

              <el-divider content-position="left">
                人工费浮动（叠加在宁波信息价之上）
              </el-divider>
              <!-- 说清楚两者关系：以前"整体浮动"填了不生效（后端只在分类浮动为空时
                   才用它），用户看不出任何异常。现在改成相加，界面也把口径写明。 -->
              <p class="rate-hint">
                有效上浮率 = <b>整体浮动</b> + <b>该类浮动</b>。
                例：整体 5% + 一类 3% → 一类工日按 +8% 计；整体浮动对一/二/三类都加。
              </p>

              <div class="grid-3">
                <el-form-item label="整体浮动 %">
                  <el-input-number
                    v-model="rates.labor_float_rate_pct"
                    :min="-100"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item label="一类工日浮动 %">
                  <el-input-number
                    v-model="rates.float_a_pct"
                    :min="-100"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item label="二类工日浮动 %">
                  <el-input-number
                    v-model="rates.float_b_pct"
                    :min="-100"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
              </div>

              <div class="grid-3">
                <el-form-item label="三类工日浮动 %">
                  <el-input-number
                    v-model="rates.float_c_pct"
                    :min="-100"
                    :precision="2"
                    controls-position="right"
                    style="width: 100%"
                  />
                </el-form-item>
                <el-form-item label="备注" class="span-2">
                  <el-input v-model="rates.notes" placeholder="如：按 2022.7.13 纪要取工艺系数" />
                </el-form-item>
              </div>

              <el-form-item>
                <el-button type="primary" :loading="savingRates" @click="saveRatesOnly">
                  保存费率
                </el-button>
                <el-button :loading="computing" @click="saveAndCompute">保存并重算</el-button>
                <span class="text-muted" style="margin-left: 8px">
                  费率修改需报价编制权限；审批人可用「重算」对齐最新数字
                </span>
                <span class="text-muted ml">
                  当前：税 {{ rates.tax_rate_pct }}% · 综合费率
                  {{ rates.mgmt_profit_rate_pct }}% · 人工整体浮动 {{ rates.labor_float_rate_pct }}%
                </span>
              </el-form-item>
            </el-form>
          </div>
        </el-collapse-transition>

        <!-- 明细表 -->
        <div class="card mt">
          <div class="card-title-row">
            <h3 class="card-title">
              <el-icon><Tickets /></el-icon>报价明细
              <el-tag v-if="quote" size="small" effect="plain">
                {{ quote.items.length }} 行
              </el-tag>
            </h3>
            <!-- 添加明细的按钮组：屏幕用，打印不要。 -->
            <div class="toolbar no-print">
              <el-input
                v-model="itemFilter.keyword"
                placeholder="筛选名称/型号"
                clearable
                size="small"
                style="width: 170px"
              />
              <el-select
                v-model="itemFilter.subsystem"
                placeholder="全部子系统"
                clearable
                size="small"
                style="width: 150px"
              >
                <el-option
                  v-for="s in pickerSubsystems"
                  :key="s.id"
                  :label="s.name"
                  :value="s.id"
                />
                <el-option label="未归属" value="__none__" />
              </el-select>
              <el-checkbox v-model="itemFilter.onlyPending" size="small">仅看待询价</el-checkbox>
              <el-button
                type="primary"
                size="small"
                :disabled="!canEdit"
                @click="openPicker"
              >
                <el-icon><Plus /></el-icon>&nbsp;从设备库添加
              </el-button>
              <el-button size="small" :disabled="!canEdit" @click="openAddManual">
                <el-icon><EditPen /></el-icon>&nbsp;手工录入
              </el-button>
              <el-button
                size="small"
                type="warning"
                plain
                :disabled="!canEdit"
                @click="openInquiryImport"
              >
                <el-icon><Upload /></el-icon>&nbsp;导入甲方清单
              </el-button>
            </div>
          </div>

          <!-- 配件/授权提示（2026-10-05）：常驻检查，缺了就一直提醒 -->
          <el-alert
            v-if="accCheck && accCheck.missing.length"
            type="warning"
            show-icon
            :closable="false"
            class="mb"
            :title="accCheck.summary"
          >
            <div v-for="(m, i) in accCheck.missing" :key="i" class="acc-miss">
              <span>
                {{ m.item_name }}
                <span class="acc-dim">（{{ m.quantity }} {{ m.device_code ? '' : '' }}
                  台）需要配「{{ m.group_label }}」</span>
              </span>
              <el-button size="small" type="primary" plain @click="openAccDialog(m)">
                选择{{ m.group_label }}
              </el-button>
            </div>
          </el-alert>

          <el-table
            :data="filteredItems"
            border
            stripe
            size="small"
            :row-class-name="rowClassName"
            max-height="520"
          >
            <el-table-column label="#" type="index" width="50" />
            <!-- 设备参数（2026-10-05）：来自设备描述的快照，可在单里改 -->
            <el-table-column label="设备参数" min-width="200">
              <template #default="{ row }">
                <el-input
                  v-model="row.spec_text"
                  size="small"
                  type="textarea"
                  :autosize="{ minRows: 1, maxRows: 3 }"
                  placeholder="设备性能参数"
                  @change="
                    (v: string) => quoteApi.updateItem(quoteId, row.id, { spec_text: v })
                  "
                />
              </template>
            </el-table-column>
            <el-table-column label="名称 / 型号" min-width="220">
              <template #default="{ row }">
                <div class="name-line">
                  {{ row.name }}
                  <span v-if="isManual(row)" class="badge-manual">手录</span>
                  <el-tag
                    v-if="isItemDiscontinued(row)"
                    size="small"
                    type="danger"
                    effect="dark"
                  >
                    已停产
                  </el-tag>
                  <el-tag v-else-if="row.is_replaced" size="small" type="success" effect="plain">
                    已换型
                  </el-tag>
                  <el-tag
                    v-if="toNum(row.unit_price) === 0"
                    size="small"
                    type="warning"
                    effect="plain"
                  >
                    待询价
                  </el-tag>
                </div>
                <div class="text-muted sub">
                  <span v-if="row.device_code" class="mono">{{ row.device_code }}</span>
                  <span v-if="row.brand">{{ row.brand }}</span>
                  <span v-if="row.model" class="mono">{{ row.model }}</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="子系统" width="120">
              <template #default="{ row }">
                <span v-if="row.subsystem_id" class="text-muted">
                  {{ subsystemName.get(row.subsystem_id) ?? '—' }}
                </span>
                <span v-else class="text-muted">未归属</span>
              </template>
            </el-table-column>
            <el-table-column label="单位" prop="unit" width="60" />
            <el-table-column label="数量" width="120" align="right">
              <template #default="{ row }">
                <el-input-number
                  v-if="canEdit"
                  :model-value="toNum(row.quantity)"
                  :min="0"
                  :precision="0"
                  :step="1"
                  size="small"
                  controls-position="right"
                  style="width: 100%"
                  @change="(v: number | undefined) => changeQuantity(row, v)"
                />
                <span v-else class="num">{{ toNum(row.quantity) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="设备单价" width="105" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.unit_price) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="设备费" width="115" align="right">
              <template #default="{ row }">
                <span class="num num-strong">{{ money(row.equipment_amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="安装单价" width="100" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.install_unit_price) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="安装费" width="105" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.install_amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="来源" width="76">
              <template #default="{ row }">
                <span :class="isManual(row) ? 'text-accent' : 'text-muted'">
                  {{ meta.label('item_source', row.source) }}
                </span>
              </template>
            </el-table-column>
            <!-- 操作列：屏幕用，打印不要。加 class-name-key="no-print" 让
                 print.css 用 tr > td:has(.no-print) 的写法不靠谱，改成给
                 整个 column 加 class-name，让列头+所有 cell 都隐藏。 -->
            <el-table-column label="操作" width="150" fixed="right" class-name="col-no-print"
              label-class-name="col-no-print">
              <template #default="{ row }">
                <div class="table-actions">
                  <el-button
                    v-if="isItemDiscontinued(row)"
                    type="danger"
                    link
                    size="small"
                    @click="openReplace(row)"
                  >
                    换型号
                  </el-button>
                  <el-button link size="small" @click="openSuggest(row)">推荐</el-button>
                  <el-button
                    v-if="canEdit"
                    link
                    size="small"
                    @click="openEditItem(row)"
                  >
                    编辑
                  </el-button>
                  <el-button
                    v-if="canEdit"
                    type="danger"
                    link
                    size="small"
                    @click="removeItem(row)"
                  >
                    删
                  </el-button>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="还没有明细行，点击「从设备库添加」或「手工录入」" />
            </template>
          </el-table>
        </div>

        <!-- 费用瀑布 -->
        <div v-if="quote" class="card">
          <h3 class="card-title"><el-icon><Money /></el-icon>费用汇总</h3>
          <QuoteTotalPanel
            :quote="quote"
            :cost="cost"
            :warnings="warnings"
            :show-quota="true"
          />
        </div>
      </el-tab-pane>

      <!-- ---------------- 客户版汇总 ---------------- -->
      <el-tab-pane label="客户版汇总" name="summary">
        <div class="card">
          <div class="card-title-row">
            <h3 class="card-title">
              <el-icon><PieChart /></el-icon>子系统汇总（可对外）
            </h3>
            <el-button size="small" :loading="summaryLoading" @click="loadSummary">
              <el-icon><Refresh /></el-icon>&nbsp;刷新
            </el-button>
          </div>
          <el-alert
            type="info"
            :closable="false"
            show-icon
            class="mb"
            title="此表只含设备费与安装费，不含成本、毛利与费率明细，可直接给甲方。"
          />
          <el-table v-loading="summaryLoading" :data="summary?.subsystems ?? []" border stripe>
            <el-table-column label="#" type="index" width="50" />
            <el-table-column prop="name" label="子系统" min-width="160" />
            <el-table-column label="点位数" width="90" align="right">
              <template #default="{ row }">
                <span class="num">{{ row.point_count }}</span>
              </template>
            </el-table-column>
            <el-table-column label="设备费" width="130" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.equipment_subtotal) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="安装费" width="130" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.install_subtotal) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="小计" width="140" align="right">
              <template #default="{ row }">
                <span class="num num-strong">{{ money(row.amount) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="占比" width="150">
              <template #default="{ row }">
                <!-- 后端回的 share 已经是百分数（amount/grand*100），不要再 ×100 -->
                <el-progress
                  :percentage="Math.min(100, Math.max(0, Number(row.share)))"
                  :stroke-width="12"
                  :show-text="true"
                  :format="() => `${Number(row.share).toFixed(1)}%`"
                />
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="还没有归属到子系统的明细行" />
            </template>
          </el-table>
          <div v-if="summary" class="sum-foot">
            合计（设备费 + 安装费）：
            <b class="num num-total">{{ money(summary.grand_total) }}</b>
          </div>
        </div>
      </el-tab-pane>

      <!-- ---------------- 操作记录 ---------------- -->
      <el-tab-pane v-if="canSeeAudit" label="操作记录" name="audit">
        <div class="card">
          <div class="card-title-row">
            <h3 class="card-title"><el-icon><Clock /></el-icon>操作留痕</h3>
            <el-button size="small" :loading="auditLoading" @click="loadAudit">
              <el-icon><Refresh /></el-icon>&nbsp;刷新
            </el-button>
          </div>
          <el-table v-loading="auditLoading" :data="auditLogs" border size="small">
            <el-table-column label="时间" width="160">
              <template #default="{ row }">{{ dateTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="action" label="动作" width="140" />
            <el-table-column prop="note" label="说明" min-width="220" />
            <el-table-column label="载荷" min-width="240">
              <template #default="{ row }">
                <span class="mono payload">{{ JSON.stringify(row.payload) }}</span>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="暂无操作记录" />
            </template>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 对话框 -->
    <DevicePickerDialog
      v-model="pickerVisible"
      :mode="pickerMode"
      :exclude-id="replacingItem?.device_id ?? null"
      :subsystems="pickerSubsystems"
      @pick="onPick"
    />

    <QuoteItemDialog
      v-model="itemDialogVisible"
      :quote-id="quoteId"
      :item="editingItem"
      :subsystems="pickerSubsystems"
      :readonly="!canEdit"
      @saved="reload"
    />

    <InquiryImportDialog
      v-model="inquiryVisible"
      :quote-id="quoteId"
      :subsystems="pickerSubsystems"
      @done="reload"
    />

    <!-- 替代型号推荐 -->
        <!-- 添加配件（2026-10-05）：从槽的候选里选，数量按设备上配的规则算 -->
    <el-dialog v-model="accDialog" title="添加配件" width="620px">
      <div v-if="accTarget">
        <p class="acc-dim">
          {{ accTarget.item_name }} 需要配「{{ accTarget.group_label }}」，
          将按 <b>{{ accQuantity(accTarget) }}</b> 件加入报价。
        </p>
        <el-radio-group v-if="accTarget.select_mode === 'one'" v-model="accChoice">
          <el-radio v-for="o in accTarget.options" :key="o.accessory_id"
                    :value="o.accessory_id" class="acc-pick">
            {{ o.name }}
            <span class="acc-dim">　参考价 {{ o.reference_price ?? '—' }}</span>
          </el-radio>
        </el-radio-group>
        <el-checkbox-group v-else v-model="accChoiceMany">
          <el-checkbox v-for="o in accTarget.options" :key="o.accessory_id"
                       :value="o.accessory_id" class="acc-pick">
            {{ o.name }}
            <span class="acc-dim">　参考价 {{ o.reference_price ?? '—' }}</span>
          </el-checkbox>
        </el-checkbox-group>
        <el-alert v-if="!accTarget.options.length" type="info" :closable="false"
                  show-icon title="这个槽还没有配置候选配件，请到产品库里补上" />
      </div>
      <template #footer>
        <el-button @click="accDialog = false">取消</el-button>
        <el-button type="primary" :loading="accAdding" @click="confirmAcc">
          加入报价
        </el-button>
      </template>
    </el-dialog>

<el-dialog v-model="suggestVisible" title="替代型号推荐" width="760px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="mb"
        title="优先使用设备库里显式配置的替代型号；没有配置时按同分类名称/型号相似度推荐。"
      />
      <el-table v-loading="suggestLoading" :data="suggestList" border size="small">
        <el-table-column prop="code" label="编号" width="90" />
        <el-table-column label="名称 / 型号" min-width="200">
          <template #default="{ row }">
            <div>{{ row.name }}</div>
            <div class="text-muted mono">{{ row.brand }} {{ row.model }}</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="meta.deviceStatusTag(row.status)" effect="plain">
              {{ meta.label('device_status', row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="参考单价" width="110" align="right">
          <template #default="{ row }">
            <span class="num">{{ money(row.reference_price) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button
              type="primary"
              link
              size="small"
              :disabled="!canEdit"
              @click="applySuggestion(row)"
            >
              采用
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="没有找到可选替代型号" />
        </template>
      </el-table>
      <p class="foot">
        当前明细行：{{ suggestFor?.name }}（{{ suggestFor?.model || '无型号' }}）
      </p>
    </el-dialog>

    <!-- 打印专用：盖章 / 签字行 + 打印时间。
         .print-only 类在 print.css 的 @media screen 下默认 display:none，
         打印时启用。 -->
    <div class="print-only print-foot">
      <div class="sign-row">
        <span>编制人：________________</span>
        <span>审核人：________________</span>
        <span>客户签收：________________</span>
      </div>
      <div class="sign-date">打印时间：{{ new Date().toLocaleString('zh-CN') }}</div>
    </div>
  </div>
</template>

<style scoped>
/* 配件提示（2026-10-05） */
.acc-miss {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 4px 0;
}
.acc-dim {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.acc-pick {
  display: flex;
  margin-bottom: 6px;
}

.mb {
  margin-bottom: 10px;
}

.mt {
  margin-top: 14px;
}

.tabs {
  margin-top: 4px;
}

.card-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.card-title-row .card-title {
  margin: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.name-line {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.sub {
  font-size: 11.5px;
  display: flex;
  gap: 8px;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
}

.sum-foot {
  margin-top: 12px;
  text-align: right;
  font-size: 14px;
}

.ml {
  margin-left: 12px;
  font-size: 12px;
}

.span-2 {
  grid-column: span 2;
}

.payload {
  font-size: 11px;
  color: var(--c-muted);
  word-break: break-all;
}

.foot {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--c-muted);
}

/* ---------- 打印专用：盖章 / 签字 / 时间 ---------- */
.print-foot {
  display: none;
}

.print-foot .sign-row {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 6mm;
  font-size: 12px;
}

.print-foot .sign-date {
  text-align: right;
  font-size: 11px;
  color: #555;
}
/* 费率面板里的一句口径说明：不抢视觉，但要让"整体/分类"的关系一眼看懂 */
.rate-hint {
  margin: -4px 0 10px 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}
</style>


