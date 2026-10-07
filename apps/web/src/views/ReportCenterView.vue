<script setup lang="ts">
/**
 * 报表中心（2026-10-04，原型顶栏导航项）。
 *
 * 一屏总览，每块都能追到明细：
 *   报价状态分布 / 待办（待审批·审核中）/ 项目状态分布 /
 *   设备库健康度 / 停产型号影响 / 人工费信息价
 *
 * 数据全部来自 `GET /reports/overview` —— 前端不自己聚合，
 * 免得"页面上的数字"和"接口/导出里的数字"两套。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { dateTime, money } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { reportApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { ProfitDashboard, ReportOverview } from '@/api/types'

const router = useRouter()
const loading = ref(false)
const data = ref<ReportOverview | null>(null)

const quoteTotal = computed(() => data.value?.quotes.amount ?? 0)

/** 占比条长度（按金额），拿状态里最大金额做基准，避免小金额看不出差别 */
const maxStatusAmount = computed(() =>
  Math.max(1, ...(data.value?.quotes.by_status ?? []).map((r) => r.amount)),
)

async function load(): Promise<void> {
  loading.value = true
  try {
    data.value = await reportApi.overview()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

const auth = useAuthStore()

// ---------- 利润看板（2026-10-04）----------
// 只有 admin/经理/财务能看；其它角色请求会 403，这里先按角色判断，避免白跑一趟。
const canSeeProfit = computed(() => auth.canSeeProfit)
const profit = ref<ProfitDashboard | null>(null)
const profitLoading = ref(false)
const profitMonths = ref(6)

/** 趋势条宽度按当月金额占最大值的比例（没有图表库，用内联样式画） */
const maxTrendAmount = computed(() =>
  Math.max(1, ...(profit.value?.trend ?? []).map((x) => x.amount)),
)

async function loadProfit(): Promise<void> {
  if (!canSeeProfit.value) return
  profitLoading.value = true
  try {
    profit.value = await reportApi.profitDashboard(profitMonths.value)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    profitLoading.value = false
  }
}

function onProfitMonthsChange(): void {
  void loadProfit()
}

onMounted(async () => {
  await load()
  await loadProfit()
})

function goProjects(): void {
  void router.push({ name: 'projects' })
}

function goDevices(): void {
  void router.push({ name: 'devices' })
}

function goLabor(): void {
  void router.push({ name: 'labor' })
}
</script>

<template>
  <div v-loading="loading" class="page rp">
    <div class="page-head">
      <div>
        <h2 class="page-title">报表中心</h2>
        <p class="page-sub">项目 / 报价 / 设备库 / 人工费的一屏总览 · 每块都可追到明细</p>
      </div>
      <div>
        <el-button @click="load">
          <el-icon><Refresh /></el-icon>&nbsp;刷新
        </el-button>
      </div>
    </div>

    <!-- 顶部四张卡 -->
    <div v-if="data" class="rp-cards">
      <div class="card rp-card">
        <div class="rp-card-label">报价单总数</div>
        <div class="rp-card-value">{{ data.quotes.total }}</div>
        <div class="text-muted">含税合计 {{ money(quoteTotal) }}</div>
      </div>
      <div class="card rp-card" :class="{ 'rp-card-warn': data.quotes.pending_count > 0 }">
        <div class="rp-card-label">待办 · 待审批 / 审核中</div>
        <div class="rp-card-value">
          {{ data.quotes.pending_count }}<span class="rp-unit">单</span>
        </div>
        <div class="text-muted">{{ money(data.quotes.pending_amount) }}</div>
      </div>
      <div class="card rp-card">
        <div class="rp-card-label">项目总数</div>
        <div class="rp-card-value">{{ data.projects.total }}</div>
        <div class="text-muted">
          <el-button link type="primary" @click="goProjects">看项目列表</el-button>
        </div>
      </div>
      <div class="card rp-card" :class="{ 'rp-card-danger': data.devices.eol > 0 }">
        <div class="rp-card-label">设备库 · 已停产 / 退市</div>
        <div class="rp-card-value">
          {{ data.devices.eol }}<span class="rp-unit">/ {{ data.devices.total }}</span>
        </div>
        <div class="text-muted">
          <el-button link type="primary" @click="goDevices">去设备库换型</el-button>
        </div>
      </div>
    </div>

    <div v-if="data" class="rp-grid">
      <!-- 报价状态分布 -->
      <div class="card rp-panel">
        <div class="rp-panel-head">
          <span>报价状态分布</span>
          <span class="text-muted">单数 / 含税金额</span>
        </div>
        <el-table :data="data.quotes.by_status" size="small" border>
          <el-table-column label="状态" width="96">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.pending ? 'warning' : 'info'">
                {{ row.label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="单数" width="64" align="right">
            <template #default="{ row }"><span class="num">{{ row.count }}</span></template>
          </el-table-column>
          <el-table-column label="金额" width="128" align="right">
            <template #default="{ row }"><span class="num">{{ money(row.amount) }}</span></template>
          </el-table-column>
          <el-table-column label="占比">
            <template #default="{ row }">
              <div class="rp-bar">
                <div
                  class="rp-bar-fill"
                  :class="{ 'rp-bar-pending': row.pending }"
                  :style="{ width: `${Math.max(2, (row.amount / maxStatusAmount) * 100)}%` }"
                />
              </div>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 项目状态分布 -->
      <div class="card rp-panel">
        <div class="rp-panel-head">
          <span>项目状态分布</span>
          <span class="text-muted">共 {{ data.projects.total }} 个</span>
        </div>
        <el-table :data="data.projects.by_status" size="small" border>
          <el-table-column label="状态" prop="label" />
          <el-table-column label="项目数" width="90" align="right">
            <template #default="{ row }"><span class="num">{{ row.count }}</span></template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 设备库健康度 -->
      <div class="card rp-panel">
        <div class="rp-panel-head">
          <span>设备库健康度</span>
          <span class="text-muted">共 {{ data.devices.total }} 个型号</span>
        </div>
        <div class="rp-kv"><span>在售</span><span class="num ok-text">{{ data.devices.on_sale }}</span></div>
        <div class="rp-kv"><span>临近停产</span><span class="num warn-text">{{ data.devices.near_eol }}</span></div>
        <div class="rp-kv"><span>已停产 / 退市</span><span class="num danger-text">{{ data.devices.eol }}</span></div>
        <div class="rp-kv"><span>已配替代型号</span><span class="num">{{ data.devices.with_replacement }}</span></div>
        <div class="rp-kv"><span>甲供</span><span class="num">{{ data.devices.owner_supplied }}</span></div>
      </div>

      <!-- 人工费信息价 -->
      <div class="card rp-panel">
        <div class="rp-panel-head">
          <span>人工费信息价（当前生效）</span>
          <span class="text-muted">
            <el-button link type="primary" @click="goLabor">人工费与定额</el-button>
          </span>
        </div>
        <div v-if="data.labor_current.length">
          <div class="rp-kv">
            <span>期次</span>
            <span>{{ data.labor_current[0].region === 'ningbo' ? '宁波市' : data.labor_current[0].region }} · {{ data.labor_current[0].effective_date }}</span>
          </div>
          <div v-for="r in data.labor_current" :key="r.labor_class" class="rp-kv">
            <span>{{ r.label }}</span>
            <span class="num">{{ money(r.price_per_day) }} 元/工日</span>
          </div>
        </div>
        <el-empty v-else description="没有生效中的人工信息价" :image-size="50" />
      </div>

      <!-- 停产型号影响 -->
      <div class="card rp-panel rp-panel-wide">
        <div class="rp-panel-head">
          <span>停产型号影响</span>
          <span class="text-muted">
            未归档的报价单里还挂着停产型号：{{ data.discontinued_impact.quote_count }} 张单 ·
            {{ data.discontinued_impact.item_count }} 行
          </span>
        </div>
        <el-alert
          v-if="data.discontinued_impact.quote_count === 0"
          type="success"
          show-icon
          :closable="false"
          title="没有未归档的报价单挂着停产型号 —— 换型都做完了"
        />
        <el-table v-else :data="data.discontinued_impact.quotes" size="small" border>
          <el-table-column prop="code" label="报价单号" width="180" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.status_label }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="停产行数" width="110" align="right">
            <template #default="{ row }"><span class="num danger-text">{{ row.item_count }}</span></template>
          </el-table-column>
          <el-table-column label="说明">
            <template #default>
              <span class="text-muted">这些行的设备已停产；报价前建议用「换型」换成替代型号</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
    <!-- 利润看板（2026-10-04）：只统计已批准、每项目取最新一版 -->
    <div v-if="canSeeProfit" class="card mt">
      <div class="card-title">
        利润看板
        <el-select
          v-model="profitMonths"
          size="small"
          style="width: 120px; margin-left: 8px"
          @change="onProfitMonthsChange"
        >
          <el-option :value="3" label="近 3 个月" />
          <el-option :value="6" label="近 6 个月" />
          <el-option :value="12" label="近 12 个月" />
          <el-option :value="24" label="近 24 个月" />
        </el-select>
      </div>

      <div v-loading="profitLoading">
        <el-row :gutter="12" class="mb">
          <el-col :span="5">
            <div class="pf-kpi">
              <div class="pf-label">项目数</div>
              <div class="pf-value">{{ profit?.summary.projects ?? 0 }}</div>
            </div>
          </el-col>
          <el-col :span="5">
            <div class="pf-kpi">
              <div class="pf-label">含税金额</div>
              <div class="pf-value num">{{ money(profit?.summary.amount) }}</div>
            </div>
          </el-col>
          <el-col :span="5">
            <div class="pf-kpi">
              <div class="pf-label">成本合计</div>
              <div class="pf-value num">{{ money(profit?.summary.cost) }}</div>
            </div>
          </el-col>
          <el-col :span="5">
            <div class="pf-kpi">
              <div class="pf-label">毛利</div>
              <div class="pf-value num" :class="(profit?.summary.profit ?? 0) < 0 ? 'danger-text' : 'ok-text'">
                {{ money(profit?.summary.profit) }}
              </div>
            </div>
          </el-col>
          <el-col :span="4">
            <div class="pf-kpi">
              <div class="pf-label">毛利率</div>
              <div class="pf-value num">
                {{ profit ? (profit.summary.rate * 100).toFixed(1) : '0.0' }}%
              </div>
            </div>
          </el-col>
        </el-row>

        <div class="mb"><b>按项目</b>（毛利从低到高，先看亏的）</div>
        <el-table :data="profit?.by_project ?? []" border size="small" max-height="300">
          <el-table-column prop="project_code" label="项目号" width="140" />
          <el-table-column prop="project_name" label="项目" min-width="160" show-overflow-tooltip />
          <el-table-column prop="customer_name" label="客户" min-width="140" show-overflow-tooltip />
          <el-table-column prop="quote_code" label="报价单" width="140" />
          <el-table-column label="含税金额" width="120" align="right">
            <template #default="{ row }"><span class="num">{{ money(row.total_with_tax) }}</span></template>
          </el-table-column>
          <el-table-column label="成本" width="120" align="right">
            <template #default="{ row }"><span class="num">{{ money(row.cost_total) }}</span></template>
          </el-table-column>
          <el-table-column label="毛利" width="120" align="right">
            <template #default="{ row }">
              <span class="num" :class="row.gross_profit < 0 ? 'danger-text' : ''">
                {{ money(row.gross_profit) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="毛利率" width="90" align="right">
            <template #default="{ row }">
              <span class="num" :class="row.gross_profit_rate < 0 ? 'danger-text' : ''">
                {{ (row.gross_profit_rate * 100).toFixed(1) }}%
              </span>
            </template>
          </el-table-column>
          <el-table-column label="批准时间" width="150">
            <template #default="{ row }">
              {{ row.approved_at ? dateTime(row.approved_at) : '—' }}
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="还没有已批准的报价单" :image-size="60" />
          </template>
        </el-table>

        <div class="mb" style="margin-top: 14px"><b>月度趋势</b></div>
        <el-table :data="profit?.trend ?? []" border size="small">
          <el-table-column prop="month" label="月份" width="100" />
          <el-table-column prop="quotes" label="单数" width="70" />
          <el-table-column label="金额" width="300">
            <template #default="{ row }">
              <div class="pf-bar-wrap">
                <div class="pf-bar" :style="{ width: `${(row.amount / maxTrendAmount) * 100}%` }" />
                <span class="num pf-bar-text">{{ money(row.amount) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="成本" width="120" align="right">
            <template #default="{ row }"><span class="num">{{ money(row.cost) }}</span></template>
          </el-table-column>
          <el-table-column label="毛利" width="120" align="right">
            <template #default="{ row }">
              <span class="num" :class="row.profit < 0 ? 'danger-text' : 'ok-text'">
                {{ money(row.profit) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="毛利率" width="90" align="right">
            <template #default="{ row }"><span class="num">{{ (row.rate * 100).toFixed(1) }}%</span></template>
          </el-table-column>
          <template #empty>
            <el-empty description="所选区间内没有已批准报价" :image-size="60" />
          </template>
        </el-table>
      </div>
    </div>

</template>

<style scoped>
.rp-cards {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.rp-card {
  flex: 1 1 200px;
  padding: 10px 14px;
}

.rp-card-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.rp-card-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.3;
}

.rp-unit {
  font-size: 13px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  margin-left: 4px;
}

.rp-card-warn {
  border-color: var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
}

.rp-card-danger {
  border-color: var(--el-color-danger-light-5);
  background: var(--el-color-danger-light-9);
}

.rp-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.rp-panel {
  padding: 10px 12px;
}

.rp-panel-wide {
  grid-column: 1 / -1;
}

.rp-panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  font-weight: 600;
  font-size: 13.5px;
  margin-bottom: 8px;
}

.rp-panel-head .text-muted {
  font-weight: 400;
  font-size: 11.5px;
}

.rp-kv {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}

.rp-bar {
  height: 10px;
  background: var(--el-fill-color-light);
  border-radius: 5px;
  overflow: hidden;
}

.rp-bar-fill {
  height: 100%;
  background: var(--el-color-primary-light-3);
}

.rp-bar-pending {
  background: var(--el-color-warning);
}

.pf-kpi {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
}
.pf-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.pf-value {
  font-size: 18px;
  font-weight: 600;
}
.pf-bar-wrap {
  position: relative;
  height: 18px;
}
.pf-bar {
  position: absolute;
  left: 0;
  top: 2px;
  height: 14px;
  background: var(--el-color-primary-light-5);
  border-radius: 3px;
}
.pf-bar-text {
  position: relative;
  font-size: 12px;
  line-height: 18px;
  padding-left: 6px;
}
</style>
