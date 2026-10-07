<script setup lang="ts">
/**
 * 供应商库（2026-10-04）。
 *
 * 原型第 5 页设备库工具栏里有「供应商库」入口（此前是置灰的占位），
 * 后端 `/suppliers` 的 CRUD 与权限矩阵其实早就有了，缺的只是页面。
 *
 * 权限（与后端一致，前端只做入口收敛，后端仍二次把关）：
 *   · 读    —— admin/sales/estimator/finance/viewer/purchaser/manager
 *   · 增改  —— admin/estimator/purchaser（销售只查，不给改）
 *   · 删除  —— 仅 admin；但页面**不提供硬删除**：供应商被 devices.supplier_id 引用，
 *              硬删会让设备失去供货来源。要用「停用」，语义与设备库的停用一致。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { supplierApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { Supplier } from '@/api/types'
import { dateTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const loading = ref(false)
const saving = ref(false)
const rows = ref<Supplier[]>([])
const total = ref(0)

const query = reactive({
  keyword: '',
  include_inactive: false,
  page: 1,
  page_size: 20,
})

const dialogVisible = ref(false)
const editing = ref<Supplier | null>(null)
const form = reactive({
  code: '',
  name: '',
  contact_name: '',
  contact_phone: '',
  contact_email: '',
  address: '',
  is_active: true,
})

const canCreate = computed(() => auth.can('supplier', 'create'))
const canUpdate = computed(() => auth.can('supplier', 'update'))

const activeCount = computed(() => rows.value.filter((r) => r.is_active).length)

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await supplierApi.list({
      keyword: query.keyword || undefined,
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

onMounted(load)

function openCreate(): void {
  editing.value = null
  Object.assign(form, {
    code: '', name: '', contact_name: '', contact_phone: '',
    contact_email: '', address: '', is_active: true,
  })
  dialogVisible.value = true
}

function openEdit(row: Supplier): void {
  editing.value = row
  Object.assign(form, {
    code: row.code,
    name: row.name,
    contact_name: row.contact_name ?? '',
    contact_phone: row.contact_phone ?? '',
    contact_email: row.contact_email ?? '',
    address: row.address ?? '',
    is_active: row.is_active,
  })
  dialogVisible.value = true
}

async function save(): Promise<void> {
  if (!form.name.trim()) {
    ElMessage.warning('请填供应商名称')
    return
  }
  if (!editing.value && !form.code.trim()) {
    ElMessage.warning('请填业务编码（如 SUP-001）')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      // code 不可改（后端 schema 里就没这个字段，另有业务键约束）
      await supplierApi.update(editing.value.id, {
        name: form.name.trim(),
        contact_name: form.contact_name.trim() || null,
        contact_phone: form.contact_phone.trim() || null,
        contact_email: form.contact_email.trim() || null,
        address: form.address.trim() || null,
        is_active: form.is_active,
      })
      ElMessage.success('供应商已更新')
    } else {
      await supplierApi.create({
        code: form.code.trim(),
        name: form.name.trim(),
        contact_name: form.contact_name.trim() || null,
        contact_phone: form.contact_phone.trim() || null,
        contact_email: form.contact_email.trim() || null,
        address: form.address.trim() || null,
      })
      ElMessage.success('供应商已新增')
    }
    dialogVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    saving.value = false
  }
}

/** 停用 / 启用（软操作）——不提供硬删除，避免设备失去供货来源。 */
async function toggleActive(row: Supplier): Promise<void> {
  const next = !row.is_active
  await ElMessageBox.confirm(
    next
      ? `启用「${row.name}」？启用后报价选型时又能选到它。`
      : `停用「${row.name}」？停用后报价选型不再显示它，已引用它的设备不受影响。`,
    next ? '启用供应商' : '停用供应商',
    { confirmButtonText: next ? '启用' : '停用', cancelButtonText: '取消', type: 'warning' },
  )
    .then(async () => {
      await supplierApi.update(row.id, { is_active: next })
      ElMessage.success(next ? '已启用' : '已停用')
      await load()
    })
    .catch(() => undefined)
}
</script>

<template>
  <div class="page sup">
    <div class="page-head">
      <div>
        <h2 class="page-title">供应商库</h2>
        <p class="page-sub">设备与材料的供货来源 · 设备的「供应商」与询价记录都挂在这里</p>
      </div>
    </div>

    <div class="card sup-bar no-print">
      <el-button v-if="canCreate" type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>&nbsp;新增供应商
      </el-button>
      <el-button @click="reload">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
      <div class="spacer" />
      <span class="text-muted sup-stat">
        共 {{ total }} 家<template v-if="!query.include_inactive">（在用 {{ activeCount }}）</template>
      </span>
    </div>

    <div class="card">
      <el-form inline class="inline-form" @submit.prevent>
        <el-form-item label="关键字">
          <el-input
            v-model="query.keyword"
            placeholder="编码 / 名称 / 联系人"
            clearable
            style="width: 240px"
            @keyup.enter="reload"
            @clear="reload"
          />
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
        <el-table-column prop="code" label="编码" width="110" />
        <el-table-column prop="name" label="供应商名称" min-width="220" show-overflow-tooltip />
        <el-table-column label="联系人" width="110">
          <template #default="{ row }">{{ row.contact_name || '—' }}</template>
        </el-table-column>
        <el-table-column label="电话" width="140">
          <template #default="{ row }">
            <span class="mono">{{ row.contact_phone || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="邮箱" min-width="180" show-overflow-tooltip>
          <template #default="{ row }">{{ row.contact_email || '—' }}</template>
        </el-table-column>
        <el-table-column label="地址" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">{{ row.address || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_active ? 'success' : 'info'" effect="plain">
              {{ row.is_active ? '在用' : '停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="150">
          <template #default="{ row }">
            <span class="text-muted">{{ row.updated_at ? dateTime(row.updated_at) : '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right" class-name="col-no-print" label-class-name="col-no-print">
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
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有供应商，点「新增供应商」开始" :image-size="70" />
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
      :title="editing ? '编辑供应商' : '新增供应商'"
      width="560px"
    >
      <el-form label-width="96px">
        <el-form-item label="编码" required>
          <el-input
            v-model="form.code"
            :disabled="!!editing"
            placeholder="SUP-001"
            maxlength="32"
          />
          <div v-if="editing" class="text-muted sup-hint">
            编码不可修改：设备与报价行按它做业务键
          </div>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="255" />
        </el-form-item>
        <el-form-item label="联系人">
          <el-input v-model="form.contact_name" maxlength="64" />
        </el-form-item>
        <el-form-item label="电话">
          <el-input v-model="form.contact_phone" maxlength="32" />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="form.contact_email" maxlength="255" />
        </el-form-item>
        <el-form-item label="地址">
          <el-input v-model="form.address" maxlength="512" />
        </el-form-item>
        <el-form-item v-if="editing" label="在用">
          <el-switch v-model="form.is_active" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <div class="sup-foot">
      <el-button link @click="router.push({ name: 'devices' })">
        <el-icon><ArrowLeft /></el-icon>&nbsp;返回设备库
      </el-button>
      <span class="text-muted">
        页面不提供硬删除：供应商被设备引用，删掉会让设备失去供货来源（要用「停用」）。
      </span>
    </div>
  </div>
</template>

<style scoped>
.sup-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 10px;
}

.sup-stat {
  font-size: 12.5px;
}

.sup-hint {
  font-size: 11.5px;
  line-height: 1.5;
}

.sup-foot {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 2px 10px;
  font-size: 11.5px;
}
</style>

