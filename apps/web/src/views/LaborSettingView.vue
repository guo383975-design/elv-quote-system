<script setup lang="ts">
import { http } from '@/api/client'
/**
 * 人工费设置（按原型第 4 页版式，2026-10-03）。
 *
 * 原型第 4 页是一个**报价单级**的人工费设置页（顶栏带报价单号），不是全局信息价维护页：
 *   ① 顶栏：← 人工费设置 ｜ 报价单号 · 项目名 ｜ 计价依据（定额）
 *   ② 信息条：计价依据 / 人工信息价期数 / **统一浮动率（+ 应用到全部工种 + 重置）** / 加减价方式
 *   ③ 表格：工种 / 本期信息价 / 浮动方式 / 浮动率 / **调整后单价** / 与信息价偏差 / 用途说明
 *
 * 与后端能力的对齐（不做什么、为什么）：
 *   ✅ 统一浮动率 —— 落到 `labor_float_rate`（整体浮动，三类都加）
 *   ✅ 逐工种浮动率 —— 落到 `labor_float_per_class`
 *   ✅ 调整后单价 / 与信息价偏差 —— 由「本期信息价 ×（1 + 整体 + 该类）」实时算出
 *   ⛔ 「加减价方式（按比例 / 按元/工日）」：后端只有比例口径，模型里没有"按元/工日"字段
 *   ⛔ 「切换信息价期数」：计算固定取 `is_current` 的期次，报价单上没有"用哪一期"的字段
 *   ⛔ 「计价依据」：定额册别没有入库字段（是交付资料里的文字）
 *   ⛔ 「人工费占本报价单」：后端没有单独的人工费金额字段（定额口径合计里含人工+机械+辅材）
 *   以上四项一律**置灰 + 鼠标说明**，不做假控件。
 *
 * 「有效上浮率 = 整体 + 该类」是 2026-10-03 修掉的那个静默失效（见 AUDIT_REPORT §14）。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { laborApi, projectApi, quoteApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { CurrentLaborRates, Project, QuoteDetail } from '@/api/types'
import { money, percentInputToRatio, ratioToPercentInput, toNum } from '@/utils/format'
import { computeSavedMessage } from '@/utils/computeMessage'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const projectId = computed(() => String(route.params.projectId))
const quoteId = computed(() => String(route.params.quoteId))

const loading = ref(false)
const saving = ref(false)
const project = ref<Project | null>(null)
const quote = ref<QuoteDetail | null>(null)
const current = ref<CurrentLaborRates | null>(null)

/** 统一浮动率（%）—— 对应后端 labor_float_rate，对三类工日都生效。 */
const overallPct = ref(0)
/** 逐工种浮动率（%）—— 对应 labor_float_per_class，与整体浮动**相加**。 */
const perClassPct = reactive({ class_a: 0, class_b: 0, class_c: 0 })

const CLASSES = [
  { key: 'class_a' as const, hint: '普工类作业（穿线、桥架、管槽）' },
  { key: 'class_b' as const, hint: '技工作业（设备安装、熔接、端接）' },
  { key: 'class_c' as const, hint: '高级技工（系统调试、专业设备）' },
]

type ClassKey = 'class_a' | 'class_b' | 'class_c'

/** 表格插槽里的 row 是 any，用这两个函数读写逐工种浮动率（避免隐式 any 索引报错）。 */
function getPct(key: ClassKey): number {
  return perClassPct[key]
}

function setPct(key: ClassKey, value: number | undefined): void {
  perClassPct[key] = value ?? 0
}

const locked = computed(() => ['approved', 'archived'].includes(String(quote.value?.status ?? '')))
const canEdit = computed(() => auth.canWrite && !locked.value)

const rows = computed(() =>
  CLASSES.map((c) => {
    const info = current.value?.rates?.[c.key]
    const price = info ? toNum(info.price_per_day) : 0
    const pct = overallPct.value + perClassPct[c.key]
    return {
      key: c.key,
      label: meta.label('labor_class', c.key),
      hint: c.hint,
      infoPrice: price,
      pct,
      adjusted: price * (1 + pct / 100),
      source: info?.source ?? '',
    }
  }),
)

const periodLabel = computed(() => {
  const c = current.value
  if (!c) return '—'
  const d = (c.effective_date ?? '').slice(0, 7).replace('-', ' 年 ') + ' 月'
  return `${c.region === 'ningbo' ? '宁波市' : c.region} · ${d}`
})

const coef = reactive({ shielded: 1.2, cat5e: 1.1, ceiling: 1.2 })
const coefSaving = ref(false)

/** 读工艺系数（app_settings 的 quote 组 ✓，缺省与后端一致 ✓） */
async function loadCoef(): Promise<void> {
  try {
    const { data } = await http.get('/settings')
    const q = ((data as Record<string, any>)?.quote ?? {}) as Record<string, unknown>
    coef.shielded = Number(q.shielded_coef ?? 1.2)
    coef.cat5e = Number(q.cat5e_coef ?? 1.1)
    coef.ceiling = Number(q.ceiling_coef ?? 1.2)
  } catch {
    /* 读不到就用默认值，不影响页面 ✓ */
  }
}

/** 保存工艺系数（重新清点后生效 ✓） */
async function saveCoef(): Promise<void> {
  coefSaving.value = true
  try {
    const { data } = await http.get('/settings')
    const doc = (data ?? {}) as Record<string, any>
    await http.patch('/settings', {
      ...doc,
      quote: {
        ...(doc.quote ?? {}),
        shielded_coef: String(coef.shielded),
        cat5e_coef: String(coef.cat5e),
        ceiling_coef: String(coef.ceiling),
      },
    })
    ElMessage.success('工艺系数已保存 —— 重新清点后生效 ✓')
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    coefSaving.value = false
  }
}

async function reload(): Promise<void> {
  void loadCoef()   // 工艺系数（报价级）✓
  loading.value = true
  try {
    const [p, q, cur] = await Promise.all([
      projectApi.get(projectId.value),
      quoteApi.get(quoteId.value),
      laborApi.current(),
    ])
    project.value = p
    quote.value = q
    current.value = cur
    const per = (q.labor_float_per_class ?? {}) as Record<string, number>
    overallPct.value = ratioToPercentInput(q.labor_float_rate)
    perClassPct.class_a = ratioToPercentInput(per.class_a ?? 0)
    perClassPct.class_b = ratioToPercentInput(per.class_b ?? 0)
    perClassPct.class_c = ratioToPercentInput(per.class_c ?? 0)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

onMounted(reload)

/** 原型反信息条里的「应用到全部工种」：整体浮动本就对三类都生效，这里顺带清掉逐工种覆盖。 */
function applyToAll(): void {
  perClassPct.class_a = 0
  perClassPct.class_b = 0
  perClassPct.class_c = 0
  ElMessage.success(`统一浮动率 ${overallPct.value}% 已应用于全部工种（逐工种覆盖已清零）`)
}

function resetAll(): void {
  overallPct.value = 0
  perClassPct.class_a = 0
  perClassPct.class_b = 0
  perClassPct.class_c = 0
}

async function save(): Promise<void> {
  if (!quote.value) return
  if (overallPct.value < -50 || overallPct.value > 200) {
    ElMessage.warning('统一浮动率允许范围 -50% ~ +200%')
    return
  }
  saving.value = true
  try {
    const res = await quoteApi.compute(quoteId.value, {
      version: quote.value.version,
      labor_float_rate: percentInputToRatio(overallPct.value),
      labor_float_per_class: {
        class_a: percentInputToRatio(perClassPct.class_a),
        class_b: percentInputToRatio(perClassPct.class_b),
        class_c: percentInputToRatio(perClassPct.class_c),
      },
    })
    // 这一页的「保存」走的也是 POST /compute ✓ → 同样会落一版快照 ✓，
    // 所以提示与工作台/报价编制页**同一口径** ✓（见 utils/computeMessage.ts）
    ElMessage.success(computeSavedMessage(res, '人工费已重算'))
    await reload()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    saving.value = false
  }
}

function back(): void {
  void router.push({
    name: 'quote-workbench',
    params: { projectId: projectId.value, quoteId: quoteId.value },
  })
}
</script>

<template>
  <div v-loading="loading" class="page ls">
    <!-- ① 顶栏 -->
    <div class="card ls-top">
      <el-button link @click="back">
        <el-icon><ArrowLeft /></el-icon>
      </el-button>
      <div>
        <div class="ls-title">人工费设置</div>
        <div class="ls-sub">
          {{ quote?.code }} · {{ project?.name }}
        </div>
      </div>
      <div class="spacer" />
      <span class="text-muted ls-rule">
        浙江省通用安装工程定额（2025 版）第五册 建筑智能化工程
      </span>
    </div>

    <!-- ② 信息条 -->
    <div class="card ls-bar">
      <div class="ls-field">
        <label>计价依据</label>
        <el-select model-value="zj2018-5" disabled style="width: 230px">
          <el-option label="浙江省通用安装工程定额 2025 版 · 第五册" value="zj2025-5" />
        <!-- 2026-10-05 换版：原写 2018 版 ✗，已同步为 2025 ✓ -->
        </el-select>
        <span class="text-muted ls-note">定额册别未入库（属交付资料文字），此处只作展示</span>
      </div>

      <div class="ls-field">
        <label>人工信息价期数</label>
        <el-select model-value="current" disabled style="width: 210px">
          <el-option :label="periodLabel" value="current" />
        </el-select>
        <span class="text-muted ls-note">
          计算固定取「当前生效」期次；报价单上还没有"用哪一期"的字段
        </span>
      </div>

      <!-- 工艺系数（2026-10-05 用户要求）：不是子目的属性，是"这次施工条件"的属性 ✓
           只有线缆用得上（明细行的 屏蔽/超五类/吊顶 开关触发 ✓） -->
      <div class="ls-field">
        <label>工艺系数</label>
        <div style="display: flex; align-items: center; gap: 8px; flex-wrap: wrap">
          <span class="text-muted">屏蔽</span>
          <el-input-number v-model="coef.shielded" :min="0" :max="9.99" :step="0.05" :precision="2"
                           size="small" :controls="false" style="width: 92px" />
          <span class="text-muted">超五类</span>
          <el-input-number v-model="coef.cat5e" :min="0" :max="9.99" :step="0.05" :precision="2"
                           size="small" :controls="false" style="width: 92px" />
          <span class="text-muted">吊顶</span>
          <el-input-number v-model="coef.ceiling" :min="0" :max="9.99" :step="0.05" :precision="2"
                           size="small" :controls="false" style="width: 92px" />
          <el-button type="primary" size="small" :loading="coefSaving" @click="saveCoef">
            保存系数
          </el-button>
        </div>
        <span class="text-muted ls-note">
          设备行与定额行统一用这三个值 ✓（原来存在每个定额子目上 ✗，同一子目在吊顶/非吊顶区分不开）
        </span>
      </div>
      <div class="ls-field">
        <label>统一浮动率</label>
        <el-input-number
          v-model="overallPct"
          :min="-50"
          :max="200"
          :precision="2"
          :step="1"
          controls-position="right"
          :disabled="!canEdit"
          style="width: 140px"
        />
        <span class="unit">%</span>
        <el-button :disabled="!canEdit" @click="applyToAll">应用到全部工种</el-button>
        <el-button :disabled="!canEdit" @click="resetAll">重置</el-button>
      </div>

      <div class="ls-field">
        <label>加减价方式</label>
        <el-select model-value="ratio" disabled style="width: 150px">
          <el-option label="按比例浮动" value="ratio" />
        </el-select>
        <span class="text-muted ls-note">后端只支持按比例（没有"按元/工日"字段）</span>
      </div>

      <div class="ls-field ls-field-right">
        <span class="text-muted ls-note">
          浮动率可逐工种单独覆盖；有效上浮率 = <b>统一浮动率 + 该工种浮动率</b>
        </span>
        <div class="ls-share">
          人工费占本报价单 <b>—</b>
          <el-tooltip content="后端没有单独的人工费金额字段（定额口径合计含人工+机械+辅材），故无法计算占比">
            <el-icon><QuestionFilled /></el-icon>
          </el-tooltip>
        </div>
      </div>
    </div>

    <!-- ③ 工种表 -->
    <div class="card ls-table">
      <div class="ls-table-head">
        <span class="ls-h">工种人工单价</span>
        <span class="text-muted">调整后单价 = 本期信息价 ×（1 + 有效上浮率）</span>
      </div>
      <el-table :data="rows" border stripe>
        <el-table-column label="工种" width="120">
          <template #default="{ row }">
            <div>{{ row.label }}</div>
            <div class="text-muted ls-cell-sub">{{ row.key }}</div>
          </template>
        </el-table-column>
        <el-table-column label="本期信息价（元/工日）" width="170" align="right">
          <template #default="{ row }">
            <span class="num">{{ money(row.infoPrice) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="浮动方式" width="100">
          <template #default>按比例</template>
        </el-table-column>
        <el-table-column label="浮动率（%）" width="150">
          <template #default="{ row }">
            <el-input-number
              :model-value="getPct(row.key)"
              :min="-50"
              :max="200"
              :precision="2"
              :step="1"
              :controls="false"
              size="small"
              :disabled="!canEdit"
              style="width: 100%"
              @change="(v: number | undefined) => setPct(row.key, v)"
            />
          </template>
        </el-table-column>
        <el-table-column label="有效上浮率（%）" width="130" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.pct.toFixed(2) }}</span>
            <span v-if="overallPct !== 0" class="text-muted ls-cell-sub">
              含统一 {{ overallPct.toFixed(2) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="调整后单价（元/工日）" width="170" align="right">
          <template #default="{ row }">
            <span class="num strong">{{ money(row.adjusted) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="与信息价偏差" width="130" align="right">
          <template #default="{ row }">
            <span class="num" :class="row.pct > 0 ? 'warn-text' : row.pct < 0 ? 'ok-text' : ''">
              {{ row.pct > 0 ? '+' : '' }}{{ row.pct.toFixed(2) }}%
            </span>
          </template>
        </el-table-column>
        <el-table-column label="用途说明" min-width="220">
          <template #default="{ row }">
            <span class="text-muted">{{ row.hint }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div class="ls-foot">
        <span class="text-muted">
          只影响**定额口径**（`quota_total`）；点位单价口径不读人工浮动。
          <template v-if="locked">当前单据已{{ quote?.status === 'approved' ? '批准' : '归档' }}，需先退回草稿才能改。</template>
        </span>
        <div class="spacer" />
        <el-button @click="back">返回工作台</el-button>
        <el-button
          v-if="canEdit"
          type="primary"
          :loading="saving"
          @click="save"
        >
          保存并重算
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ls {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.ls-top {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ls-title {
  font-size: 16px;
  font-weight: 700;
}

.ls-sub {
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
}

.ls-rule {
  font-size: 12.5px;
}

.ls-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 14px 24px;
  align-items: flex-start;
}

.ls-field {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ls-field > label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  min-width: 84px;
}

.ls-field-right {
  margin-left: auto;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
}

.ls-note {
  font-size: 11.5px;
}

.ls-share {
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 4px;
}

.unit {
  color: var(--el-text-color-secondary);
}

.ls-table-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 8px;
}

.ls-h {
  font-weight: 600;
}

.ls-cell-sub {
  font-size: 11.5px;
}

.ls-foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
}
</style>

