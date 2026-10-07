<script setup lang="ts">
/**
 * 费用瀑布 + 双行并列口径对比。
 *
 * 为什么不做成「同一张表两个金额列」：后端的定额口径只算到合计，
 * 没有逐项拆分，硬做成两列会得到一列全是「—」的表。所以这里拆成两块：
 *   1) 费用瀑布表 —— 点位单价口径的逐项构成
 *   2) 双行并列对比 —— 两个口径的含税合计 + 差额（这才是决策要看的东西）
 *
 * 两个口径合计不同是设计意图，不是 bug：
 *   - 点位单价口径：设备单价 + 安装单价 × 数量，投标/合同口径
 *   - 定额计价口径：浙江省 2018 定额子目 + 宁波人工信息价 × 工艺系数，造价审核口径
 */
import { computed } from 'vue'

import type { QuoteCost, QuoteDetail } from '@/api/types'
import { money, percent, toNum, wan } from '@/utils/format'

const props = defineProps<{
  quote: QuoteDetail
  /** 是否显示定额口径（后端算不出来时会带 warning，此时降级隐藏） */
  showQuota?: boolean
  /**
   * 成本/毛利。**不再从 quote 上读** —— 通用 DTO 刻意不带这三个字段，
   * 必须由页面在确认权限后单独调 GET /quotes/{id}/cost 取。
   * null 表示无权或未加载。
   */
  cost?: QuoteCost | null
  /** 后端返回的告警，例如「定额库为空，已退化为安装费口径」 */
  warnings?: string[]
}>()

const showQuota = computed(() => props.showQuota !== false)
const showCost = computed(() => props.cost !== null && props.cost !== undefined)
const warnings = computed(() => props.warnings ?? [])
const cost = computed(() => props.cost ?? null)

const q = computed(() => props.quote)

const auxBaseLabel = computed(() => {
  const m: Record<string, string> = {
    device: '设备费',
    device_install: '设备费 + 安装费',
    by_quantity: '按工程量',
  }
  return m[q.value.auxiliary_base] ?? q.value.auxiliary_base
})

const feeBaseLabel = computed(() => {
  const m: Record<string, string> = {
    device_only: '仅设备费',
    device_aux_install: '设备费 + 辅材 + 安装费',
    direct_fee: '直接费',
  }
  return m[q.value.fee_base] ?? q.value.fee_base
})

const taxMethodLabel = computed(() => {
  const m: Record<string, string> = {
    general_9pct: '一般计税 9%',
    simple_3pct: '简易计税 3%',
    general: '一般计税',
    simple: '简易计税',
  }
  return m[q.value.tax_method] ?? q.value.tax_method
})

/** 组成可读的费用瀑布（顺序与 services/quote_compute.py 一致）。 */
const rows = computed(() => {
  const d = q.value
  return [
    { key: 'equipment_total', label: '设备费', hint: 'Σ 数量 × 设备单价', v: d.equipment_total },
    { key: 'install_total', label: '安装调试费', hint: 'Σ 数量 × 安装单价', v: d.install_total },
    {
      key: 'auxiliary_total',
      label: '辅材费',
      // 按工程量口径下费率不参与计算（2026-10-04）：提示必须跟着口径变，
      // 否则面板上写着"按工程量 × 6%"，用户会以为费率还在生效。
      hint:
        q.value.auxiliary_base === 'by_quantity'
          ? '按工程量：Σ(数量 × 单位辅材费)'
          : `${auxBaseLabel.value} × ${percent(d.auxiliary_rate)}`,
      v: d.auxiliary_total,
    },
    { key: 'design_fee', label: '设计费', hint: `按 ${percent(d.design_rate)}`, v: d.design_fee },
    {
      key: 'warranty_fee',
      label: '质保 / 维保费',
      hint: `按设备费 ${percent(d.warranty_rate)}`,
      v: d.warranty_fee,
    },
    { key: 'training_fee', label: '培训费', hint: '固定金额', v: d.training_fee },
    {
      key: 'subtotal_before_tax',
      label: '税前小计',
      hint: '以上各项合计',
      v: d.subtotal_before_tax,
      emphasis: true,
    },
    {
      key: 'mgmt_profit',
      label: '管理费 + 利润',
      hint: `税前小计 × ${percent(d.mgmt_profit_rate)}`,
      v: d.mgmt_profit,
    },
    {
      key: 'tax_amount',
      label: '税金',
      hint: `${taxMethodLabel.value} × ${percent(d.tax_rate)}`,
      v: d.tax_amount,
    },
  ]
})

/** 差额 = 点位单价口径 − 定额计价口径。负数说明定额口径更高。 */
const diff = computed(() => toNum(q.value.total_with_tax) - toNum(q.value.quota_total_with_tax))
const diffPct = computed(() => {
  const base = toNum(q.value.quota_total_with_tax)
  return base === 0 ? 0 : diff.value / base
})
const diffClass = computed(() => (diff.value >= 0 ? 'positive' : 'negative'))
const grossClass = computed(() => (toNum(cost.value?.gross_profit) >= 0 ? 'positive' : 'negative'))
</script>

<template>
  <div class="total-wrap">
    <el-alert
      v-for="(w, i) in warnings"
      :key="i"
      type="warning"
      :closable="false"
      show-icon
      class="mb"
      :title="w"
    />

    <!-- 1) 费用瀑布（点位单价口径） -->
    <table class="dual-table">
      <thead>
        <tr>
          <th style="width: 58%">费用项</th>
          <th style="width: 42%">金额（元）</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.key" :class="r.emphasis ? 'subtotal-row' : ''">
          <td class="label">
            {{ r.label }}
            <div class="hint">{{ r.hint }}</div>
          </td>
          <td class="amount" :class="r.emphasis ? 'num-strong' : ''">{{ money(r.v) }}</td>
        </tr>
        <tr class="total-row">
          <td class="label">
            含税合计
            <div class="hint">税前小计 + 管理费利润 + 税金</div>
          </td>
          <td class="amount num-total">{{ money(q.total_with_tax) }}</td>
        </tr>
      </tbody>
    </table>

    <!-- 2) 双行并列口径对比 -->
    <div v-if="showQuota" class="cmp-block">
      <table class="dual-table">
        <thead>
          <tr>
            <th style="width: 46%">计价口径</th>
            <th style="width: 30%">含税合计（元）</th>
            <th style="width: 24%">相对差</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td class="label">
              点位单价口径
              <div class="hint">设备 + 安装单价 × 数量 · 投标 / 合同口径</div>
            </td>
            <td class="amount num-strong">{{ money(q.total_with_tax) }}</td>
            <td class="amount num-muted">基准</td>
          </tr>
          <tr class="quota-row">
            <td class="label">
              定额计价口径
              <div class="hint">浙江省 2018 定额 + 宁波人工信息价 × 工艺系数</div>
            </td>
            <td class="amount num-strong">{{ money(q.quota_total_with_tax) }}</td>
            <td class="amount" :class="diffClass">
              {{ diff >= 0 ? '+' : '' }}{{ money(diff) }}
              <div class="hint">{{ diff >= 0 ? '+' : '' }}{{ percent(diffPct) }}</div>
            </td>
          </tr>
        </tbody>
      </table>
      <p class="foot-note">
        两个口径合计不同属正常：定额口径用于造价审核与结算比对，点位单价口径用于投标与合同。
        差额为「点位单价口径 − 定额计价口径」，负数表示定额口径更高（存在低于定额价的风险）。
      </p>
    </div>

    <div v-else class="foot-note">
      定额库为空或未匹配到定额子目，本次仅输出点位单价口径。
    </div>

    <p class="foot-note">
      不含税小计 {{ money(q.subtotal_before_tax) }} 元 · 约 {{ wan(q.total_with_tax) }} 元 ·
      附加费基数：{{ feeBaseLabel }} · 辅材基数：{{ auxBaseLabel }}
    </p>

    <!-- 3) 对内成本与毛利 -->
    <div v-if="showCost" class="cost-block">
      <div class="cost-title">
        <el-icon><Money /></el-icon>&nbsp;对内成本与毛利
        <el-tag size="small" type="danger" effect="plain" class="ml">仅造价 / 财务可见</el-tag>
      </div>
      <el-descriptions :column="4" border size="small">
        <el-descriptions-item label="成本合计">
          <span class="num">{{ money(cost?.cost_total) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="毛利">
          <span class="num" :class="grossClass">{{ money(cost?.gross_profit) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="毛利率">
          <span class="num">{{ percent(cost?.gross_profit_rate) }}</span>
        </el-descriptions-item>
        <el-descriptions-item label="含税报价">
          <span class="num num-total">{{ money(cost?.total_with_tax) }}</span>
        </el-descriptions-item>
      </el-descriptions>
      <p class="cost-hint">
        成本价取自设备库 cost_price；缺失时后端按「设备单价 × 75%」估算，
        因此毛利率仅作内部参考，正式对外报价前请补全成本价。
      </p>
    </div>
  </div>
</template>

<style scoped>
.total-wrap {
  width: 100%;
}

.mb {
  margin-bottom: 10px;
}

.th-sub {
  font-weight: 400;
  font-size: 11px;
  color: var(--c-muted);
}

.hint {
  font-size: 11px;
  color: var(--c-muted);
  font-weight: 400;
}

.subtotal-row td {
  background: #f8fafc;
}

.cmp-block {
  margin-top: 16px;
}

.foot-note {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--c-muted);
  line-height: 1.7;
}

.cost-block {
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px dashed var(--c-border);
}

.cost-title {
  display: flex;
  align-items: center;
  margin-bottom: 10px;
  font-size: 14px;
  font-weight: 700;
  color: var(--c-navy);
}

.ml {
  margin-left: 8px;
}

.positive {
  color: var(--c-green);
  font-weight: 700;
}

.negative {
  color: #dc2626;
  font-weight: 700;
}

.cost-hint {
  margin: 8px 0 0;
  font-size: 11.5px;
  color: var(--c-muted);
}
</style>
