<script setup lang="ts">
/**
 * 子系统管理（按项目维度）。
 *
 * 为什么独立成页：在 ProjectListView / QuoteEditorView 里塞 Tab 会让两个
 * 已经很拥挤的页面继续膨胀；子系统本身数量少（11 类枚举，每个项目一般
 * 3-8 个），单独一屏完全够用，编辑流程也更直接。
 *
 * 后端提供的能力：
 *   GET  /projects/{id}/subsystems     列出该项目全部子系统
 *   POST /subsystems                   新建（payload 含 project_id / subsystem_type / supplier_id 可选）
 *   PATCH /subsystems/{id}             改名 / 排序 / 改供货商
 *   DELETE /subsystems/{id}            删除（**只解除明细行归属，不删明细行** ——
 *                                      quote_items.subsystem_id 是 ON DELETE SET NULL ✓）
 *
 * 2026-09-29 接通 supplier 下拉：子系统表里 supplier_id 列已有（FK → suppliers.id），
 * 此前 UI 只读不写。现在弹窗里加了「供货商」下拉，新建/编辑都能挂/改。
 * 软删的供应商会从下拉里消失（listActive 只取 is_active=true 的），避免选到一个
 * 实际已停用的死引用。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { projectApi, supplierApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type {
  Project as ProjectT,
  Subsystem,
  SubsystemType,
  Supplier,
} from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const projectId = computed(() => String(route.params.projectId))

const project = ref<ProjectT | null>(null)
const subsystems = ref<Subsystem[]>([])
const suppliers = ref<Supplier[]>([])
const loading = ref(false)
const saving = ref(false)
const editingId = ref<string | null>(null)

const dialogVisible = ref(false)
const formRef = ref()

type SubsystemForm = {
  subsystem_type: SubsystemType | ''
  custom_name: string
  notes: string
  supplier_id: string | null
}

function emptyForm(): SubsystemForm {
  return {
    subsystem_type: '',
    custom_name: '',
    notes: '',
    supplier_id: null,
  }
}

const form = reactive<SubsystemForm>(emptyForm())

const rules = {
  subsystem_type: [{ required: true, message: '请选择子系统类型', trigger: 'change' }],
}

/** id → name 反查表。`undefined` 表示数据还没回来，统一显示「—」。 */
const supplierNameMap = computed<Record<string, string>>(() => {
  const m: Record<string, string> = {}
  for (const s of suppliers.value) m[s.id] = `${s.code} · ${s.name}`
  return m
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const [p, subs, sups] = await Promise.all([
      projectApi.get(projectId.value),
      projectApi.subsystems(projectId.value),
      // 一次性把当前所有启用的供应商拉回来。下拉 + 列表显示名都用。
      // 失败不让整个页面崩 —— 没供货商权限（sales/viewer 没有 supplier.read 时理论上也有，
      // 这里实际是 sales/viewer 也有 supplier.read）就当空处理。
      supplierApi.listActive().catch(() => [] as Supplier[]),
    ])
    project.value = p
    subsystems.value = subs
    suppliers.value = sups
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await Promise.all([meta.load(), load()])
})

function openCreate(): void {
  editingId.value = null
  Object.assign(form, emptyForm())
  dialogVisible.value = true
}

async function openEdit(row: Subsystem): Promise<void> {
  editingId.value = row.id
  // 先把当前数据填进表单；如果后端字段还没回来（早期 schema 不含 supplier_id），
  // 用 null 兜底，下拉显示空选项，编辑完保存即可补上。
  form.subsystem_type = row.subsystem_type
  form.custom_name = row.custom_name ?? ''
  form.notes = row.notes ?? ''
  form.supplier_id = row.supplier_id ?? null
  dialogVisible.value = true
}

async function save(): Promise<void> {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  if (!form.subsystem_type) {
    ElMessage.warning('请选择子系统类型')
    return
  }

  saving.value = true
  try {
    if (editingId.value) {
      // PATCH：只把表单上能改的字段塞过去，避免把 sort_order / point_count / 各种小计
      // 意外重置。注意 supplier_id 允许 null（解除供货商），不传则不动。
      const payload: Record<string, unknown> = {
        custom_name: form.custom_name.trim() || null,
        notes: form.notes.trim() || null,
        supplier_id: form.supplier_id,
      }
      await projectApi.updateSubsystem(editingId.value, payload)
      ElMessage.success('子系统已更新')
    } else {
      await projectApi.createSubsystem({
        project_id: projectId.value,
        subsystem_type: form.subsystem_type,
        custom_name: form.custom_name.trim() || null,
        notes: form.notes.trim() || null,
        supplier_id: form.supplier_id,
      })
      ElMessage.success('子系统已创建')
    }
    dialogVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  } finally {
    saving.value = false
  }
}

async function remove(row: Subsystem): Promise<void> {
  const name = row.custom_name || meta.label('subsystem_type', row.subsystem_type)
  await ElMessageBox.confirm(
    `确认删除子系统「${name}」？删除后该子系统下的明细行会保留，但其子系统归属被清空。`,
    '高危操作',
    { confirmButtonText: '确认删除', cancelButtonText: '取消', type: 'warning' },
  )
    .then(async () => {
      await projectApi.removeSubsystem(row.id)
      ElMessage.success('子系统已删除')
      await load()
    })
    .catch(() => undefined)
}

async function clearSupplier(row: Subsystem): Promise<void> {
  // 列表行上的「×」单独走 PATCH，只清 supplier_id，不弹整个编辑窗
  await projectApi.updateSubsystem(row.id, { supplier_id: null })
  ElMessage.success('已解除供货商')
  await load()
}

function goBack(): void {
  void router.push({ name: 'projects' })
}

function goQuote(): void {
  // 没有报价单时跳到项目列表让用户点「编制报价」（那里会自动建草稿）。
  void router.push({ name: 'projects' })
}

/** 列表显示名：custom_name 优先，否则查 /meta/labels 拿中文。 */
function displayName(row: Subsystem): string {
  return row.custom_name || meta.label('subsystem_type', row.subsystem_type)
}

/** 列表里展示的供货商名。空表示未挂；展示统一格式 code · name。 */
function displaySupplier(row: Subsystem): string {
  if (!row.supplier_id) return '—'
  return supplierNameMap.value[row.supplier_id] ?? row.supplier_id
}

const canEditSupplier = computed(() => suppliers.value.length > 0)
</script>

<template>
  <div v-loading="loading" class="page">
    <!-- 头部 -->
    <div class="page-head">
      <div>
        <h2 class="page-title">
          <el-button link @click="goBack">
            <el-icon><ArrowLeft /></el-icon>
          </el-button>
          {{ project?.name ?? '子系统管理' }}
        </h2>
        <p class="page-sub" v-if="project">
          {{ project.code }} · 客户 {{ project.customer_name }} ·
          共 {{ subsystems.length }} 个子系统
        </p>
      </div>
      <div>
        <el-button
          v-if="auth.can('project', 'update')"
          type="primary"
          :disabled="!project"
          @click="openCreate"
        >
          <el-icon><Plus /></el-icon>&nbsp;新建子系统
        </el-button>
        <el-button :disabled="!project" @click="goQuote">
          <el-icon><Tickets /></el-icon>&nbsp;回到报价编制
        </el-button>
      </div>
    </div>

    <!-- 子系统列表 -->
    <div class="card">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="mb"
        title="子系统用于把明细行按专业分类汇总，方便客户版汇总表的呈现。"
      >
        <template #default>
          若不填写「自定义名称」，将按子系统类型使用默认中文（如「视频监控」）。
        </template>
      </el-alert>

      <el-table v-if="subsystems.length" :data="subsystems" border stripe size="small">
        <el-table-column label="#" type="index" width="50" />
        <el-table-column label="子系统" min-width="200">
          <template #default="{ row }">
            <div class="name-line">
              <span>{{ displayName(row) }}</span>
              <el-tag v-if="row.custom_name" size="small" effect="plain" type="info">
                自定义
              </el-tag>
            </div>
            <div class="text-muted mono sub">
              {{ meta.label('subsystem_type', row.subsystem_type) }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="供货商" min-width="180">
          <template #default="{ row }">
            <div v-if="row.supplier_id" class="supplier-cell">
              <el-icon class="supplier-icon"><OfficeBuilding /></el-icon>
              <span class="supplier-name">{{ displaySupplier(row) }}</span>
              <el-button
                v-if="auth.can('project', 'update')"
                type="danger"
                link
                size="small"
                class="supplier-clear"
                title="解除供货商"
                @click="clearSupplier(row)"
              >
                <el-icon><Close /></el-icon>
              </el-button>
            </div>
            <span v-else class="text-muted">— 未挂 —</span>
          </template>
        </el-table-column>
        <el-table-column label="点位数" width="100" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.point_count }}</span>
          </template>
        </el-table-column>
        <el-table-column label="排序" width="80" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.sort_order }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="160" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="auth.can('project', 'update')"
              type="primary"
              link
              size="small"
              @click="openEdit(row)"
            >
              编辑
            </el-button>
            <el-button
              v-if="auth.can('subsystem', 'delete')"
              type="danger"
              link
              size="small"
              @click="remove(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="该项目还没有子系统，点击「新建子系统」开始划分" />
        </template>
      </el-table>

      <el-empty
        v-else-if="!loading"
        description="该项目还没有子系统，点击「新建子系统」开始划分"
      />
    </div>

    <!-- 新建 / 编辑弹窗 -->
    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑子系统' : '新建子系统'"
      width="640px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="92px">
        <el-form-item label="子系统类型" prop="subsystem_type">
          <!-- 编辑模式下类型不允许改：同一项目下同类型唯一，改类型会撞唯一约束 -->
          <el-select
            v-model="form.subsystem_type"
            placeholder="选择类型"
            style="width: 100%"
            :disabled="!!editingId"
          >
            <el-option
              v-for="o in meta.options('subsystem_type')"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="自定义名称">
          <el-input
            v-model="form.custom_name"
            placeholder="可填：覆盖默认中文名（如：综合布线）"
          />
        </el-form-item>
        <el-form-item label="供货商">
          <el-select
            v-model="form.supplier_id"
            placeholder="选择供货商（可选）"
            style="width: 100%"
            clearable
            filterable
            :disabled="!canEditSupplier"
          >
            <el-option
              v-for="s in suppliers"
              :key="s.id"
              :label="`${s.code} · ${s.name}`"
              :value="s.id"
            />
            <template #empty>
              <span class="text-muted">暂无可用供货商（先在「供货商管理」页录入）</span>
            </template>
          </el-select>
          <div v-if="!canEditSupplier" class="text-muted hint">
            未拿到供货商列表（可能没有 supplier.read 权限，或网络故障）。
          </div>
        </el-form-item>
        <el-form-item label="备注">
          <el-input
            v-model="form.notes"
            type="textarea"
            :rows="2"
            placeholder="如：本次只含办公区点位，不含地下车库"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mb {
  margin-bottom: 12px;
}

.name-line {
  display: flex;
  align-items: center;
  gap: 6px;
}

.sub {
  font-size: 11.5px;
  margin-top: 2px;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
}

.supplier-cell {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  max-width: 100%;
}

.supplier-icon {
  color: var(--el-color-primary);
  flex-shrink: 0;
}

.supplier-name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.supplier-clear {
  flex-shrink: 0;
  padding: 0 4px;
}

.hint {
  font-size: 12px;
  margin-top: 4px;
}
</style>