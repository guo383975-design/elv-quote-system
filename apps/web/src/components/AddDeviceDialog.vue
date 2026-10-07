<script setup lang="ts">
/**
 * 添加设备 / 材料（原型第 2 页）—— 整页弹窗，2026-10-03。
 *
 * 版式依据：原型第 2 页（`原型规格_坐标级.md` / `原型页面/proto-2.png`）：
 *   ① 顶部：← 添加设备 / 材料 ｜ 当前子系统：xxx ｜ 设备库：企业库 N 条 · 最近询价 <日期>
 *   ② 页签：从设备库选择 / 按点位模板批量生成 / 手动录入
 *   ③ 筛选行：分类 / 全部品牌 / 状态 / 搜索 / 只看常用设备 / 按最近询价排序 ｜ 匹配 N 条
 *   ④ 左侧：分类计数列表（全部设备 + 各分类数量）   ⑤ 中间：设备表（可勾选）
 *   ⑥ 右侧：设备详情（参考价 / 最近询价 / 成本价 / 安装单价 / 起订量 / 质保 / 状态）
 *   ⑦ 底部：+ 手动新增设备 / 材料 ｜ 已选 N 项 ｜ 取消 / 确定加入
 *
 * 未实现的两项（原型有、后端没有）一律**置灰 + 说明**，不做假控件：
 *   · 「按点位模板批量生成」页签：点位模板是原型里的新功能，后端没有模板表
 *   · 详情里的「点位模板构成」「供应商报价对比」：没有模板表 / 没有多次询价记录
 *
 * 与旧的 `DevicePickerDialog` 的分工：那个继续服务旧编辑器（回退用），
 * 这个服务新的报价工作台，多了第三个页签（手动录入）与详情面板。
 */
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'

import { deviceApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { Device, DeviceCategoryNode, DeviceStats } from '@/api/types'
import { dateOnly, money, toNum } from '@/utils/format'
import { useMetaStore } from '@/stores/meta'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    /** 项目已划分的子系统，供「加入哪个子系统」选择 */
    subsystems?: { id: string; name: string }[]
    /** 打开时默认选中的子系统（工作台当前筛选的那个） */
    defaultSubsystemId?: string | null
  }>(),
  { subsystems: () => [], defaultSubsystemId: null },
)

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  pick: [payload: { devices: Device[]; subsystemId: string | null }]
  manual: [payload: Record<string, unknown>]
  /** 第 2 个页签：让父组件打开「套用点位模板」生成器（同一套 UI，不重复实现） */
  'open-template': []
}>()

const meta = useMetaStore()

// 弹窗尺寸（2026-10-05 用户要求"做成 1920×1080"）：
// 表格高度按视口算 —— 1080 屏上约 760px（原来是写死的 380 ✗），
// 1366×768 的笔记本上自动缩小，不会溢出 ✓。
const viewportH = ref(typeof window === 'undefined' ? 1080 : window.innerHeight)
const tableHeight = computed(() => Math.max(360, viewportH.value - 320))

function onViewportResize(): void {
  viewportH.value = window.innerHeight
}
onMounted(() => window.addEventListener('resize', onViewportResize))
onBeforeUnmount(() => window.removeEventListener('resize', onViewportResize))

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const tab = ref<'library' | 'manual'>('library')
const loading = ref(false)
const rows = ref<Device[]>([])
const total = ref(0)
const tree = ref<DeviceCategoryNode[]>([])
const stats = ref<DeviceStats | null>(null)
const selected = ref<Device[]>([])
const detail = ref<Device | null>(null)
const subsystemId = ref<string | null>(null)

const query = reactive({
  keyword: '',
  category_id: '' as string,
  brand: '' as string,
  status: 'on_sale' as string,
  only_common: false,
  sort_by: 'code' as 'code' | 'inquiry',
  page: 1,
  page_size: 20,
})

/** 手动录入表单（原型第 2 页第三个页签）。 */
const manual = reactive({
  name: '',
  brand: '',
  model: '',
  unit: '台',
  quantity: 1,
  unit_price: 0,
  install_unit_price: 0,
  notes: '',
})

const latestInquiry = computed(() => {
  const dates = rows.value.map((r) => r.last_inquiry_at).filter(Boolean) as string[]
  if (!dates.length) return ''
  return dates.sort().at(-1) ?? ''
})

const latestInquiryLabel = computed(() =>
  latestInquiry.value ? dateOnly(latestInquiry.value) : '—',
)

/** 左栏：一级分类 + 含子树的设备数（原型是一层扁平的"分类 + 计数"）。 */
interface CatRow {
  id: string
  name: string
  count: number
  depth: number
}

/** 递归摊平整棵分类树（2026-10-05）：以前只平铺顶层 ✗ ——
 *  「网络设备 → 锐捷网络设备 → 易交换机」这样的层级根本点不到 ✓ */
function flattenCats(
  nodes: Array<{ id: string; name: string; device_count?: number; total_count?: number;
                 children?: unknown[] }>,
  depth = 0,
  out: CatRow[] = [],
): CatRow[] {
  for (const n of nodes) {
    out.push({ id: n.id, name: n.name, count: Number(n.total_count ?? n.device_count ?? 0), depth })
    const kids = (n.children ?? []) as typeof nodes
    if (kids.length) flattenCats(kids, depth + 1, out)
  }
  return out
}

const categoryRows = computed<CatRow[]>(() => flattenCats(tree.value as never))

const brandOptions = computed(() => {
  const set = new Set(rows.value.map((r) => r.brand).filter(Boolean) as string[])
  if (query.brand) set.add(query.brand)
  return [...set].sort()
})

/** 「按最近询价排序」只在当前页内排序（分页在后端），按钮上如实标注。 */
const tableRows = computed(() => {
  if (query.sort_by !== 'inquiry') return rows.value
  return [...rows.value].sort((a, b) => {
    const av = a.last_inquiry_at ?? ''
    const bv = b.last_inquiry_at ?? ''
    return bv.localeCompare(av)
  })
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await deviceApi.list({
      keyword: query.keyword || undefined,
      category_id: query.category_id || undefined,
      brand: query.brand || undefined,
      status: query.status || undefined,
      only_common: query.only_common || undefined,
      page: query.page,
      page_size: query.page_size,
    })
    rows.value = page.items
    total.value = page.total
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

async function loadAux(): Promise<void> {
  const [t, s] = await Promise.all([
    deviceApi.categoryTree().catch(() => null),
    deviceApi.stats().catch(() => null),
  ])
  tree.value = t?.nodes ?? []
  stats.value = s
}

function reload(): void {
  query.page = 1
  void load()
}

function selectCategory(id: string): void {
  query.category_id = query.category_id === id ? '' : id
  reload()
}

function openManual(): void {
  tab.value = 'manual'
}

function submitManual(): void {
  if (!manual.name.trim()) {
    ElMessage.warning('请填设备 / 材料名称')
    return
  }
  emit('manual', {
    name: manual.name.trim(),
    brand: manual.brand.trim() || null,
    model: manual.model.trim() || null,
    unit: manual.unit || '台',
    quantity: manual.quantity,
    unit_price: manual.unit_price,
    install_unit_price: manual.install_unit_price,
    source: 'manual',
    notes: manual.notes.trim() || null,
    subsystem_id: subsystemId.value,
  })
  Object.assign(manual, {
    name: '', brand: '', model: '', unit: '台',
    quantity: 1, unit_price: 0, install_unit_price: 0, notes: '',
  })
  visible.value = false
}

function confirmPick(): void {
  if (!selected.value.length) {
    ElMessage.warning('还没勾选设备')
    return
  }
  emit('pick', { devices: selected.value, subsystemId: subsystemId.value })
  selected.value = []
  visible.value = false
}

function onSelectionChange(list: Device[]): void {
  selected.value = list
}

function onRowClick(row: Device): void {
  detail.value = row
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    tab.value = 'library'
    subsystemId.value = props.defaultSubsystemId
    detail.value = null
    selected.value = []
    query.page = 1
    void load()
    void loadAux()
  },
)

onMounted(() => {
  void meta.load()
})
</script>

<template>
  <el-dialog
    v-model="visible"
    width="96vw"
    top="2vh"
    :close-on-click-modal="false"
    class="add-device-dialog"
  >
    <!-- ① 顶部：标题 + 当前子系统 + 设备库统计 -->
    <template #header>
      <div class="ad-head">
        <span class="ad-title">添加设备 / 材料</span>
        <span class="ad-cur">
          当前子系统：
          <el-select v-model="subsystemId" size="small" style="width: 190px" placeholder="全部（不归子系统）">
            <el-option :label="'不归子系统'" :value="null" />
            <el-option
              v-for="s in subsystems"
              :key="s.id"
              :label="s.name"
              :value="s.id"
            />
          </el-select>
        </span>
        <div class="spacer" />
        <span class="text-muted ad-lib">
          设备库：企业库 {{ stats?.total ?? '—' }} 条 · 最近询价 {{ latestInquiryLabel }}
        </span>
      </div>
    </template>

    <!-- ② 页签 -->
    <el-tabs v-model="tab">
      <el-tab-pane label="从设备库选择" name="library">
        <!-- ③ 筛选行 -->
        <div class="ad-filter">
          <el-input
            v-model="query.keyword"
            size="small"
            placeholder="名称 / 型号 / 品牌 / 编号"
            clearable
            style="width: 220px"
            @keyup.enter="reload"
            @clear="reload"
          />
          <el-select v-model="query.brand" size="small" placeholder="全部品牌" clearable style="width: 130px" @change="reload">
            <el-option v-for="b in brandOptions" :key="b" :label="b" :value="b" />
          </el-select>
          <el-select v-model="query.status" size="small" placeholder="状态" clearable style="width: 120px" @change="reload">
            <el-option v-for="o in meta.options('device_status')" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-checkbox v-model="query.only_common" size="small" @change="reload">只看常用设备</el-checkbox>
          <el-checkbox
            :model-value="query.sort_by === 'inquiry'"
            size="small"
            @change="(v: boolean) => { query.sort_by = v ? 'inquiry' : 'code' }"
          >
            按最近询价排序（当前页）
          </el-checkbox>
          <div class="spacer" />
          <span class="text-muted">匹配 {{ total }} 条</span>
        </div>

        <!-- ④ 左分类 + ⑤ 表格 + ⑥ 详情 -->
        <div class="ad-body">
          <aside class="ad-cats">
            <div
              class="ad-cat"
              :class="{ active: !query.category_id }"
              @click="query.category_id = ''; reload()"
            >
              <span>全部设备</span>
              <span class="num">{{ stats?.total ?? '—' }}</span>
            </div>
            <div
              v-for="c in categoryRows"
              :key="c.id"
              class="ad-cat"
              :class="{ active: query.category_id === c.id }"
              @click="selectCategory(c.id)"
            >
              <span :style="{ paddingLeft: c.depth * 12 + 'px' }">
                {{ c.depth ? '· ' : '' }}{{ c.name }}
              </span>
              <span class="num">{{ c.count }}</span>
            </div>
            <div
              v-if="!categoryRows.length"
              class="text-muted"
              style="padding: 6px 8px; font-size: 12px"
            >
              分类没加载出来（可能是权限或网络）✓ 点「全部设备」可先列表浏览 ✓
            </div>
          </aside>

          <div class="ad-table">
            <el-table
              v-loading="loading"
              :data="tableRows"
              border
              stripe
              size="small"
              :height="tableHeight"
              row-key="id"
              highlight-current-row
              @selection-change="onSelectionChange"
              @row-click="onRowClick"
            >
              <el-table-column type="selection" width="38" />
              <el-table-column label="设备 / 材料名称" min-width="170">
                <template #default="{ row }">
                  <div>{{ row.name }}</div>
                  <div class="text-muted ad-sub">{{ row.category_path || meta.label('device_category', row.category) }}</div>
                </template>
              </el-table-column>
              <el-table-column label="品牌" width="100">
                <template #default="{ row }">{{ row.brand || '—' }}</template>
              </el-table-column>
              <el-table-column label="型号规格" min-width="150">
                <template #default="{ row }">{{ row.model || '—' }}</template>
              </el-table-column>
              <el-table-column label="单位" width="56" prop="unit" />
              <el-table-column label="参考价" width="96" align="right">
                <template #default="{ row }"><span class="num">{{ money(row.reference_price) }}</span></template>
              </el-table-column>
              <el-table-column label="安装单价" width="90" align="right">
                <template #default="{ row }"><span class="num">{{ money(row.install_unit_price) }}</span></template>
              </el-table-column>
              <el-table-column label="最近询价" width="106">
                <template #default="{ row }">
                  <span v-if="row.last_inquiry_at">{{ dateOnly(row.last_inquiry_at) }}</span>
                  <span v-else class="text-muted">—</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="86">
                <template #default="{ row }">
                  <el-tag size="small" :type="meta.deviceStatusTag(row.status)" effect="plain">
                    {{ meta.label('device_status', row.status) }}
                  </el-tag>
                </template>
              </el-table-column>
            </el-table>

            <el-pagination
              v-model:current-page="query.page"
              :page-size="query.page_size"
              :total="total"
              layout="prev, pager, next, total"
              small
              class="ad-page"
              @current-change="load"
            />
          </div>

          <aside class="ad-detail">
            <div class="ad-detail-head">设备详情</div>
            <template v-if="detail">
              <div class="ad-detail-name">{{ detail.name }}</div>
              <div class="ad-row"><span>品牌 · 型号</span><span>{{ detail.brand || '—' }} · {{ detail.model || '—' }}</span></div>
              <div class="ad-row"><span>规格说明</span><span>{{ detail.spec || '—' }}</span></div>
              <div class="ad-row"><span>参考价</span><span class="num strong">{{ money(detail.reference_price) }} 元/{{ detail.unit }}</span></div>
              <div class="ad-row">
                <span>最近询价</span>
                <span>
                  <template v-if="detail.last_inquiry_at">
                    {{ dateOnly(detail.last_inquiry_at) }}
                    <template v-if="detail.last_inquiry_price"> · {{ money(detail.last_inquiry_price) }}</template>
                  </template>
                  <span v-else class="text-muted">未记录</span>
                </span>
              </div>
              <div class="ad-row"><span>成本价</span><span class="num num-muted">{{ money(detail.cost_price) }}</span></div>
              <div class="ad-row"><span>安装单价</span><span class="num">{{ money(detail.install_unit_price) }}</span></div>
              <div class="ad-row"><span>起订量 / 质保</span><span>{{ detail.moq }} / {{ detail.warranty_years }} 年</span></div>
              <div class="ad-row"><span>甲供</span><span>{{ detail.is_owner_supplied ? '是（只计安装费）' : '否' }}</span></div>
              <div class="ad-row">
                <span>状态</span>
                <el-tag size="small" :type="meta.deviceStatusTag(detail.status)" effect="plain">
                  {{ meta.label('device_status', detail.status) }}
                </el-tag>
              </div>

              <div class="ad-disabled" title="原型已设计：点位模板是新功能，后端没有模板表">
                <div class="ad-disabled-title">球机点位模板（自动带出安装单价）</div>
                <div class="text-muted">— 未实现，仅原型设计 —</div>
              </div>
              <div class="ad-disabled" title="原型已设计：需要多次询价记录，后端只存最后一次询价">
                <div class="ad-disabled-title">供应商报价对比</div>
                <div class="text-muted">— 未实现，仅原型设计 —</div>
              </div>
            </template>
            <el-empty v-else description="点表格行看详情" :image-size="56" />
          </aside>
        </div>
      </el-tab-pane>

      <el-tab-pane label="按点位模板批量生成" name="points">
        <el-alert
          type="info"
          show-icon
          :closable="false"
          class="mb"
          title="按「点位模板」一次生成整套设备行与安装费"
        >
          <template #default>
            一个点位常由多台设备构成（如 球机 + 支架 + 电源）。这里填点位数，系统按模板
            生成对应行，并把该点位的安装单价计在主设备行上。
            <el-button link type="primary" @click="emit('open-template')">
              打开点位模板生成器
            </el-button>
          </template>
        </el-alert>
      </el-tab-pane>

      <el-tab-pane label="手动录入" name="manual">
        <el-alert
          type="info"
          show-icon
          :closable="false"
          class="mb"
          title="库里没有的设备 / 材料在这里直接录入：单价不随询价更新，只作用于本报价单"
        />
        <el-form label-width="110px" class="ad-form">
          <el-row :gutter="16">
            <el-col :span="12">
              <el-form-item label="名称" required>
                <el-input v-model="manual.name" placeholder="如：网络高清枪型摄像机" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="品牌">
                <el-input v-model="manual.brand" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="单位">
                <el-input v-model="manual.unit" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="型号规格">
                <el-input v-model="manual.model" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="数量">
                <el-input-number v-model="manual.quantity" :min="0" :precision="2" style="width: 100%" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="设备单价">
                <el-input-number v-model="manual.unit_price" :min="0" :precision="2" :controls="false" style="width: 100%" />
              </el-form-item>
            </el-col>
            <el-col :span="6">
              <el-form-item label="安装单价">
                <el-input-number v-model="manual.install_unit_price" :min="0" :precision="2" :controls="false" style="width: 100%" />
              </el-form-item>
            </el-col>
            <el-col :span="18">
              <el-form-item label="备注">
                <el-input v-model="manual.notes" placeholder="选填，例如甲方指定型号 / 含税说明" />
              </el-form-item>
            </el-col>
          </el-row>
          <div class="text-muted ad-manual-hint">
            合计预览：设备 {{ money(toNum(manual.quantity) * toNum(manual.unit_price)) }} +
            安装 {{ money(toNum(manual.quantity) * toNum(manual.install_unit_price)) }}
          </div>
        </el-form>
      </el-tab-pane>
    </el-tabs>

    <!-- ⑦ 底部 -->
    <template #footer>
      <div class="ad-foot">
        <el-button link @click="openManual">+ 手动新增设备 / 材料</el-button>
        <div class="spacer" />
        <span v-if="tab === 'library'" class="text-muted">已选 {{ selected.length }} 项</span>
        <el-button @click="visible = false">取消</el-button>
        <el-button
          v-if="tab === 'library'"
          type="primary"
          :disabled="!selected.length"
          @click="confirmPick"
        >
          确定加入（{{ selected.length }}）
        </el-button>
        <el-button v-else type="primary" @click="submitManual">加入报价单</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.ad-head {
  display: flex;
  align-items: center;
  gap: 14px;
}

.ad-title {
  font-size: 15.5px;
  font-weight: 700;
}

.ad-cur {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.ad-lib {
  font-size: 12.5px;
}

.ad-filter {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.ad-body {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.ad-cats {
  width: 168px;
  flex: 0 0 168px;
  /* 随视口走（原来是写死的 420px ✗）：大屏能给到 58vh ≈ 626px */
  max-height: 58vh;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 4px;
}

.ad-cat {
  display: flex;
  justify-content: space-between;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 4px;
  font-size: 12.5px;
  cursor: pointer;
}

.ad-cat:hover {
  background: var(--el-fill-color-light);
}

.ad-cat.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}

.ad-table {
  flex: 1 1 auto;
  min-width: 0;
}

.ad-page {
  margin-top: 8px;
}

.ad-detail {
  width: 262px;
  flex: 0 0 262px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
  max-height: 62vh;   /* 原来是写死的 456px ✗ */
  overflow: auto;
}

.ad-detail-head {
  font-weight: 600;
  font-size: 13px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 6px;
}

.ad-detail-name {
  font-size: 13.5px;
  font-weight: 600;
  margin-bottom: 4px;
}

.ad-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  padding: 2px 0;
}

.ad-row > span:first-child {
  color: var(--el-text-color-secondary);
  flex: 0 0 auto;
}

.ad-row > span:last-child {
  text-align: right;
  word-break: break-all;
}

.ad-disabled {
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px dashed var(--el-border-color-lighter);
  opacity: 0.72;
  font-size: 12px;
}

.ad-disabled-title {
  font-weight: 600;
}

.ad-sub {
  font-size: 11.5px;
}

.ad-form {
  margin-top: 6px;
}

.ad-manual-hint {
  font-size: 12.5px;
  padding-left: 110px;
}

.ad-foot {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 弹窗体：内容多时自己滚，别把底部按钮顶出屏幕（2026-10-05 放大后需要） */
.add-device-dialog :deep(.el-dialog__body) {
  max-height: calc(100vh - 170px);
  overflow: auto;
}
</style>

