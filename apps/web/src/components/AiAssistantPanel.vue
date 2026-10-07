<script setup lang="ts">
/**
 * AI 助理（2026-10-04 重做：从"表单"改成"对话"）。
 *
 * 用户要的是"像人一样直接语言交流"，所以重点在四件事：
 *   · **多轮对话**：消息落库，刷新/换设备能接着聊，历史可回看；
 *   · **流式输出**：边生成边显示（`streamChat` 用 fetch 读 SSE，axios 拿不到增量）；
 *   · **工具过程可见**：助理查了什么、查到几条都显示成小标签 ——
 *     不显示过程，用户没法判断这个数字可不可信；
 *   · **写操作要确认**：助理只能产出"待确认"，落库还是要人点。
 *
 * 悬浮窗可手动开关，状态记在 localStorage。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { CloseBold, Loading, Select } from '@element-plus/icons-vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import { renderMarkdown } from '@/utils/markdown'
import { aiApi, chatApi, streamChat, supplierApi } from '@/api'
import type { AiParseItem, AiParseResult } from '@/api/types'

const OPEN_KEY = 'ai-panel-open'
const open = ref(localStorage.getItem(OPEN_KEY) === '1')

function toggle(): void {
  open.value = !open.value
  localStorage.setItem(OPEN_KEY, open.value ? '1' : '0')
}

// ---------- 会话与消息 ----------
interface UiTool {
  name: string
  label: string
  ok: boolean
  summary: string
  running: boolean
}
interface UiAttachment {
  job_id: string
  filename: string
  item_count: number
}
interface UiMsg {
  id: string
  role: 'user' | 'assistant'
  content: string
  tools: UiTool[]
  attachments: UiAttachment[]
  streaming?: boolean
  error?: string
}

const conversationId = ref<string | null>(null)
const messages = ref<UiMsg[]>([])
const input = ref('')
const sending = ref(false)
const convList = ref<
  { id: string; title: string; message_count: number; last_message_at: string | null }[]
>([])
const showHistory = ref(false)
const listEl = ref<HTMLElement | null>(null)
let abortCtl: AbortController | null = null

function scrollToBottom(): void {
  void nextTick(() => {
    const el = listEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function newConversation(): void {
  conversationId.value = null
  messages.value = []
  showHistory.value = false
  input.value = ''
}

async function loadConversations(): Promise<void> {
  try {
    convList.value = (await chatApi.conversations()).items
  } catch {
    convList.value = []
  }
}

/** 从落库的工具结果里抠一句人话摘要（历史回看时用） */
function summarizeTool(raw: string | null): string {
  if (!raw) return '已执行'
  try {
    const d = JSON.parse(raw) as Record<string, unknown>
    if (typeof d.error === 'string') return `失败：${d.error}`
    if (Array.isArray(d.devices)) return `找到 ${d.count ?? d.devices.length} 台设备`
    if (Array.isArray(d.quotes)) return `共 ${d.count ?? d.quotes.length} 张报价单`
    if (Array.isArray(d.projects)) return `找到 ${d.count ?? d.projects.length} 个项目`
    if (Array.isArray(d.customers)) return `找到 ${d.count ?? d.customers.length} 个客户`
    if (Array.isArray(d.items)) return `读到 ${d.items.length} 行候选`
    if (d.total_with_tax !== undefined) return `含税 ${d.total_with_tax}`
    if (d.verdict) return String(d.verdict)
    if (d.needs_confirmation) return '已生成待确认'
    return '已执行'
  } catch {
    return '已执行'
  }
}

async function openConversation(id: string): Promise<void> {
  try {
    const detail = await chatApi.messages(id)
    conversationId.value = id
    showHistory.value = false
    const out: UiMsg[] = []
    for (const m of detail.messages) {
      if (m.role === 'user') {
        out.push({
          id: m.id,
          role: 'user',
          content: m.content ?? '',
          tools: [],
          attachments: (m.attachments ?? []) as unknown as UiAttachment[],
        })
      } else if (m.role === 'assistant') {
        out.push({
          id: m.id,
          role: 'assistant',
          content: m.content ?? '',
          tools: [],
          attachments: [],
        })
      } else if (m.role === 'tool') {
        const last = out[out.length - 1]
        if (last && last.role === 'assistant') {
          last.tools.push({
            name: m.tool_name ?? '',
            label: m.tool_name ?? '',
            ok: !(m.content ?? '').includes('"error"'),
            summary: summarizeTool(m.content),
            running: false,
          })
        }
      }
    }
    messages.value = out
    scrollToBottom()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '加载会话失败')
  }
}

async function removeConversation(id: string): Promise<void> {
  try {
    await ElMessageBox.confirm('删除这次对话？', '删除对话', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await chatApi.remove(id)
    if (conversationId.value === id) newConversation()
    await loadConversations()
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '删除失败')
  }
}

// ---------- 发消息（流式）----------
function onEvent(ev: Record<string, unknown>, target: UiMsg): void {
  const type = String(ev.type ?? '')
  if (type === 'conversation' && ev.conversation_id) {
    conversationId.value = String(ev.conversation_id)
  } else if (type === 'delta') {
    target.content += String(ev.text ?? '')
    scrollToBottom()
  } else if (type === 'tool_start') {
    target.tools.push({
      name: String(ev.name ?? ''),
      label: String(ev.label ?? ev.name ?? ''),
      ok: true,
      summary: '执行中…',
      running: true,
    })
    scrollToBottom()
  } else if (type === 'tool_result') {
    const running = [...target.tools].reverse().find((t) => t.running)
    if (running) {
      running.running = false
      running.ok = Boolean(ev.ok)
      running.summary = String(ev.summary ?? '完成')
    }
  } else if (type === 'error') {
    target.error = String(ev.message ?? '出错了')
  }
}

async function send(): Promise<void> {
  const text = input.value.trim()
  if (!text || sending.value) return

  const attachments = pendingParse.value
    ? [
        {
          kind: 'parse_job',
          job_id: pendingParse.value.job_id,
          // 兜底：文件名丢了也要有个可读的占位，别把 undefined 存进消息里
          // （存进去之后连历史记录都会一直显示 undefined）
          filename: pendingParse.value.filename || '附件',
          item_count: pendingParse.value.items.length,
        },
      ]
    : []

  messages.value.push({
    id: `u-${Date.now()}`,
    role: 'user',
    content: text,
    tools: [],
    attachments: attachments.map((a) => ({
      job_id: String(a.job_id),
      filename: String(a.filename),
      item_count: Number(a.item_count),
    })),
  })
  const target: UiMsg = {
    id: `a-${Date.now()}`,
    role: 'assistant',
    content: '',
    tools: [],
    attachments: [],
    streaming: true,
  }
  messages.value.push(target)
  input.value = ''
  sending.value = true
  scrollToBottom()

  abortCtl = new AbortController()
  try {
    await streamChat(
      { message: text, conversation_id: conversationId.value, attachments },
      (ev) => onEvent(ev, target),
      abortCtl.signal,
    )
  } catch (e) {
    target.error = e instanceof Error ? e.message : '对话失败'
  } finally {
    target.streaming = false
    sending.value = false
    abortCtl = null
    scrollToBottom()
    void loadConversations()
  }
}

function stop(): void {
  abortCtl?.abort()
  sending.value = false
}

function onKeydown(e: KeyboardEvent): void {
  // Enter 发送、Shift+Enter 换行（聊天框的肌肉记忆）
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    void send()
  }
}

// ---------- 附件：解析供应商报价 ----------
const pendingParse = ref<AiParseResult | null>(null)
const parseDialog = ref(false)
const uploading = ref(false)
const suppliers = ref<{ id: string; name: string; code: string }[]>([])
const supplierId = ref<string | null>(null)
/** 解析方式（2026-10-05）：默认"自动"—— 先按规则试，表头认不出来才调大模型。
 *  用户明确要过这个开关：他问过"这份 Excel 到底是内置程序解析还是大模型解析"。 */
const engineMode = ref<'auto' | 'rule' | 'llm'>('auto')
const rows = ref<
  {
    pick: boolean
    name: string
    brand: string
    model: string
    unit: string
    quantity: number | null
    unit_price: number | null
    device_id: string | null
    verified: Record<string, boolean>
    warnings: string[]
    matchLabel: string
    alternatives: { device_id: string | null; code: string | null; name: string | null }[]
    priceVsRef: { reference_price: number; delta: number } | null
  }[]
>([])
const committing = ref(false)
const commitResult = ref<{
  batch_no: string
  updated: number
  skipped: number
  changes: string[]
  skipped_detail: { row: number | null; name: string; message: string }[]
} | null>(null)

const adoptedCount = computed(() => rows.value.filter((r) => r.pick).length)

/** 当前解析方式的中文名（确认框里显示，让人一眼知道这批候选行是怎么来的）。 */
const engineModeLabel = computed(
  () => ({ auto: '自动（先规则，认不出再大模型）', rule: '只用规则（不调模型）',
           llm: '强制大模型' })[engineMode.value],
)

/**
 * 换解析方式重跑：拿同一份文件按当前选择再来一次。
 *
 * 为什么需要：解析方式是**上传时**决定的，而 AI 助理无法替你重新上传
 * （它只能说"请在上传端换解析方式重跑"）。这个按钮把那句话变成一次点击。
 */
async function reparseWithMode(): Promise<void> {
  if (!lastFile) {
    ElMessage.warning('没有可重跑的文件（请重新选择一次文件）')
    return
  }
  pendingParse.value = null
  rows.value = []
  parseDialog.value = false
  await uploadQuote(lastFile)
}

function onFileChange(f: unknown): void {
  const raw = (f as { raw?: File }).raw
  if (raw) {
    // 记住这份文件：解析方式可以换，"换方式重跑"时不必让用户重新选文件
    lastFile = raw
    void uploadQuote(raw)
  }
}

/** 最近一次上传的文件（用于换解析方式重跑）。 */
let lastFile: File | null = null

/** 解析方式的中文说明（选择器上悬停可见）。 */
const ENGINE_MODE_HINT: Record<string, string> = {
  auto: '自动：先按内置规则解析，表头认不出来才调用大模型（省钱、够用，默认）',
  rule: '只用规则：绝不调用大模型，不产生模型费用（适合规整的价目表）',
  llm: '强制大模型：即使表头能识别也交给大模型（适合格式乱、需要语义判断的）',
}

/** 后台解析的进度状态（轮询用） */
const parseState = ref<'idle' | 'pending' | 'ready' | 'failed'>('idle')
const parseElapsed = ref(0)
let parseTimer: number | null = null

function stopParsePolling(): void {
  if (parseTimer !== null) {
    window.clearInterval(parseTimer)
    parseTimer = null
  }
}

/**
 * 上传供应商报价。
 *
 * **不再同步等模型**：后端只做"抽取 + 建任务"就返回 job_id，
 * 这里立刻把附件挂上并轮询进度 —— 以前同步等，最坏几分钟，界面一直转圈
 * （用户报的「一直在上传」）。
 */
async function uploadQuote(file: File): Promise<void> {
  uploading.value = true
  stopParsePolling()
  try {
    const res = await aiApi.parseSupplierQuote({
      file,
      supplierId: supplierId.value ?? undefined,
      engineMode: engineMode.value,
    })
    pendingParse.value = res
    buildRows(res.items)
    if (!input.value) {
      input.value = `这份报价（${res.filename}）有什么要注意的？帮我看看价格是否合理`
    }
    if (res.status === 'pending') {
      parseState.value = 'pending'
      parseElapsed.value = 0
      ElMessage.info('文件已收到，正在后台解析…（可以先聊别的，解析完我会更新）')
      parseTimer = window.setInterval(() => {
        parseElapsed.value += 2
        void pollParse(res.job_id)
      }, 2000)
    } else {
      parseState.value = res.items.length ? 'ready' : 'failed'
      ElMessage.success(`已解析 ${res.items.length} 行`)
    }
  } catch (e) {
    parseState.value = 'failed'
    ElMessage.error(e instanceof Error ? e.message : '解析失败')
  } finally {
    uploading.value = false
  }
}

/** 轮询后台解析结果（最多约 5 分钟，超时后提示重传） */
async function pollParse(jobId: string): Promise<void> {
  try {
    const job = await aiApi.parseJob(jobId)
    if (job.status === 'pending') {
      if (parseElapsed.value > 300) {
        stopParsePolling()
        parseState.value = 'failed'
        ElMessage.warning('解析超过 5 分钟仍未完成，建议把文件拆小或只传关键页后重新上传')
      }
      return
    }
    stopParsePolling()
    const r = job.result as AiParseResult
    if (job.status === 'ready' && r?.items) {
      // ⚠ 必须**保留原有字段**（尤其 filename）：`job.result` 只是解析结果，
      // 不含文件名 —— 直接 {...r} 会把附件名抹成 undefined，
      // 界面上就出现「附件：undefined（123 行）」。
      pendingParse.value = {
        ...pendingParse.value,
        ...r,
        // 后端详情接口也带 filename，优先用它（刷新页面后也能显示）
        filename: job.filename ?? pendingParse.value?.filename ?? '附件',
        job_id: jobId,
        status: 'ready',
      }
      buildRows(r.items)
      parseState.value = 'ready'
      ElMessage.success(
        `解析完成：${r.items.length} 行（${r.engine === 'rule' ? '规则解析，未产生模型费用' : '大模型解析'}）`,
      )
    } else {
      parseState.value = 'failed'
      const warn = (r?.warnings ?? []).slice(0, 2).join('；')
      ElMessage.error(`解析失败${warn ? `：${warn}` : ''}（可换一份文件或截图重传）`)
    }
  } catch {
    // 轮询失败不打断用户；下一次 tick 再试
  }
}

function buildRows(items: AiParseItem[]): void {
  rows.value = items.map((it) => ({
    pick: !!it.match?.device_id,
    name: it.name ?? '',
    brand: it.brand ?? '',
    model: it.model ?? '',
    unit: it.unit ?? it.match?.unit ?? '',
    quantity: it.quantity === null || it.quantity === undefined ? null : Number(it.quantity),
    unit_price:
      it.unit_price === null || it.unit_price === undefined ? null : Number(it.unit_price),
    // 对外价（京东控价/零售价）：与进价分开显示，别让人误以为进价那么高
    reference_price: it.reference_price ?? null,
    device_id: it.match?.device_id ?? null,
    verified: it.verified ?? {},
    warnings: it.warnings ?? [],
    matchLabel: it.match
      ? `${it.match.code} ${it.match.name}（${it.match.match_type === 'exact' ? '精确' : '模糊'}）`
      : '未匹配到设备库',
    alternatives: (it.match_alternatives ?? []).filter((x) => x.device_id),
    priceVsRef: it.price_vs_reference
      ? {
          reference_price: it.price_vs_reference.reference_price,
          delta: it.price_vs_reference.delta,
        }
      : null,
  }))
}

/** 清掉附件与轮询状态（模板里写多行内联表达式是非法的，所以收成方法） */
function clearAttachment(): void {
  pendingParse.value = null
  parseState.value = 'idle'
  stopParsePolling()
}

async function commitParse(): Promise<void> {
  if (!pendingParse.value) return
  const payloadRows = rows.value
    .filter((r) => r.pick)
    .map((r) => ({
      name: r.name,
      brand: r.brand || null,
      model: r.model || null,
      unit: r.unit || null,
      quantity: r.quantity,
      unit_price: r.unit_price ?? 0,
      device_id: r.device_id,
    }))
  if (!payloadRows.length) {
    ElMessage.warning('没有可落库的行（未匹配到设备的行请先在设备库建好）')
    return
  }
  committing.value = true
  try {
    commitResult.value = await aiApi.commitParse(pendingParse.value.job_id, {
      rows: payloadRows,
      supplier_id: supplierId.value,
    })
    ElMessage.success(
      `已落库：更新 ${commitResult.value.updated} 条（批次 ${commitResult.value.batch_no}）`,
    )
    pendingParse.value = null
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '落库失败')
  } finally {
    committing.value = false
  }
}

const EXAMPLES = [
  'AI-LOOP-1 这个型号库里多少钱？',
  '最近有哪些报价单在等审批？',
  '这个月的毛利情况怎么样？',
  '哪些单子毛利偏低？',
]

onBeforeUnmount(() => stopParsePolling())

onMounted(async () => {
  await loadConversations()
  try {
    const page = await supplierApi.list({ page_size: 100 })
    suppliers.value = page.items.map((s) => ({ id: s.id, name: s.name, code: s.code }))
  } catch {
    suppliers.value = []
  }
})
</script>

<template>
  <div class="ai-root no-print">
    <button class="ai-fab" :class="{ 'is-open': open }" title="AI 助理" @click="toggle">
      <span v-if="!open">AI</span>
      <span v-else>×</span>
    </button>

    <transition name="ai-slide">
      <aside v-if="open" class="ai-panel">
        <header class="ai-head">
          <div class="ai-title">
            <b>AI 助理</b>
            <span class="text-muted ai-sub">
              {{ conversationId ? '继续对话' : '可以问我设备价、报价单、毛利…' }}
            </span>
          </div>
          <div class="ai-head-actions">
            <el-button link size="small" @click="newConversation">新对话</el-button>
            <el-button link size="small" @click="showHistory = !showHistory">历史</el-button>
            <el-button link size="small" @click="toggle">收起</el-button>
          </div>
        </header>

        <div v-if="showHistory" class="ai-history">
          <el-empty v-if="!convList.length" description="还没有历史对话" :image-size="50" />
          <div v-for="cv in convList" :key="cv.id" class="ai-history-item">
            <span class="ai-history-title" @click="openConversation(cv.id)">{{ cv.title }}</span>
            <span class="text-muted ai-history-meta">{{ cv.message_count }} 条</span>
            <el-button link type="danger" size="small" @click="removeConversation(cv.id)">
              删除
            </el-button>
          </div>
        </div>

        <div ref="listEl" class="ai-list">
          <el-empty
            v-if="!messages.length"
            description="问点什么？比如「XX 型号多少钱」「哪些单毛利偏低」"
            :image-size="56"
          />
          <div v-if="!messages.length" class="ai-examples">
            <el-tag
              v-for="ex in EXAMPLES"
              :key="ex"
              class="ai-example"
              effect="plain"
              @click="input = ex"
            >
              {{ ex }}
            </el-tag>
          </div>

          <div
            v-for="m in messages"
            :key="m.id"
            class="ai-msg"
            :class="m.role === 'user' ? 'is-user' : 'is-assistant'"
          >
            <div class="ai-bubble">
              <div v-if="m.attachments.length" class="ai-attach">
                <el-tag
                  v-for="a in m.attachments"
                  :key="a.job_id"
                  size="small"
                  type="info"
                  effect="plain"
                >
                  附件：{{ a.filename || '附件' }}（{{ a.item_count }} 行）
                </el-tag>
              </div>
              <!-- AI 的回答本来就是 Markdown（表格/列表/粗体），渲染出来才看得懂；
                   用户自己的话保持纯文本（免得他打的 * 号被当成语法）。
                   渲染器禁用了原始 HTML，见 utils/markdown.ts 的说明。 -->
              <div
                v-if="m.content && m.role !== 'user'"
                class="ai-text ai-md"
                v-html="renderMarkdown(m.content)"
              />
              <div v-else-if="m.content" class="ai-text">{{ m.content }}</div>
              <span v-if="m.streaming && !m.content" class="ai-typing">思考中…</span>

              <div v-if="m.tools.length" class="ai-tools">
                <div
                  v-for="(t, i) in m.tools"
                  :key="i"
                  class="ai-tool"
                  :class="{ 'is-err': !t.ok }"
                >
                  <el-icon v-if="t.running" class="is-loading"><Loading /></el-icon>
                  <el-icon v-else-if="t.ok"><Select /></el-icon>
                  <el-icon v-else><CloseBold /></el-icon>
                  <span>{{ t.label }}</span>
                  <span class="text-muted">· {{ t.summary }}</span>
                </div>
              </div>

              <el-alert
                v-if="m.error"
                type="error"
                :closable="false"
                show-icon
                class="mt4"
                :title="m.error"
              />
            </div>
          </div>
        </div>

        <div v-if="pendingParse" class="ai-pending">
          <el-icon v-if="parseState === 'pending'" class="is-loading"><Loading /></el-icon>
          <div>
            <b>{{ pendingParse.filename || '附件' }}</b>
            <span class="text-muted">
              <template v-if="parseState === 'pending'">
                · 后台解析中（已 {{ parseElapsed }} 秒）—— 可以先聊别的
              </template>
              <template v-else-if="parseState === 'failed'">
                · 解析失败，可重新上传
              </template>
              <template v-else>
                · 解析 {{ pendingParse.items.length }} 行，待落库 {{ adoptedCount }} 行
              </template>
            </span>
          </div>
          <el-button
            size="small"
            type="primary"
            plain
            :disabled="parseState !== 'ready'"
            @click="parseDialog = true"
          >
            查看/确认
          </el-button>
          <el-button size="small" link @click="clearAttachment">移除附件</el-button>
        </div>

        <div class="ai-composer">
          <el-input
            v-model="input"
            type="textarea"
            :rows="2"
            resize="none"
            placeholder="说点什么…（Enter 发送，Shift+Enter 换行）"
            :disabled="sending"
            @keydown="onKeydown"
          />
          <div class="ai-composer-actions">
            <el-upload
              :auto-upload="false"
              :show-file-list="false"
              :limit="1"
              accept=".xlsx,.xlsm,.pdf,.png,.jpg,.jpeg,.webp,.csv,.txt"
              :on-change="onFileChange"
            >
              <el-button size="small" :loading="uploading">上传报价</el-button>
            </el-upload>
            <!-- 解析方式（2026-10-05）
                 ⚠ 这里**必须**用分段按钮而不是 el-select：面板是 z-index 2100 的
                 固定侧边栏，而下拉浮层会被传送到 body 且层级更低 —— 浮层会落在
                 面板底下，用户"点得着却选不中"（用户报障：只能选自动解析）。
                 分段按钮不产生浮层，常驻可见、一次点击。 -->
            <el-radio-group v-model="engineMode" size="small">
              <el-radio-button value="auto" :title="ENGINE_MODE_HINT.auto">
                自动
              </el-radio-button>
              <el-radio-button value="rule" :title="ENGINE_MODE_HINT.rule">
                只用规则
              </el-radio-button>
              <el-radio-button value="llm" :title="ENGINE_MODE_HINT.llm">
                强制大模型
              </el-radio-button>
            </el-radio-group>
            <el-dropdown v-if="suppliers.length" trigger="click">
              <el-button size="small" link>
                {{
                  supplierId
                    ? suppliers.find((s) => s.id === supplierId)?.name
                    : '供应商（选填）'
                }}
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item @click="supplierId = null">不指定</el-dropdown-item>
                  <el-dropdown-item
                    v-for="s in suppliers"
                    :key="s.id"
                    @click="supplierId = s.id"
                  >
                    {{ s.name }}
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <div class="ai-spacer" />
            <el-button v-if="sending" size="small" @click="stop">停止</el-button>
            <el-button
              v-else
              size="small"
              type="primary"
              :disabled="!input.trim()"
              @click="send"
            >
              发送
            </el-button>
          </div>
        </div>
      </aside>
    </transition>

    <el-dialog
      v-model="parseDialog"
      title="供应商报价 · 确认落库"
      width="900px"
      append-to-body
    >
      <div class="ai-rowline mb" style="align-items: center; gap: 8px">
        <span class="ai-hint">当前解析方式：{{ engineModeLabel }}</span>
        <el-button
          size="small"
          :loading="uploading"
          :disabled="!lastFile"
          @click="reparseWithMode"
        >
          换解析方式重跑
        </el-button>
        <span class="ai-hint">
          规则没认出来就换「强制大模型」；不想产生模型费用就选「只用规则」。
        </span>
      </div>
      <el-alert
        v-for="(w, i) in pendingParse?.warnings ?? []"
        :key="i"
        type="info"
        :closable="false"
        show-icon
        class="mb"
        :title="w"
      />
      <el-table :data="rows" size="small" border max-height="50vh">
        <el-table-column label="采用" width="54">
          <template #default="{ row }">
            <el-checkbox v-model="row.pick" :disabled="!row.device_id" />
          </template>
        </el-table-column>
        <el-table-column label="内容（可改）" min-width="240">
          <template #default="{ row }">
            <el-input v-model="row.name" size="small" placeholder="名称" />
            <el-input
              v-model="row.model"
              size="small"
              placeholder="型号"
              class="mt4"
              :class="{ 'ai-unverified': row.verified.model === false }"
            />
            <div class="ai-rowline">
              <el-input-number
                v-model="row.quantity"
                size="small"
                :min="0"
                :controls="false"
                style="width: 78px"
              />
              <el-input-number
                v-model="row.unit_price"
                size="small"
                :min="0"
                :precision="2"
                :controls="false"
                style="width: 100px"
                :class="{ 'ai-unverified': row.verified.unit_price === false }"
              />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="参考价" width="96">
          <!-- 对外价（京东控价/零售价）：与「进价」分开显示，避免混淆 -->
          <template #default="{ row }">
            <span :class="row.reference_price ? '' : 'text-muted'">
              {{ row.reference_price ?? '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="匹配设备库" min-width="190">
          <template #default="{ row }">
            <div :class="row.device_id ? '' : 'danger-text'" style="font-size: 12px">
              {{ row.matchLabel }}
            </div>
            <el-select
              v-if="row.alternatives.length"
              size="small"
              clearable
              placeholder="换一个"
              style="width: 100%; margin-top: 4px"
              @change="(v: string | null) => v && (row.device_id = v)"
            >
              <el-option
                v-for="a in row.alternatives"
                :key="a.device_id ?? ''"
                :label="`${a.code} ${a.name}`"
                :value="a.device_id ?? ''"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="核对" width="140">
          <template #default="{ row }">
            <el-tag
              v-if="'model' in row.verified"
              size="small"
              effect="plain"
              :type="row.verified.model === false ? 'danger' : 'success'"
            >
              型号{{ row.verified.model === false ? '?' : '✓' }}
            </el-tag>
            <el-tag
              v-if="'unit_price' in row.verified"
              size="small"
              effect="plain"
              style="margin-left: 4px"
              :type="row.verified.unit_price === false ? 'danger' : 'success'"
            >
              单价{{ row.verified.unit_price === false ? '?' : '✓' }}
            </el-tag>
            <div
              v-for="(w, i) in row.warnings"
              :key="i"
              class="danger-text"
              style="font-size: 11px"
            >
              {{ w }}
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="commitResult" class="mt">
        <el-alert
          type="success"
          :closable="false"
          show-icon
          :title="`已落库：更新 ${commitResult.updated} 条，批次 ${commitResult.batch_no}`"
        />
        <div v-for="(c, i) in commitResult.changes" :key="i" class="mono" style="font-size: 12px">
          {{ c }}
        </div>
      </div>

      <template #footer>
        <el-button @click="parseDialog = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="committing"
          :disabled="!adoptedCount || !!commitResult"
          @click="commitParse"
        >
          确认落库（{{ adoptedCount }} 行）
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ai-fab {
  position: fixed;
  right: 18px;
  bottom: 28px;
  z-index: 2100;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  border: none;
  cursor: pointer;
  background: var(--el-color-primary);
  color: #fff;
  font-weight: 700;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.22);
}
.ai-fab.is-open {
  background: var(--el-color-info);
}
.ai-panel {
  position: fixed;
  right: 0;
  top: 56px;
  bottom: 0;
  width: 520px;
  max-width: 96vw;
  z-index: 2050;
  background: var(--el-bg-color);
  border-left: 1px solid var(--el-border-color-light);
  box-shadow: -4px 0 18px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  padding: 8px 12px 10px;
}
.ai-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.ai-sub {
  font-size: 12px;
  margin-left: 8px;
}
.ai-head-actions {
  display: flex;
  gap: 2px;
}
.ai-history {
  border-bottom: 1px dashed var(--el-border-color-lighter);
  max-height: 180px;
  overflow-y: auto;
  padding: 4px 0;
}
.ai-history-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 2px;
  font-size: 13px;
}
.ai-history-title {
  flex: 1;
  cursor: pointer;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ai-history-meta {
  font-size: 11px;
}
.ai-list {
  flex: 1;
  overflow-y: auto;
  padding: 10px 2px;
}
.ai-examples {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}
.ai-example {
  cursor: pointer;
}
.ai-msg {
  display: flex;
  margin-bottom: 10px;
}
.ai-msg.is-user {
  justify-content: flex-end;
}
.ai-bubble {
  max-width: 88%;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  font-size: 13px;
  line-height: 1.6;
}
.ai-msg.is-user .ai-bubble {
  background: var(--el-color-primary-light-8);
}
.ai-text {
  white-space: pre-wrap;
  word-break: break-word;
}
.ai-typing {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.ai-tools {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ai-tool {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  color: var(--el-color-primary);
}
.ai-tool.is-err {
  color: var(--el-color-danger);
}
.ai-attach {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}
.ai-pending {
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--el-color-primary-light-5);
  border-radius: 6px;
  padding: 6px 8px;
  margin-bottom: 6px;
  font-size: 12px;
}
.ai-composer {
  border-top: 1px solid var(--el-border-color-lighter);
  padding-top: 6px;
}
.ai-composer-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}
.ai-spacer {
  flex: 1;
}
.ai-rowline {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}
.mt4 {
  margin-top: 4px;
}
.ai-unverified :deep(.el-input__wrapper) {
  box-shadow: 0 0 0 1px var(--el-color-danger) inset;
}
.ai-slide-enter-active,
.ai-slide-leave-active {
  transition: transform 0.2s ease;
}
.ai-slide-enter-from,
.ai-slide-leave-to {
  transform: translateX(100%);
}
@media print {
  .ai-root {
    display: none !important;
  }
}

/* AI 回答的 Markdown 样式：面板窄，留白与字号都收紧一点 */
.ai-md :deep(p) {
  margin: 0 0 6px;
}
.ai-md :deep(p:last-child) {
  margin-bottom: 0;
}
.ai-md :deep(h1),
.ai-md :deep(h2),
.ai-md :deep(h3),
.ai-md :deep(h4) {
  font-size: 13px;
  font-weight: 600;
  margin: 8px 0 4px;
}
.ai-md :deep(ul),
.ai-md :deep(ol) {
  margin: 4px 0 6px;
  padding-left: 18px;
}
.ai-md :deep(li) {
  margin: 2px 0;
}
.ai-md :deep(code) {
  background: #f2f3f5;
  padding: 1px 4px;
  border-radius: 3px;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
}
.ai-md :deep(pre) {
  background: #f7f8fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 8px;
  overflow-x: auto;
  margin: 6px 0;
}
.ai-md :deep(pre code) {
  background: none;
  padding: 0;
}
.ai-md :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 6px 0;
  font-size: 12px;
  display: block;
  overflow-x: auto;
}
.ai-md :deep(th),
.ai-md :deep(td) {
  border: 1px solid #ebeef5;
  padding: 3px 6px;
  text-align: left;
}
.ai-md :deep(th) {
  background: #f5f7fa;
  font-weight: 600;
}
.ai-md :deep(blockquote) {
  margin: 6px 0;
  padding-left: 8px;
  border-left: 3px solid #dcdfe6;
  color: #606266;
}
.ai-md :deep(a) {
  color: #409eff;
}
.ai-md :deep(hr) {
  border: none;
  border-top: 1px solid #ebeef5;
  margin: 8px 0;
}
</style>
