<script setup lang="ts">
/**
 * 报价单预览 · 客户版（按原型第 3 页，2026-10-03）。
 *
 * 版式依据：`原型规格_坐标级.md` / `原型页面/proto-3.png`：
 *   ① 顶栏：← 报价单预览 · 客户版 ｜ 说明「送客户前可先预览确认；成本、毛利、采购价字段自动隐藏」｜ 报价单号
 *   ② 工具条：纸张 / 模板 / ☑显示品牌型号 / ☐安装费单列 / ☐显示成本毛利（客户版不可选）｜ 下载 PDF ｜ 打印
 *   ③ 文档：公司抬头 + 标题 + 信息表(2 列 4 行) + 按子系统分组的明细表 + 小计 + 报价说明 + 合计 + 签章区
 *
 * 三个真实生效的开关（不是摆设）：
 *   · 纸张     → 通过运行时注入 `@page` 改变实际打印纸张（A4 横向 / 纵向）
 *   · 显示品牌型号 → 控制「品牌」「型号规格」两列
 *   · 安装费单列   → 关：「单价 = 设备单价 + 安装单价」（与原型第 3 页的 680+80=760 一致）；
 *                    开：拆成「设备单价 / 安装单价 / 安装费」三列
 * 「显示成本毛利」在客户版永远不可选（置灰 + 说明），成本数据即使是主管也不渲染。
 *
 * ⚠ 两处如实标注、不粉饰的地方：
 *   1. 「报价说明」6 条条款**目前只存在于设计稿**：后端 PDF 导出（export_pdf.py）里没有这段，
 *      页面上用醒目标签标出，避免"预览有、导出没有"造成误判；
 *   2. 公司抬头取后端导出用的同一家公司名（export_excel.py / export_pdf.py 里的常量），
 *      系统里没有"公司信息配置"，所以还不支持改抬头。
 */
import { ElMessage } from 'element-plus'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { projectApi, quoteApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { Project, QuoteDetail, QuoteItem, Subsystem } from '@/api/types'
import { dateOnly, money, qty, toNum } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const projectId = computed(() => String(route.params.projectId))
const quoteId = computed(() => String(route.params.quoteId))

const loading = ref(false)
const exporting = ref(false)
const project = ref<Project | null>(null)
const quote = ref<QuoteDetail | null>(null)

// ----- 工具条开关 -----
const paper = ref<'A4-landscape' | 'A4-portrait'>('A4-landscape')

// ==================== 屏幕分页 = 打印分页（2026-10-05 用户反馈 ✓）====================
// 用户看到的问题 ✓：屏幕页脚写死「第 1 页 / 共 1 页」✗，而实际打印已经用到第 2 页 ✓。
// 根因 ✓：屏幕上是一整张**长条**纸 ✗（没有分页）✓ → 只能自己按真实 A4 算分页 ✓。
//
// 一页内容高怎么算 ✓（用真实纸张几何，不用纸的宽高比 ✗）：
//   · 打印边距 12mm ✓ → 可打印区 273 × 186 mm（A4 横向 297×210 ✓）
//   · 屏幕纸内容宽 1068px（1120 − 左右 padding 26×2 ✓）↔ 273mm
//   · 所以 1mm ≈ 1068/273 px ✓，一页高 = 186 × 1068/273 ≈ 728px ✓
const sheetRef = ref<HTMLElement | null>(null)
const bottomRef = ref<HTMLElement | null>(null)
/** 「—— 分页 ——」那段人工空白 ✓（量的时候要把它扣掉 ✓，见 measurePagination ✓） */
const spacerRef = ref<HTMLElement | null>(null)
const pageCount = ref(1)
/** 报价说明+费用合计这一块要不要另起一页 ✓（剩余不足半页时 ✓） */
const breakBeforeFees = ref(false)
/** 另起一页时，屏幕上留出的空白高度 ✓（与打印的空白对应 ✓） */
const breakGap = ref(0)

const PAGE_MARGIN_MM = 12

function pageContentHeight(): number {
  const el = sheetRef.value
  if (!el) return 728
  const style = window.getComputedStyle(el)
  const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
  const contentW = el.clientWidth - padX || el.clientWidth
  const landscape = paper.value === 'A4-landscape'
  const paperW = landscape ? 297 : 210
  const paperH = landscape ? 210 : 297
  const printableW = paperW - 2 * PAGE_MARGIN_MM
  const printableH = paperH - 2 * PAGE_MARGIN_MM
  return (contentW / printableW) * printableH      // 单位：px ✓
}

// ==================== A 步骤①：分页计划（只算不画 ✓）====================
// 2026-10-05 ✓：客户版要"每页各自写页码"✗，就必须先知道**每一页放哪些行** ✓。
// 本函数只做计算 ✓，把计划写进 __qpDebug ✓（渲染照旧 ✗ → 零视觉风险 ✓）。
// 用**普通数组**存计划 ✓（响应式代理在 JSON.stringify 里会丢内容 ✗，实测过 ✓）
const pagePlanText = ref<string>('')

function buildPagePlan(): void {
  const sheet = sheetRef.value
  if (!sheet) return
  const pageH = pageContentHeight()
  const pick = (sel: string) => sheet.querySelector(sel) as HTMLElement | null
  const hOf = (el: HTMLElement | null) => (el ? el.offsetHeight : 0)

  const headH = hOf(pick('.doc-head'))
  const infoH = hOf(pick('.doc-info'))
  const bottomH = hOf(pick('.doc-bottom'))
  const signH = hOf(pick('.doc-sign'))
  const theadH = hOf(pick('.doc-table thead'))
  const rows = Array.from(sheet.querySelectorAll('.doc-table tbody tr')) as HTMLElement[]

  const plan: Array<Record<string, unknown>> = []
  let page = 1
  let used = headH + infoH
  let tableStarted = false
  let onPage = 0
  let firstRow = 0
  let lastRow = -1
  const push = (tail: string) => {
    plan.push({
      page,
      usedPx: Math.round(used),
      rowFrom: firstRow,
      rowTo: lastRow,
      rows: onPage,
      tail,
    })
  }

  for (let i = 0; i < rows.length; i++) {
    const rh = rows[i].offsetHeight
    if (!tableStarted) {
      used += theadH           // 首页要放表头 ✓
      tableStarted = true
    }
    if (used + rh > pageH) {
      push('')                 // 本页到此为止 ✓
      page += 1
      used = theadH            // 续页重复表头 ✓
      onPage = 0
      firstRow = i
    }
    used += rh
    lastRow = i
    onPage += 1
  }

  // 最后一块：剩余不足半页 → 整块另起一页 ✓（用户明确要求 ✓）
  const tailH = bottomH + signH
  const remain = pageH - used
  const breakTail = rows.length > 0 && remain < pageH / 2 && tailH <= pageH
  if (breakTail) {
    push('余额不足半页 → 报价说明整块另起一页')
    page += 1
    used = tailH
    onPage = 0
    firstRow = -1
    lastRow = -1
  } else {
    used += tailH
  }
  push('末尾（报价说明+费用+签章）')

  pagePlanText.value = JSON.stringify(plan, null, 1)
  // A 步骤②：把计划原文交给渲染 ✓（深拷贝成**普通数组** ✓ —— 计划里若混进响应式代理，
  //   JSON.stringify 会丢内容 ✗，实测过 ✓）；有计划了才允许隐藏量尺纸 ✓。
  pagePlanRaw.value = JSON.parse(JSON.stringify(plan)) as Array<Record<string, unknown>>
  pagesReady.value = plan.length > 0
  // 页数以**计划**为准 ✓（2026-10-05 实测：按流的长度算会漏掉"插入的分页" ✗
  //   → 屏幕说 1 页、打印 2 页 ✓）。计划里最后一个 page 就是总页数 ✓。
  if (plan.length) pageCount.value = Number(plan[plan.length - 1].page)
}

/** 量一次：页数 + 报价说明那块要不要另起一页 ✓ */
function measurePagination(): void {
  const sheet = sheetRef.value
  if (!sheet) return
  const pageH = pageContentHeight()

  // 50% 规则 ✓：这块在它所在页的剩余空间不足半页 → 整块下移到下一页 ✓
  const bottom = bottomRef.value
  if (!bottom) {
    breakBeforeFees.value = false
    pageCount.value = Math.max(1, Math.ceil((sheet.scrollHeight - 2) / pageH))
    return
  }
  // 用 getBoundingClientRect 求「块顶 → 纸内容顶」的距离 ✓（2026-10-05 A 步骤② 修正 ✓）：
  //   量尺纸一旦被 .qp-measure-hidden 移出文档流（position: absolute ✓），
  //   它的 offsetParent 就跟着变 ✓ → 原来的 bottom.offsetTop - sheet.offsetTop 是**跨坐标系相减** ✗
  //   （算出来是垃圾值 ✓）。rect 是视口坐标 ✓，与定位方式无关 ✓。
  //
  // ⚠ 还要**扣掉自己插进去的那段空白**（.qp-break-spacer ✓），否则是个自反馈环 ✗：
  //   加空白 → 这块被推到下一页的边界上 ✓ → 再量就变成"整页都空着" ✗ → 判定不用加 ✓
  //     → 空白撤掉 ✓ → 又判定要加 ✓ …… 页面在两种状态之间无限抖动 ✓。
  //   实测证据：同一份单子、同样数据，刷新两次分别量到 breakGap ≈ 0.008px 与 ≈ 196px ✓。
  //   扣掉之后量到的就是**自然排布**位置 ✓，与加不加空白无关 ✓ → 一次收敛 ✓。
  //
  // 用 rect 的高度而**不是** offsetHeight ✗（后者会四舍五入到整数 ✓，扣不干净会留下零点几像素 ✓）。
  const spacerH = spacerRef.value ? spacerRef.value.getBoundingClientRect().height : 0
  const topInSheet =
    bottom.getBoundingClientRect().top -
    sheet.getBoundingClientRect().top -
    parseFloat(window.getComputedStyle(sheet).paddingTop) -
    spacerH
  const usedOnPage = topInSheet % pageH
  const remain = pageH - usedOnPage
  const need = bottom.offsetHeight
  const shouldBreak = remain < pageH / 2 && need <= pageH
  breakBeforeFees.value = shouldBreak
  // 屏幕上留出与打印一致的空白 ✓ —— 让"屏幕看到的"与"打印出来的"分页相同 ✓
  breakGap.value = shouldBreak ? remain : 0
  // 页数必须**扣掉**为对齐预览而加的空白 ✓（2026-10-05 修正 ✓）：
  //   上一版直接用 sheet.scrollHeight ✗ → 把那段空白也算成内容 ✓ → 多报一页 ✓
  //   而打印时这段空白是 0 ✓（由真正的分页替代 ✓）→ 屏幕就比打印多一页 ✓✗
  const countBy = () => {
    // 有分页计划时**以计划为准** ✓（2026-10-05 A 步骤② ✓）：
    //   计划是唯一知道"此处要插一页"的地方 ✓；用纸的长度算会漏掉插入的那一页 ✗
    //   → 屏幕说 1 页、打印 2 页 ✗（步骤①之前就是这个毛病 ✓）。
    if (pageBoxes.value.length) return pageBoxes.value.length
    const gap = breakGap.value || 0
    return Math.max(1, Math.ceil((sheet.scrollHeight - gap - 2) / pageH))
  }
  pageCount.value = countBy()
  nextTick(() => {
    pageCount.value = countBy()
  })
}

/** 重量一次 ✓：断点/页数（measurePagination ✓）+ 分页计划（buildPagePlan ✓）。
 *  换纸张、内容变、开关一动都要走这里 ✓ —— 少算一次，屏幕与打印就对不上 ✗。 */
function refreshPages(): void {
  measurePagination()
  buildPagePlan()
}

// ==================== A 步骤②：把计划画成 N 张 A4 纸（2026-10-05 ✓）====================
// 步骤①只算不画 ✓（屏幕上还是一整张长条纸 ✗ → 页码只能写死 "第 1 页" ✗）；
// 这一步按计划**画**出来 ✓：N 个 .qp-page 纸盒、每页各自写页码 ✓，
// 打印时一盒 = 一页 ✓（break-after: page ✓）→ 屏幕所见 = 打印所得 ✓。
//
// 计划原文用**普通数组**存 ✓（非响应式 ✓：响应式代理在 JSON.stringify 里会丢内容 ✗，实测过 ✓）：
const pagePlanRaw = ref<Array<Record<string, unknown>>>([])
/** 页盒就绪前，原来的 .qp-sheet 继续显示 ✓（数据没回来/计划为空时不至于整张空白 ✓） */
const pagesReady = ref(false)

/** 行描述：顺序必须与 DOM 里 .doc-table tbody tr 的顺序**完全一致** ✓
 *  —— 步骤①量的就是那个顺序 ✓，错一个索引就整页错位 ✓。 */
type RowDesc =
  | { kind: 'group'; gi: number }
  | { kind: 'item'; gi: number; ii: number }
  | { kind: 'subtotal'; gi: number }
  | { kind: 'grand' }
  | { kind: 'empty' }

const rowDescs = computed<RowDesc[]>(() => {
  const out: RowDesc[] = []
  if (!groups.value.length) return [{ kind: 'empty' }]
  groups.value.forEach((g, gi) => {
    out.push({ kind: 'group', gi })
    g.items.forEach((_it, ii) => out.push({ kind: 'item', gi, ii }))
    out.push({ kind: 'subtotal', gi })
  })
  out.push({ kind: 'grand' })
  return out
})

/** 页盒里一行的**取值结果** ✓：数字/字符串全在脚本里算好 ✓。
 *  模板只做展示 ✗ 不再碰 QuoteItem ✗（判别联合类型在模板里窄化不可靠 ✓，
 *  那种"模板里类型对了但值取错"的坑，比多写几行脚本贵得多 ✓）。 */
interface PageRow {
  kind: RowDesc['kind']
  /** group = 组标题（含「一、」前缀 ✓）；item = 名称 ✓；subtotal/grand = 标签 ✓ */
  text: string
  serial: number
  /** 定额子目行 → 缩进显示 ✓ */
  indent: boolean
  brand: string
  model: string
  unit: string
  quantity: number
  equipmentUnit: number
  installUnit: number
  installAmount: number
  unitPrice: number
  amount: number
  note: string
}

function toPageRow(d: RowDesc): PageRow {
  const base: PageRow = {
    kind: d.kind,
    text: '',
    serial: 0,
    indent: false,
    brand: '—',
    model: '—',
    unit: '',
    quantity: 0,
    equipmentUnit: 0,
    installUnit: 0,
    installAmount: 0,
    unitPrice: 0,
    amount: 0,
    note: '—',
  }
  if (d.kind === 'group') {
    const g = groups.value[d.gi]
    return { ...base, text: g ? `${groupIndex(d.gi)}${g.title}` : '' }
  }
  if (d.kind === 'item') {
    const it = groups.value[d.gi]?.items[d.ii]
    // 索引越界不该**静默少一行** ✗：退回空行 ✓（页面上看得见 ✓），别把内容悄悄吃掉 ✓
    if (!it) return { ...base, kind: 'empty' }
    return {
      ...base,
      text: displayName(it),
      serial: d.ii + 1,
      indent: !!it.parent_item_id,
      brand: it.brand || '—',
      model: it.model || '—',
      unit: it.unit,
      quantity: toNum(it.quantity),
      equipmentUnit: toNum(it.unit_price),
      installUnit: toNum(it.install_unit_price),
      installAmount: toNum(it.install_amount),
      unitPrice: rowUnitPrice(it),
      amount: rowAmount(it),
      note: rowNote(it),
    }
  }
  if (d.kind === 'subtotal') {
    return { ...base, text: '小计', amount: groups.value[d.gi]?.subtotal ?? 0 }
  }
  if (d.kind === 'grand') {
    return { ...base, text: '合计', amount: grandTotal.value }
  }
  return base
}

interface PageBox {
  /** 页码 ✓（与计划里的 page 一致 ✓） */
  index: number
  /** 第 1 页才有抬头 + 信息表 ✓ */
  head: boolean
  showTable: boolean
  rows: PageRow[]
  /** 末页块：报价说明 + 费用合计 + 签章 ✓ */
  tail: boolean
}

const pageBoxes = computed<PageBox[]>(() => {
  const plan = pagePlanRaw.value
  if (!plan.length) return []
  const descs = rowDescs.value
  return plan.map((p) => {
    const idx = Number(p.page ?? 1)
    const from = Number(p.rowFrom ?? -1)
    const to = Number(p.rowTo ?? -2)
    const rows =
      from >= 0 && to >= from ? descs.slice(from, to + 1).map((d) => toPageRow(d)) : []
    // 计划的 tail 文案以「签章）」结尾的就是末页块 ✓（见 buildPagePlan ✓）
    const tail = String(p.tail ?? '').endsWith('签章）')
    return { index: idx, head: idx === 1, showTable: rows.length > 0, rows, tail }
  })
})

const showBrandModel = ref(true)
const splitInstall = ref(false)
// 这两个开关一变，列数就变 ✓ → 每行的高度跟着变 ✓（型号列一出现，行长可能从 1 行变 2 行 ✓）
// → 分页计划必须**立刻重算** ✗ 否则页盒里还是旧计划 ✓：该分页的没分 ✗、行会溢出纸外 ✗。
watch([showBrandModel, splitInstall], () => nextTick(refreshPages))

/** 公司抬头来自「系统设置」（与 PDF 导出同一处），下面的常量只作接口未返回时的兜底。 */
const COMPANY_FALLBACK = 'XX科技有限公司'
const company = ref({ name: COMPANY_FALLBACK, address: '', phone: '', email: '' })
const companyLine = computed(() =>
  [company.value.address, company.value.phone ? `电话：${company.value.phone}` : '', company.value.email]
    .filter(Boolean)
    .join('　·　'),
)

/**
 * 报价说明条款 —— **从后端取**（`export_data.QUOTE_CLAUSES`，PDF 导出读同一份常量）。
 * 不在这里写死文案，否则"预览有条款、导出没有"这类不一致会再次出现。
 */
const clauses = ref<string[]>([])
const validDays = ref(30)

async function reload(): Promise<void> {
  loading.value = true
  try {
    // 子系统真名（口径与工作台一致 ✓，2026-10-05）
    void loadSubsystems()
    const [p, q, cl] = await Promise.all([
      projectApi.get(projectId.value),
      quoteApi.get(quoteId.value),
      quoteApi
        .exportClauses(quoteId.value)
        .catch(() => ({ clauses: [], valid_days: 30, company: null })),
    ])
    project.value = p
    quote.value = q
    clauses.value = cl.clauses
    validDays.value = cl.valid_days
    if (cl.company) {
      company.value = {
        name: cl.company.name || COMPANY_FALLBACK,
        address: cl.company.address ?? '',
        phone: cl.company.phone ?? '',
        email: cl.company.email ?? '',
      }
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await Promise.all([meta.load(), reload()])
  applyPaper()
})

// ----- 纸张：运行时注入 @page（print.css 默认 A4 portrait）-----
let pageStyleEl: HTMLStyleElement | null = null

function applyPaper(): void {
  if (!pageStyleEl) {
    pageStyleEl = document.createElement('style')
    pageStyleEl.id = 'quote-preview-page'
    document.head.appendChild(pageStyleEl)
  }
  pageStyleEl.textContent = `@media print { @page { size: ${paper.value.replace('-', ' ')}; margin: 12mm; } }`
}

watch(paper, applyPaper)
// 纸张/内容一变就重量分页 ✓（2026-10-05 ✓）
watch(paper, () => nextTick(refreshPages))
// ⚠ 只在挂载时量一次是不够的（2026-10-05 实测抓到 ✓）：
//   挂载那一刻**报价数据还没回来** ✓ → 纸是空的 ✓ → 页数恒为 1 ✗、
//   "报价说明是否要另起一页"也恒为 false ✗（于是该换页的没换 ✓）。
//   改法：① 数据到了重量一次 ✓ ② 用 ResizeObserver 盯着纸的高度 ✓（内容一变就重量 ✓）。
//   ⚠ 观察回调里必须**先比较再赋值** ✗ —— 否则"赋值→尺寸变→再触发"会自激死循环 ✓。
let _ro: ResizeObserver | null = null
onMounted(() => {
  nextTick(refreshPages)
  // 字体就绪后再量 ✓（2026-10-05 实测：折行规则一改，行高就变 ✓，
  //   量早了算出来的"剩余半页"是错的 ✗ → 该换页的没换 ✓）
  void document.fonts.ready.then(() => refreshPages())
  setTimeout(refreshPages, 600)
  setTimeout(refreshPages, 1500)
  setTimeout(() => {
    refreshPages()           // A 步骤①：断点/页数 ✓ A 步骤②：分页计划 + 页盒 ✓
    // 把量到的关键数字挂到 window 上 ✓：便于"预览自检"脚本把真实几何打出来 ✓
    ;(window as unknown as Record<string, unknown>).__qpDebug = {
      pageH: pageContentHeight(),
      sheetH: sheetRef.value?.scrollHeight ?? 0,
      bottomTop: bottomRef.value
        ? bottomRef.value.getBoundingClientRect().top -
          (sheetRef.value?.getBoundingClientRect().top ?? 0)
        : -1,
      blockH: bottomRef.value?.offsetHeight ?? 0,
      breakGap: breakGap.value,
      pageCount: pageCount.value,
      plan: pagePlanText.value,
      pageBoxes: pageBoxes.value.length,
    }
  }, 2000)
  const el = sheetRef.value
  if (el && typeof ResizeObserver !== 'undefined') {
    _ro = new ResizeObserver(() => {
      const before = pageCount.value
      const beforeGap = breakGap.value
      const beforePlan = pagePlanText.value
      refreshPages()
      if (
        pageCount.value === before &&
        breakGap.value === beforeGap &&
        pagePlanText.value === beforePlan
      ) {
        return
      }
      // 值确实变了 → 允许下一次回调（尺寸变化是预期的 ✓）
    })
    _ro.observe(el)
  }
})
onUnmounted(() => {
  _ro?.disconnect()
  _ro = null
})
onUnmounted(() => {
  pageStyleEl?.remove()
  pageStyleEl = null
})

// ----- 明细：按子系统分组 -----
const subsystems = ref<Subsystem[]>([])

/** 真实子系统名（口径与工作台一致 ✓）：自定义名 优先，否则类型中文名 ✓
 *  以前这里是拿 subsystem_id 前 6 位拼「子系统 xxxxxx」✗ —— 那是假名字 ✓ */
const subsystemNames = computed(() => {
  const m = new Map<string, string>()
  for (const s of subsystems.value) {
    m.set(s.id, s.custom_name || meta.label('subsystem_type', s.subsystem_type))
  }
  return m
})

async function loadSubsystems(): Promise<void> {
  try {
    subsystems.value = await projectApi.subsystems(String(route.params.projectId))
  } catch {
    /* 拿不到就显示「未命名子系统」，绝不显示 UUID ✓ */
  }
}


/** 客户版单行金额：单价 = 设备单价 + 安装单价（原型里 680 + 80 = 760）。 */
/** 客户版显示名：名称开头若是型号就剥掉（与后端 export_data 同规则 ✓，2026-10-05）。
 *  否则"屏幕与打印不一致"又会回来 ✗。 */
function displayName(it: { name?: string | null; model?: string | null }): string {
  const n = String(it.name ?? '').trim()
  const m = String(it.model ?? '').trim()
  if (!n || !m || n === m) return n
  for (const sep of [' ', '　', '-', '_']) {
    if (n.startsWith(m + sep)) return n.slice((m + sep).length).trim() || n
  }
  if (n.startsWith(m)) return n.slice(m.length).trim() || n
  return n
}

function rowUnitPrice(it: QuoteItem): number {
  return toNum(it.unit_price) + toNum(it.install_unit_price)
}

function rowAmount(it: QuoteItem): number {
  return toNum(it.quantity) * rowUnitPrice(it)
}

function rowNote(it: QuoteItem): string {
  if (it.is_owner_supplied) return '甲供（只计安装）'
  if (toNum(it.install_unit_price) > 0) return '含安装'
  return '—'
}

interface Group {
  key: string
  title: string
  items: QuoteItem[]
  subtotal: number
}

const groups = computed<Group[]>(() => {
  const order: string[] = []
  const buckets = new Map<string, QuoteItem[]>()
  for (const it of quote.value?.items ?? []) {
    const key = it.subsystem_id ?? '__none__'
    if (!buckets.has(key)) {
      buckets.set(key, [])
      order.push(key)
    }
    buckets.get(key)!.push(it)
  }
  return order.map((key) => {
    const items = buckets.get(key)!
    return {
      key,
      // 2026-10-05 ✓：「未归子系统」是内部叫法 ✗，不该印在客户版上 ✓ → 改中性称呼 ✓
              title: key === '__none__' ? '其他设备' : (subsystemNames.value.get(key) ?? '其他设备'),
      items,
      subtotal: items.reduce((s, it) => s + rowAmount(it), 0),
    }
  })
})

const grandTotal = computed(() =>
  groups.value.reduce((s, g) => s + (Number.isFinite(g.subtotal) ? g.subtotal : 0), 0),
)

const CN_NUM = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十']
function groupIndex(i: number): string {
  return `${CN_NUM[i] ?? String(i + 1)}、`
}

/** 报价日期 / 有效期至（有效期天数取自后端 export_data.QUOTE_VALID_DAYS）。 */
const quoteDate = computed(() => (quote.value?.created_at ? dateOnly(quote.value.created_at) : '—'))
const validUntil = computed(() => {
  const d = quote.value?.created_at
  if (!d) return '—'
  const dt = new Date(d)
  dt.setDate(dt.getDate() + validDays.value)
  return dateOnly(dt.toISOString())
})

const feeRows = computed(() => {
  const q = quote.value
  if (!q) return []
  return [
    { label: '设备费', value: q.equipment_total },
    { label: '安装调试费', value: q.install_total },
    { label: '辅材费', value: q.auxiliary_total },
    { label: '设计费', value: q.design_fee },
    { label: '维保费（首年）', value: q.warranty_fee },
    { label: '培训费', value: q.training_fee },
    { label: '管理费及利润', value: q.mgmt_profit },
  ]
})

const taxLabel = computed(() => {
  const rate = toNum(quote.value?.tax_rate) * 100
  return `税金（${rate.toFixed(rate % 1 === 0 ? 0 : 1)}%）`
})

/** 导出文件（2026-10-05 用户要求 ✓）：打印/导出统一在本页 ✓，**支持所有格式** ✓。
 *  internal = true 时带成本（仅对有成本权限的人显示按钮 ✓）。 */
async function downloadFile(format: 'xlsx' | 'pdf', internal = false): Promise<void> {
  exporting.value = true
  try {
    const { blob, filename } = await quoteApi.exportFile(quoteId.value, format, internal)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(
      `已导出${internal ? '内部版' : '客户版'} ${format.toUpperCase()}：${filename}`,
    )
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    exporting.value = false
  }
}

function doPrint(): void {
  window.print()
}

function back(): void {
  void router.push({
    name: 'quote-workbench',
    params: { projectId: projectId.value, quoteId: quoteId.value },
  })
}
</script>

<template>
  <div v-loading="loading" class="page qp">
    <!-- ① 顶栏 -->
    <div class="card qp-top no-print">
      <el-button link @click="back"><el-icon><ArrowLeft /></el-icon></el-button>
      <div>
        <div class="qp-title">报价单预览 · 客户版</div>
        <div class="qp-sub">送客户前可先预览确认；成本、毛利、采购价字段自动隐藏</div>
      </div>
      <div class="spacer" />
      <span class="mono">{{ quote?.code }}</span>
    </div>

    <!-- ② 工具条 -->
    <div class="card qp-bar no-print">
      <div class="qp-field">
        <label>纸张</label>
        <el-select v-model="paper" size="small" style="width: 130px">
          <el-option label="A4 横向" value="A4-landscape" />
          <el-option label="A4 纵向" value="A4-portrait" />
        </el-select>
      </div>
      <div class="qp-field">
        <label>模板</label>
        <el-select model-value="standard" size="small" style="width: 190px">
          <el-option label="标准智能化报价单" value="standard" />
        </el-select>
        <span class="text-muted qp-note">当前只实现了这一个模板</span>
      </div>
      <el-checkbox v-model="showBrandModel" size="small">显示品牌型号</el-checkbox>
      <el-checkbox v-model="splitInstall" size="small">安装费单列</el-checkbox>
      <el-checkbox disabled size="small">
        <span title="客户版永远不显示成本/毛利/采购价（后端导出同样如此）">显示成本毛利（客户版不可选）</span>
      </el-checkbox>
      <div class="spacer" />
      <!-- 导出（2026-10-05 ✓）：所有格式都在客户版预览里出 ✓ -->
      <el-button size="small" :loading="exporting" @click="downloadFile('xlsx')">
        <el-icon><Download /></el-icon>&nbsp;客户版 Excel
      </el-button>
      <el-button size="small" :loading="exporting" @click="downloadFile('pdf')">
        <el-icon><Download /></el-icon>&nbsp;客户版 PDF
      </el-button>
      <el-dropdown v-if="auth.canSeeCost" @command="(c: string) => downloadFile(c.split(':')[0] as 'xlsx' | 'pdf', true)">
        <el-button size="small" :loading="exporting">
          内部版<el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </el-button>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="xlsx:internal">内部版 Excel（含成本）</el-dropdown-item>
            <el-dropdown-item command="pdf:internal">内部版 PDF（含成本）</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
      <el-button size="small" type="primary" @click="doPrint">
        <el-icon><Printer /></el-icon>&nbsp;打印
      </el-button>
    </div>

    <!-- ③ 文档（打印时只留这一块） -->
    <div class="qp-sheet-wrap">
      <!-- 量尺纸（2026-10-05 A 步骤② ✓）：整张流**保留** ✓ ——
           分页计划就是量它的真实几何算出来的 ✓（换纸张/内容一变就要重量 ✓）。
           页盒一旦就绪就把它藏起来 ✓（用 visibility 不用 display ✗ —— display:none 量不到高度 ✗）。 -->
      <div
        ref="sheetRef"
        class="qp-sheet"
        :class="[
          paper === 'A4-portrait' ? 'portrait' : 'landscape',
          { 'qp-measure-hidden': pagesReady },
        ]"
      >
        <!-- 抬头 -->
        <div class="doc-head">
          <div class="doc-company">
            <div class="doc-brand">XX科技</div>
            <div>
              <div class="doc-company-name">{{ company.name }}</div>
              <div class="doc-company-meta">{{ companyLine }}</div>
            </div>
          </div>
          <div class="doc-title">智能化弱电工程报价单</div>
        </div>

        <!-- 信息表 -->
        <div class="doc-info">
          <div class="doc-info-item"><span>项目名称：</span><b>{{ project?.name ?? '—' }}</b></div>
          <div class="doc-info-item"><span>报价单号：</span><b class="mono">{{ quote?.code }}</b></div>
          <div class="doc-info-item"><span>客户名称：</span><b>{{ quote?.customer_name || project?.customer_name || '—' }}</b></div>
          <div class="doc-info-item"><span>报价日期：</span><b>{{ quoteDate }}</b></div>
          <div class="doc-info-item">
            <span>联 系 人：</span>
            <b>{{ quote?.customer_contact || project?.customer_contact || '—' }}　{{ quote?.customer_phone || project?.customer_phone || '' }}</b>
          </div>
          <div class="doc-info-item"><span>有效期至：</span><b>{{ validUntil }}</b></div>
          <div class="doc-info-item"><span>工程地点：</span><b>{{ project?.address || '—' }}</b></div>
          <div class="doc-info-item"><span>报价人：</span><b>{{ auth.displayName }}</b></div>
        </div>

        <!-- 明细表 -->
        <table class="doc-table">
          <thead>
            <tr>
              <th style="width: 46px">序号</th>
              <th>设备 / 材料名称</th>
              <th v-if="showBrandModel" style="width: 96px">品牌</th>
              <th v-if="showBrandModel" style="width: 150px">型号规格</th>
              <th style="width: 54px">单位</th>
              <th style="width: 62px">数量</th>
              <template v-if="splitInstall">
                <th style="width: 84px">设备单价</th>
                <th style="width: 84px">安装单价</th>
                <th style="width: 84px">安装费</th>
              </template>
              <th v-else style="width: 92px">单价（元）</th>
              <th style="width: 104px">合价（元）</th>
              <th style="width: 84px">备注</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="(g, gi) in groups" :key="g.key">
              <tr class="group-row">
                <td colspan="2" class="group-title">{{ groupIndex(gi) }}{{ g.title }}</td>
                <td :colspan="showBrandModel ? 3 : 1" />
                <td />
                <td :colspan="splitInstall ? 5 : 3" class="group-note">主材 + 安装调试</td>
              </tr>
              <tr v-for="(it, ii) in g.items" :key="it.id">
                <td class="c">{{ ii + 1 }}</td>
                <td>
                <span v-if="it.parent_item_id" class="text-muted">　　└ </span>{{ displayName(it) }}
              </td>
                <td v-if="showBrandModel">{{ it.brand || '—' }}</td>
                <td v-if="showBrandModel">{{ it.model || '—' }}</td>
                <td class="c">{{ it.unit }}</td>
                <td class="r num">{{ qty(it.quantity) }}</td>
                <template v-if="splitInstall">
                  <td class="r num">{{ money(it.unit_price) }}</td>
                  <td class="r num">{{ money(it.install_unit_price) }}</td>
                  <td class="r num">{{ money(it.install_amount) }}</td>
                </template>
                <td v-else class="r num">{{ money(rowUnitPrice(it)) }}</td>
                <td class="r num">{{ money(rowAmount(it)) }}</td>
                <td class="c text-muted">{{ rowNote(it) }}</td>
              </tr>
              <tr class="subtotal-row">
                <td :colspan="showBrandModel ? 7 : 5" class="r">小计</td>
                <td class="r num">{{ money(g.subtotal) }}</td>
                <td />
              </tr>
            </template>
            <tr v-if="groups.length" class="grand-row">
              <td :colspan="(showBrandModel ? 5 : 3) + 1" class="r">合计</td>
              <td :colspan="splitInstall ? 3 : 1" />
              <td class="r num">{{ money(grandTotal) }}</td>
              <td />
            </tr>
            <tr v-if="!groups.length">
              <td :colspan="showBrandModel ? 9 : 7" class="c text-muted">这张报价单还没有明细</td>
            </tr>
          </tbody>
        </table>

        <!-- 报价说明 + 合计 -->
        <!-- 分页标（2026-10-05 ✓）：屏幕上这段空白是为对齐打印分页留的 ✓，
             以前毫无说明 ✗ 看着像布局坏了 ✓ → 现在画一条虚线并标注 ✓；打印时隐藏 ✓。 -->
        <div
          v-if="breakGap"
          ref="spacerRef"
          class="qp-break-spacer"
          :style="{ height: breakGap + 'px' }"
        >
          <span>—— 分页 ——</span>
        </div>

        <div
          ref="bottomRef"
          class="doc-bottom"
          :class="{ 'qp-break-before': breakBeforeFees }"
        >
          <div class="doc-clauses">
            <div class="doc-section-title">报价说明</div>
            <ol>
              <li v-for="(c, i) in clauses" :key="i">{{ c }}</li>
            </ol>
          </div>
          <div class="doc-fees">
            <div v-for="f in feeRows" :key="f.label" class="doc-fee-row">
              <span>{{ f.label }}</span><span class="num">{{ money(f.value) }}</span>
            </div>
            <div class="doc-fee-row">
              <span>{{ taxLabel }}</span><span class="num">{{ money(quote?.tax_amount) }}</span>
            </div>
            <div class="doc-fee-row doc-fee-grand">
              <span>报价合计（含税）</span>
              <span class="num">¥ {{ money(quote?.total_with_tax) }}</span>
            </div>
          </div>
        </div>

        <!-- 签章区 -->
        <div class="doc-sign">
          <div>报价单位（盖章）：</div>
          <div>授权代表（签字）：</div>
          <div>日期：{{ quoteDate }}</div>
        </div>

        <div class="doc-foot">
          第 1 页 / 共 {{ pageCount }} 页
        </div>
      </div>

      <!-- ==================== A 步骤②：N 张真实 A4 纸（2026-10-05 ✓）====================
           一盒 = 一页 ✓：内容按分页计划切开 ✓，每页各自写页码 ✓，打印时 break-after: page ✓。
           刻意**不设** overflow: hidden ✓ —— 万一计划与实际有偏差，宁可内容露出来看得见 ✓，
           也不要被静默裁掉 ✓（客户版少一行明细是最坏的结果 ✓）。
           注意：这里是 .qp-sheet-wrap 的**兄弟节点** ✓，没有去动量尺纸的结构 ✗。 -->
      <div
        v-for="pg in pageBoxes"
        :key="pg.index"
        class="qp-page"
        :class="paper === 'A4-portrait' ? 'portrait' : 'landscape'"
      >
        <!-- 第 1 页：抬头 + 信息表 ✓（与量尺纸里逐字一致 ✓，只放在 v-if="pg.head" 里 ✓） -->
        <template v-if="pg.head">
          <div class="doc-head">
            <div class="doc-company">
              <div class="doc-brand">XX科技</div>
              <div>
                <div class="doc-company-name">{{ company.name }}</div>
                <div class="doc-company-meta">{{ companyLine }}</div>
              </div>
            </div>
            <div class="doc-title">智能化弱电工程报价单</div>
          </div>

          <div class="doc-info">
            <div class="doc-info-item"><span>项目名称：</span><b>{{ project?.name ?? '—' }}</b></div>
            <div class="doc-info-item"><span>报价单号：</span><b class="mono">{{ quote?.code }}</b></div>
            <div class="doc-info-item"><span>客户名称：</span><b>{{ quote?.customer_name || project?.customer_name || '—' }}</b></div>
            <div class="doc-info-item"><span>报价日期：</span><b>{{ quoteDate }}</b></div>
            <div class="doc-info-item">
              <span>联 系 人：</span>
              <b>{{ quote?.customer_contact || project?.customer_contact || '—' }}　{{ quote?.customer_phone || project?.customer_phone || '' }}</b>
            </div>
            <div class="doc-info-item"><span>有效期至：</span><b>{{ validUntil }}</b></div>
            <div class="doc-info-item"><span>工程地点：</span><b>{{ project?.address || '—' }}</b></div>
            <div class="doc-info-item"><span>报价人：</span><b>{{ auth.displayName }}</b></div>
          </div>
        </template>

        <!-- 明细表：只放**本页**的行 ✓（续页重复表头 ✓；td 结构与量尺纸里逐字一致 ✓） -->
        <table v-if="pg.showTable" class="doc-table">
          <thead>
            <tr>
              <th style="width: 46px">序号</th>
              <th>设备 / 材料名称</th>
              <th v-if="showBrandModel" style="width: 96px">品牌</th>
              <th v-if="showBrandModel" style="width: 150px">型号规格</th>
              <th style="width: 54px">单位</th>
              <th style="width: 62px">数量</th>
              <template v-if="splitInstall">
                <th style="width: 84px">设备单价</th>
                <th style="width: 84px">安装单价</th>
                <th style="width: 84px">安装费</th>
              </template>
              <th v-else style="width: 92px">单价（元）</th>
              <th style="width: 104px">合价（元）</th>
              <th style="width: 84px">备注</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="(pr, ri) in pg.rows" :key="ri">
              <tr v-if="pr.kind === 'group'" class="group-row">
                <td colspan="2" class="group-title">{{ pr.text }}</td>
                <td :colspan="showBrandModel ? 3 : 1" />
                <td />
                <td :colspan="splitInstall ? 5 : 3" class="group-note">主材 + 安装调试</td>
              </tr>
              <tr v-else-if="pr.kind === 'item'">
                <td class="c">{{ pr.serial }}</td>
                <td>
                <span v-if="pr.indent" class="text-muted">　　└ </span>{{ pr.text }}
              </td>
                <td v-if="showBrandModel">{{ pr.brand }}</td>
                <td v-if="showBrandModel">{{ pr.model }}</td>
                <td class="c">{{ pr.unit }}</td>
                <td class="r num">{{ qty(pr.quantity) }}</td>
                <template v-if="splitInstall">
                  <td class="r num">{{ money(pr.equipmentUnit) }}</td>
                  <td class="r num">{{ money(pr.installUnit) }}</td>
                  <td class="r num">{{ money(pr.installAmount) }}</td>
                </template>
                <td v-else class="r num">{{ money(pr.unitPrice) }}</td>
                <td class="r num">{{ money(pr.amount) }}</td>
                <td class="c text-muted">{{ pr.note }}</td>
              </tr>
              <tr v-else-if="pr.kind === 'subtotal'" class="subtotal-row">
                <td :colspan="showBrandModel ? 7 : 5" class="r">小计</td>
                <td class="r num">{{ money(pr.amount) }}</td>
                <td />
              </tr>
              <tr v-else-if="pr.kind === 'grand'" class="grand-row">
                <td :colspan="(showBrandModel ? 5 : 3) + 1" class="r">合计</td>
                <td :colspan="splitInstall ? 3 : 1" />
                <td class="r num">{{ money(pr.amount) }}</td>
                <td />
              </tr>
              <tr v-else>
                <td :colspan="showBrandModel ? 9 : 7" class="c text-muted">这张报价单还没有明细</td>
              </tr>
            </template>
          </tbody>
        </table>

        <!-- 末页块：报价说明 + 费用合计 + 签章 ✓（整块落在同一页 ✓ 不被截断 ✓） -->
        <template v-if="pg.tail">
          <div class="doc-bottom">
            <div class="doc-clauses">
              <div class="doc-section-title">报价说明</div>
              <ol>
                <li v-for="(c, i) in clauses" :key="i">{{ c }}</li>
              </ol>
            </div>
            <div class="doc-fees">
              <div v-for="f in feeRows" :key="f.label" class="doc-fee-row">
                <span>{{ f.label }}</span><span class="num">{{ money(f.value) }}</span>
              </div>
              <div class="doc-fee-row">
                <span>{{ taxLabel }}</span><span class="num">{{ money(quote?.tax_amount) }}</span>
              </div>
              <div class="doc-fee-row doc-fee-grand">
                <span>报价合计（含税）</span>
                <span class="num">¥ {{ money(quote?.total_with_tax) }}</span>
              </div>
            </div>
          </div>

          <div class="doc-sign">
            <div>报价单位（盖章）：</div>
            <div>授权代表（签字）：</div>
            <div>日期：{{ quoteDate }}</div>
          </div>
        </template>

        <!-- 每页各自的页码 ✓（这正是本次任务要的东西 ✓） -->
        <div class="doc-foot">第 {{ pg.index }} 页 / 共 {{ pageBoxes.length }} 页</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 分页标（2026-10-05 ✓）：虚线 + 居中文字，说明"此处换页" ✓ */
.qp-break-spacer {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
}
.qp-break-spacer::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  border-top: 1px dashed #c0c4cc;
}
.qp-break-spacer span {
  position: relative;
  padding: 0 10px;
  background: #fff;
  color: #909399;
  font-size: 12px;
  letter-spacing: 1px;
}

.qp {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.qp-top {
  display: flex;
  align-items: center;
  gap: 12px;
}

.qp-title {
  font-size: 16px;
  font-weight: 700;
}

.qp-sub {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}

.qp-bar {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 8px 12px;
}

.qp-field {
  display: flex;
  align-items: center;
  gap: 6px;
}

.qp-field > label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.qp-note {
  font-size: 11px;
}

/* 竖向排列 ✓（2026-10-05 A 步骤② ✓）：量尺纸 + N 张纸盒，一张一张往下摞 ✓ */
.qp-sheet-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 18px;
  padding-bottom: 20px;
}

/* 屏幕上的纸张：横向 1120px / 纵向 800px，比例贴近 A4 */
.qp-sheet {
  background: #fff;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.14);
  padding: 22px 26px 14px;
  font-size: 12px;
  color: #1f2937;
}

.qp-sheet.landscape {
  width: 1120px;
}

.qp-sheet.portrait {
  width: 794px;
}

/* 量尺纸的隐藏（2026-10-05 A 步骤② ✓）：
   用 visibility 而**不是** display ✗ —— display:none 之后 offsetHeight 全为 0 ✗，就量不到了 ✓。
   移出文档流（position: absolute ✓）避免它占着屏幕最上面一大块 ✓。
   ⚠ 移出流后它的 offsetParent 会变 ✓ → 量"块顶到纸顶"的距离必须改用 rect ✓（见 measurePagination ✓）。 */
.qp-measure-hidden {
  position: absolute;
  left: -100000px;
  top: 0;
  visibility: hidden;
}

/* 纸盒：一张 = 一页 ✓（宽度与量尺纸一致 ✓ → 折行、行高一致 ✓ → 打印分页才对得上 ✓） */
.qp-page {
  background: #fff;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.14);
  padding: 22px 26px 14px;
  font-size: 12px;
  color: #1f2937;
  position: relative;
  display: flex;
  flex-direction: column;
  /* 刻意的：不设 overflow: hidden ✓ —— 计划若与实际有偏差，内容要**露出来看得见** ✓，
     而不是被静默裁掉 ✓（"打印少了几行"是客户版最坏的一类故障 ✓） */
}

.qp-page.landscape {
  width: 1120px;
  min-height: 728px;
}

.qp-page.portrait {
  width: 794px;
  min-height: 1030px;
}

/* 页码钉在纸张底部 ✓（margin-top: auto 把页脚推到底 ✓） */
.qp-page > .doc-foot {
  margin-top: auto;
}

.doc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  border-bottom: 2px solid #1f2937;
  padding-bottom: 8px;
}

.doc-company {
  display: flex;
  align-items: center;
  gap: 8px;
}

.doc-brand {
  /* 2026-10-05 用户要求：品牌字由「弱」改为「XX科技」✓ → 方框改成能容两个字的圆角块 ✓ */
  min-width: 42px;
  height: 26px;
  padding: 0 7px;
  border-radius: 6px;
  background: #14243d;
  color: #fff;
  font-size: 13px;
  letter-spacing: 1px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}

.doc-company-name {
  font-size: 15px;
  font-weight: 700;
}

.doc-company-meta {
  font-size: 11px;
  color: #6b7280;
}

.doc-title {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 2px;
}

.doc-info {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 24px;
  padding: 8px 0;
  border-bottom: 1px solid #e5e7eb;
}

.doc-info-item {
  font-size: 12px;
}

.doc-info-item > span {
  color: #6b7280;
}

.doc-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 8px;
  table-layout: fixed;
}

.doc-table th,
.doc-table td {
  border: 1px solid #d1d5db;
  padding: 3px 5px;
  font-size: 11.5px;
  /* 2026-10-05 ✓：break-all 会在单词中间断开 ✗（出现 "…7.58Tb / ps" ✓）；
     normal 让拉丁词优先在空格/标点处折 ✓，实在放不下才断词 ✓。 */
  word-break: normal;
  overflow-wrap: break-word;
}

.doc-table th {
  background: #f3f4f6;
  font-weight: 600;
}

.doc-table td.c {
  text-align: center;
}

.doc-table td.r {
  text-align: right;
}

.group-row td {
  background: #eef2f7;
  font-weight: 600;
}

.group-note {
  color: #6b7280;
  font-weight: 400;
}

.grand-row td {
  background: #eef2f7;
  font-weight: 700;
}

.subtotal-row td {
  background: #fafafa;
  font-weight: 600;
}

.doc-bottom {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  margin-top: 10px;
}

.doc-clauses {
  flex: 1 1 auto;
  min-width: 0;
}

.doc-section-title {
  font-weight: 700;
  margin-bottom: 4px;
}

.doc-flag {
  margin-bottom: 4px;
}

.doc-clauses ol {
  margin: 0;
  padding-left: 18px;
  font-size: 11.5px;
  line-height: 1.7;
  color: #374151;
}

.doc-fees {
  flex: 0 0 300px;
  border: 1px solid #e5e7eb;
  border-radius: 4px;
  padding: 6px 10px;
}

.doc-fee-row {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  padding: 2px 0;
}

.doc-fee-grand {
  border-top: 1px solid #d1d5db;
  margin-top: 4px;
  padding-top: 4px;
  font-size: 14px;
  font-weight: 700;
  color: #14243d;
}

.doc-sign {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  margin-top: 26px;
  font-size: 12px;
}

.doc-foot {
  margin-top: 10px;
  padding-top: 6px;
  border-top: 1px solid #e5e7eb;
  font-size: 10.5px;
  color: #9ca3af;
  text-align: center;
}

/* ---------- 打印：只留 N 张纸，工具条/量尺纸/外壳都隐藏 ---------- */
@media print {
  .no-print {
    display: none !important;
  }

  .qp-sheet-wrap {
    display: block;
    padding: 0;
    gap: 0;
  }

  /* 量尺纸只用来测量 ✓ 打印时绝不能出现 ✗（否则每张纸后面还跟着一整张长条纸 ✓） */
  .qp-sheet {
    display: none !important;
  }

  /* 一盒 = 一页 ✓（break-after: page ✓）；min-height 归零 ✓：
     屏幕上的 728px 是"屏幕比例下的 A4 高" ✓，直接带去打印会比可打印区(约 703px)高 ✓
     → 每页后面多出一张空白页 ✗（实测过的坑 ✓）。 */
  .qp-page {
    display: block;
    width: 100% !important;
    min-height: 0 !important;
    box-shadow: none !important;
    padding: 0 !important;
    break-after: page;
    page-break-after: always;
  }

  /* 最后一页后面不要再补一张空白页 ✓ */
  .qp-page:last-child {
    break-after: auto;
    page-break-after: auto;
  }
}
</style>



