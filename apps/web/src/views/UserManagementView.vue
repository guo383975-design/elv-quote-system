<script setup lang="ts">
/**
 * 用户管理：管理员创建账号、分配角色、改角色、停用/启用。
 *
 * 权限边界：菜单按 user.read（admin / finance）显示；建号/改号/停用
 * 按钮按 user.create / user.update / user.delete 控制，只有 admin 能写。
 *
 * 2026-10-02：改成**左树右列表** —— 原来所有用户平铺在一起，人一多就只能靠
 * 关键字搜。现在左侧是组织架构树（公司 → 部门 → 组），点任意节点看该节点
 * **及其全部下级**的人；节点后面是人数。组织架构由管理员在树上直接维护。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { authApi } from '@/api'
import { extractErrorMessage } from '@/api/client'
import type { Department, DepartmentTree, User } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'
import { dateTime } from '@/utils/format'

const auth = useAuthStore()
const meta = useMetaStore()

const UNASSIGNED_ID = '__none__'

const loading = ref(false)
const rows = ref<User[]>([])
const total = ref(0)
const query = reactive({
  keyword: '',
  page: 1,
  page_size: 20,
  /** 组织架构当前节点 */
  department_id: '',
  unassigned: false,
})

const canCreate = computed(() => auth.can('user', 'create'))
const canUpdate = computed(() => auth.can('user', 'update'))
const canDelete = computed(() => auth.can('user', 'delete'))

// 角色清单（2026-10-05 修「下拉选不了」✓）：
//   以前用 meta.options('role') ✗ —— 那是静态枚举，认不出**自定义角色** ✓，
//   缺分组时还会整个空掉 ✓（表现为"下拉打开了却没有可选项" ✓）。
//   现在用后端 GET /users/roles ✓（内置 + 自定义都齐 ✓）；失败才回退 ✓。
const roleList = ref<Array<{ key: string; name: string }>>([])

const roleOptions = computed(() => {
  if (roleList.value.length) {
    return roleList.value.map((r) => ({ value: r.key, label: r.name }))
  }
  return meta.options('role')
})

async function loadRoles(): Promise<void> {
  try {
    roleList.value = await authApi.roleOptions()
  } catch {
    roleList.value = []   // 回退到 meta ✓，不至于全空 ✗
  }
}

/** 角色中文名（自定义角色也能显示 ✓） */
void loadRoles()   // 进页面就拉角色清单 ✓（2026-10-05）

function roleLabel(key: string | null | undefined): string {
  if (!key) return '—'
  return roleList.value.find((r) => r.key === key)?.name ?? meta.label('role', key)
}

const STATUS_OPTIONS = [
  { value: 'active', label: '正常' },
  { value: 'disabled', label: '已停用' },
  { value: 'pending', label: '待激活' },
]

const dialogVisible = ref(false)
const editing = ref<User | null>(null)
const saving = ref(false)
const form = reactive({
  username: '',
  email: '',
  password: '',
  full_name: '',
  phone: '',
  /** 组织架构节点；自由文本的 department 由后端按节点名反推，前端不再手填 */
  department_id: null as string | null,
  role: 'sales',
  status: 'active',
})

// ---------------------------------------------------------------------------
// 组织架构树
// ---------------------------------------------------------------------------

const tree = ref<DepartmentTree | null>(null)
const treeRef = ref()
const treeFilter = ref('')
const currentDept = ref<{ id: string; path: string } | null>(null)

const canMaintain = computed(() => auth.can('user', 'create'))
const treeProps = { label: 'name', children: 'children' }

/** 树上多挂一个「未分配」伪节点，让没归属部门的人也能被找出来。 */
const treeData = computed<Department[]>(() => {
  const nodes = tree.value?.nodes ?? []
  const unassigned = tree.value?.unassigned ?? 0
  if (!unassigned) return nodes
  const ghost: Department = {
    id: UNASSIGNED_ID,
    parent_id: null,
    code: UNASSIGNED_ID,
    name: '未分配',
    sort_order: 9999,
    is_active: true,
    notes: null,
    path: '未分配',
    user_count: unassigned,
    total_count: unassigned,
    children: [],
  }
  return [...nodes, ghost]
})

/** 表单里的部门选择器用真实节点（不要「未分配」伪节点）。 */
const selectableTree = computed<Department[]>(() => tree.value?.nodes ?? [])

async function loadTree(): Promise<void> {
  try {
    tree.value = await authApi.departmentTree()
  } catch {
    tree.value = null
  }
}

function nodeLabel(data: Department): string {
  const n = data.total_count ?? 0
  return n > 0 ? `${data.name}（${n}）` : data.name
}

function findNode(id: string): Department | null {
  const walk = (nodes: Department[]): Department | null => {
    for (const n of nodes) {
      if (n.id === id) return n
      const hit = walk(n.children ?? [])
      if (hit) return hit
    }
    return null
  }
  return walk(tree.value?.nodes ?? [])
}

function onTreeClick(data: Department): void {
  if (data.id === UNASSIGNED_ID) {
    currentDept.value = { id: UNASSIGNED_ID, path: '未分配' }
    query.department_id = ''
    query.unassigned = true
  } else {
    currentDept.value = { id: data.id, path: data.path || data.name }
    query.department_id = data.id
    query.unassigned = false
  }
  resetAndLoad()
}

function clearDept(): void {
  currentDept.value = null
  query.department_id = ''
  query.unassigned = false
  treeRef.value?.setCurrentKey(null)
  resetAndLoad()
}

function filterTreeNode(value: string, data: Department): boolean {
  if (!value) return true
  return data.name.includes(value) || (data.path ?? '').includes(value)
}

watch(treeFilter, (v) => treeRef.value?.filter(v))

/** 当前选中节点（含下级）的人数，给顶部标签用。 */
const currentNodeCount = computed(() => {
  const id = currentDept.value?.id
  if (!id) return tree.value?.total ?? 0
  if (id === UNASSIGNED_ID) return tree.value?.unassigned ?? 0
  return findNode(id)?.total_count ?? 0
})

// ----- 组织架构维护 -----

const deptDialogVisible = ref(false)
const deptSaving = ref(false)
const deptForm = reactive({
  id: '',
  parent_id: null as string | null,
  parent_name: '',
  code: '',
  name: '',
  sort_order: 100,
  is_active: true,
})

/** 子节点代号：父代号 + 名字内容哈希（与设备分类树同思路，幂等）。 */
function suggestCode(parentCode: string, name: string): string {
  let h = 0
  for (const ch of name) h = (h * 31 + (ch.codePointAt(0) ?? 0)) % 0xffffffff
  return `${parentCode}.c${h.toString(16).slice(0, 8)}`
}

function openDeptCreate(parent: Department | null): void {
  Object.assign(deptForm, {
    id: '',
    parent_id: parent && parent.id !== UNASSIGNED_ID ? parent.id : null,
    parent_name: parent && parent.id !== UNASSIGNED_ID ? parent.name : '',
    code: '',
    name: '',
    sort_order: 100,
    is_active: true,
  })
  deptDialogVisible.value = true
}

function openDeptEdit(node: Department): void {
  Object.assign(deptForm, {
    id: node.id,
    parent_id: node.parent_id,
    parent_name: '',
    code: node.code,
    name: node.name,
    sort_order: node.sort_order,
    is_active: node.is_active,
  })
  deptDialogVisible.value = true
}

async function saveDept(): Promise<void> {
  const name = deptForm.name.trim()
  if (!name) {
    ElMessage.warning('请填写部门名称')
    return
  }
  deptSaving.value = true
  try {
    if (deptForm.id) {
      await authApi.updateDepartment(deptForm.id, {
        name,
        sort_order: deptForm.sort_order,
        is_active: deptForm.is_active,
      })
      ElMessage.success('部门已更新')
    } else {
      const parent = deptForm.parent_id ? findNode(deptForm.parent_id) : null
      await authApi.createDepartment({
        code: parent ? suggestCode(parent.code, name) : localCode(name),
        name,
        parent_id: deptForm.parent_id,
        sort_order: deptForm.sort_order,
      })
      ElMessage.success('部门已创建')
    }
    deptDialogVisible.value = false
    await Promise.all([loadTree(), load()])
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    deptSaving.value = false
  }
}

/** 顶级部门代号：dept.<内容哈希>，稳定且不与既有冲突。 */
function localCode(name: string): string {
  return suggestCode('dept', name)
}

async function removeDept(node: Department): Promise<void> {
  const ok = await ElMessageBox.confirm(
    `删除部门「${node.path || node.name}」？\n` +
      '只有在它没有下级部门、也没有人的时候才能删除（后端会拦住并说明原因）。',
    '删除部门',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  )
    .then(() => true)
    .catch(() => false)
  if (!ok) return
  try {
    await authApi.removeDepartment(node.id)
    ElMessage.success('部门已删除')
    if (currentDept.value?.id === node.id) clearDept()
    await Promise.all([loadTree(), load()])
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

function statusTag(status: string): 'success' | 'danger' | 'info' {
  if (status === 'active') return 'success'
  if (status === 'disabled') return 'danger'
  return 'info'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await authApi.listUsers({
      page: query.page,
      page_size: query.page_size,
      keyword: query.keyword || undefined,
      department_id: query.department_id || undefined,
      unassigned: query.unassigned || undefined,
    })
    rows.value = page.items
    total.value = page.total
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    loading.value = false
  }
}

function resetAndLoad(): void {
  query.page = 1
  void load()
}

function openCreate(): void {
  editing.value = null
  Object.assign(form, {
    username: '',
    email: '',
    password: '',
    full_name: '',
    phone: '',
    // 默认落在当前选中的部门上，少点一次
    department_id:
      currentDept.value && currentDept.value.id !== UNASSIGNED_ID
        ? currentDept.value.id
        : null,
    role: 'sales',
    status: 'active',
  })
  dialogVisible.value = true
}

function openEdit(row: User): void {
  editing.value = row
  Object.assign(form, {
    username: row.username,
    email: row.email,
    password: '',
    full_name: row.full_name ?? '',
    phone: row.phone ?? '',
    department_id: row.department_id,
    role: row.role,
    status: row.status,
  })
  dialogVisible.value = true
}

async function save(): Promise<void> {
  if (!form.username.trim() || !form.email.trim()) {
    ElMessage.warning('请填写用户名和邮箱')
    return
  }
  if (!editing.value && !form.password) {
    ElMessage.warning('新建账号需要填写初始密码')
    return
  }
  saving.value = true
  try {
    if (editing.value) {
      const payload: Record<string, unknown> = {
        email: form.email.trim(),
        full_name: form.full_name.trim() || null,
        phone: form.phone.trim() || null,
        // 只提交部门节点：自由文本的 department 由后端按节点名反推
        department_id: form.department_id,
        role: form.role,
        status: form.status,
      }
      if (form.password) payload.password = form.password
      await authApi.updateUser(editing.value.id, payload)
      ElMessage.success('已保存')
    } else {
      await authApi.createUser({
        username: form.username.trim(),
        email: form.email.trim(),
        password: form.password,
        full_name: form.full_name.trim() || null,
        phone: form.phone.trim() || null,
        department_id: form.department_id,
        role: form.role,
        status: form.status,
      })
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await Promise.all([load(), loadTree()])
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    saving.value = false
  }
}

async function toggleStatus(row: User): Promise<void> {
  const enable = row.status !== 'active'
  const action = enable ? '启用' : '停用'
  try {
    await ElMessageBox.confirm(`确认${action}账号「${row.username}」？`, '提示', { type: 'warning' })
  } catch {
    return
  }
  try {
    if (enable) {
      await authApi.updateUser(row.id, { status: 'active' })
    } else {
      // 后端 DELETE /users/{id} 是软删除（置为已停用）
      await authApi.deleteUser(row.id)
    }
    ElMessage.success(`已${action}`)
    await load()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

onMounted(() => {
  void meta.load()
  void loadTree()
  void load()
})
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h2 class="page-title">用户管理</h2>
        <p class="page-sub">
          共 {{ total }} 个账号
          <template v-if="currentDept">
            · 当前部门 <b>{{ currentDept.path }}</b>（{{ currentNodeCount }} 人）
          </template>
          · 角色决定可见菜单与可执行操作
        </p>
      </div>
      <el-button v-if="canCreate" type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>&nbsp;新建用户
      </el-button>
    </div>

    <div class="user-layout">
      <!-- 左侧：组织架构树。点父部门能看到下面所有组里的人。 -->
      <aside class="card dept-panel">
        <div class="dept-head">
          <span>组织架构</span>
          <el-button v-if="canMaintain" link type="primary" @click="openDeptCreate(null)">
            + 顶级部门
          </el-button>
        </div>
        <el-input
          v-model="treeFilter"
          size="small"
          placeholder="搜索部门"
          clearable
          class="mb"
        />
        <el-tree
          ref="treeRef"
          :data="treeData"
          :props="treeProps"
          node-key="id"
          highlight-current
          default-expand-all
          :expand-on-click-node="false"
          :filter-node-method="filterTreeNode"
          @node-click="onTreeClick"
        >
          <template #default="{ data }">
            <span class="dept-node">
              <span class="dept-label">{{ nodeLabel(data) }}</span>
              <span v-if="canMaintain && data.id !== '__none__'" class="dept-ops">
                <el-icon title="加下级部门" @click.stop="openDeptCreate(data)">
                  <Plus />
                </el-icon>
                <el-icon title="编辑部门" @click.stop="openDeptEdit(data)">
                  <Edit />
                </el-icon>
                <el-icon title="删除部门" @click.stop="removeDept(data)">
                  <Delete />
                </el-icon>
              </span>
            </span>
          </template>
        </el-tree>
        <div class="dept-foot">
          <el-button link @click="clearDept">显示全部</el-button>
          <span class="text-muted">共 {{ tree?.total ?? 0 }} 人</span>
        </div>
      </aside>

      <section class="user-main">
    <div class="card">
      <el-form inline class="inline-form" @submit.prevent>
        <el-form-item label="关键字">
          <el-input
            v-model="query.keyword"
            placeholder="用户名 / 姓名 / 邮箱"
            clearable
            style="width: 240px"
            @keyup.enter="resetAndLoad"
            @clear="resetAndLoad"
          />
        </el-form-item>
        <el-form-item label="当前部门">
          <el-tag v-if="currentDept" closable type="info" @close="clearDept">
            {{ currentDept.path }}（{{ currentNodeCount }}）
          </el-tag>
          <span v-else class="text-muted">全部（左侧选部门）</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="resetAndLoad">
            <el-icon><Search /></el-icon>&nbsp;查询
          </el-button>
        </el-form-item>
      </el-form>

      <el-table v-loading="loading" :data="rows" border stripe>
        <el-table-column prop="username" label="用户名" min-width="120" />
        <el-table-column label="姓名" min-width="100">
          <template #default="{ row }">{{ row.full_name || '—' }}</template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="190" show-overflow-tooltip />
        <el-table-column label="手机" min-width="120">
          <template #default="{ row }">{{ row.phone || '—' }}</template>
        </el-table-column>
        <el-table-column label="部门" min-width="180">
          <template #default="{ row }">
            <span v-if="row.department_path">{{ row.department_path }}</span>
            <span v-else class="text-muted">未分配</span>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ roleLabel(row.role) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">
              {{ meta.label('user_status', row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后登录" min-width="150">
          <template #default="{ row }">{{ dateTime(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column v-if="canUpdate || canDelete" label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button v-if="canUpdate" type="primary" link @click="openEdit(row)">
              编辑
            </el-button>
            <el-button
              v-if="canDelete && row.username !== auth.user?.username"
              :type="row.status === 'active' ? 'danger' : 'success'"
              link
              @click="toggleStatus(row)"
            >
              {{ row.status === 'active' ? '停用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无账号，点击右上角「新建用户」创建" />
        </template>
      </el-table>

      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.page_size"
        :total="total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        class="pager"
        @current-change="load"
        @size-change="resetAndLoad"
      />
    </div>
      </section>
    </div>

    <!-- 部门维护（加下级 / 改名 / 停用） -->
    <el-dialog
      v-model="deptDialogVisible"
      :title="deptForm.id ? '编辑部门' : deptForm.parent_id ? '新建下级部门' : '新建顶级部门'"
      width="440px"
    >
      <el-form label-width="96px">
        <el-form-item v-if="deptForm.parent_id" label="上级部门">
          <span>{{ deptForm.parent_name || findNode(deptForm.parent_id)?.path || '—' }}</span>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="deptForm.name" placeholder="如 设计部 / 智能化设计组" />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="deptForm.sort_order" :min="0" :max="9999" />
        </el-form-item>
        <el-form-item v-if="deptForm.id" label="启用">
          <el-switch v-model="deptForm.is_active" />
        </el-form-item>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="部门代号由名称自动生成且不可改（历史数据与外部对照按它对齐）。"
        />
      </el-form>
      <template #footer>
        <el-button @click="deptDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="deptSaving" @click="saveDept">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="dialogVisible"
      :title="editing ? '编辑用户' : '新建用户'"
      width="560px"
      :close-on-click-modal="false"
    >
      <el-form :model="form" label-width="88px">
        <el-form-item label="用户名" required>
          <el-input v-model="form.username" :disabled="!!editing" placeholder="3-64 字符" />
        </el-form-item>
        <el-form-item label="邮箱" required>
          <el-input v-model="form.email" placeholder="用于登录与找回" />
        </el-form-item>
        <el-form-item :label="editing ? '重置密码' : '初始密码'" :required="!editing">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            :placeholder="editing ? '留空则不修改' : '至少 8 位'"
          />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="form.full_name" />
        </el-form-item>
        <el-form-item label="手机">
          <el-input v-model="form.phone" />
        </el-form-item>
        <el-form-item label="所属部门">
          <!-- 部门是树上的一个节点；旧的那个自由文本「部门」输入框已去掉，
               否则同一个部门会被写出"设计部/设计/设计中心"三种名字 -->
          <el-tree-select
            v-model="form.department_id"
            :data="selectableTree"
            node-key="id"
            :props="treeProps"
            value-key="id"
            default-expand-all
            check-strictly
            clearable
            placeholder="选择部门（可多级）"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="角色" required>
          <el-select v-model="form.role" style="width: 100%">
            <el-option v-for="o in roleOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="form.status" style="width: 100%">
            <el-option v-for="s in STATUS_OPTIONS" :key="s.value" :label="s.label" :value="s.value" />
          </el-select>
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
/* ---------- 组织架构树 + 列表的两列布局 ---------- */
.user-layout {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.dept-panel {
  width: 250px;
  flex: 0 0 250px;
  /* 树长起来时自己滚动，不把整页撑高 */
  max-height: calc(100vh - 240px);
  overflow: auto;
  padding: 10px 12px;
}

.user-main {
  flex: 1 1 auto;
  min-width: 0;
}

.dept-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 8px;
}

.dept-node {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding-right: 4px;
}

.dept-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 操作图标平时淡一点，悬停到节点上才明显，避免树上到处是小图标 */
.dept-ops {
  display: none;
  gap: 6px;
  color: var(--el-color-primary);
}

.dept-node:hover .dept-ops {
  display: inline-flex;
}

.dept-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--el-border-color-lighter);
  font-size: 12px;
}
</style>
