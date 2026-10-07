<script setup lang="ts">
/**
 * 人工费与定额（对应原型 Page 4）。
 *
 * 两件事：
 *   1) 宁波人工市场信息价的期次管理（新期次录入后自动把旧期次置为非当前）
 *   2) 调价测算：给定各工种工日数，比对「旧价 vs 新价」下的人工费差额
 *
 * 定额子目带 2022.7.13 纪要的三条工艺系数（屏蔽/超五类/吊顶），
 * 这是定额口径能对上审核的关键，所以表里单独成列。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { laborApi } from '@/api'
import type { AdjustSimulation, CurrentLaborRates, LaborRate, QuotaItem } from '@/api/types'
import { money, toNum } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const auth = useAuthStore()
const meta = useMetaStore()

const activeTab = ref('labor')

// ---------- 人工信息价 ----------

const laborLoading = ref(false)
const laborRows = ref<LaborRate[]>([])
const current = ref<CurrentLaborRates | null>(null)

async function loadLabor(): Promise<void> {
  laborLoading.value = true
  try {
    laborRows.value = await laborApi.list(false)
    try {
      current.value = await laborApi.current()
    } catch {
      current.value = null
    }
  } finally {
    laborLoading.value = false
  }
}

const dedupedLabor = computed(() => {
  // 同一期次有 3 个工种（一/二/三类），按日期 + region 聚合成一行更好读
  const byKey = new Map<string, LaborRate[]>()
  for (const r of laborRows.value) {
    const key = `${r.region}|${r.effective_date}`
    const list = byKey.get(key) ?? []
    list.push(r)
    byKey.set(key, list)
  }
  return [...byKey.entries()]
    .map(([key, list]) => {
      const [region, effective_date] = key.split('|')
      const pick = (cls: string) => list.find((r) => r.labor_class === cls)
      return {
        key,
        region,
        effective_date,
        is_current: list.some((r) => r.is_current),
        class_a: pick('class_a'),
        class_b: pick('class_b'),
        class_c: pick('class_c'),
        source: list[0]?.source ?? '',
      }
    })
    .sort((a, b) => (a.effective_date < b.effective_date ? 1 : -1))
})

const laborDialogVisible = ref(false)
const laborSaving = ref(false)
const laborFormRef = ref()

const laborForm = reactive({
  region: 'ningbo',
  effective_date: '' as string,
  class_a: 0,
  class_b: 0,
  class_c: 0,
  source: '宁波市建设工程造价信息网',
})

function openLaborDialog(): void {
  const today = new Date()
  const iso = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-01`
  Object.assign(laborForm, {
    region: 'ningbo',
    effective_date: iso,
    // 用当前生效价做默认值，减少手填
    class_a: current.value?.rates.class_a?.price_per_day ?? 150,
    class_b: current.value?.rates.class_b?.price_per_day ?? 163,
    class_c: current.value?.rates.class_c?.price_per_day ?? 186,
    source: '宁波市建设工程造价信息网',
  })
  laborDialogVisible.value = true
}

async function saveLabor(): Promise<void> {
  if (!laborForm.effective_date) {
    ElMessage.warning('请选择生效日期')
    return
  }
  // 一个期次要提交三条记录（一/二/三类）。如果中期某条被后端 409 拒绝，
  // 前面的已经落库了 —— 那就会出现「一类是 2026 价、二三类还是 2025 价」的
  // 半截期次，人工费算出来是错的且很难发现。所以先自己查重，别把半截数据写进去。
  const dup = dedupedLabor.value.find(
    (r) => r.region === laborForm.region && r.effective_date === laborForm.effective_date,
  )
  if (dup) {
    ElMessage.warning(
      `${laborForm.effective_date} 这一期已经存在，请改用其它日期，或先删掉旧记录。`,
    )
    return
  }

  laborSaving.value = true
  const done: string[] = []
  try {
    for (const cls of ['class_a', 'class_b', 'class_c'] as const) {
      await laborApi.create({
        region: laborForm.region,
        labor_class: cls,
        effective_date: laborForm.effective_date,
        price_per_day: laborForm[cls],
        source: laborForm.source,
      })
      done.push(meta.label('labor_class', cls))
    }
    ElMessage.success('新期次已录入，旧的当前价已自动置为非当前')
    laborDialogVisible.value = false
    await loadLabor()
  } catch (err) {
    if (done.length) {
      // 明确告诉用户写进去了几条，避免他以为整批都失败了
      ElMessage.error(
        `只写入了 ${done.join('、')}，本次期次不完整，请到列表里核对后再补齐。`,
      )
      await loadLabor()
    } else {
      throw err
    }
  } finally {
    laborSaving.value = false
  }
}

// ---------- 调价测算 ----------

const simVisible = ref(false)
const simLoading = ref(false)
const simResult = ref<AdjustSimulation | null>(null)

const simForm = reactive({
  class_a: 0,
  class_b: 0,
  class_c: 0,
})

function openSim(): void {
  Object.assign(simForm, { class_a: 0, class_b: 0, class_c: 0 })
  simResult.value = null
  simVisible.value = true
}

async function runSim(): Promise<void> {
  const days: Record<string, number> = {}
  if (simForm.class_a > 0) days.class_a = simForm.class_a
  if (simForm.class_b > 0) days.class_b = simForm.class_b
  if (simForm.class_c > 0) days.class_c = simForm.class_c
  if (!Object.keys(days).length) {
    ElMessage.warning('请至少填写一个工种的工日数')
    return
  }
  simLoading.value = true
  try {
    simResult.value = await laborApi.simulate({ labor_days: days })
  } finally {
    simLoading.value = false
  }
}

const simRows = computed(() => {
  const r = simResult.value
  if (!r) return []
  const classes = ['class_a', 'class_b', 'class_c']
  return classes
    .filter((c) => (r.labor_days[c] ?? 0) > 0)
    .map((c) => ({
      cls: c,
      label: meta.label('labor_class', c),
      days: r.labor_days[c] ?? 0,
      oldRate: r.old_rates[c] ?? 0,
      newRate: r.new_rates[c] ?? 0,
      oldAmount: (r.labor_days[c] ?? 0) * (r.old_rates[c] ?? 0),
      newAmount: (r.labor_days[c] ?? 0) * (r.new_rates[c] ?? 0),
    }))
})

const simDeltaClass = computed(() => (toNum(simResult.value?.delta) >= 0 ? 'up' : 'down'))

// ---------- 定额子目 ----------

const quotaLoading = ref(false)
const quotaRows = ref<QuotaItem[]>([])
const quotaQuery = reactive({ category: '', keyword: '' })

async function loadQuota(): Promise<void> {
  quotaLoading.value = true
  try {
    quotaRows.value = await laborApi.quotaItems({
      category: quotaQuery.category || undefined,
      keyword: quotaQuery.keyword || undefined,
    })
  } finally {
    quotaLoading.value = false
  }
}

function onTabChange(name: string | number): void {
  // el-tabs 的回调签名是 TabPaneName = string | number
  if (String(name) === 'quota' && !quotaRows.value.length) void loadQuota()
}

onMounted(async () => {
  await Promise.all([meta.load(), loadLabor()])
})

function rateSourceLabel(source: string): string {
  const m: Record<string, string> = {
    db: '数据库当期价',
    'default-seed': '内置兜底价（未录入当期信息价）',
  }
  return m[source] ?? source
}
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">人工费与定额</h2>
        <p class="page-sub">
          宁波人工市场信息价 · 浙江省通用安装工程预算定额（2018）第五册 建筑智能化工程
        </p>
      </div>
      <div>
        <el-button @click="openSim">
          <el-icon><DataAnalysis /></el-icon>&nbsp;调价测算
        </el-button>
        <el-button
          v-if="auth.can('labor_rate', 'create')"
          type="primary"
          @click="openLaborDialog"
        >
          <el-icon><Plus /></el-icon>&nbsp;录入新期次
        </el-button>
      </div>
    </div>

    <!-- 当前生效价 -->
    <div v-if="current" class="cur-card">
      <div class="cur-head">
        <el-icon><Coin /></el-icon>
        <span>当前生效价</span>
        <el-tag size="small" effect="plain">{{ current.effective_date }}</el-tag>
        <el-tag size="small" type="info" effect="plain">{{ current.region }}</el-tag>
      </div>
      <div class="cur-row">
        <div v-for="cls in ['class_a', 'class_b', 'class_c']" :key="cls" class="cur-item">
          <span class="cur-label">{{ meta.label('labor_class', cls) }}</span>
          <span class="cur-value">
            {{ money(current.rates[cls]?.price_per_day) }}
            <small>元 / 工日</small>
          </span>
        </div>
      </div>
      <el-alert
        v-if="current.stale_warning"
        type="warning"
        :closable="false"
        show-icon
        class="mt"
        :title="current.stale_warning"
      />
    </div>
    <el-alert
      v-else
      type="warning"
      show-icon
      :closable="false"
      class="mb"
      title="尚未录入任何人工信息价，定额口径将使用内置兜底价（一类 150 / 二类 163 / 三类 186 元/工日）。"
    />

    <el-tabs v-model="activeTab" @tab-change="onTabChange">
      <!-- ---------- 信息价期次 ---------- -->
      <el-tab-pane label="信息价期次" name="labor">
        <div class="card">
          <h3 class="card-title"><el-icon><Calendar /></el-icon>历史期次</h3>
          <el-table v-loading="laborLoading" :data="dedupedLabor" border stripe>
            <el-table-column label="生效日期" width="130">
              <template #default="{ row }">
                <span class="mono">{{ row.effective_date }}</span>
                <el-tag v-if="row.is_current" size="small" type="success" effect="dark" class="ml">
                  当前
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="一类工日" width="110" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.class_a?.price_per_day) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="二类工日" width="110" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.class_b?.price_per_day) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="三类工日" width="110" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.class_c?.price_per_day) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="地区" prop="region" width="100" />
            <el-table-column label="来源" prop="source" min-width="180" show-overflow-tooltip />
            <template #empty>
              <el-empty description="还没有录入信息价" />
            </template>
          </el-table>
        </div>
      </el-tab-pane>

      <!-- ---------- 定额子目 ---------- -->
      <el-tab-pane label="定额子目" name="quota">
        <div class="card">
          <el-alert
            type="warning"
            :closable="false"
            show-icon
            class="mb"
            title="⚠ 定额编号与人工消耗量待造价员按纸质定额书实际核对后再用于正式报价。"
          >
            <template #default>
              库内消耗量为结构示意值，用于打通「明细行 → 定额口径」的计算链路；
              正式对甲方或送审前，请以《浙江省通用安装工程预算定额（2018）第五册》为准逐条核对。
            </template>
          </el-alert>

          <el-form inline class="inline-form" @submit.prevent>
            <el-form-item label="章节">
              <el-select
                v-model="quotaQuery.category"
                placeholder="全部"
                clearable
                style="width: 150px"
                @change="loadQuota"
              >
                <el-option
                  v-for="o in meta.options('quota_category')"
                  :key="o.value"
                  :label="o.label"
                  :value="o.value"
                />
              </el-select>
            </el-form-item>
            <el-form-item label="关键字">
              <el-input
                v-model="quotaQuery.keyword"
                placeholder="定额编号 / 名称"
                clearable
                style="width: 220px"
                @keyup.enter="loadQuota"
                @clear="loadQuota"
              />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="loadQuota">
                <el-icon><Search /></el-icon>&nbsp;查询
              </el-button>
            </el-form-item>
          </el-form>

          <el-table v-loading="quotaLoading" :data="quotaRows" border stripe size="small">
            <el-table-column prop="code" label="定额编号" width="120" />
            <el-table-column prop="name" label="子目名称" min-width="200" show-overflow-tooltip />
            <el-table-column label="章节" width="90">
              <template #default="{ row }">
                {{ meta.label('quota_category', row.category) }}
              </template>
            </el-table-column>
            <el-table-column prop="unit" label="单位" width="70" />
            <el-table-column label="一类工日" width="100" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.labor_a_day, 3) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="二类工日" width="100" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.labor_b_day, 3) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="三类工日" width="100" align="right">
              <template #default="{ row }">
                <span class="num">{{ money(row.labor_c_day, 3) }}</span>
              </template>
            </el-table-column>
            <!-- 工艺系数（屏蔽/超五类/吊顶）不在定额库里显示（2026-10-05）：
                 它不是子目的属性，而是"这次施工条件"的属性 ✓ →
                 已改成报价级参数，在「人工费设置」页调整 ✓ -->
            <template #empty>
              <el-empty description="没有匹配的定额子目" />
            </template>
          </el-table>
          <p class="foot">
            工艺系数依据 2022.7.13 宁波智能化工程计价纪要：屏蔽线 ×1.20、超五类 ×1.10、吊顶内敷设 ×1.20。
          </p>
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 录入新期次 -->
    <el-dialog v-model="laborDialogVisible" title="录入人工信息价新期次" width="620px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="mb"
        title="一次录入会生成一类/二类/三类共三条记录，并把同地区旧期次自动置为非当前。"
      />
      <el-form ref="laborFormRef" :model="laborForm" label-width="110px">
        <div class="grid-2">
          <el-form-item label="地区">
            <el-input v-model="laborForm.region" />
          </el-form-item>
          <el-form-item label="生效日期">
            <el-date-picker
              v-model="laborForm.effective_date"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="选择日期"
              style="width: 100%"
            />
          </el-form-item>
        </div>
        <div class="grid-3">
          <el-form-item label="一类（元/工日）">
            <el-input-number
              v-model="laborForm.class_a"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="二类（元/工日）">
            <el-input-number
              v-model="laborForm.class_b"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="三类（元/工日）">
            <el-input-number
              v-model="laborForm.class_c"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
        </div>
        <el-form-item label="来源">
          <el-input v-model="laborForm.source" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="laborDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="laborSaving" @click="saveLabor">录入</el-button>
      </template>
    </el-dialog>

    <!-- 调价测算 -->
    <el-dialog v-model="simVisible" title="人工费调价测算" width="760px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="mb"
        title="输入各工种工日数，系统按「当前生效价」与「即将执行的新价」分别计算人工费差额。"
      />
      <el-form label-width="110px">
        <div class="grid-3">
          <el-form-item label="一类工日">
            <el-input-number
              v-model="simForm.class_a"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="二类工日">
            <el-input-number
              v-model="simForm.class_b"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="三类工日">
            <el-input-number
              v-model="simForm.class_c"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
        </div>
        <el-form-item>
          <el-button type="primary" :loading="simLoading" @click="runSim">开始测算</el-button>
        </el-form-item>
      </el-form>

      <div v-if="simResult">
        <el-table :data="simRows" border size="small">
          <el-table-column label="工种" width="90">
            <template #default="{ row }">{{ row.label }}</template>
          </el-table-column>
          <el-table-column label="工日" width="90" align="right">
            <template #default="{ row }"><span class="num">{{ row.days }}</span></template>
          </el-table-column>
          <el-table-column label="旧单价" width="100" align="right">
            <template #default="{ row }"><span class="num">{{ money(row.oldRate) }}</span></template>
          </el-table-column>
          <el-table-column label="新单价" width="100" align="right">
            <template #default="{ row }"><span class="num">{{ money(row.newRate) }}</span></template>
          </el-table-column>
          <el-table-column label="旧人工费" width="120" align="right">
            <template #default="{ row }">
              <span class="num">{{ money(row.oldAmount) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="新人工费" width="120" align="right">
            <template #default="{ row }">
              <span class="num num-strong">{{ money(row.newAmount) }}</span>
            </template>
          </el-table-column>
        </el-table>

        <el-descriptions :column="4" border size="small" class="mt">
          <el-descriptions-item label="旧合计">
            <span class="num">{{ money(simResult.old_total) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="新合计">
            <span class="num num-strong">{{ money(simResult.new_total) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="差额">
            <span class="num" :class="simDeltaClass">
              {{ simResult.delta >= 0 ? '+' : '' }}{{ money(simResult.delta) }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="幅度">
            <span class="num" :class="simDeltaClass">
              {{ toNum(simResult.delta_pct).toFixed(2) }}%
            </span>
          </el-descriptions-item>
        </el-descriptions>
        <p class="foot">
          费率来源：{{ rateSourceLabel(simResult.rate_source) }}。
          测算结果只覆盖人工费本身，不含管理费、利润与税金的变化。
        </p>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: 10px;
}

.mt {
  margin-top: 12px;
}

.ml {
  margin-left: 6px;
}

.cur-card {
  padding: 14px 16px;
  background: #fff;
  border: 1px solid var(--c-border);
  border-left: 3px solid var(--c-primary);
  border-radius: 6px;
  margin-bottom: 14px;
}

.cur-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 700;
  color: var(--c-navy);
  margin-bottom: 12px;
}

.cur-row {
  display: flex;
  gap: 40px;
  flex-wrap: wrap;
}

.cur-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cur-label {
  font-size: 12px;
  color: var(--c-muted);
}

.cur-value {
  font-size: 19px;
  font-weight: 700;
  color: var(--c-navy);
  font-variant-numeric: tabular-nums;
}

.cur-value small {
  font-size: 11.5px;
  font-weight: 400;
  color: var(--c-muted);
  margin-left: 4px;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
}

.foot {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--c-muted);
  line-height: 1.7;
}

.up {
  color: #dc2626;
  font-weight: 700;
}

.down {
  color: var(--c-green);
  font-weight: 700;
}
</style>
