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
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
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
const showBrandModel = ref(true)
const splitInstall = ref(false)

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
      title: key === '__none__' ? '未归子系统' : (subsystemNames.value.get(key) ?? '（未命名子系统）'),
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

async function downloadPdf(): Promise<void> {
  exporting.value = true
  try {
    const { blob, filename } = await quoteApi.exportFile(quoteId.value, 'pdf', false)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出客户版 PDF：${filename}`)
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
      <el-button size="small" :loading="exporting" @click="downloadPdf">
        <el-icon><Download /></el-icon>&nbsp;下载 PDF
      </el-button>
      <el-button size="small" type="primary" @click="doPrint">
        <el-icon><Printer /></el-icon>&nbsp;打印
      </el-button>
    </div>

    <!-- ③ 文档（打印时只留这一块） -->
    <div class="qp-sheet-wrap">
      <div class="qp-sheet" :class="paper === 'A4-portrait' ? 'portrait' : 'landscape'">
        <!-- 抬头 -->
        <div class="doc-head">
          <div class="doc-company">
            <div class="doc-brand">弱</div>
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
                <span v-if="it.parent_item_id" class="text-muted">　　└ </span>{{ it.name }}
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

        <!-- 签章区 -->
        <div class="doc-sign">
          <div>报价单位（盖章）：</div>
          <div>授权代表（签字）：</div>
          <div>日期：{{ quoteDate }}</div>
        </div>

        <div class="doc-foot">
          第 1 页 / 共 1 页
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
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

.qp-sheet-wrap {
  display: flex;
  justify-content: center;
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
  width: 26px;
  height: 26px;
  border-radius: 6px;
  background: #14243d;
  color: #fff;
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
  word-break: break-all;
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

/* ---------- 打印：只留文档，工具条/外壳都隐藏 ---------- */
@media print {
  .no-print {
    display: none !important;
  }

  .qp-sheet-wrap {
    padding: 0;
  }

  .qp-sheet {
    width: 100% !important;
    box-shadow: none !important;
    padding: 0 !important;
  }
}
</style>



