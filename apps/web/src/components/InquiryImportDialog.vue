<script setup lang="ts">
/**
 * 场景二：甲方询价清单智能匹配导入。
 *
 * 流程：上传 → 逐行匹配预览 → 人工确认/改判 → 写入报价单。
 *
 * 关键设计：**不自动写入**。模糊匹配永远可能错（「400万筒机」到底对哪一款），
 * 所以每一行都允许人工改判，或者显式标成「不匹配（待询价）」。
 * 提交时后端对停产型号会自动换成替代型号，并在结果里回报 auto_replaced 数量。
 */
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { importApi } from '@/api'
import type {
  InquiryCommitLine,
  InquiryPreview,
  InquiryPreviewRow,
  MatchCandidate,
} from '@/api/types'
import { money, toNum } from '@/utils/format'
import { useMetaStore } from '@/stores/meta'

const props = defineProps<{
  modelValue: boolean
  quoteId: string
  subsystems: { id: string; name: string }[]
}>()

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  done: []
}>()

const meta = useMetaStore()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const step = ref<'upload' | 'preview' | 'done'>('upload')
const busy = ref(false)
const file = ref<File | null>(null)
const threshold = ref(75)
const preview = ref<InquiryPreview | null>(null)
const targetSubsystem = ref<string | null>(null)
const result = ref<Awaited<ReturnType<typeof importApi.commitInquiry>> | null>(null)

/** 每行的改判结果：key = row 号，value = 选中的 device_id 或 ''（=待询价）。 */
const overrides = ref<Record<number, string>>({})

const uploadRef = ref<{ clearFiles: () => void } | null>(null)

function reset(): void {
  step.value = 'upload'
  busy.value = false
  file.value = null
  preview.value = null
  result.value = null
  overrides.value = {}
  targetSubsystem.value = props.subsystems[0]?.id ?? null
}

// 每次打开都回到初始态，避免上一批的改判串到这一批。
// 必须 watch 而不是监听 el-dialog 的 update:modelValue：
// 「打开」是父组件驱动的，父改 prop 不会触发 dialog 自己的事件，
// 那样只有「关闭」时会重置，重开时反而留着上一批的数据。
watch(
  () => props.modelValue,
  (open) => {
    if (open) reset()
  },
)

function onFileChange(f: { raw?: File }): void {
  file.value = f.raw ?? null
  preview.value = null
  step.value = 'upload'
}

/** :limit="1" 的限制下，弹窗不关时再选文件会走 on-exceed。
 *  默认实现是把新文件丢掉 —— 用户会看到「界面还是旧文件名，点预览说没选文件」。
 *  这里改成用新文件替换。 */
function onFileExceed(files: File[]): void {
  const next = files[0]
  if (!next) return
  onFileChange({ raw: next })
  uploadRef.value?.clearFiles()
}

async function doPreview(): Promise<void> {
  if (!file.value) {
    ElMessage.warning('请先选择甲方清单文件')
    return
  }
  busy.value = true
  try {
    // 不传 limit：上限由后端决定（见 importApi.previewInquiry 的说明）。
    preview.value = await importApi.previewInquiry(file.value, threshold.value)
    overrides.value = {}
    step.value = 'preview'
    if (preview.value?.truncated) {
      ElMessage.error(
        `清单共 ${preview.value.total_rows} 行，本次只匹配了前 ${preview.value.previewed_rows} 行；` +
          '为避免漏项，已禁止写入 —— 请把文件拆成更小的分次导入。',
      )
    }
  } finally {
    busy.value = false
  }
}

/** 该行最终采用的设备 id：优先人工改判，否则用自动匹配的最佳结果。 */
function effectiveDeviceId(row: InquiryPreviewRow): string {
  if (row.row in overrides.value) return overrides.value[row.row]
  return row.match_type === 'none' ? '' : (row.best?.device_id ?? '')
}

/** 该行可选的设备：候选列表 + 空选项。 */
function optionsFor(row: InquiryPreviewRow): MatchCandidate[] {
  const list = [...(row.candidates ?? [])]
  if (row.best && !list.some((c) => c.device_id === row.best?.device_id)) {
    list.unshift(row.best)
  }
  return list
}

function matchTagType(t: string): 'success' | 'warning' | 'danger' {
  if (t === 'exact') return 'success'
  if (t === 'fuzzy') return 'warning'
  return 'danger'
}

function matchLabel(t: string): string {
  const m: Record<string, string> = { exact: '精确', fuzzy: '模糊', none: '无匹配' }
  return m[t] ?? t
}

/** 最终会写入的行：至少要有名字。 */
const willInsert = computed(() => preview.value?.results ?? [])

/** 待询价行数（没有选中任何设备）。 */
const pendingCount = computed(
  () => willInsert.value.filter((r) => !effectiveDeviceId(r)).length,
)

/** 自动匹配到停产型号的行数——提交时后端会自动换型。 */
const discontinuedCount = computed(
  () =>
    willInsert.value.filter((r) => {
      const id = effectiveDeviceId(r)
      if (!id) return false
      const all = optionsFor(r).find((c) => c.device_id === id)
      return all ? meta.isDiscontinued(all.status) : false
    }).length,
)

async function doCommit(): Promise<void> {
  // 代码里再挡一道：`:disabled` 只是界面约束，不能当作唯一防线。
  if (preview.value?.truncated) {
    ElMessage.error(
      `清单共 ${preview.value.total_rows} 行，只匹配了前 ${preview.value.previewed_rows} 行，` +
        '不允许写入（会漏项）。请拆分文件后重新匹配。',
    )
    return
  }
  const rows = willInsert.value
  if (!rows.length) {
    ElMessage.warning('没有可写入的行')
    return
  }

  const lines: InquiryCommitLine[] = rows.map((r) => {
    const deviceId = effectiveDeviceId(r)
    return {
      row: r.row,
      device_id: deviceId || null,
      name: r.query.name,
      model: r.query.model,
      brand: r.query.brand,
      // quantity 在顶层：match_device 的 query 只是查询条件的回显，不含数量。
      // 以前读 r.query.quantity 永远是 undefined，于是所有行都被写成 ×1。
      quantity: r.quantity || 1,
      subsystem_id: targetSubsystem.value,
    }
  })

  busy.value = true
  try {
    result.value = await importApi.commitInquiry(props.quoteId, lines)
    step.value = 'done'
    emit('done')
    ElMessage.success(`已写入 ${result.value.added} 行（待询价 ${result.value.pending_inquiry} 行）`)
  } finally {
    busy.value = false
  }
}

// statLabel 已随原型第 7 页的「统计条」改版删除：统计项现在直接写在模板里，
// 名称与后端 stat 的 key（exact/fuzzy/none/discontinued）一一对应。

/** 人工改判某一行。空值 = 待询价。 */
/** 原型第 7 页是 4 步：上传甲方清单 → 智能匹配 → 人工确认 → 加入报价单。 */
const reviewPhase = ref<'matching' | 'confirm'>('matching')
/** 右侧「候选设备 · 第 N 行」当前选中的行。 */
const activeRow = ref<InquiryPreviewRow | null>(null)

const activeStep = computed(() => {
  if (step.value === 'upload') return 0
  if (step.value === 'done') return 3
  return reviewPhase.value === 'confirm' ? 2 : 1
})

/** 预计金额：按「将写入的每行数量 × 采用设备的参考价」估算；未匹配行按 0 计。 */
const estimatedAmount = computed(() => {
  let sum = 0
  for (const row of willInsert.value) {
    const id = effectiveDeviceId(row)
    const dev = optionsFor(row).find((c) => c.device_id === id)
    if (dev) sum += toNum(row.quantity) * toNum(dev.reference_price)
  }
  return sum
})

/** 原型「采用选中的候选」：把某行的候选设备设为采用型号。 */
function adoptCandidate(rowNo: number, deviceId: string): void {
  setOverride(rowNo, deviceId)
}

function setOverride(rowNo: number, deviceId: string | null): void {
  overrides.value = { ...overrides.value, [rowNo]: deviceId ?? '' }
}

/**
 * el-select 的 `@update:model-value` 收口。
 *
 * tsconfig 没有引入 `element-plus/global`，所以 el-select 在 vue-tsc 眼里是未解析
 * 组件，事件载荷是 any；模板里直接写 `(v) => ...` 会报 TS7006（隐式 any）。
 * 这里显式声明 unknown 再接住，顺手把 clearable 产生的 '' / undefined 归一成 null。
 */
function onOverrideChange(rowNo: number, value: unknown): void {
  const v = value === '' || value === undefined ? null : String(value)
  setOverride(rowNo, v)
}

function rowClassName({ row }: { row: InquiryPreviewRow }): string {
  return row.match_type === 'none' ? 'row-discontinued' : ''
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="导入甲方询价清单（智能匹配）"
    width="1120px"
    top="5vh"
    :close-on-click-modal="false"
    destroy-on-close
  >
    <template #header>
      <div class="iq-head">
        <span class="iq-title">批量导入 · 场景二：导入甲方询价清单</span>
        <span class="text-muted iq-sub">
          甲方清单通常只有名称和数量，系统自动去设备库做精确 / 模糊匹配
        </span>
        <div class="spacer" />
        <span class="text-muted">
          <template v-if="preview">
            来源文件：{{ preview.file_name }} · {{ preview.total_rows }} 行
          </template>
          <template v-else>尚未选择文件</template>
        </span>
      </div>
    </template>

    <el-steps :active="activeStep" simple>
      <el-step title="上传甲方清单" />
      <el-step title="智能匹配" />
      <el-step title="人工确认" />
      <el-step title="加入报价单" />
    </el-steps>

    <div class="body">
      <!-- 第 1 步：上传甲方清单 -->
      <template v-if="step === 'upload'">
        <el-alert
          type="info"
          :closable="false"
          show-icon
          class="mb"
          title="按「名称 + 型号 + 品牌」与设备库做精确/模糊匹配；匹配不上的行会以「待询价」写入，不会丢行。"
        >
          <template #default>
            置信度为「名称 40% + 型号 40% + 品牌 20%」；清单里没有型号时，名称权重自动升到 80%。
            低于阈值（默认 75 分）判为模糊匹配，需要人工确认。
          </template>
        </el-alert>

        <el-upload
          ref="uploadRef"
          drag
          :auto-upload="false"
          :limit="1"
          accept=".xlsx,.xls"
          :on-change="onFileChange"
          :on-exceed="onFileExceed"
        >
          <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
          <div class="el-upload__text">把甲方清单拖到这里，或<em>点击选择</em></div>
          <template #tip>
            <div v-if="file" class="el-upload__tip">已选择：{{ file.name }}</div>
          </template>
        </el-upload>

        <el-form inline class="mt">
          <el-form-item label="匹配阈值">
            <el-slider v-model="threshold" :min="30" :max="100" :step="5" style="width: 220px" />
            <span class="text-muted ml">{{ threshold }} 分</span>
          </el-form-item>
          <el-form-item label="整批归入子系统">
            <el-select v-model="targetSubsystem" clearable placeholder="不归属" style="width: 180px">
              <el-option v-for="s in subsystems" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
          </el-form-item>
        </el-form>
      </template>

      <!-- 第 2 / 3 步：智能匹配 + 人工确认 -->
      <template v-else-if="step === 'preview' && preview">
        <el-alert
          v-if="preview.truncated"
          class="mb"
          type="error"
          :closable="false"
          show-icon
          title="这份清单没有被完整匹配"
          :description="`文件共 ${preview.total_rows} 行，本次只处理了前 ${preview.previewed_rows} 行。直接写入会漏掉其余 ${preview.total_rows - preview.previewed_rows} 行，因此已禁用提交；请把文件拆成更小的分次导入。`"
        />

        <!-- 匹配统计条 + 匹配策略 + 相似度阈值（原型 y118-142） -->
        <div class="iq-stats">
          <div class="iq-stat">
            <span class="iq-stat-label">清单总行数</span>
            <b class="iq-stat-value">{{ preview.total_rows }}</b>
          </div>
          <div class="iq-stat">
            <span class="iq-stat-label">精确匹配</span>
            <b class="iq-stat-value ok-text">{{ preview.stat.exact ?? 0 }}</b>
          </div>
          <div class="iq-stat">
            <span class="iq-stat-label">模糊匹配</span>
            <b class="iq-stat-value warn-text">{{ preview.stat.fuzzy ?? 0 }}</b>
          </div>
          <div class="iq-stat">
            <span class="iq-stat-label">未匹配</span>
            <b class="iq-stat-value danger-text">{{ preview.stat.none ?? 0 }}</b>
          </div>
          <div class="iq-stat">
            <span class="iq-stat-label">已含停产</span>
            <b class="iq-stat-value danger-text">{{ preview.stat.discontinued ?? 0 }}</b>
          </div>
          <div class="iq-stat">
            <span class="iq-stat-label">预计金额</span>
            <b class="iq-stat-value num">{{ money(estimatedAmount) }}</b>
          </div>
          <div class="spacer" />

          <div class="iq-strategy">
            <span class="text-muted">匹配策略</span>
            <el-checkbox disabled>
              <span title="原型已设计：匹配器只有相似度阈值，没有这些策略开关">品牌优先</span>
            </el-checkbox>
            <el-checkbox disabled>
              <span title="原型已设计：匹配器只有相似度阈值，没有这些策略开关">仅在售型号</span>
            </el-checkbox>
            <el-checkbox disabled>
              <span title="原型已设计：写入时已固定为「停产自动换替代」，不可切换">停产自动换替代</span>
            </el-checkbox>
            <el-checkbox disabled>
              <span title="原型已设计：没有「排除甲供」筛选参数">排除甲供</span>
            </el-checkbox>
          </div>

          <div class="iq-threshold">
            <span class="text-muted">相似度阈值</span>
            <el-input-number
              v-model="threshold"
              :min="30"
              :max="100"
              :step="5"
              :controls="false"
              size="small"
              style="width: 64px"
            />
            <span class="text-muted">%</span>
            <el-button size="small" :loading="busy" @click="doPreview">重新匹配</el-button>
          </div>
        </div>

        <!-- 三栏：原文 / 匹配结果 / 候选设备 -->
        <div class="iq-split">
          <div class="iq-col">
            <div class="iq-col-head">
              甲方询价清单（原文）
              <span class="text-muted">不可编辑</span>
            </div>
            <el-table
              :data="willInsert"
              border
              size="small"
              height="288"
              highlight-current-row
              @row-click="(r: InquiryPreviewRow) => (activeRow = r)"
            >
              <el-table-column prop="row" label="行" width="46" />
              <el-table-column prop="raw_name" label="甲方设备名称" min-width="130" show-overflow-tooltip />
              <el-table-column label="单位" width="52">
                <template #default="{ row }">{{ row.unit ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="数量" width="60" align="right">
                <template #default="{ row }"><span class="num">{{ row.quantity }}</span></template>
              </el-table-column>
              <el-table-column label="品牌要求" width="86">
                <template #default="{ row }">{{ row.query.brand || '—' }}</template>
              </el-table-column>
            </el-table>
          </div>

          <div class="iq-col">
            <div class="iq-col-head">
              匹配结果
              <span class="text-muted">点行查看候选</span>
            </div>
            <el-table
              :data="willInsert"
              border
              size="small"
              height="288"
              highlight-current-row
              :row-class-name="rowClassName"
              @row-click="(r: InquiryPreviewRow) => (activeRow = r)"
            >
              <el-table-column prop="row" label="行" width="46" />
              <el-table-column label="匹配状态" width="80">
                <template #default="{ row }">
                  <el-tag size="small" :type="matchTagType(row.match_type)" effect="plain">
                    {{ matchLabel(row.match_type) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="得分" width="62" align="right">
                <template #default="{ row }">
                  <span class="num">{{ row.best ? row.best.score : '—' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="采用的设备（可改判）" min-width="260">
                <template #default="{ row }">
                  <el-select
                    :model-value="effectiveDeviceId(row)"
                    placeholder="待询价（不匹配）"
                    clearable
                    filterable
                    size="small"
                    style="width: 100%"
                    @update:model-value="(v: unknown) => onOverrideChange(row.row, v)"
                  >
                    <el-option label="待询价（不匹配）" value="" />
                    <el-option
                      v-for="c in optionsFor(row)"
                      :key="c.device_id"
                      :label="`${c.code} ${c.name} ${c.model || ''} · ${money(c.reference_price)}`"
                      :value="c.device_id"
                    >
                      <span class="opt">
                        <el-tag size="small" :type="meta.deviceStatusTag(c.status)" effect="plain">
                          {{ meta.label('device_status', c.status) }}
                        </el-tag>
                        <span class="mono">{{ c.code }}</span>
                        {{ c.name }}
                        <b class="num">{{ money(c.reference_price) }}</b>
                        <span class="text-muted">({{ c.score }}分)</span>
                      </span>
                    </el-option>
                  </el-select>
                </template>
              </el-table-column>
              <el-table-column label="提示" min-width="150">
                <template #default="{ row }">
                  <ul v-if="row.issues.length" class="issues">
                    <li v-for="(i, idx) in row.issues" :key="idx">{{ i }}</li>
                  </ul>
                  <span v-else class="text-muted">—</span>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <div class="iq-col iq-col-cand">
            <div class="iq-col-head">
              候选设备 · 第 {{ activeRow?.row ?? '—' }} 行
              <span class="text-muted">共 {{ activeRow ? optionsFor(activeRow).length : 0 }} 个候选</span>
            </div>
            <template v-if="activeRow">
              <div class="iq-cands">
                <div
                  v-for="c in optionsFor(activeRow)"
                  :key="c.device_id"
                  class="iq-cand"
                  :class="{ picked: effectiveDeviceId(activeRow) === c.device_id }"
                >
                  <div class="iq-cand-main">
                    <div class="iq-cand-name">{{ c.name }}</div>
                    <div class="text-muted mono">{{ c.code }} · {{ c.brand || '—' }} {{ c.model || '' }}</div>
                    <div class="text-muted">
                      参考价 {{ money(c.reference_price) }} · 安装 {{ money(c.install_unit_price) }}
                    </div>
                  </div>
                  <div class="iq-cand-right">
                    <div class="iq-score">{{ c.score }}%</div>
                    <el-button
                      size="small"
                      plain
                      :type="effectiveDeviceId(activeRow) === c.device_id ? 'success' : 'primary'"
                      @click="adoptCandidate(activeRow.row, c.device_id)"
                    >
                      {{ effectiveDeviceId(activeRow) === c.device_id ? '已采用' : '采用' }}
                    </el-button>
                  </div>
                </div>
              </div>
              <el-button link size="small" @click="setOverride(activeRow.row, '')">
                这一行标为「待询价」
              </el-button>
            </template>
            <el-empty v-else description="点左边表格的一行看候选" :image-size="50" />
          </div>
        </div>

        <div class="sum-line">
          本次将写入 {{ willInsert.length }} 行 · 待询价
          <b class="text-accent">{{ pendingCount }}</b> 行 · 命中停产
          <b class="danger-text">{{ discontinuedCount }}</b> 行（提交时自动换替代型号）
        </div>

        <el-form inline>
          <el-form-item label="整批归入子系统">
            <el-select v-model="targetSubsystem" clearable placeholder="不归属" style="width: 200px">
              <el-option v-for="s in subsystems" :key="s.id" :label="s.name" :value="s.id" />
            </el-select>
          </el-form-item>
        </el-form>
      </template>

      <el-result
        v-else-if="step === 'done' && result"
        icon="success"
        title="已写入报价单"
        :sub-title="`匹配成功 ${result.added} 行 · 待询价 ${result.pending_inquiry} 行 · 自动换型 ${result.auto_replaced} 行 · 含税合计 ${money(result.total_with_tax)}`"
      />
    </div>

    <template #footer>
      <el-button @click="visible = false">{{ step === 'done' ? '关闭' : '取消' }}</el-button>
      <template v-if="step === 'upload'">
        <el-button type="primary" :loading="busy" @click="doPreview">下一步：智能匹配</el-button>
      </template>
      <template v-else-if="step === 'preview'">
        <template v-if="reviewPhase === 'matching'">
          <el-button @click="step = 'upload'">上一步</el-button>
          <el-button type="primary" @click="reviewPhase = 'confirm'">
            下一步：人工确认（{{ pendingCount + (preview?.stat.fuzzy ?? 0) }} 行待看）
          </el-button>
        </template>
        <template v-else>
          <el-button @click="reviewPhase = 'matching'">上一步：修改匹配</el-button>
          <el-button
            type="primary"
            :loading="busy"
            :disabled="preview?.truncated === true"
            @click="doCommit"
          >
            加入报价单（{{ willInsert.length }} 行）
          </el-button>
        </template>
      </template>
      <el-button v-else type="primary" @click="visible = false">完成</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* ---------- 场景二导入向导（原型第 7 页） ---------- */
.iq-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.iq-title {
  font-size: 15.5px;
  font-weight: 700;
}

.iq-sub {
  font-size: 12.5px;
}

.iq-stats {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  padding: 8px 10px;
  margin: 10px 0;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
}

.iq-stat {
  display: flex;
  flex-direction: column;
  min-width: 66px;
}

.iq-stat-label {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.iq-stat-value {
  font-size: 15px;
}

.iq-strategy {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  opacity: 0.75;
}

.iq-threshold {
  display: flex;
  align-items: center;
  gap: 6px;
}

.iq-split {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.iq-col {
  flex: 1 1 0;
  min-width: 0;
}

.iq-col-cand {
  flex: 0 0 264px;
  width: 264px;
}

.iq-col-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}

.iq-col-head .text-muted {
  font-weight: 400;
  font-size: 11px;
}

.iq-cands {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 300px;
  overflow: auto;
}

.iq-cand {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.iq-cand.picked {
  border-color: var(--el-color-success-light-5);
  background: var(--el-color-success-light-9);
}

.iq-cand-main {
  min-width: 0;
  font-size: 12px;
}

.iq-cand-name {
  font-weight: 600;
  font-size: 12.5px;
}

.iq-cand-right {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex: 0 0 auto;
}

.iq-score {
  font-size: 13px;
  font-weight: 700;
  color: var(--el-color-primary);
}

.body {
  margin-top: 16px;
}

.mb {
  margin-bottom: 12px;
}

.mt {
  margin-top: 12px;
}

.ml {
  margin-left: 10px;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 11.5px;
}

.sum-line {
  margin: 12px 0;
  font-size: 13px;
  color: #374151;
}

.danger-text {
  color: #dc2626;
}

.opt {
  display: flex;
  align-items: center;
  gap: 8px;
}

.issues {
  margin: 0;
  padding-left: 16px;
  color: #b45309;
  font-size: 12px;
}
</style>



