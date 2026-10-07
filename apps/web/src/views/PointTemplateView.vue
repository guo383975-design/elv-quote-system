<script setup lang="ts">
/**
 * 点位模板管理（原型顶栏「点位模板」，2026-10-04）。
 *
 * 一个模板 = 一个「点位」的物料构成 + 该点位的安装单价（元/点）：
 *   球机点位模板｜安装单价 180 元/点
 *     · 网络高清球型摄像机 ×1  ← 主设备（安装单价只落在这一行）
 *     · 壁装支架           ×1
 *     · 电源适配器         ×1
 *
 * 「主设备」这个开关是必须的：一个点位常有三四个组件，若每行都收一次
 * "180 元/点"，安装费会翻几倍。没勾主设备时会退化为第一行，但页面会提示。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { deviceApi, pointTemplateApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { Device, PointTemplate, PointTemplateItem } from '@/api/types'
import { money, toNum } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const auth = useAuthStore()
const meta = useMetaStore()

const loading = ref(false)
const saving = ref(false)
const rows = ref<PointTemplate[]>([])
const total = ref(0)
const devices = ref<Device[]>([])

const query = reactive({
  keyword: '',
  subsystem_type: '',
  include_inactive: false,
  page: 1,
  page_size: 20,
})

const dialogVisible = ref(false)
const editing = ref<PointTemplate | null>(null)
const form = reactive({
  code: '',
  name: '',
  subsystem_type: '' as string,
  install_unit_price: 0,
  notes: '',
  is_active: true,
})
/** 明细行（前端编辑态）：设备 + 数量 + 是否主设备 */
const items = ref<{ device_id: string; quantity: number; is_primary: boolean }[]>([])

const canCreate = computed(() => auth.can('point_template', 'create'))
const canUpdate = computed(() => auth.can('point_template', 'update'))
const canDelete = computed(() => auth.can('point_template', 'delete'))

const deviceOptions = computed(() =>
  devices.value.map((d) => ({
    value: d.id,
    label: `${d.code} ${d.name}${d.model ? ` ${d.model}` : ''} · ${money(d.reference_price)}/${d.unit}`,
  })),
)

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await pointTemplateApi.list({
      keyword: query.keyword || undefined,
      subsystem_type: query.subsystem_type || undefined,
      include_inactive: query.include_inactive || undefined,
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

function reload(): void {
  query.page = 1
  void load()
}

onMounted(async () => {
  await Promise.all([meta.load(), load()])
  try {
    const page = await deviceApi.list({ page_size: 200, status: 'on_sale' })
    devices.value = page.items
  } catch {
    devices.value = []
  }
})

/** 该点位的物料成本小计（参考价合计），让用户建模板时心里有数。 */
function materialsTotal(tpl: PointTemplate): number {
  return tpl.items.reduce(
    (s, it) => s + toNum(it.device_reference_price) * toNum(it.quantity),
    0,
  )
}

function primaryCode(tpl: PointTemplate): string {
  const p = tpl.items.find((i) => i.is_primary)
  if (p) return p.device_name ?? '—'
  return tpl.items.length ? `${tpl.items[0].device_name ?? '—'}（未指定，取第一行）` : '—'
}

function openCreate(): void {
  editing.value = null
  Object.assign(form, {
    code: '', name: '', subsystem_type: '', install_unit_price: 0, notes: '', is_active: true,
  })
  items.value = [{ device_id: '', quantity: 1, is_primary: true }]
  dialogVisible.value = true
}

function openEdit(tpl: PointTemplate): void {
  editing.value = tpl
  Object.assign(form, {
    code: tpl.code,
    name: tpl.name,
    subsystem_type: tpl.subsystem_type ?? '',
    install_unit_price: toNum(tpl.install_unit_price),
    notes: tpl.notes ?? '',
    is_active: tpl.is_active,
  })
  items.value = tpl.items.map((i) => ({
    device_id: i.device_id ?? '',
    quantity: toNum(i.quantity),
    is_primary: i.is_primary,
  }))
  dialogVisible.value = true
}

function addItem(): void {
  items.value.push({ device_id: '', quantity: 1, is_primary: items.value.length === 0 })
}

function removeItem(idx: number): void {
  items.value.splice(idx, 1)
}

/** 主设备单选：勾了新的就把旧的取消（否则安装单价会被算到多行）。 */
function setPrimary(idx: number): void {
  items.value.forEach((it, i) => {
    it.is_primary = i === idx
  })
}

async function save(): Promise<void> {
  if (!form.name.trim()) {
    ElMessage.warning('请填模板名称')
    return
  }
  if (!editing.value && !form.code.trim()) {
    ElMessage.warning('请填模板编码（如 PT-DOME）')
    return
  }
  const payloadItems = items.value
    .filter((i) => i.device_id)
    .map((i, idx) => ({
      device_id: i.device_id,
      quantity: i.quantity,
      is_primary: i.is_primary,
      sort_order: idx,
    }))
  if (!payloadItems.length) {
    ElMessage.warning('至少加一行设备明细')
    return
  }
  if (!payloadItems.some((i) => i.is_primary)) {
    ElMessage.warning('请指定一个「主设备」——安装单价（元/点）只计在主设备行上')
    return
  }

  saving.value = true
  try {
    const body = {
      code: form.code.trim(),
      name: form.name.trim(),
      subsystem_type: form.subsystem_type || null,
      install_unit_price: form.install_unit_price,
      notes: form.notes.trim() || null,
      is_active: form.is_active,
      items: payloadItems,
    }
    if (editing.value) {
      const { code: _c, ...rest } = body
      await pointTemplateApi.update(editing.value.id, rest)
      ElMessage.success('模板已更新')
    } else {
      await pointTemplateApi.create(body)
      ElMessage.success('模板已新增')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    saving.value = false
  }
}

async function toggleActive(tpl: PointTemplate): Promise<void> {
  const next = !tpl.is_active
  try {
    await pointTemplateApi.update(tpl.id, { is_active: next })
    ElMessage.success(next ? '已启用' : '已停用')
    await load()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

async function remove(tpl: PointTemplate): Promise<void> {
  await ElMessageBox.confirm(
    `删除模板「${tpl.name}」？删除后不能再套用到报价单（已生成的报价行不受影响）。`,
    '删除点位模板',
    { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
  )
    .then(async () => {
      await pointTemplateApi.remove(tpl.id)
      ElMessage.success('已删除')
      await load()
    })
    .catch(() => undefined)
}
</script>

<template>
  <div class="page pt">
    <div class="page-head">
      <div>
        <h2 class="page-title">点位模板</h2>
        <p class="page-sub">
          一个「点位」的物料构成 + 该点位安装单价（元/点）· 报价编制时可整批生成
        </p>
      </div>
    </div>

    <div class="card pt-bar no-print">
      <el-button v-if="canCreate" type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>&nbsp;新增模板
      </el-button>
      <el-button @click="reload">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
      <div class="spacer" />
      <span class="text-muted pt-stat">共 {{ total }} 个模板</span>
    </div>

    <div class="card">
      <el-form inline class="inline-form" @submit.prevent>
        <el-form-item label="关键字">
          <el-input
            v-model="query.keyword"
            placeholder="编码 / 名称"
            clearable
            style="width: 200px"
            @keyup.enter="reload"
            @clear="reload"
          />
        </el-form-item>
        <el-form-item label="子系统">
          <el-select
            v-model="query.subsystem_type"
            placeholder="全部"
            clearable
            style="width: 160px"
            @change="reload"
          >
            <el-option
              v-for="o in meta.options('subsystem_type')"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="query.include_inactive" @change="reload">显示已停用</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="reload">
            <el-icon><Search /></el-icon>&nbsp;查询
          </el-button>
        </el-form-item>
      </el-form>

      <el-table v-loading="loading" :data="rows" border stripe size="small">
        <el-table-column prop="code" label="编码" width="120" />
        <el-table-column prop="name" label="模板名称" min-width="160" show-overflow-tooltip />
        <el-table-column label="子系统" width="110">
          <template #default="{ row }">
            {{ row.subsystem_type ? meta.label('subsystem_type', row.subsystem_type) : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="安装单价（元/点）" width="140" align="right">
          <template #default="{ row }">
            <span class="num strong">{{ money(row.install_unit_price) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="点位构成" min-width="240">
          <template #default="{ row }">
            <div v-for="it in row.items" :key="it.id" class="pt-item-line">
              <el-tag v-if="it.is_primary" size="small" type="success" effect="plain">主</el-tag>
              <span>{{ it.device_name || '（设备已删除）' }}</span>
              <span class="text-muted">× {{ it.quantity }}</span>
              <span class="num text-muted">{{ money(it.device_reference_price) }}</span>
            </div>
            <div v-if="!row.items.length" class="text-muted">（没有明细）</div>
          </template>
        </el-table-column>
        <el-table-column label="物料小计" width="120" align="right">
          <template #default="{ row }">
            <span class="num">{{ money(materialsTotal(row)) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="主设备" width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="row.items.some((i: PointTemplateItem) => i.is_primary) ? '' : 'warn-text'">
              {{ primaryCode(row) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="76">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_active ? 'success' : 'info'" effect="plain">
              {{ row.is_active ? '在用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right" class-name="col-no-print" label-class-name="col-no-print">
          <template #default="{ row }">
            <el-button link size="small" :disabled="!canUpdate" @click="openEdit(row)">编辑</el-button>
            <el-button
              link
              size="small"
              :type="row.is_active ? 'danger' : 'success'"
              :disabled="!canUpdate"
              @click="toggleActive(row)"
            >
              {{ row.is_active ? '停用' : '启用' }}
            </el-button>
            <el-button link size="small" type="danger" :disabled="!canDelete" @click="remove(row)">
              删
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有点位模板，点「新增模板」开始" :image-size="70" />
        </template>
      </el-table>

      <el-pagination
        v-model:current-page="query.page"
        :page-size="query.page_size"
        :total="total"
        layout="prev, pager, next, total"
        class="mt"
        @current-change="load"
      />
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editing ? '编辑点位模板' : '新增点位模板'"
      width="880px"
      top="5vh"
    >
      <el-form label-width="120px">
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="编码" required>
              <el-input v-model="form.code" :disabled="!!editing" placeholder="PT-DOME" maxlength="32" />
            </el-form-item>
          </el-col>
          <el-col :span="10">
            <el-form-item label="名称" required>
              <el-input v-model="form.name" placeholder="球机点位模板" maxlength="255" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="在用">
              <el-switch v-model="form.is_active" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="10">
            <el-form-item label="默认子系统">
              <el-select v-model="form.subsystem_type" placeholder="不指定" clearable style="width: 100%">
                <el-option
                  v-for="o in meta.options('subsystem_type')"
                  :key="o.value"
                  :label="o.label"
                  :value="o.value"
                />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="安装单价">
              <el-input-number
                v-model="form.install_unit_price"
                :min="0"
                :precision="2"
                :controls="false"
                style="width: 120px"
              />
              <span class="text-muted ml">元/点</span>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="备注">
          <el-input v-model="form.notes" type="textarea" :rows="2" maxlength="2048" />
        </el-form-item>
      </el-form>

      <el-divider content-position="left">点位构成（一个点位用到的设备）</el-divider>
      <el-alert
        type="info"
        show-icon
        :closable="false"
        class="mb"
        title="安装单价（元/点）只计在「主设备」那一行；其余组件行安装费为 0，避免一个点位重复收安装费。"
      />
      <el-table :data="items" border size="small">
        <el-table-column label="主设备" width="76" align="center">
          <template #default="{ $index }">
            <el-radio
              :model-value="items[$index].is_primary"
              :value="true"
              @change="setPrimary($index)"
            >
              <span />
            </el-radio>
          </template>
        </el-table-column>
        <el-table-column label="设备" min-width="340">
          <template #default="{ row }">
            <el-select v-model="row.device_id" filterable placeholder="选设备" style="width: 100%">
              <el-option
                v-for="o in deviceOptions"
                :key="o.value"
                :label="o.label"
                :value="o.value"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="数量" width="120">
          <template #default="{ row }">
            <el-input-number v-model="row.quantity" :min="0.01" :precision="2" :controls="false" style="width: 100%" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ $index }">
            <el-button link size="small" type="danger" @click="removeItem($index)">移除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button class="mt" @click="addItem">
        <el-icon><Plus /></el-icon>&nbsp;加一行设备
      </el-button>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.pt-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 10px;
}

.pt-stat {
  font-size: 12.5px;
}

.pt-item-line {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
}
</style>

