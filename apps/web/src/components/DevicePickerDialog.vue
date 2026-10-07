<script setup lang="ts">
/**
 * 设备选择器。两种用途：
 *   - add     ：给报价单加一行明细，默认只列在售型号
 *   - replace ：给已停产的明细行换型号，高亮替代推荐
 */
import { ElMessage } from 'element-plus'
import { computed, nextTick, ref, watch } from 'vue'

import { deviceApi } from '@/api'
import type { Device, DeviceCategoryNode, DeviceStatus } from '@/api/types'
import { money } from '@/utils/format'
import { useMetaStore } from '@/stores/meta'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    mode?: 'add' | 'replace'
    /** replace 模式下被替换的设备 id，用于排除它自己 */
    excludeId?: string | null
    /** 该项目已划分的子系统，供「加入哪个子系统」选择 */
    subsystems?: { id: string; name: string }[]
  }>(),
  { mode: 'add', excludeId: null, subsystems: () => [] },
)

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  pick: [payload: { devices: Device[]; subsystemId: string | null }]
}>()

const meta = useMetaStore()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const loading = ref(false)
const rows = ref<Device[]>([])
const total = ref(0)

const query = ref({
  keyword: '',
  category: '' as string,
  /** 分类树节点（设备的实际归属；比一级分类精确） */
  category_id: '' as string,
  status: 'on_sale' as string,
  page: 1,
  page_size: 20,
})

/** 分类树（与设备库页面同一份数据）。 */
const categoryTree = ref<DeviceCategoryNode[]>([])

async function loadCategoryTree(): Promise<void> {
  try {
    categoryTree.value = (await deviceApi.categoryTree()).nodes
  } catch {
    categoryTree.value = []
  }
}

const selectedIds = ref<string[]>([])
/** 选中行的**对象**，不只是 id。
 *  el-table 加了 row-key + reserve-selection 之后，selection-change 给的是
 *  「跨页累计的全部选中行」；而 rows 只有当前页。以前用 rows.filter(id 命中)
 *  反推选中项，翻到第 2 页再选就会把第 1 页的选中丢掉，
 *  底部「已选 N 项」和实际写入的行数还会不一致。 */
const selectedRows = ref<Device[]>([])
const targetSubsystem = ref<string | null>(null)

/** 用来在重开弹窗时清掉 el-table 自己保留的勾选（reserve-selection）。 */
const tableRef = ref<{ clearSelection: () => void } | null>(null)

const isReplace = computed(() => props.mode === 'replace')

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await deviceApi.list({
      keyword: query.value.keyword || undefined,
      category: query.value.category || undefined,
      category_id: query.value.category_id || undefined,
      status: query.value.status || undefined,
      page: query.value.page,
      page_size: query.value.page_size,
    })
    rows.value = isReplace.value && props.excludeId
      ? page.items.filter((d) => d.id !== props.excludeId)
      : page.items
    total.value = page.total
  } finally {
    loading.value = false
  }
}

watch(visible, async (open) => {
  if (open) {
    selectedIds.value = []
    selectedRows.value = []
    targetSubsystem.value = props.subsystems[0]?.id ?? null
    query.value.page = 1
    // reserve-selection 让 el-table 自己记着上一次的勾选，
    // 重开弹窗要显式清掉，否则上次选的型号会带着勾回来。
    await nextTick()
    tableRef.value?.clearSelection()
    void loadCategoryTree()
    void load()
  }
})

function resetAndLoad(): void {
  query.value.page = 1
  void load()
}

function onSelectionChange(selection: Device[]): void {
  selectedIds.value = selection.map((d) => d.id)
  selectedRows.value = selection
}

/**
 * add 模式可多选批量加入；replace 模式只允许单选。
 *
 * 只 emit 一次（带整批设备），不要 for 循环逐个 emit：
 * 父组件每个 pick 都要 addItem + reload，N 个并发请求互相覆盖，
 * 最后界面显示的可能是某个中间状态。
 */
function confirm(): void {
  const picked = selectedRows.value.length
    ? selectedRows.value
    : rows.value.filter((d) => selectedIds.value.includes(d.id))
  if (!picked.length) {
    ElMessage.warning('请先勾选设备')
    return
  }
  emit('pick', {
    devices: isReplace.value ? [picked[0]] : picked,
    subsystemId: targetSubsystem.value,
  })
  visible.value = false
}

function isDiscontinued(status: DeviceStatus): boolean {
  return meta.isDiscontinued(status)
}

/** replace 模式下不允许再挑一个停产型号（后端会直接 400）。 */
function selectable(row: Device): boolean {
  if (!isReplace.value) return true
  return !isDiscontinued(row.status)
}

function rowClassName({ row }: { row: Device }): string {
  return isDiscontinued(row.status) ? 'row-discontinued' : ''
}
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="isReplace ? '选择替代型号' : '从设备库添加设备'"
    width="1000px"
    top="6vh"
    :close-on-click-modal="false"
  >
    <el-alert
      v-if="isReplace"
      type="warning"
      :closable="false"
      show-icon
      class="mb"
      title="优先选择在售型号，并确认与现场协议/接口兼容后再替换。"
    />
    <el-alert
      v-else
      type="info"
      :closable="false"
      show-icon
      class="mb"
      title="默认只列在售型号；如需选用临近停产型号，请把「状态」切到「临近停产」。"
    />

    <el-form inline class="inline-form" @submit.prevent>
      <el-form-item label="关键字">
        <el-input
          v-model="query.keyword"
          placeholder="名称 / 型号 / 品牌 / 编号"
          clearable
          style="width: 220px"
          @keyup.enter="resetAndLoad"
          @clear="resetAndLoad"
        />
      </el-form-item>
      <el-form-item label="分类">
        <!-- 与设备库同一棵分类树：按节点筛选会带上它的整棵子树 -->
        <el-tree-select
          v-model="query.category_id"
          :data="categoryTree"
          node-key="id"
          :props="{ label: 'name', children: 'children' }"
          value-key="id"
          clearable
          check-strictly
          default-expand-all
          placeholder="全部"
          style="width: 200px"
          @change="resetAndLoad"
        />
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="query.status" style="width: 130px" @change="resetAndLoad">
          <el-option
            v-for="o in meta.options('device_status')"
            :key="o.value"
            :label="o.label"
            :value="o.value"
          />
          <el-option label="全部" value="" />
        </el-select>
      </el-form-item>
      <el-form-item v-if="subsystems.length" label="加入子系统">
        <el-select v-model="targetSubsystem" clearable placeholder="不归属" style="width: 160px">
          <el-option v-for="s in subsystems" :key="s.id" :label="s.name" :value="s.id" />
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
      ref="tableRef"
      :data="rows"
      border
      stripe
      height="380"
      row-key="id"
      :row-class-name="rowClassName"
      @selection-change="onSelectionChange"
    >
      <!-- reserve-selection + row-key：翻页后仍保留已勾选的行，
           配合 selectedRows 让「已选 N 项」和实际加入的行数一致。
           replace 模式是单选，不需要跨页保留。 -->
      <el-table-column
        type="selection"
        width="46"
        :selectable="selectable"
        :reserve-selection="!isReplace"
      />
      <el-table-column prop="code" label="编号" width="90" />
      <el-table-column label="名称 / 型号" min-width="230">
        <template #default="{ row }">
          <!-- 悬停显示设备描述/性能参数（2026-10-05 用户要求）：
               选型时最想知道的正是"这台什么规格"，不该逼人点开详情 -->
          <el-tooltip
            :content="row.description || '（这个设备还没填描述）'"
            placement="right"
            :show-after="250"
          >
            <div class="pick-name">
              <div>{{ row.name }}</div>
              <div class="text-muted mono">
                {{ row.brand || '—' }} {{ row.model || '' }}
              </div>
            </div>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="分类" min-width="150">
        <template #default="{ row }">
          {{ row.category_path || meta.label('device_category', row.category) }}
        </template>
      </el-table-column>
      <el-table-column label="状态" width="96">
        <template #default="{ row }">
          <el-tag size="small" :type="meta.deviceStatusTag(row.status)" effect="plain">
            {{ meta.label('device_status', row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="单位" width="64" prop="unit" />
      <el-table-column label="参考单价" width="110" align="right">
        <template #default="{ row }">
          <span class="num">{{ money(row.reference_price) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="安装单价" width="110" align="right">
        <template #default="{ row }">
          <span class="num">{{ money(row.install_unit_price) }}</span>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="没有匹配的设备" />
      </template>
    </el-table>

    <el-pagination
      v-model:current-page="query.page"
      v-model:page-size="query.page_size"
      :total="total"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      class="pager"
      @current-change="load"
      @size-change="resetAndLoad"
    />

    <template #footer>
      <span class="picked">已选 {{ selectedIds.length }} 项</span>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" @click="confirm">
        {{ isReplace ? '确认替换' : '加入报价单' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* 悬浮显示描述的触发区（2026-10-05）：整块可悬停，不用精确对准文字 */
.pick-name {
  cursor: help;
}

.mb {
  margin-bottom: 12px;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
}

.pager {
  margin-top: 12px;
  justify-content: flex-end;
}

.picked {
  float: left;
  font-size: 13px;
  color: var(--c-muted);
  line-height: 32px;
}
</style>
