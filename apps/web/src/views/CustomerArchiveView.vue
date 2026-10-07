<script setup lang="ts">
/**
 * 客户档案（2026-10-04）。
 *
 * 以前甲方只是项目上的三个自由文本字段，同一个客户能写成十种名字 ——
 * 按客户统计做不了、联系人每次重问、开票信息散在聊天记录里。
 * 这里维护主数据，项目引用它（选了客户就自动带出联系人/电话）。
 *
 * 删除 = 停用：历史项目还引用着它，物理删除会让老项目的客户名变空白。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { customerApi } from '@/api'
import type { Customer } from '@/api/types'
import { extractErrorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const loading = ref(false)
const rows = ref<Customer[]>([])
const total = ref(0)
const query = reactive({ keyword: '', include_inactive: false, page: 1, page_size: 20 })

const canWrite = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await customerApi.list({ ...query })
    rows.value = page.items
    total.value = page.total
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

// ----- 新增 / 编辑 -----
const dialog = ref(false)
const saving = ref(false)
const editingId = ref<string | null>(null)
const form = reactive({
  name: '',
  short_name: '',
  contact_name: '',
  contact_phone: '',
  contact_email: '',
  address: '',
  tax_no: '',
  bank_name: '',
  bank_account: '',
  industry: '',
  note: '',
})

function resetForm(): void {
  Object.assign(form, {
    name: '', short_name: '', contact_name: '', contact_phone: '', contact_email: '',
    address: '', tax_no: '', bank_name: '', bank_account: '', industry: '', note: '',
  })
}

function openCreate(): void {
  editingId.value = null
  resetForm()
  dialog.value = true
}

function openEdit(row: Customer): void {
  editingId.value = row.id
  Object.assign(form, {
    name: row.name,
    short_name: row.short_name ?? '',
    contact_name: row.contact_name ?? '',
    contact_phone: row.contact_phone ?? '',
    contact_email: row.contact_email ?? '',
    address: row.address ?? '',
    tax_no: row.tax_no ?? '',
    bank_name: row.bank_name ?? '',
    bank_account: row.bank_account ?? '',
    industry: row.industry ?? '',
    note: row.note ?? '',
  })
  dialog.value = true
}

async function save(): Promise<void> {
  if (!form.name.trim()) {
    ElMessage.warning('客户名称必填')
    return
  }
  saving.value = true
  try {
    const payload = { ...form }
    if (editingId.value) {
      await customerApi.update(editingId.value, payload)
      ElMessage.success('客户已更新')
    } else {
      await customerApi.create(payload)
      ElMessage.success('客户已创建')
    }
    dialog.value = false
    await load()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    saving.value = false
  }
}

async function deactivate(row: Customer): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `停用客户「${row.name}」？\n\n停用后新建项目时选不到它，但历史项目里的客户名保留不变（不会删数据）。`,
      '停用客户',
      { type: 'warning', confirmButtonText: '停用', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await customerApi.deactivate(row.id)
    ElMessage.success('已停用')
    await load()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

function search(): void {
  query.page = 1
  void load()
}

onMounted(async () => {
  canWrite.value = auth.can('customer', 'create')
  await load()
})
</script>

<template>
  <div class="page">
    <div class="card">
      <div class="card-title">客户档案</div>
      <el-form inline class="mb" @submit.prevent>
        <el-form-item label="关键词">
          <el-input
            v-model="query.keyword"
            placeholder="名称 / 简称 / 编号 / 联系人"
            clearable
            style="width: 240px"
            @keyup.enter="search"
            @clear="search"
          />
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="query.include_inactive" @change="search">
            含已停用
          </el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="search">
            <el-icon><Search /></el-icon>&nbsp;查询
          </el-button>
        </el-form-item>
        <el-form-item v-if="canWrite">
          <el-button type="primary" plain @click="openCreate">
            <el-icon><Plus /></el-icon>&nbsp;新建客户
          </el-button>
        </el-form-item>
        <el-form-item>
          <span class="text-muted">共 {{ total }} 家</span>
        </el-form-item>
      </el-form>

      <el-table v-loading="loading" :data="rows" border size="small">
        <el-table-column prop="code" label="编号" width="100">
          <template #default="{ row }"><span class="mono">{{ row.code }}</span></template>
        </el-table-column>
        <el-table-column prop="name" label="客户名称" min-width="200" show-overflow-tooltip />
        <el-table-column prop="short_name" label="简称" width="120" />
        <el-table-column prop="contact_name" label="联系人" width="100" />
        <el-table-column prop="contact_phone" label="电话" width="140">
          <template #default="{ row }"><span class="mono">{{ row.contact_phone || '—' }}</span></template>
        </el-table-column>
        <el-table-column prop="industry" label="行业" width="110" />
        <el-table-column prop="tax_no" label="税号" min-width="160" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.tax_no || '—' }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag size="small" effect="plain" :type="row.is_active ? 'success' : 'info'">
              {{ row.is_active ? '在用' : '已停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" class-name="col-no-print">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button
              v-if="row.is_active"
              link
              type="danger"
              size="small"
              @click="deactivate(row)"
            >
              停用
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有客户档案" :image-size="60" />
        </template>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.page_size"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          @current-change="load"
          @size-change="search"
        />
      </div>
    </div>

    <el-dialog v-model="dialog" :title="editingId ? '编辑客户' : '新建客户'" width="640">
      <el-form label-width="90px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="客户名称" required>
              <el-input v-model="form.name" placeholder="营业执照全称" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="简称">
              <el-input v-model="form.short_name" placeholder="列表与图表里显示" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="联系人">
              <el-input v-model="form.contact_name" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="电话">
              <el-input v-model="form.contact_phone" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="邮箱">
              <el-input v-model="form.contact_email" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="行业">
              <el-input v-model="form.industry" placeholder="如：商业综合体 / 医院" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="地址">
              <el-input v-model="form.address" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="税号">
              <el-input v-model="form.tax_no" placeholder="开票用" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="开户行">
              <el-input v-model="form.bank_name" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="账号">
              <el-input v-model="form.bank_account" />
            </el-form-item>
          </el-col>
          <el-col :span="24">
            <el-form-item label="备注">
              <el-input v-model="form.note" type="textarea" :rows="2" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  padding: 12px;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
</style>
