<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { projectApi, quoteApi , customerApi } from '@/api'
import type { AuditLogEntry, Project, Quote , Customer } from '@/api/types'
import { dateOnly, dateTime, money, percentInputToRatio, ratioToPercentInput, toNum } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const loading = ref(false)
const opening = ref<string | null>(null)
const rows = ref<Project[]>([])
const total = ref(0)

const query = reactive({
  keyword: '',
  status: '',
  page: 1,
  page_size: 20,
})

// ---------------------------------------------------------------------------
// 状态机 + 报价单子表（2026-10-02）
//
// 项目状态的"能改成什么"完全由后端 /projects/status-options 决定；
// 展开行里列出该项目的报价单，并给出快捷的提交/批准/驳回/退回 ——
// 经理不用为了一张待批的单子逐个点进编辑器。
// ---------------------------------------------------------------------------

const statusOptions = ref<Record<string, string[]>>({})

const canQuoteUpdate = computed(() => auth.can('quote', 'update'))
const canQuoteApprove = computed(() => auth.can('quote', 'approve'))
const canSeeAudit = computed(() => auth.can('audit_log', 'read'))

async function loadStatusOptions(): Promise<void> {
  try {
    statusOptions.value = await projectApi.statusOptions()
  } catch {
    statusOptions.value = {}
  }
}

/** 编辑时下拉的基准状态：用**打开表单时**的状态算选项，避免改到一半选项跟着变。 */
const baseStatus = ref('draft')

/** 新建/编辑表单里可选的状态（含当前值本身）。 */
const formStatusChoices = computed(() => {
  const base = dialogMode.value === 'create' ? 'draft' : baseStatus.value
  const targets = statusOptions.value[base] ?? []
  return [base, ...targets.filter((s) => s !== base)]
})

const expandedIds = ref<string[]>([])
const quotesByProject = reactive<Record<string, Quote[]>>({})
const quotesLoading = reactive<Record<string, boolean>>({})
const quoteBusy = ref<string | null>(null)

async function loadQuotes(projectId: string): Promise<void> {
  quotesLoading[projectId] = true
  try {
    const page = await quoteApi.list({ project_id: projectId, page: 1, page_size: 50 })
    quotesByProject[projectId] = page.items
  } catch {
    quotesByProject[projectId] = []
  } finally {
    quotesLoading[projectId] = false
  }
}

/** 展开某一行就顺带把它的报价单拉出来（不展开不请求）。 */
function onExpandChange(row: Project, expanded: Project[]): void {
  expandedIds.value = expanded.map((p) => p.id)
  if (expandedIds.value.includes(row.id)) void loadQuotes(row.id)
}

function quoteLocked(q: Quote): boolean {
  return ['approved', 'archived'].includes(String(q.status))
}

/** 已提交 / 审核中 = 等审批；**只有这两个状态能批准/驳回**（必须先提交）。 */
/** 已提交 = 等人接手；审核中 = 已接手。**只有「审核中」能批准/驳回**。 */
function quoteCanStartReview(q: Quote): boolean {
  return canQuoteApprove.value && String(q.status) === 'submitted'
}

function quoteCanSubmit(q: Quote): boolean {
  return canQuoteUpdate.value && ['draft', 'rejected'].includes(String(q.status))
}

function quoteCanDecide(q: Quote): boolean {
  return canQuoteApprove.value && String(q.status) === 'reviewing'
}

function quoteCanRevert(q: Quote): boolean {
  return canQuoteApprove.value && quoteLocked(q)
}

/** 快捷状态流转（与编辑器里同一套规则，规则本身在后端）。 */
async function changeQuoteStatus(q: Quote, target: string, label: string): Promise<void> {
  const needConfirm = ['approved', 'rejected', 'draft', 'archived'].includes(target)
  if (needConfirm) {
    const hint =
      target === 'approved'
        ? '批准后这张报价单的内容将被锁定，如需修改必须先退回草稿。确认批准？'
        : target === 'rejected'
          ? '驳回后销售/设计需要修改并重新提交，确认驳回？'
          : target === 'draft'
            ? '退回草稿后这张单可以继续修改（批准状态将被清除），确认退回？'
            : '归档后单据锁定且不再出现在常规列表里，确认归档？'
    const go = await ElMessageBox.confirm(hint, label, {
      confirmButtonText: label,
      cancelButtonText: '取消',
      type: 'warning',
    })
      .then(() => true)
      .catch(() => false)
    if (!go) return
  }
  quoteBusy.value = q.id
  try {
    await quoteApi.update(q.id, { status: target, version: q.version })
    ElMessage.success(`已${label}`)
    await Promise.all([loadQuotes(q.project_id), load()])
  } catch (e) {
    // 业务规则拒绝（如"必须先提交才能批准"）时后端会 409，把原因原样告诉用户
    ElMessage.error(e instanceof Error ? e.message : '状态变更被拒绝')
  } finally {
    quoteBusy.value = null
  }
}

function openQuoteEditor(projectId: string, quoteId: string): void {
  void router.push({
    name: 'quote-workbench',
    params: { projectId, quoteId },
  })
}

// ----- 项目操作留痕 -----

const auditVisible = ref(false)
const auditLoading = ref(false)
const auditRows = ref<AuditLogEntry[]>([])
const auditTitle = ref('')

async function openAudit(row: Project): Promise<void> {
  auditTitle.value = `${row.code} ${row.name}`
  auditRows.value = []
  auditVisible.value = true
  auditLoading.value = true
  try {
    auditRows.value = await projectApi.auditLogs(row.id)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '读取留痕失败')
  } finally {
    auditLoading.value = false
  }
}

function auditText(entry: AuditLogEntry): string {
  const from = entry.payload?.from as string | null | undefined
  const to = entry.payload?.to as string | null | undefined
  const fromLabel = from ? meta.label('project_status', from) : '新建'
  const toLabel = to ? meta.label('project_status', to) : '—'
  return `${fromLabel} → ${toLabel}`
}

// ----- 新建 / 编辑项目 -----
// 用 type 而不是 interface：type 的对象字面量有隐式索引签名，
// 展开成 Record<string, unknown> 提交时不需要强转。
type ProjectForm = {
  id: string
  code: string
  name: string
  customer_id: string | null
  customer_name: string
  customer_contact: string
  customer_phone: string
  address: string
  project_type: string
  status: string
  expected_start: string | null
  notes: string
  // 项目级费率（百分数输入，提交时转比例）。建报价单时抄进报价单作为快照。
  tax_rate_pct: number
  fee_base: string
  auxiliary_base: string
  auxiliary_rate_pct: number
  design_rate_pct: number
  warranty_rate_pct: number
  training_fee: number
  mgmt_profit_rate_pct: number
}

function emptyForm(): ProjectForm {
  return {
    id: '',
    code: '',
    name: '',
    customer_id: null,
    customer_name: '',
    customer_contact: '',
    customer_phone: '',
    address: '',
    project_type: 'commercial',
    status: 'draft',
    expected_start: null,
    notes: '',
    // 与后端 ProjectCreate 的默认值保持一致（改这里要同步那边）
    tax_rate_pct: 9,
    fee_base: 'device_aux_install',
    auxiliary_base: 'device',
    auxiliary_rate_pct: 6,
    design_rate_pct: 2,
    warranty_rate_pct: 3,
    training_fee: 0,
    mgmt_profit_rate_pct: 8,
  }
}

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const saving = ref(false)
const codeLoading = ref(false)
// ---------- 客户档案（2026-10-04）----------
const customerOptions = ref<Customer[]>([])
const customerLoading = ref(false)

async function searchCustomers(keyword: string): Promise<void> {
  customerLoading.value = true
  try {
    customerOptions.value = await customerApi.options(keyword || undefined)
  } catch {
    customerOptions.value = []
  } finally {
    customerLoading.value = false
  }
}

/** 选中客户 → 把档案里的名称/联系人/电话填进表单（用户仍可改） */
function applyCustomer(id: string | null): void {
  const c = customerOptions.value.find((x) => x.id === id)
  if (!c) return
  form.customer_name = c.name
  form.customer_contact = c.contact_name ?? form.customer_contact
  form.customer_phone = c.contact_phone ?? form.customer_phone
}

const formRef = ref()

const form = reactive<ProjectForm>(emptyForm())

const rules = {
  code: [{ required: true, message: '请输入项目编号', trigger: 'blur' }],
  name: [{ required: true, message: '请输入项目名称', trigger: 'blur' }],
  // 客户名与客户档案二选一（后端也是这么校验的）：
  // 直接要求必填会挡住"从客户档案里挑一家"的正常流程。
  customer_name: [
    {
      validator: (_r: unknown, value: string, cb: (e?: Error) => void) => {
        if (form.customer_id || (value ?? '').trim()) cb()
        else cb(new Error('请填写客户名称，或从客户档案里选择一家'))
      },
      trigger: 'blur',
    },
  ],
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await projectApi.list({
      keyword: query.keyword || undefined,
      status: query.status || undefined,
      page: query.page,
      page_size: query.page_size,
    })
    rows.value = page.items
    total.value = page.total
  } finally {
    loading.value = false
  }
}

function resetAndLoad(): void {
  query.page = 1
  void load()
}

onMounted(() => {
  void loadStatusOptions()
  void load()
})

/**
 * 这个页面被 `keep-alive` 缓存（保留筛选与展开状态），但**数据不能跟着一起缓存**：
 * 别处改了数据、或库被清空之后切回来，看到的必须是现在的数据。
 * 首次挂载时 onActivated 也会触发，用标志跳过，避免首屏拉两遍。
 */
let firstActivate = true
onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  void load()
})

function openCreate(): void {
  void searchCustomers('')
  dialogMode.value = 'create'
  Object.assign(form, emptyForm())
  dialogVisible.value = true
  // 预填一个编号，省得用户自己想；想用自定义编号直接改掉即可。
  void generateCode()
}

/** 让后端给出下一个 PRJ-YYYYMM-NNNN（与设备库的「自动生成」同一套做法）。 */
async function generateCode(): Promise<void> {
  codeLoading.value = true
  try {
    form.code = await projectApi.nextCode()
  } catch {
    // 失败就留空，让用户手填 —— 编号本来就可手填，不必打断建单流程
  } finally {
    codeLoading.value = false
  }
}

function openEdit(row: Project): void {
  dialogMode.value = 'edit'
  // 下拉选项基于"打开表单时的状态"，改到一半选项不会跟着跳
  baseStatus.value = row.status
  Object.assign(form, {
    id: row.id,
    code: row.code,
    name: row.name,
    customer_id: row.customer_id ?? null,
    customer_name: row.customer_name,
    customer_contact: row.customer_contact ?? '',
    customer_phone: row.customer_phone ?? '',
    address: row.address ?? '',
    project_type: row.project_type,
    status: row.status,
    expected_start: row.expected_start ? row.expected_start.slice(0, 10) : null,
    notes: row.notes ?? '',
    tax_rate_pct: ratioToPercentInput(row.tax_rate),
    fee_base: row.fee_base,
    auxiliary_base: row.auxiliary_base,
    auxiliary_rate_pct: ratioToPercentInput(row.auxiliary_rate),
    design_rate_pct: ratioToPercentInput(row.design_rate),
    warranty_rate_pct: ratioToPercentInput(row.warranty_rate),
    training_fee: toNum(row.training_fee),
    mgmt_profit_rate_pct: ratioToPercentInput(row.mgmt_profit_rate),
  })
  dialogVisible.value = true
}

async function save(): Promise<void> {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  saving.value = true
  try {
    const payload: Record<string, unknown> = { ...form }
    delete payload.id
    // 编号不允许改（后端 PATCH 也不收 code），避免破坏已有引用
    if (dialogMode.value === 'edit') delete payload.code
    // 费率字段：界面用百分数输入，接口收比例
    for (const key of ['tax_rate', 'auxiliary_rate', 'design_rate', 'warranty_rate',
                       'mgmt_profit_rate'] as const) {
      const pctKey = `${key}_pct` as keyof ProjectForm
      payload[key] = percentInputToRatio(form[pctKey] as number)
      delete payload[pctKey]
    }
    // 空字符串一律转 null，避免把 "" 写进电话/日期字段
    for (const [k, v] of Object.entries(payload)) {
      if (v === '') payload[k] = null
    }

    if (dialogMode.value === 'create') {
      await projectApi.create(payload)
      ElMessage.success('项目已创建')
    } else {
      await projectApi.update(form.id, payload)
      ElMessage.success('项目已更新')
    }
    dialogVisible.value = false
    await load()
  } finally {
    saving.value = false
  }
}

async function remove(row: Project): Promise<void> {
  await ElMessageBox.confirm(
    `删除项目「${row.name}」将同时删除其下全部子系统与报价单，且不可恢复。确认删除？`,
    '高危操作',
    { confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning' },
  )
    .then(async () => {
      await projectApi.remove(row.id)
      ElMessage.success('项目已删除')
      await load()
    })
    .catch(() => undefined)
}

/** 进入报价编制：项目下没有报价单就自动建一张草稿。 */
async function openQuote(row: Project): Promise<void> {
  opening.value = row.id
  try {
    const page = await quoteApi.list({ project_id: row.id, page: 1, page_size: 1 })
    let quoteId = page.items[0]?.id
    if (!quoteId) {
      if (!auth.can('quote', 'create')) {
        ElMessage.warning('当前角色无法创建报价单，请联系销售或管理员')
        return
      }
      // 2026-10-05 用户要求：报价模式在建单时就必须确定，之后不可切换 ✓
      //   套定额 = 人工费按定额库取 ✓；手工 = 自己填人工费 ✓
      //   这里用两个明确命名的按钮 ✓（不选 = 取消建单 ✗ 不会悄悄建成 manual ✓）
      const mode = await ElMessageBox.confirm(
        '这张报价单用哪种报价模式？\n\n· 套定额：人工费按定额库套用（可 AI 预套定额）\n' +
          '· 手工人工费：人工费由你自行填写\n\n创建之后**不可切换**，请按本次报价的实际情况选择。',
        '选择报价模式',
        {
          confirmButtonText: '套定额模式',
          cancelButtonText: '手工人工费',
          distinguishCancelAndClose: true,
          type: 'info',
          customClass: 'mode-pick-box',
        },
      ).then(() => 'quota').catch((a: string) => (a === 'cancel' ? 'manual' : null))
      if (mode === null) return
      const created = await quoteApi.create({ project_id: row.id, install_mode: mode })
      quoteId = created.id
    }
    await router.push({ name: 'quote-workbench', params: { projectId: row.id, quoteId } })
  } finally {
    opening.value = null
  }
}

/** 进入子系统管理：独立页面，用于建/删子系统。 */
async function openSubsystems(row: Project): Promise<void> {
  await router.push({ name: 'project-subsystems', params: { projectId: row.id } })
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">项目与报价</h2>
        <p class="page-sub">共 {{ total }} 个项目 · 点击「编制报价」进入报价编制主界面</p>
      </div>
      <el-button v-if="auth.can('project', 'create')" type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>&nbsp;新建项目
      </el-button>
    </div>

    <div class="card">
      <el-form inline class="inline-form" @submit.prevent>
        <el-form-item label="关键字">
          <el-input
            v-model="query.keyword"
            placeholder="项目编号 / 名称 / 客户"
            clearable
            style="width: 240px"
            @keyup.enter="resetAndLoad"
            @clear="resetAndLoad"
          />
        </el-form-item>
        <el-form-item label="状态">
          <el-select
            v-model="query.status"
            placeholder="全部"
            clearable
            style="width: 140px"
            @change="resetAndLoad"
          >
            <el-option
              v-for="o in meta.options('project_status')"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="resetAndLoad">
            <el-icon><Search /></el-icon>&nbsp;查询
          </el-button>
        </el-form-item>
      </el-form>

      <el-table
        v-loading="loading"
        :data="rows"
        row-key="id"
        border
        stripe
        @expand-change="onExpandChange"
      >
        <!-- 展开行：这一单下面的报价单 + 快捷审批动作 -->
        <el-table-column type="expand" width="44">
          <template #default="{ row }">
            <div class="quote-panel">
              <div class="quote-panel-head">
                <span>报价单（{{ row.quote_count }}）</span>
                <el-button
                  v-if="auth.can('quote', 'create')"
                  link
                  type="primary"
                  @click="openQuote(row)"
                >
                  + 新建报价单
                </el-button>
              </div>
              <el-table
                v-loading="quotesLoading[row.id]"
                :data="quotesByProject[row.id] ?? []"
                size="small"
                border
              >
                <el-table-column prop="code" label="报价单号" width="180" />
                <el-table-column label="版本" width="70">
                  <template #default="{ row: q }">v{{ q.version }}</template>
                </el-table-column>
                <el-table-column label="状态" width="100">
                  <template #default="{ row: q }">
                    <el-tag
                      size="small"
                      effect="plain"
                      :type="quoteLocked(q) ? 'success' : 'info'"
                    >
                      {{ meta.label('quote_status', q.status) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="含税合计" width="140" align="right">
                  <template #default="{ row: q }">
                    <span class="num">{{ money(q.total_with_tax) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="更新时间" min-width="150">
                  <template #default="{ row: q }">{{ dateTime(q.updated_at) }}</template>
                </el-table-column>
                <el-table-column label="操作" width="260" fixed="right">
                  <template #default="{ row: q }">
                    <div class="table-actions">
                      <el-button link type="primary" @click="openQuoteEditor(row.id, q.id)">
                        编制
                      </el-button>
                      <el-button
                        v-if="quoteCanSubmit(q)"
                        link
                        :loading="quoteBusy === q.id"
                        @click="changeQuoteStatus(q, 'submitted', '提交审核')"
                      >
                        {{ q.status === 'rejected' ? '重新提交' : '提交审核' }}
                      </el-button>
                      <!-- 接手是批准/驳回的前置步骤，所以列表里也要能点 -->
                      <el-button
                        v-if="quoteCanStartReview(q)"
                        link
                        type="primary"
                        :loading="quoteBusy === q.id"
                        @click="changeQuoteStatus(q, 'reviewing', '开始审核')"
                      >
                        开始审核
                      </el-button>
                      <template v-if="quoteCanDecide(q)">
                        <el-button
                          link
                          type="success"
                          :loading="quoteBusy === q.id"
                          @click="changeQuoteStatus(q, 'approved', '批准')"
                        >
                          批准
                        </el-button>
                        <el-button
                          link
                          type="danger"
                          :loading="quoteBusy === q.id"
                          @click="changeQuoteStatus(q, 'rejected', '驳回')"
                        >
                          驳回
                        </el-button>
                      </template>
                      <el-button
                        v-if="quoteCanRevert(q)"
                        link
                        :loading="quoteBusy === q.id"
                        @click="changeQuoteStatus(q, 'draft', '退回草稿')"
                      >
                        退回草稿
                      </el-button>
                    </div>
                  </template>
                </el-table-column>
                <template #empty>
                  <el-empty description="还没有报价单" :image-size="60" />
                </template>
              </el-table>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="code" label="项目编号" width="140" />
        <el-table-column prop="name" label="项目名称" min-width="200" show-overflow-tooltip />
        <el-table-column prop="customer_name" label="客户" min-width="150" show-overflow-tooltip />
        <el-table-column label="项目类型" width="110">
          <template #default="{ row }">
            {{ meta.label('project_type', row.project_type) }}
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">
              {{ meta.label('project_status', row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="报价单" width="110">
          <template #default="{ row }">
            <span v-if="row.quote_count" class="text-muted">{{ row.quote_count }} 张</span>
            <span v-else class="text-muted">—</span>
            <!-- 待审批徽标：经理一眼看到"哪几单在等我" -->
            <el-tag
              v-if="row.pending_quote_count"
              size="small"
              type="warning"
              effect="dark"
              class="ml"
            >
              待批 {{ row.pending_quote_count }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="期望开工" width="110">
          <template #default="{ row }">{{ dateOnly(row.expected_start) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="330" fixed="right">
          <template #default="{ row }">
            <div class="table-actions">
              <el-button type="primary" link :loading="opening === row.id" @click="openQuote(row)">
                编制报价
              </el-button>
              <el-button link @click="openSubsystems(row)">
                子系统
              </el-button>
              <el-button v-if="canSeeAudit" link @click="openAudit(row)">
                变更记录
              </el-button>
              <el-button v-if="auth.can('project', 'update')" link @click="openEdit(row)">
                编辑
              </el-button>
              <el-button
                v-if="auth.can('project', 'delete')"
                type="danger"
                link
                @click="remove(row)"
              >
                删除
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无项目，点击右上角「新建项目」开始" />
        </template>
      </el-table>

      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.page_size"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        class="pager"
        @current-change="load"
        @size-change="resetAndLoad"
      />
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新建项目' : '编辑项目'"
      width="720px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="96px">
        <div class="grid-2">
          <el-form-item label="项目编号" prop="code">
            <el-input
              v-model="form.code"
              :disabled="dialogMode === 'edit'"
              placeholder="如 NB2026-001"
            >
              <template #append>
                <el-button
                  v-if="dialogMode === 'create'"
                  :loading="codeLoading"
                  @click="generateCode"
                >
                  自动生成
                </el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="项目类型">
            <el-select v-model="form.project_type" style="width: 100%">
              <el-option
                v-for="o in meta.options('project_type')"
                :key="o.value"
                :label="o.label"
                :value="o.value"
              />
            </el-select>
          </el-form-item>
        </div>

        <el-form-item label="项目名称" prop="name">
          <el-input v-model="form.name" placeholder="如 宁波某某医院智能化弱电工程" />
        </el-form-item>

        <div class="grid-2">
          <el-form-item label="客户档案">
            <el-select
              v-model="form.customer_id"
              filterable
              remote
              clearable
              :remote-method="searchCustomers"
              :loading="customerLoading"
              placeholder="选一家 → 自动带出联系人与电话"
              style="width: 100%"
              @change="applyCustomer"
            >
              <el-option
                v-for="c in customerOptions"
                :key="c.id"
                :label="`${c.name}（${c.code}）`"
                :value="c.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="客户名称" prop="customer_name">
            <el-input v-model="form.customer_name" />
          </el-form-item>
          <el-form-item label="客户联系人">
            <el-input v-model="form.customer_contact" />
          </el-form-item>
        </div>

        <div class="grid-2">
          <el-form-item label="联系电话">
            <el-input v-model="form.customer_phone" />
          </el-form-item>
          <el-form-item label="期望开工">
            <el-date-picker
              v-model="form.expected_start"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="选择日期"
              style="width: 100%"
            />
          </el-form-item>
        </div>

        <el-form-item label="项目地址">
          <el-input v-model="form.address" />
        </el-form-item>

        <el-form-item label="状态">
          <!-- 只列**能改成的**状态（后端状态机的唯一真相）：
               给全部 6 个值、点下去再 409，是在拿用户当校验器。
               新建时默认草稿，可选的只有「报价中」（边谈边做） -->
          <el-select v-model="form.status" style="width: 100%">
            <el-option
              v-for="s in formStatusChoices"
              :key="s"
              :label="meta.label('project_status', s)"
              :value="s"
            />
          </el-select>
          <span v-if="dialogMode === 'edit'" class="text-muted" style="margin-left: 8px">
            仅列出可从「{{ meta.label('project_status', baseStatus) }}」流转到的状态
          </span>
        </el-form-item>

        <el-form-item label="备注">
          <el-input v-model="form.notes" type="textarea" :rows="2" />
        </el-form-item>

        <!-- 项目级费率（H-12）：这些值会在**新建报价单**时抄进报价单作为快照 -->
        <el-collapse class="rate-collapse">
          <el-collapse-item name="rates">
            <template #title>
              <span class="rate-title">项目费率</span>
              <span class="text-muted rate-sub">
                新建报价单时作为默认值抄入（税率 {{ form.tax_rate_pct }}% ·
                设计 {{ form.design_rate_pct }}% · 管理+利润 {{ form.mgmt_profit_rate_pct }}%）
              </span>
            </template>
            <p class="text-muted rate-hint">
              这里配的是**默认值快照**：建报价单时抄一份进去，之后改这里<strong>不会</strong>
              回改已建的报价单（避免历史报价被静默改数）。已建单要改费率，去报价编制页改。
            </p>
            <div class="grid-3">
              <el-form-item label="税率 %">
                <el-input-number v-model="form.tax_rate_pct" :min="0" :max="100"
                                 :precision="2" controls-position="right" style="width: 100%" />
              </el-form-item>
              <el-form-item label="费用基数">
                <el-select v-model="form.fee_base" style="width: 100%">
                  <el-option v-for="o in meta.options('fee_base')" :key="o.value"
                             :label="o.label" :value="o.value" />
                </el-select>
              </el-form-item>
              <el-form-item label="辅材基数">
                <el-select v-model="form.auxiliary_base" style="width: 100%">
                  <el-option v-for="o in meta.options('auxiliary_base')" :key="o.value"
                             :label="o.label" :value="o.value" />
                </el-select>
              </el-form-item>
            </div>
            <div class="grid-3">
              <el-form-item label="辅材费率 %">
                <el-input-number v-model="form.auxiliary_rate_pct" :min="0" :max="100"
                                 :precision="2" controls-position="right" style="width: 100%" />
              </el-form-item>
              <el-form-item label="设计费率 %">
                <el-input-number v-model="form.design_rate_pct" :min="0" :max="100"
                                 :precision="2" controls-position="right" style="width: 100%" />
              </el-form-item>
              <el-form-item label="维保费率 %">
                <el-input-number v-model="form.warranty_rate_pct" :min="0" :max="100"
                                 :precision="2" controls-position="right" style="width: 100%" />
              </el-form-item>
            </div>
            <div class="grid-3">
              <el-form-item label="管理费+利润 %">
                <el-input-number v-model="form.mgmt_profit_rate_pct" :min="0" :max="100"
                                 :precision="2" controls-position="right" style="width: 100%" />
              </el-form-item>
              <el-form-item label="培训费（元）">
                <el-input-number v-model="form.training_fee" :min="0" :precision="2"
                                 controls-position="right" style="width: 100%" />
              </el-form-item>
            </div>
          </el-collapse-item>
        </el-collapse>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 项目操作留痕（状态变更记录：谁在什么时候把它改成了什么） -->
    <el-dialog v-model="auditVisible" :title="`变更记录 · ${auditTitle}`" width="560px">
      <el-table v-loading="auditLoading" :data="auditRows" size="small" border>
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ dateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="动作" width="90">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.action }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态变化" min-width="180">
          <template #default="{ row }">{{ auditText(row) }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有状态变更记录" :image-size="60" />
        </template>
      </el-table>
      <template #footer>
        <el-button @click="auditVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}

/* 展开行里的报价单子表：缩进一点，视觉上从属于上面那一行 */
.quote-panel {
  padding: 8px 12px 12px 48px;
  background: var(--el-fill-color-lighter);
}

.quote-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 6px;
}

.ml {
  margin-left: 6px;
}
/* 项目费率折叠区：默认收起，避免新建项目时被 8 个输入框淹没 */
.rate-collapse {
  margin-top: 4px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.rate-title {
  font-weight: 600;
}

.rate-sub {
  margin-left: 10px;
  font-size: 12px;
}

.rate-hint {
  margin: 0 0 10px 0;
  font-size: 12px;
  line-height: 1.6;
}
</style>


