<script setup lang="ts">
/**
 * 套用点位模板（原型第 1 页的「套用点位模板」「批量添加点位」）。
 *
 * 流程：选模板 → 选子系统 → 填点位数 → **预览**（将生成哪些行、多少安装费）
 * → 确认生成。预览走 `dry_run=true`，不写库 —— 一按就写几十行的操作必须能反悔。
 *
 * 安装单价（元/点）只落在模板的主设备行上；模板没指定主设备时后端会退化取第一行，
 * 并在返回里用 `primary_fallback` 标出来，这里据实提示。
 */
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { pointTemplateApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { PointTemplate, PointTemplateApplyResult } from '@/api/types'
import { money } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    quoteId: string
    subsystems?: { id: string; name: string }[]
    defaultSubsystemId?: string | null
  }>(),
  { subsystems: () => [], defaultSubsystemId: null },
)

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  applied: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const loading = ref(false)
const busy = ref(false)
const templates = ref<PointTemplate[]>([])
const preview = ref<PointTemplateApplyResult | null>(null)

const form = reactive({
  template_id: '',
  subsystem_id: null as string | null,
  point_count: 10,
})

const selected = computed(() =>
  templates.value.find((t) => t.id === form.template_id) ?? null,
)

async function loadTemplates(): Promise<void> {
  loading.value = true
  try {
    const page = await pointTemplateApi.list({ page_size: 100 })
    templates.value = page.items
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    preview.value = null
    form.subsystem_id = props.defaultSubsystemId
    void loadTemplates()
  },
)

onMounted(() => {
  // 打开时若还没加载过模板，先拉一次（dialog 可能被 v-if 之外的方式显示）
  if (props.modelValue) void loadTemplates()
})


function onInputChange(): void {
  preview.value = null
}

async function run(dryRun: boolean): Promise<void> {
  if (!form.template_id) {
    ElMessage.warning('先选一个点位模板')
    return
  }
  if (!form.point_count || form.point_count <= 0) {
    ElMessage.warning('点位数要大于 0')
    return
  }
  busy.value = true
  try {
    const res = await pointTemplateApi.apply(form.template_id, {
      quote_id: props.quoteId,
      subsystem_id: form.subsystem_id,
      point_count: form.point_count,
      dry_run: dryRun,
    })
    preview.value = res
    if (dryRun) {
      ElMessage.success(`将生成 ${res.rows.length} 行，确认后写入`)
    } else {
      ElMessage.success(`已生成 ${res.created} 行（含安装费 ${money(res.install_total)}）`)
      emit('applied')
      visible.value = false
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="套用点位模板 / 批量添加点位"
    width="1000px"
    top="5vh"
    :close-on-click-modal="false"
  >
    <el-alert
      type="info"
      show-icon
      :closable="false"
      class="mb"
      title="点位数 × 模板用量 = 每行数量；安装单价（元/点）只计在主设备那一行。先预览，确认后才写入。"
    />

    <el-form inline>
      <el-form-item label="点位模板">
        <el-select
          v-model="form.template_id"
          filterable
          placeholder="选模板"
          style="width: 280px"
          :loading="loading"
          @change="onInputChange"
        >
          <el-option
            v-for="t in templates"
            :key="t.id"
            :label="`${t.code} ${t.name} · ${t.install_unit_price} 元/点`"
            :value="t.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="归入子系统">
        <el-select
          v-model="form.subsystem_id"
          clearable
          placeholder="不归属"
          style="width: 190px"
          @change="onInputChange"
        >
          <el-option v-for="s in subsystems" :key="s.id" :label="s.name" :value="s.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="点位数">
        <el-input-number
          v-model="form.point_count"
          :min="1"
          :precision="0"
          style="width: 130px"
          @change="onInputChange"
        />
      </el-form-item>
    </el-form>

    <div v-if="selected" class="text-muted ap-note">
      模板构成：
      <span v-for="(it, i) in selected.items" :key="it.id">
        <template v-if="i"> + </template>
        {{ it.device_name || '（设备已删除）' }}×{{ it.quantity }}<template v-if="it.is_primary">（主）</template>
      </span>
    </div>

    <template v-if="preview">
      <el-alert
        v-if="preview.primary_fallback"
        type="warning"
        show-icon
        :closable="false"
        class="mt"
        title="该模板没有指定主设备，安装单价已退化计在第一行上；建议去模板里指定主设备。"
      />
      <el-alert
        v-for="(s, i) in preview.skipped"
        :key="i"
        type="warning"
        show-icon
        :closable="false"
        class="mt"
        :title="`模板第 ${s.sort_order + 1} 行已跳过：${s.reason}`"
      />

      <el-table :data="preview.rows" border size="small" height="300" class="mt">
        <el-table-column prop="device_code" label="编码" width="100" />
        <el-table-column prop="name" label="设备名称" min-width="180" show-overflow-tooltip />
        <el-table-column prop="model" label="型号规格" min-width="140" show-overflow-tooltip />
        <el-table-column prop="unit" label="单位" width="56" />
        <el-table-column label="数量" width="80" align="right">
          <template #default="{ row }"><span class="num">{{ row.quantity }}</span></template>
        </el-table-column>
        <el-table-column label="设备单价" width="100" align="right">
          <template #default="{ row }"><span class="num">{{ money(row.unit_price) }}</span></template>
        </el-table-column>
        <el-table-column label="安装单价" width="100" align="right">
          <template #default="{ row }">
            <span class="num" :class="row.install_unit_price > 0 ? 'strong' : 'text-muted'">
              {{ money(row.install_unit_price) }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="设备金额" width="110" align="right">
          <template #default="{ row }"><span class="num">{{ money(row.equipment_amount) }}</span></template>
        </el-table-column>
        <el-table-column label="安装费" width="100" align="right">
          <template #default="{ row }"><span class="num">{{ money(row.install_amount) }}</span></template>
        </el-table-column>
        <el-table-column label="" width="64" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.is_primary" size="small" type="success" effect="plain">主</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <div class="sum-line">
        {{ preview.dry_run ? '将生成' : '已生成' }} <b>{{ preview.rows.length }}</b> 行 ·
        设备 <b class="num">{{ money(preview.equipment_total) }}</b> +
        安装 <b class="num text-accent">{{ money(preview.install_total) }}</b>
        <span class="text-muted">（{{ preview.point_count }} 点位 × {{ money(preview.install_unit_price) }} 元/点）</span>
      </div>
    </template>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button :loading="busy" @click="run(true)">预览将生成的行</el-button>
      <el-button type="primary" :loading="busy" :disabled="!preview" @click="run(false)">
        确认生成{{ preview ? `（${preview.rows.length} 行）` : '' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.ap-note {
  font-size: 12px;
  margin-top: 4px;
}
</style>

