<script setup lang="ts">
import QuotaPickerDialog from '@/components/QuotaPickerDialog.vue'
/**
 * 明细行新增 / 编辑对话框。
 *
 * 只有「手工录入」的行才会走这里改价格；来自设备库的行如果想改价，
 * 前端允许改（现场折让很常见），但会把 source 保持为原值，
 * 由后端记录审计日志，避免「改了价但看不出是手工改的」。
 */
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'

import { quoteApi } from '@/api'
import type { QuoteItem } from '@/api/types'
import { toNum } from '@/utils/format'

const props = defineProps<{
  modelValue: boolean
  quoteId: string
  /** null 表示新增 */
  item?: QuoteItem | null
  subsystems: { id: string; name: string }[]
  /** 只读角色打开时禁用保存 */
  readonly?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [v: boolean]
  saved: []
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const isEdit = computed(() => Boolean(props.item?.id))
const saving = ref(false)
const formRef = ref()

type ItemForm = {
  name: string
  brand: string
  model: string
  unit: string
  quantity: number
  unit_price: number
  install_unit_price: number
  subsystem_id: string | null
  is_owner_supplied: boolean
  notes: string
  // 工艺系数：定额口径用。后端 schema 已规划 is_shielded / is_cat5e / is_ceiling
  // 三个 boolean（见 API 类型注），但当前 Pydantic schema 还没更新；
  // 这里先把 UI 接上，提交时挂载到 payload，但当前 PATCH 会被后端
  // 静默丢弃（extra fields ignored）。等 TODO(后端核心) 0002 迁移落地后
  // 自动生效，不需要再改前端。
  is_shielded: boolean
  is_cat5e: boolean
  is_ceiling: boolean
}

function emptyForm(): ItemForm {
  return {
    name: '',
    brand: '',
    model: '',
    unit: '台',
    quantity: 1,
    unit_price: 0,
    install_unit_price: 0,
    subsystem_id: props.subsystems[0]?.id ?? null,
    is_owner_supplied: false,
    notes: '',
    is_shielded: false,
    is_cat5e: false,
    is_ceiling: false,
  }
}

const form = ref<ItemForm>(emptyForm())

const rules = {
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  quantity: [{ required: true, message: '请输入数量', trigger: 'blur' }],
}

/** 实时预览设备费 / 安装费，避免用户按完保存才发现算错。 */
const equipmentAmount = computed(() => toNum(form.value.quantity) * toNum(form.value.unit_price))
const installAmount = computed(
  () => toNum(form.value.quantity) * toNum(form.value.install_unit_price),
)

watch(visible, (open) => {
  if (!open) return
  if (props.item) {
    form.value = {
      name: props.item.name,
      brand: props.item.brand ?? '',
      model: props.item.model ?? '',
      unit: props.item.unit || '台',
      quantity: toNum(props.item.quantity),
      unit_price: toNum(props.item.unit_price),
      install_unit_price: toNum(props.item.install_unit_price),
      subsystem_id: props.item.subsystem_id,
      is_owner_supplied: props.item.is_owner_supplied,
      notes: props.item.notes ?? '',
      // 工艺系数：可能 undefined（后端 schema 还没加），默认 false。
      is_shielded: Boolean(props.item.is_shielded),
      is_cat5e: Boolean(props.item.is_cat5e),
      is_ceiling: Boolean(props.item.is_ceiling),
    }
  } else {
    form.value = emptyForm()
  }
})

async function save(): Promise<void> {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      name: form.value.name.trim(),
      brand: form.value.brand.trim() || null,
      model: form.value.model.trim() || null,
      unit: form.value.unit.trim() || '台',
      quantity: form.value.quantity,
      unit_price: form.value.unit_price,
      install_unit_price: form.value.install_unit_price,
      subsystem_id: form.value.subsystem_id,
      is_owner_supplied: form.value.is_owner_supplied,
      notes: form.value.notes.trim() || null,
      // 工艺系数：一并带上。
      // TODO(后端核心): 等 Pydantic schema 加 is_shielded / is_cat5e / is_ceiling
      // 三个 boolean 后，这里 PATCH 才会真正落库。当前会被 FastAPI 当成
      // 未知字段忽略（model_config = extra="ignore"），不会影响其他字段。
      is_shielded: form.value.is_shielded,
      is_cat5e: form.value.is_cat5e,
      is_ceiling: form.value.is_ceiling,
    }

    if (isEdit.value && props.item) {
      await quoteApi.updateItem(props.quoteId, props.item.id, payload)
      ElMessage.success('明细行已更新')
    } else {
      await quoteApi.addItem(props.quoteId, payload)
      ElMessage.success('已加入报价单')
    }
    visible.value = false
    emit('saved')
  } finally {
    saving.value = false
  }
}
const quotaPickVisible = ref(false)
const QUOTA_MARK = 'quota:'

/** 当前套的定额编号（来自备注 ✓） */
const quotaCode = computed(() => {
  const notes = String(form.value.notes || '')
  const i = notes.indexOf(QUOTA_MARK)
  if (i < 0) return ''
  return notes.slice(i + QUOTA_MARK.length).trim().split(/\s+/)[0] || ''
})

/** 写回备注：保留其它说明 ✓、替换旧的 quota:xxx ✓ */
function withQuota(notes: string | null | undefined, code: string): string {
  const kept = String(notes || '')
    .split(/\s+/)
    .filter((w) => w && !w.startsWith(QUOTA_MARK))
    .join(' ')
  return [kept, code ? `${QUOTA_MARK}${code}` : ''].filter(Boolean).join(' ')
}

function onQuotaPicked(q: { code: string }): void {
  form.value.notes = withQuota(form.value.notes, q.code)
  ElMessage.success(`已套定额 ${q.code} —— 保存后生效 ✓`)
}

</script>

<template>
  <el-dialog
    v-model="visible"
    :title="isEdit ? '编辑明细行' : '手工添加明细行'"
    width="720px"
    :close-on-click-modal="false"
  >
    <el-alert
      v-if="!isEdit"
      type="info"
      :closable="false"
      show-icon
      class="mb"
      title="手工录入的行会以琥珀色标记，便于与设备库来源的行区分。单价填 0 将被视为「待询价」。"
    />

    <el-form ref="formRef" :model="form" :rules="rules" label-width="92px">
      <el-form-item label="名称" prop="name">
        <el-input v-model="form.name" placeholder="如 网络半球摄像机" />
      </el-form-item>

      <div class="grid-2">
        <el-form-item label="定额">
          <el-button link type="primary" @click="quotaPickVisible = true">
            {{ quotaCode || '未套定额（点击选择子目）' }}
          </el-button>
          <el-button v-if="quotaCode" link type="danger"
                     @click="form.notes = withQuota(form.notes, '')">清除</el-button>
          <span class="text-muted" style="margin-left:8px;font-size:12px">
            选中后写入备注 quota:编号 ✓，该行即按定额口径算人工费 ✓
          </span>
        </el-form-item>
        <el-form-item label="品牌">
          <el-input v-model="form.brand" placeholder="选填" />
        </el-form-item>
        <el-form-item label="型号">
          <el-input v-model="form.model" placeholder="选填" />
        </el-form-item>
      </div>

      <div class="grid-3">
        <el-form-item label="单位">
          <el-input v-model="form.unit" />
        </el-form-item>
        <el-form-item label="数量" prop="quantity">
          <el-input-number
            v-model="form.quantity"
            :min="0"
            :precision="0"
            :step="1"
            controls-position="right"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="所属子系统">
          <el-select v-model="form.subsystem_id" clearable placeholder="不归属" style="width: 100%">
            <el-option v-for="s in subsystems" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
      </div>

      <div class="grid-2">
        <el-form-item label="设备单价">
          <el-input-number
            v-model="form.unit_price"
            :min="0"
            :precision="2"
            :step="100"
            controls-position="right"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="安装单价">
          <el-input-number
            v-model="form.install_unit_price"
            :min="0"
            :precision="2"
            :step="10"
            controls-position="right"
            style="width: 100%"
          />
        </el-form-item>
      </div>

      <el-form-item label="甲方供货">
        <el-switch v-model="form.is_owner_supplied" />
        <span class="text-muted ml">甲供设备不计入设备费，仅计入安装费</span>
      </el-form-item>

      <!-- 工艺系数（定额口径用）：影响「定额计价口径」行的换算单价。
           TODO(后端核心): 等 Pydantic schema 加 is_shielded / is_cat5e / is_ceiling
           三个 boolean 后，勾选状态会真正随 PATCH 落库。 -->
      <el-form-item label="工艺系数">
        <div class="craft-row">
          <el-checkbox v-model="form.is_shielded">屏蔽线缆（×1.20）</el-checkbox>
          <el-checkbox v-model="form.is_cat5e">六类线（×1.10）</el-checkbox>
          <el-checkbox v-model="form.is_ceiling">吊顶敷设（×1.20）</el-checkbox>
        </div>
        <div class="text-muted craft-hint">
          三个系数作用于定额口径的人工费；点单价口径不受影响。
        </div>
      </el-form-item>

      <el-form-item label="备注">
        <el-input
          v-model="form.notes"
          type="textarea"
          :rows="2"
          placeholder="可填写定额编号 quote:5-1-12 以便参与定额口径计价"
        />
      </el-form-item>
    </el-form>

    <div class="preview">
      <span>设备费 <b class="num">{{ equipmentAmount.toFixed(2) }}</b></span>
      <span class="dot">·</span>
      <span>安装费 <b class="num">{{ installAmount.toFixed(2) }}</b></span>
      <span class="dot">·</span>
      <span>小计 <b class="num">{{ (equipmentAmount + installAmount).toFixed(2) }}</b></span>
    </div>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="readonly" @click="save">
        保存
      </el-button>
    </template>
  </el-dialog>

    <QuotaPickerDialog v-model="quotaPickVisible" @pick="onQuotaPicked" />
  </template>

<style scoped>
.mb {
  margin-bottom: 12px;
}

.ml {
  margin-left: 10px;
  font-size: 12px;
}

.craft-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
}

.craft-hint {
  font-size: 11.5px;
  margin-top: 4px;
}

.preview {
  margin-top: 4px;
  padding: 10px 12px;
  background: #f5f7fa;
  border-radius: 4px;
  font-size: 13px;
  color: #374151;
}

.dot {
  margin: 0 8px;
  color: #cbd5e1;
}
</style>
