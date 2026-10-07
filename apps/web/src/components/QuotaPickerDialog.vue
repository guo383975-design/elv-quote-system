<script setup lang="ts">
/**
 * 定额选择器（2026-10-05 新增）。
 *
 * 为什么需要：以前挂定额只能**手打** `quota:5-1-12` 到备注里 ✗ —— 界面上没有入口 ✗。
 * 现在按编号/名称搜索 ✓，显示单位/二类工日/基价 ✓，选中即写备注 ✓。
 */
import { ref, watch } from 'vue'
import { laborApi } from '@/api'
import type { QuotaItem } from '@/api/types'
import { extractErrorMessage } from '@/api/client'

const props = defineProps<{
  modelValue: boolean
  /** 只搜某一章（如 '5-2'），不传=全库 */
  category?: string
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'pick', q: QuotaItem): void
}>()

const keyword = ref('')
const rows = ref<QuotaItem[]>([])
const loading = ref(false)
const err = ref('')

async function load(): Promise<void> {
  loading.value = true
  err.value = ''
  try {
    rows.value = await laborApi.quotaItems({
      category: props.category || undefined,
      keyword: keyword.value.trim() || undefined,
    })
  } catch (e) {
    err.value = extractErrorMessage(e)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.modelValue,
  (v) => {
    if (v) {
      keyword.value = ''
      void load()
    }
  },
)

function pick(q: QuotaItem): void {
  emit('pick', q)
  emit('update:modelValue', false)
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    title="选择定额子目"
    width="920px"
    top="6vh"
    :close-on-click-modal="false"
    append-to-body
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-input
      v-model="keyword"
      placeholder="按定额编号或名称搜索（如 5-1-1 / 双绞线 / 配线架）"
      clearable
      @keyup.enter="load"
      @clear="load"
    >
      <template #append>
        <el-button @click="load">搜索</el-button>
      </template>
    </el-input>
    <div v-if="err" class="qp-err">{{ err }}</div>

    <el-table
      v-loading="loading"
      :data="rows"
      height="440"
      size="small"
      border
      stripe
      class="mt"
      @row-dblclick="pick"
    >
      <el-table-column prop="code" label="定额编号" width="112" />
      <el-table-column prop="name" label="子目名称" min-width="240" show-overflow-tooltip />
      <el-table-column prop="unit" label="单位" width="76" />
      <el-table-column label="二类工日" width="100" align="right">
        <template #default="{ row }">
          <span class="num">{{ row.labor_b_day }}</span>
        </template>
      </el-table-column>
      <el-table-column label="基价" width="100" align="right">
        <template #default="{ row }">
          <span class="num">{{ row.base_price }}</span>
        </template>
      </el-table-column>
      <el-table-column width="86" align="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="pick(row)">选用</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="没有匹配的定额子目（试试编号或名称关键词）" />
      </template>
    </el-table>

    <template #footer>
      <span class="text-muted">
        双击行或点「选用」= 把该定额挂到这一行（写入备注 <code>quota:编号</code> ✓）
      </span>
    </template>
  </el-dialog>
</template>

<style scoped>
.qp-err {
  color: var(--el-color-danger);
  margin-top: 8px;
  font-size: 13px;
}
.mt {
  margin-top: 10px;
}
</style>
