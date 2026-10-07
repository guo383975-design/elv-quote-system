<script setup lang="ts">
/**
 * 设备库（对应原型 Page 2 添加设备 / Page 5 停产筛选 / 场景一 Excel 导入）。
 *
 * 停产闭环在这里收口：
 *   - 列表用状态筛选 + 红色行标出停产型号
 *   - 新增/编辑时若把状态设为已停产却没填替代型号，前端直接拦住（后端只 warning）
 *   - 顶部统计条显示停产占比，避免「库里堆了一堆买不到的型号」没人发现
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onActivated, onMounted, reactive, ref, watch } from 'vue'

import { deviceApi, importApi } from '@/api'
import DevicePickerDialog from '@/components/DevicePickerDialog.vue'
import { extractErrorMessage } from '@/api/client'
import type {
  AccessoryGroup,
  BulkPriceResult,
  Device,
  DeviceCategoryNode,
  DeviceCategoryTree,
  DeviceStats,
  DuplicatePolicy,
  ImportBatch,
  ImportBatchDetail,
  PriceTierInput,
} from '@/api/types'
import { dateTime, money } from '@/utils/format'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'

const auth = useAuthStore()
const router = useRouter()
const meta = useMetaStore()

const loading = ref(false)
const exporting = ref(false)

// ----- 批量调价（原型第 5 页工具栏）-----
const bulkVisible = ref(false)
const bulkBusy = ref(false)
const bulkPreviewed = ref(false)
const selectedRows = ref<Device[]>([])
const bulk = reactive({
  field: 'reference_price' as 'reference_price' | 'cost_price',
  mode: 'percent' as 'percent' | 'amount',
  value: 5,
  round_yuan: true,
})
const bulkResult = ref<BulkPriceResult | null>(null)

function onSelectionChange(rows: Device[]): void {
  selectedRows.value = rows
}

function openBulk(): void {
  bulkPreviewed.value = false
  bulkResult.value = null
  bulkVisible.value = true
}

async function runBulk(dryRun: boolean): Promise<void> {
  if (!selectedRows.value.length) {
    ElMessage.warning('先在表格里勾选要调价的设备')
    return
  }
  bulkBusy.value = true
  try {
    const res = await deviceApi.bulkPrice({
      device_ids: selectedRows.value.map((r) => r.id),
      field: bulk.field,
      mode: bulk.mode,
      value: bulk.value,
      round_yuan: bulk.round_yuan,
      dry_run: dryRun,
    })
    bulkResult.value = res
    if (dryRun) {
      bulkPreviewed.value = true
      ElMessage.success(`已算出 ${res.count} 行的变更，确认后才会写入`)
    } else {
      ElMessage.success(`已调价 ${res.count} 行`)
      bulkVisible.value = false
      selectedRows.value = []
      await Promise.all([load(), loadStats()])
    }
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    bulkBusy.value = false
  }
}

/** 品牌下拉：后端没有品牌字典端点，用当前页出现过的品牌（外加已选中的）凑够可用性。 */
const brandOptions = computed(() => {
  const set = new Set(rows.value.map((r) => r.brand).filter(Boolean) as string[])
  if (query.brand) set.add(query.brand)
  return [...set].sort()
})
const rows = ref<Device[]>([])
const total = ref(0)
const stats = ref<DeviceStats | null>(null)

const query = reactive({
  keyword: '',
  /** 分类树的当前节点（设备库现在按树浏览，见下方 tree 相关逻辑） */
  category_id: '',
  uncategorized: false,
  status: '',
  only_common: false,
  /** 原型第 5 页的筛选条：品牌 / 仅看停产有替代 / 仅看甲供设备 */
  brand: '',
  only_eol_with_replacement: false,
  only_owner_supplied: false,
  page: 1,
  page_size: 20,
})

/** 右侧「设备详情」面板（原型第 5 页右侧那一栏）。 */
const detail = ref<Device | null>(null)

function onRowClick(row: Device): void {
  detail.value = row
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await deviceApi.list({
      keyword: query.keyword || undefined,
      category_id: query.category_id || undefined,
      uncategorized: query.uncategorized || undefined,
      status: query.status || undefined,
      only_common: query.only_common || undefined,
      brand: query.brand || undefined,
      is_owner_supplied: query.only_owner_supplied ? true : undefined,
      has_replacement: query.only_eol_with_replacement ? true : undefined,
      page: query.page,
      page_size: query.page_size,
    })
    rows.value = page.items
    total.value = page.total
  } finally {
    loading.value = false
  }
}

// ---------------------------------------------------------------------------
// 设备分类树（2026-09-30）
//
// 以前"分类"是一个扁平下拉（12 个一级分类），几百条设备全堆在一起。
// 现在左侧是一棵树：
//   · 点父节点 → 后端按**子树**筛选（list_devices 的 include_descendants 默认开）
//   · 每个节点后面跟着"含子树的设备数"
//   · 有权限的人可以在树上直接加子类 / 改名 / 停用 / 删除
// ---------------------------------------------------------------------------

const UNCATEGORIZED_ID = '__none__'

const tree = ref<DeviceCategoryTree | null>(null)
const treeRef = ref()
const treeFilter = ref('')
const currentCategory = ref<{ id: string; path: string } | null>(null)

// 分类树的展开状态（2026-10-05 用户要求「默认不展开」）：
// 不用 el-tree 的 `default-expand-all` —— 它只在**首次渲染**生效，而树数据是
// 异步来的（初始为空）→ 表现成"时而展开时而收起" ✗。
// 改成：渲染时读一次本地记忆，没有记忆就全部收起 ✓；用户展开过哪些记下来 ✓。
const TREE_OPEN_KEY = 'device-tree-expanded'
const treeExpandedKeys = ref<string[]>([])
try {
  const savedExpanded = localStorage.getItem(TREE_OPEN_KEY)
  if (savedExpanded) treeExpandedKeys.value = JSON.parse(savedExpanded) as string[]
} catch {
  treeExpandedKeys.value = []      // 记忆坏了就当没展开过，不影响使用
}

/** 展开/收起后记一下（只记 node-key，不记整棵树） */
function rememberTreeExpand(): void {
  const t = treeRef.value as { getExpandedKeys?: () => string[] } | null
  const keys = t?.getExpandedKeys?.() ?? []
  treeExpandedKeys.value = keys
  try {
    localStorage.setItem(TREE_OPEN_KEY, JSON.stringify(keys))
  } catch {
    /* 隐身模式等写不了 localStorage：静默忽略，不影响功能 */
  }
}

const canMaintain = computed(() => auth.can('device_library', 'create'))
const canDeleteCategory = computed(() => auth.can('device_library', 'delete'))

const treeProps = { label: 'name', children: 'children' }

/** 表单里的分类选择器用真实节点（不要那个「未分类」伪节点）。 */
const selectableTree = computed<DeviceCategoryNode[]>(() => tree.value?.nodes ?? [])

/** 树上多挂一个「未分类」伪节点，让没归属的设备也能被找出来。 */
const treeData = computed<DeviceCategoryNode[]>(() => {  const nodes = tree.value?.nodes ?? []
  const unassigned = tree.value?.unassigned ?? 0
  if (!unassigned) return nodes
  const ghost: DeviceCategoryNode = {
    id: UNCATEGORIZED_ID,
    parent_id: null,
    code: UNCATEGORIZED_ID,
    name: '未分类',
    sort_order: 9999,
    is_active: true,
    legacy_enum: null,
    code_prefix: null,
    notes: null,
    path: '未分类',
    device_count: unassigned,
    total_count: unassigned,
    children: [],
  }
  return [...nodes, ghost]
})

async function loadTree(): Promise<void> {  try {
    tree.value = await deviceApi.categoryTree()
  } catch {
    tree.value = null
  }
}

function nodeLabel(data: DeviceCategoryNode): string {
  const n = data.total_count ?? 0
  return n > 0 ? `${data.name}（${n}）` : data.name
}

function findNode(id: string): DeviceCategoryNode | null {
  const walk = (nodes: DeviceCategoryNode[]): DeviceCategoryNode | null => {
    for (const n of nodes) {
      if (n.id === id) return n
      const hit = walk(n.children ?? [])
      if (hit) return hit
    }
    return null
  }
  return walk(tree.value?.nodes ?? [])
}

function onTreeClick(data: DeviceCategoryNode): void {
  if (data.id === UNCATEGORIZED_ID) {
    currentCategory.value = { id: UNCATEGORIZED_ID, path: '未分类' }
    query.category_id = ''
    query.uncategorized = true
  } else {
    currentCategory.value = { id: data.id, path: data.path || data.name }
    query.category_id = data.id
    query.uncategorized = false
  }
  resetAndLoad()
}

function clearCategory(): void {
  currentCategory.value = null
  query.category_id = ''
  query.uncategorized = false
  treeRef.value?.setCurrentKey(null)
  resetAndLoad()
}

function filterTreeNode(value: string, data: DeviceCategoryNode): boolean {
  if (!value) return true
  return data.name.includes(value) || (data.path ?? '').includes(value)
}

watch(treeFilter, (v) => treeRef.value?.filter(v))

/** 从树的某个节点反查它所属的一级分类（写设备时用它填 category）。 */
function rootEnumOfNode(nodeId: string | null): string | null {
  if (!nodeId) return null
  const walk = (nodes: DeviceCategoryNode[], inherited: string | null): string | null => {
    for (const n of nodes) {
      const own = n.legacy_enum ?? inherited
      if (n.id === nodeId) return own
      const hit = walk(n.children ?? [], own)
      if (hit) return hit
    }
    return null
  }
  return walk(tree.value?.nodes ?? [], null)
}

/** 当前选中节点（含子树）的设备数，给页头用。 */
const currentNodeCount = computed(() => {
  const id = currentCategory.value?.id
  if (!id) return tree.value?.total ?? 0
  if (id === UNCATEGORIZED_ID) return tree.value?.unassigned ?? 0
  return findNode(id)?.total_count ?? 0
})

// ---------- 分类维护 ----------

const catDialogVisible = ref(false)
const catSaving = ref(false)
const catForm = reactive({
  id: '',
  parent_id: null as string | null,
  parent_name: '',
  code: '',
  name: '',
  legacy_enum: '',
  sort_order: 100,
  is_active: true,
})

/** 子分类代号：父代号 + 名字内容哈希，保证同名同父得到同一个代号（与后端同思路）。 */
function suggestCode(parentCode: string, name: string): string {
  let h = 0
  for (const ch of name) h = (h * 31 + (ch.codePointAt(0) ?? 0)) % 0xffffffff
  return `${parentCode}.c${h.toString(16).slice(0, 8)}`
}

function openCatCreate(parent: DeviceCategoryNode | null): void {
  Object.assign(catForm, {
    id: '',
    parent_id: parent && parent.id !== UNCATEGORIZED_ID ? parent.id : null,
    parent_name: parent && parent.id !== UNCATEGORIZED_ID ? parent.name : '',
    code: '',
    name: '',
    legacy_enum: '',
    sort_order: 100,
    is_active: true,
  })
  catDialogVisible.value = true
}

function openCatEdit(node: DeviceCategoryNode): void {
  Object.assign(catForm, {
    id: node.id,
    parent_id: node.parent_id,
    parent_name: '',
    code: node.code,
    name: node.name,
    legacy_enum: node.legacy_enum ?? '',
    sort_order: node.sort_order,
    is_active: node.is_active,
  })
  catDialogVisible.value = true
}

async function saveCategory(): Promise<void> {
  const name = catForm.name.trim()
  if (!name) {
    ElMessage.warning('请填写分类名称')
    return
  }
  catSaving.value = true
  try {
    if (catForm.id) {
      await deviceApi.updateCategory(catForm.id, {
        name,
        sort_order: catForm.sort_order,
        is_active: catForm.is_active,
      })
      ElMessage.success('分类已更新')
    } else if (catForm.parent_id) {
      const parent = findNode(catForm.parent_id)
      await deviceApi.createCategory({
        code: suggestCode(parent?.code ?? 'cat', name),
        name,
        parent_id: catForm.parent_id,
        sort_order: catForm.sort_order,
      })
      ElMessage.success('子分类已创建')
    } else {
      if (!catForm.legacy_enum) {
        ElMessage.warning('一级分类必须指定它对应的一级分类取值')
        catSaving.value = false
        return
      }
      await deviceApi.createCategory({
        code: catForm.legacy_enum,
        name,
        legacy_enum: catForm.legacy_enum,
        sort_order: catForm.sort_order,
      })
      ElMessage.success('一级分类已创建')
    }
    catDialogVisible.value = false
    await Promise.all([loadTree(), load()])
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    catSaving.value = false
  }
}

async function removeCategory(node: DeviceCategoryNode): Promise<void> {
  const ok = await ElMessageBox.confirm(
    `删除分类「${node.path || node.name}」？\n` +
      '只有在它没有子分类、也没有挂设备时才能删除（后端会拦住并说明原因）。',
    '删除分类',
    { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
  )
    .then(() => true)
    .catch(() => false)
  if (!ok) return
  try {
    await deviceApi.removeCategory(node.id)
    ElMessage.success('分类已删除')
    if (currentCategory.value?.id === node.id) clearCategory()
    await loadTree()
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

async function loadStats(): Promise<void> {
  try {
    stats.value = await deviceApi.stats()
  } catch {
    stats.value = null
  }
}

function resetAndLoad(): void {
  query.page = 1
  void load()
}

// ---------- 导入记录（原型 #IM-xxx）----------
const batchDrawer = ref(false)
const batchLoading = ref(false)
const batchKind = ref<string>('')
const batches = ref<ImportBatch[]>([])
const batchDetailOpen = ref(false)
const batchDetail = ref<ImportBatchDetail | null>(null)

function openBatches(): void {
  batchDrawer.value = true
}

async function loadBatches(): Promise<void> {
  batchLoading.value = true
  try {
    batches.value = await importApi.batches({
      kind: batchKind.value || undefined,
      limit: 100,
    })
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    batchLoading.value = false
  }
}

async function openBatchDetail(row: ImportBatch): Promise<void> {
  try {
    batchDetail.value = await importApi.batchDetail(row.id)
    batchDetailOpen.value = true
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  }
}

onMounted(async () => {
  await Promise.all([meta.load(), loadTree(), load(), loadStats()])
})

/**
 * 这个页面被 `keep-alive` 缓存了（保留筛选与滚动位置），但**数据不能跟着一起缓存**：
 * 别处改了数据、或库被清空之后切回来，看到的必须是**现在**的数据。
 * 真实事故：清库后切回设备库，旧列表还在，用户以为"数据又回来了"。
 */
let firstActivate = true
onActivated(() => {
  if (firstActivate) {
    firstActivate = false
    return
  }
  void Promise.all([loadTree(), load(), loadStats()])
})

/** 停产 + 退市数量，用于顶部提醒。 */
const deadCount = computed(() => {
  const s = stats.value?.by_status ?? {}
  return (s.eol ?? 0) + (s.discontinued ?? 0)
})

const deadRatio = computed(() => {
  const t = stats.value?.total ?? 0
  return t === 0 ? 0 : Math.round((deadCount.value / t) * 100)
})

function rowClassName({ row }: { row: Device }): string {
  return meta.isDiscontinued(row.status) ? 'row-discontinued' : ''
}

// ---------- 新增 / 编辑 ----------

type DeviceForm = {
  id: string
  code: string
  name: string
  brand: string
  model: string
  spec: string
  /** 设备描述 / 性能参数（2026-10-05）：多行文本 */
  description: string
  unit: string
  category: string
  /** 分类树节点：实际归属（一级分类由后端从节点反推） */
  category_id: string | null
  reference_price: number
  cost_price: number
  install_unit_price: number
  min_price: number
  auxiliary_unit_price: number
  status: string
  replacement_id: string | null
  is_owner_supplied: boolean
  is_common: boolean
  warranty_years: number
  moq: number
  eol_at: string | null
}

function emptyForm(): DeviceForm {
  return {
    id: '',
    code: '',
    name: '',
    brand: '',
    model: '',
    spec: '',
    description: '',
    unit: '台',
    category: 'video',
    category_id: null,
    reference_price: 0,
    cost_price: 0,
    install_unit_price: 0,
    min_price: 0,
    auxiliary_unit_price: 0,
    status: 'on_sale',
    replacement_id: null,
    is_owner_supplied: false,
    is_common: false,
    warranty_years: 2,
    moq: 1,
    eol_at: null,
  }
}

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const saving = ref(false)
const formRef = ref()
// ---------- 阶梯价（2026-10-04）----------
// 档位是一组有顺序的数据：区间不能重叠（后端会把关），最后一档 max 留空 = 该档以上。
interface TierRow {
  min_qty: number | null
  max_qty: number | null
  unit_price: number
  note: string
}
const tierRows = ref<TierRow[]>([])

function addTier(): void {
  const last = tierRows.value[tierRows.value.length - 1]
  tierRows.value.push({
    // 新档默认接在上一档后面（省得每次手算边界）
    min_qty: last ? (last.max_qty ?? last.min_qty ?? 0) + 1 : 1,
    max_qty: null,
    unit_price: form.reference_price || 0,
    note: '',
  })
}

function removeTier(idx: number): void {
  tierRows.value.splice(idx, 1)
}

async function loadTiers(deviceId: string): Promise<void> {
  try {
    const tiers = await deviceApi.priceTiers(deviceId)
    tierRows.value = tiers.map((x) => ({
      min_qty: Number(x.min_qty),
      max_qty: x.max_qty === null ? null : Number(x.max_qty),
      unit_price: Number(x.unit_price),
      note: x.note ?? '',
    }))
  } catch {
    tierRows.value = []
  }
}

async function saveTiers(deviceId: string): Promise<void> {
  const payload: PriceTierInput[] = tierRows.value
    .filter((r) => r.min_qty !== null)
    .map((r) => ({
      min_qty: String(r.min_qty),
      max_qty: r.max_qty === null || r.max_qty === undefined ? null : String(r.max_qty),
      unit_price: String(r.unit_price),
      note: r.note || null,
    }))
  if (!payload.length) return
  await deviceApi.replacePriceTiers(deviceId, payload)
}

const form = reactive<DeviceForm>(emptyForm())
const codeLoading = ref(false)

const rules = {
  code: [{ required: true, message: '请输入设备编号', trigger: 'blur' }],
  name: [{ required: true, message: '请输入设备名称', trigger: 'blur' }],
}

/** 替代型号候选：同类别的其它在售设备。 */
const replacementOptions = ref<Device[]>([])

async function loadReplacementOptions(category: string, excludeId: string): Promise<void> {
  try {
    const page = await deviceApi.list({
      category,
      status: 'on_sale',
      page: 1,
      page_size: 200,
    })
    replacementOptions.value = page.items.filter((d) => d.id !== excludeId)
  } catch {
    replacementOptions.value = []
  }
}

async function openCreate(): Promise<void> {
  tierRows.value = []
  dialogMode.value = 'create'
  Object.assign(form, emptyForm())
  dialogVisible.value = true
  replacementOptions.value = []
  // 预填下一个编号，省得用户猜规则（前缀跟着分类树节点走）
  codeLoading.value = true
  try {
    form.code = await deviceApi.nextCode(form.category, form.category_id)
  } catch {
    // 拿不到就让用户自己填
  } finally {
    codeLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// 配件 / 授权（2026-10-05）
// ---------------------------------------------------------------------------
// 场景：买防火墙要买授权，授权有几个档可选；配单时没选要提示。
// 候选是**产品库里的设备**（授权也是设备），所以用远程搜索选。
const accGroups = ref<AccessoryGroup[]>([])
const accSaving = ref(false)

// 加候选走「选产品」弹窗（自带搜索 + 多选），而不是内联单选框：
// 用户反馈内联那个"输入名字不检索、还不能多选"。
const accPickVisible = ref(false)
/** 往哪个槽里加（存对象引用而不是下标：删槽/换序后下标会错位） */
const accPickGroup = ref<AccessoryGroup | null>(null)

function openAccPicker(group: AccessoryGroup): void {
  accPickGroup.value = group
  accPickVisible.value = true
}

/** 弹窗里勾选完 → 一次性把多条候选择加进槽（自动去重） */
function onAccPicked(payload: { devices: { id: string; code: string; name: string;
                                           unit: string }[] }): void {
  const g = accPickGroup.value
  if (!g) return
  let added = 0
  for (const d of payload.devices) {
    if (g.options.some((o) => o.accessory_id === d.id)) continue
    g.options.push({
      accessory_id: d.id, code: d.code, name: d.name, unit: d.unit,
      is_default: false,
    })
    added += 1
  }
  accPickVisible.value = false
  ElMessage.success(added ? `已添加 ${added} 个候选配件` : '选中的配件都已在列表里')
}

async function loadAccessories(deviceId: string): Promise<void> {
  accGroups.value = []
  try {
    const groups = await deviceApi.accessories(deviceId)
    accGroups.value = groups.map((g) => ({
      ...g,
      default_quantity: Number(g.default_quantity ?? 1),
      select_mode: (g.select_mode as 'one' | 'many') ?? 'one',
      quantity_mode: (g.quantity_mode as 'per_unit' | 'fixed') ?? 'per_unit',
      options: g.options.map((o) => ({ ...o })),
    }))
  } catch {
    accGroups.value = []
  }
}

function addAccGroup(): void {
  accGroups.value.push({
    label: '授权', is_required: true, select_mode: 'one',
    quantity_mode: 'per_unit', default_quantity: 1, options: [],
  })
}

async function saveAccessories(): Promise<void> {
  const id = form.id
  if (!id) return
  accSaving.value = true
  try {
    await deviceApi.saveAccessories(
      id,
      accGroups.value.map((g, gi) => ({
        label: g.label,
        is_required: g.is_required,
        select_mode: g.select_mode,
        quantity_mode: g.quantity_mode,
        default_quantity: Number(g.default_quantity ?? 1),
        note: g.note ?? null,
        sort_order: gi,
        is_active: g.is_active ?? true,
        options: g.options
          .filter((o) => o.accessory_id)
          .map((o, oi) => ({
            accessory_id: o.accessory_id, code: o.code, name: o.name,
            unit: o.unit, is_default: o.is_default,
            note: o.note ?? null, sort_order: oi,
          })),
      })),
    )
    ElMessage.success('配件已保存')
    await loadAccessories(id)
  } catch (e) {
    ElMessage.error(e instanceof Error ? e.message : '配件保存失败')
  } finally {
    accSaving.value = false
  }
}

function openEdit(row: Device): void {
  dialogMode.value = 'edit'
  // 载入阶梯价（2026-10-04）：档位是独立接口，编辑时要拉回来
  void loadTiers(row.id)
  // 配件/授权（2026-10-05）：同样是独立接口
  void loadAccessories(row.id)
  Object.assign(form, {
    id: row.id,
    code: row.code,
    name: row.name,
    brand: row.brand ?? '',
    model: row.model ?? '',
    spec: row.spec ?? '',
    description: row.description ?? '',
    unit: row.unit,
    category: row.category,
    category_id: row.category_id,
    reference_price: Number(row.reference_price ?? 0),
    cost_price: Number(row.cost_price ?? 0),
    install_unit_price: Number(row.install_unit_price ?? 0),
    min_price: Number(row.min_price ?? 0),
    auxiliary_unit_price: Number(row.auxiliary_unit_price ?? 0),
    status: row.status,
    replacement_id: row.replacement_id,
    is_owner_supplied: row.is_owner_supplied,
    is_common: row.is_common,
    warranty_years: row.warranty_years ?? 2,
    moq: row.moq ?? 1,
    eol_at: row.eol_at ? row.eol_at.slice(0, 10) : null,
  })
  dialogVisible.value = true
  void loadReplacementOptions(row.category, row.id)
}

/** 切换分类节点：一级分类跟着节点走，编号前缀与替代型号候选都要重算。 */
async function onCategoryChange(): Promise<void> {
  const rootEnum = rootEnumOfNode(form.category_id)
  if (rootEnum) form.category = rootEnum
  if (dialogMode.value === 'create') {
    codeLoading.value = true
    try {
      form.code = await deviceApi.nextCode(form.category, form.category_id)
    } finally {
      codeLoading.value = false
    }
  }
  await loadReplacementOptions(form.category, form.id)
}

/** 停产但没有替代型号 —— 这是「静默的数据腐化」源头，必须拦住。 */
const needsReplacementWarning = computed(
  () => ['eol', 'discontinued', 'near_eol'].includes(form.status) && !form.replacement_id,
)

async function save(): Promise<void> {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  if (needsReplacementWarning.value) {
    // 后端只 warning 不拦截；这里做成「必须显式确认」的软门禁
    const go = await ElMessageBox.confirm(
      '该型号被标记为停产/临近停产，但没有指定替代型号。\n' +
        '没有替代型号时，报价里出现这个型号将无法自动换型。确认继续？',
      '停产但未指定替代型号',
      { confirmButtonText: '仍然保存', cancelButtonText: '返回填写', type: 'warning' },
    )
      .then(() => true)
      .catch(() => false)
    if (!go) return
  }

  saving.value = true
  try {
    const payload: Record<string, unknown> = { ...form }
    delete payload.id
    if (dialogMode.value === 'edit') delete payload.code
    for (const [k, v] of Object.entries(payload)) {
      if (v === '') payload[k] = null
    }

    let savedId = form.id
    if (dialogMode.value === 'create') {
      const created = await deviceApi.create(payload)
      savedId = (created as { id?: string }).id ?? ''
      ElMessage.success('设备已新增')
    } else {
      await deviceApi.update(form.id, payload)
      ElMessage.success('设备已更新')
    }
    // 阶梯价单独一组接口（整体替换）：有档位才调用，避免把已有档位清掉
    if (savedId && tierRows.value.length) {
      try {
        await saveTiers(savedId)
        ElMessage.success('阶梯价已保存')
      } catch (e) {
        ElMessage.warning(`设备已保存，但阶梯价写入失败：${extractErrorMessage(e)}`)
      }
    }
    dialogVisible.value = false
    await Promise.all([load(), loadStats()])
  } finally {
    saving.value = false
  }
}

async function deactivate(row: Device): Promise<void> {
  await ElMessageBox.confirm(
    `停用设备「${row.name}」后，报价单里已引用的行不受影响，但设备库不再可选。确认停用？`,
    '提示',
    { confirmButtonText: '确认停用', cancelButtonText: '取消', type: 'warning' },
  )
    .then(async () => {
      await deviceApi.remove(row.id)
      ElMessage.success('已停用')
      await Promise.all([load(), loadStats()])
    })
    .catch(() => undefined)
}

// ---------- 场景一：设备库 Excel 导入 ----------

const importVisible = ref(false)
const importFile = ref<File | null>(null)
const importPolicy = ref<DuplicatePolicy>('skip')
const importPreview = ref<Awaited<ReturnType<typeof importApi.previewDevices>> | null>(null)
const importBusy = ref(false)
const importStep = ref<'upload' | 'preview' | 'done'>('upload')
const importResult = ref<Awaited<ReturnType<typeof importApi.commitDevices>> | null>(null)

// el-upload 的 :limit="1" + 挂载后不卸载 ⇒ 选过一次文件后，fileList 里还留着那一条，
// 再选新文件时 EP 直接走 on-exceed（我们没绑，默认空函数）把文件丢掉，
// 「on-change」不再触发。表现是：界面还显示旧文件名，点「校验预览」却提示
// 「请先选择 Excel 文件」，不刷新页面就再也导不进去。
// 每次打开弹窗时把 el-upload 的内部文件列表清掉。
const uploadRef = ref<{ clearFiles: () => void } | null>(null)

function openImport(): void {
  importVisible.value = true
  importFile.value = null
  importPreview.value = null
  importResult.value = null
  importStep.value = 'upload'
  uploadRef.value?.clearFiles()
}

function onFileChange(file: { raw?: File }): void {
  importFile.value = file.raw ?? null
  importPreview.value = null
  importStep.value = 'upload'
}

/** :limit="1" 之后再选文件会走这里（默认实现是把文件丢掉）。
 *  改成「新文件替换旧文件」，和用户直觉一致。 */
function onFileExceed(files: File[]): void {
  const next = files[0]
  if (!next) return
  onFileChange({ raw: next })
  uploadRef.value?.clearFiles()
}

async function doPreview(): Promise<void> {
  if (!importFile.value) {
    ElMessage.warning('请先选择 Excel 文件')
    return
  }
  importBusy.value = true
  try {
    importPreview.value = await importApi.previewDevices(importFile.value, importPolicy.value, 100)
    importStep.value = 'preview'
  } finally {
    importBusy.value = false
  }
}

async function doCommit(): Promise<void> {
  if (!importFile.value) return
  importBusy.value = true
  try {
    importResult.value = await importApi.commitDevices(importFile.value, importPolicy.value, true)
    importStep.value = 'done'
    await Promise.all([load(), loadStats()])
    ElMessage.success('导入完成')
  } finally {
    importBusy.value = false
  }
}

/** 导出设备库（原型第 5 页工具栏；导出的 xlsx 列与导入模板一致，可回灌）。 */
async function exportDevices(): Promise<void> {
  exporting.value = true
  try {
    const { blob, filename } = await deviceApi.exportXlsx()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出：${filename}（可直接改完再导回来）`)
  } catch (e) {
    ElMessage.error(extractErrorMessage(e))
  } finally {
    exporting.value = false
  }
}

/** 下载设备库导入模板（真 xlsx：列与导入/导出一致，填完能直接导回） */
async function downloadTemplate(): Promise<void> {
  try {
    const { blob, filename } = await importApi.downloadDeviceTemplate()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 0)
    ElMessage.success(`模板已下载：${filename}（填完可直接用「Excel 批量导入」传回）`)
  } catch (err) {
    ElMessage.error(extractErrorMessage(err))
  }
}

/** Excel 列序号 → 字母（0 → A）。原型第 6 页映射表左列显示「A 列 设备名称」。 */
function colLetter(idx: number): string {
  let s = ''
  let n = idx + 1
  while (n > 0) {
    const r = (n - 1) % 26
    s = String.fromCharCode(65 + r) + s
    n = Math.floor((n - 1) / 26)
  }
  return s
}

/** 系统字段 → 中文名（原型「映射到系统字段」那列）。 */
const FIELD_LABELS: Record<string, string> = {
  name: '设备名称',
  brand: '品牌',
  model: '型号规格',
  unit: '单位',
  reference_price: '参考单价',
  cost_price: '成本价',
  install_unit_price: '安装单价',
  category: '所属分类',
  moq: '起订量',
  warranty_years: '质保年限',
  supplier: '供应商',
  spec: '规格说明',
  status: '设备状态',
  is_owner_supplied: '是否甲供',
  code: '设备编号',
  notes: '备注',
  subsystem_default: '默认子系统',
}

function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field
}

/** 已选文件大小（原型文件条上有 2.4 MB）。 */
const importFileSize = computed(() => {
  const f = importFile.value
  if (!f) return ''
  const kb = f.size / 1024
  return kb > 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.round(kb)} KB`
})

/** 文件条上的 X：清掉文件回到第 1 步。 */
function clearImportFile(): void {
  importFile.value = null
  importPreview.value = null
  importStep.value = 'upload'
  uploadRef.value?.clearFiles()
}

/** 「重新选择文件」：把 el-upload 内部 input 点开。 */
function pickAgain(): void {
  // el-upload 的实例类型里没有 $el，这里按 DOM 节点取（换个写法避免类型报错）
  const inst = uploadRef.value as unknown as { $el?: HTMLElement } | null
  inst?.$el?.querySelector<HTMLInputElement>('input[type=file]')?.click()
}

/** 原型底部的「校验结果」四张卡（后端 stat: ok/warn/error/discontinued/duplicate）。 */
const verifyCards = computed(() => {
  const s = importPreview.value?.stat ?? {}
  const dis = s.discontinued ?? 0
  const warn = s.warn ?? 0
  return [
    { label: '可正常导入', value: s.ok ?? 0, tone: 'ok' as const },
    { label: '警告（停产）', value: dis, tone: 'warn' as const },
    { label: '警告（型号 / 分类）', value: Math.max(warn - dis, 0), tone: 'warn' as const },
    { label: '错误（必须处理）', value: s.error ?? 0, tone: 'error' as const },
  ]
})

function levelTag(level: string): 'success' | 'warning' | 'danger' {
  if (level === 'ok') return 'success'
  if (level === 'warn') return 'warning'
  return 'danger'
}

function actionLabel(action: string): string {
  const m: Record<string, string> = {
    ok: '通过',
    warn: '需确认',
    error: '报错',
    create: '新增',
    update: '覆盖',
    skip: '跳过',
    duplicate: '重复',
    discontinued: '停产',
  }
  return m[action] ?? action
}
</script>

<template>
  <div class="page">
    <!-- 原型第 5 页：标题 + 副标题 + 工具栏（6 个操作）+ 右侧统计文字 -->
    <div class="page-head">
      <div>
        <h2 class="page-title">设备库管理</h2>
        <p class="page-sub">企业标准设备库 · 供报价时选型</p>
      </div>
    </div>

    <div class="card dev-toolbar no-print">
      <el-button v-if="auth.can('device_library', 'create')" type="primary" @click="openCreate">
        <el-icon><Plus /></el-icon>&nbsp;新增设备
      </el-button>
      <el-button v-if="auth.can('device_library', 'create')" @click="openImport">
        <el-icon><Upload /></el-icon>&nbsp;Excel 批量导入
      </el-button>
      <el-button @click="downloadTemplate">
        <el-icon><Download /></el-icon>&nbsp;下载模板
      </el-button>
      <el-button :loading="exporting" @click="exportDevices">
        <el-icon><Download /></el-icon>&nbsp;导出设备库
      </el-button>
      <el-button @click="openBatches">
        <el-icon><Clock /></el-icon>&nbsp;导入记录
      </el-button>
      <el-button @click="router.push({ name: 'suppliers' })">
        <el-icon><OfficeBuilding /></el-icon>&nbsp;供应商库
      </el-button>
      <el-button
        :disabled="!selectedRows.length || !auth.can('device_library', 'update')"
        :title="selectedRows.length ? `对勾选的 ${selectedRows.length} 台设备调价` : '先在表格里勾选设备'"
        @click="openBulk"
      >
        批量调价{{ selectedRows.length ? `（${selectedRows.length}）` : '' }}
      </el-button>
      <el-button disabled title="原型已设计，后端未实现">批量修改字段</el-button>
      <div class="spacer" />
      <span v-if="stats" class="dev-stats">
        共 {{ stats.total }} 条 · 在售 {{ stats.by_status.on_sale ?? 0 }} ·
        停产 {{ deadCount }} · 含替代型号 {{ stats.with_replacement }} ·
        甲供 {{ stats.owner_supplied ?? 0 }}
      </span>
    </div>
    <el-alert
      v-if="deadCount > 0"
      type="warning"
      show-icon
      :closable="false"
      class="mb"
      :title="`停产型号占 ${deadRatio}%（${deadCount} / ${stats?.total ?? 0}）`"
    >
      <template #default>
        这些型号不能再用在新报价里。请在设备库中为它们指定替代型号，
        报价编制时才能一键换型。
        <el-button link type="primary" @click="query.status = 'eol'; resetAndLoad()">
          只看已停产
        </el-button>
      </template>
    </el-alert>

    <div class="lib-layout">
      <!-- 左侧：分类树。点父节点看整棵子树，节点后面的数字是"含子树的设备数"。 -->
      <aside class="card tree-panel">
        <div class="tree-head">
          <span>设备分类</span>
          <el-button
            v-if="canMaintain"
            link
            type="primary"
            @click="openCatCreate(null)"
          >
            + 一级分类
          </el-button>
        </div>
        <el-input
          v-model="treeFilter"
          size="small"
          placeholder="搜索分类"
          clearable
          class="mb"
        />
        <!-- 分类树：**默认收起**（2026-10-05 用户要求）
             不用 default-expand-all：它只作用于首次渲染，而数据是异步来的 →
             时而展开时而收起。改为渲染后按本地记忆决定（没记忆 = 全收起）✓ -->
        <el-tree
          ref="treeRef"
          :data="treeData"
          :props="treeProps"
          node-key="id"
          highlight-current
          :default-expanded-keys="treeExpandedKeys"
          :expand-on-click-node="false"
          :filter-node-method="filterTreeNode"
          @node-click="onTreeClick"
          @node-expand="rememberTreeExpand"
          @node-collapse="rememberTreeExpand"
        >
          <template #default="{ data }">
            <span class="tree-node">
              <span class="tree-label">{{ nodeLabel(data) }}</span>
              <span v-if="canMaintain && data.id !== '__none__'" class="tree-ops">
                <el-icon title="加子分类" @click.stop="openCatCreate(data)">
                  <Plus />
                </el-icon>
                <el-icon title="编辑分类" @click.stop="openCatEdit(data)">
                  <Edit />
                </el-icon>
                <el-icon
                  v-if="canDeleteCategory"
                  title="删除分类"
                  @click.stop="removeCategory(data)"
                >
                  <Delete />
                </el-icon>
              </span>
            </span>
          </template>
        </el-tree>
        <div class="tree-foot">
          <el-button link @click="clearCategory">显示全部</el-button>
          <span class="text-muted">共 {{ tree?.total ?? 0 }} 台</span>
        </div>
      </aside>

      <section class="lib-main">
    <div class="card">
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
        <el-form-item label="当前分类">
          <el-tag v-if="currentCategory" closable type="info" @close="clearCategory">
            {{ currentCategory.path }}（{{ currentNodeCount }}）
          </el-tag>
          <span v-else class="text-muted">全部（左侧选分类）</span>
        </el-form-item>
        <el-form-item label="状态">
          <el-select
            v-model="query.status"
            placeholder="全部"
            clearable
            style="width: 130px"
            @change="resetAndLoad"
          >
            <el-option
              v-for="o in meta.options('device_status')"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="品牌">
          <el-select
            v-model="query.brand"
            placeholder="全部品牌"
            clearable
            style="width: 140px"
            @change="resetAndLoad"
          >
            <el-option v-for="b in brandOptions" :key="b" :label="b" :value="b" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="query.only_common" @change="resetAndLoad">仅常用</el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="query.only_eol_with_replacement" @change="resetAndLoad">
            仅看停产有替代
          </el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="query.only_owner_supplied" @change="resetAndLoad">
            仅看甲供设备
          </el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-checkbox disabled>
            <span title="原型已设计：后端未记录价格更新时间">价格超 30 天未更新</span>
          </el-checkbox>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="resetAndLoad">
            <el-icon><Search /></el-icon>&nbsp;查询
          </el-button>
          <span class="text-muted" style="margin-left: 10px">匹配 {{ total }} 条</span>
        </el-form-item>
      </el-form>

      <div class="dev-split">
        <div class="dev-table-col">
      <el-table
        v-loading="loading"
        :data="rows"
        border
        stripe
        highlight-current-row
        :row-class-name="rowClassName"
        @row-click="onRowClick"
        @selection-change="onSelectionChange"
      >
        <el-table-column
          type="selection"
          width="40"
          class-name="col-no-print"
          label-class-name="col-no-print"
        />
        <el-table-column prop="code" label="编号" width="100" />
        <el-table-column label="名称 / 型号" min-width="230">
          <template #default="{ row }">
            <div>{{ row.name }}</div>
            <div class="text-muted mono">{{ row.brand || '—' }} {{ row.model || '' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="分类" min-width="170">
          <template #default="{ row }">
            <span v-if="row.category_path">{{ row.category_path }}</span>
            <span v-else class="text-muted">
              {{ meta.label('device_category', row.category) }}（未细分）
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag size="small" :type="meta.deviceStatusTag(row.status)" effect="plain">
              {{ meta.label('device_status', row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="替代型号" width="110">
          <template #default="{ row }">
            <span v-if="row.replacement_id" class="ok-text">已配置</span>
            <span v-else-if="meta.isDiscontinued(row.status)" class="danger-text">缺失</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="单位" prop="unit" width="60" />
        <el-table-column label="起订量" width="76" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.moq ?? 1 }}</span>
          </template>
        </el-table-column>
        <el-table-column label="质保" width="70" align="right">
          <template #default="{ row }">
            <span class="num">{{ row.warranty_years ?? '—' }} 年</span>
          </template>
        </el-table-column>
        <el-table-column label="甲供" width="64" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.is_owner_supplied" size="small" type="info" effect="plain">甲供</el-tag>
            <span v-else class="text-muted">否</span>
          </template>
        </el-table-column>
        <el-table-column label="参考单价" width="110" align="right">
          <template #default="{ row }">
            <span class="num">{{ money(row.reference_price) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="成本价" width="110" align="right">
          <template #default="{ row }">
            <span class="num num-muted">{{ money(row.cost_price) }}</span>
          </template>
        </el-table-column>
        <!-- 安装单价列已移除：安装调试费改为在报价单里手动填写（2026-10-05） -->
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <div class="table-actions">
              <el-button
                v-if="auth.can('device_library', 'update')"
                link
                @click="openEdit(row)"
              >
                编辑
              </el-button>
              <el-button
                v-if="auth.can('device_library', 'delete')"
                type="danger"
                link
                @click="deactivate(row)"
              >
                停用
              </el-button>
            </div>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="没有匹配的设备" />
        </template>
      </el-table>
        </div>

        <!-- 右侧：设备详情（原型第 5 页 x1074 那一栏） -->
        <aside class="dev-detail card">
          <div class="dd-head">
            设备详情 <span v-if="detail" class="mono text-muted">{{ detail.code }}</span>
          </div>
          <template v-if="detail">
            <div class="dd-name">{{ detail.name }}</div>
            <div class="dd-row"><span>品牌</span><span>{{ detail.brand || '—' }}</span></div>
            <div class="dd-row"><span>型号规格</span><span>{{ detail.model || '—' }}</span></div>
            <div class="dd-row"><span>分类</span><span>{{ detail.category_path || meta.label('device_category', detail.category) }}</span></div>
            <div class="dd-row"><span>单位 / 起订量</span><span>{{ detail.unit }} / {{ detail.moq ?? 1 }}</span></div>
            <div class="dd-row"><span>参考单价</span><span class="num">{{ money(detail.reference_price) }}</span></div>
            <div class="dd-row"><span>成本价</span><span class="num num-muted">{{ money(detail.cost_price) }}</span></div>
            <div class="dd-row"><span>质保</span><span>{{ detail.warranty_years ?? '—' }} 年</span></div>
            <div class="dd-row">
              <span>甲供</span>
              <span>{{ detail.is_owner_supplied ? '是（只计安装费）' : '否' }}</span>
            </div>
            <div class="dd-row">
              <span>状态</span>
              <el-tag size="small" :type="meta.deviceStatusTag(detail.status)" effect="plain">
                {{ meta.label('device_status', detail.status) }}
              </el-tag>
            </div>
            <div class="dd-row">
              <span>替代型号</span>
              <span>
                <span v-if="detail.replacement_id" class="ok-text">已配置</span>
                <span v-else-if="meta.isDiscontinued(detail.status)" class="danger-text">缺失</span>
                <span v-else class="text-muted">—</span>
              </span>
            </div>
            <div class="dd-actions no-print">
              <el-button size="small" :disabled="!auth.can('device_library', 'update')" @click="openEdit(detail)">
                编辑
              </el-button>
              <el-button size="small" type="danger" plain :disabled="!auth.can('device_library', 'delete')" @click="deactivate(detail)">
                删除
              </el-button>
            </div>
          </template>
          <el-empty v-else description="点左侧表格的一行查看详情" :image-size="60" />
        </aside>
      </div>

      <el-pagination
        v-model:current-page="query.page"
        v-model:page-size="query.page_size"
        :total="total"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        class="pager"
        @current-change="load"
        @size-change="resetAndLoad"
      />
    </div>
      </section>
    </div>

    <!-- 分类维护（加子类 / 改名 / 停用） -->
    <el-dialog
      v-model="catDialogVisible"
      :title="catForm.id ? '编辑分类' : catForm.parent_id ? '新建子分类' : '新建一级分类'"
      width="460px"
    >
      <el-form label-width="96px">
        <el-form-item v-if="catForm.parent_id" label="上级分类">
          <span>{{ catForm.parent_name || findNode(catForm.parent_id)?.path || '—' }}</span>
        </el-form-item>
        <el-form-item v-else-if="!catForm.id" label="一级分类取值">
          <el-select v-model="catForm.legacy_enum" placeholder="选择它对应的一级分类" style="width: 100%">
            <el-option
              v-for="o in meta.options('device_category')"
              :key="o.value"
              :label="o.label"
              :value="o.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="名称" required>
          <el-input v-model="catForm.name" placeholder="如 摄像机 / 光缆与光模块" />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="catForm.sort_order" :min="0" :max="9999" />
        </el-form-item>
        <el-form-item v-if="catForm.id" label="启用">
          <el-switch v-model="catForm.is_active" />
        </el-form-item>
        <el-alert
          v-if="!catForm.id && !catForm.parent_id"
          type="info"
          :closable="false"
          show-icon
          title="一级分类与设备库的既有分类一一对应，一个取值只能有一个节点。"
        />
      </el-form>
      <template #footer>
        <el-button @click="catDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="catSaving" @click="saveCategory">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新增 / 编辑设备 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新增设备' : '编辑设备'"
      width="820px"
      :close-on-click-modal="false"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <div class="grid-3">
          <el-form-item label="设备编号" prop="code">
            <el-input v-model="form.code" :disabled="dialogMode === 'edit'">
              <template #suffix>
                <el-icon v-if="codeLoading" class="is-loading"><Loading /></el-icon>
              </template>
            </el-input>
          </el-form-item>
          <el-form-item label="所属分类">
            <!-- 树选择：设备和"它落在哪一格"是同一件事，一级分类由节点反推 -->
            <el-tree-select
              v-model="form.category_id"
              :data="selectableTree"
              node-key="id"
              :props="treeProps"
              value-key="id"
              default-expand-all
              check-strictly
              clearable
              placeholder="选择分类（可多级）"
              style="width: 100%"
              @change="onCategoryChange"
            />
          </el-form-item>
          <el-form-item label="单位">
            <el-input v-model="form.unit" />
          </el-form-item>
        </div>

        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="如 网络半球摄像机 400万 星光级" />
        </el-form-item>

        <div class="grid-3">
          <el-form-item label="品牌">
            <el-input v-model="form.brand" />
          </el-form-item>
          <el-form-item label="型号">
            <el-input v-model="form.model" placeholder="用于模糊匹配，请尽量完整" />
          </el-form-item>
          <el-form-item label="规格">
            <el-input v-model="form.spec" />
          </el-form-item>
        </div>

        <el-form-item label="设备描述">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            maxlength="4000"
            show-word-limit
            placeholder="性能参数 / 基础信息，例如：吞吐量 10 Gbps；并发连接 200 万；接口 8×GE + 4×SFP+；IPsec 隧道 2000 条"
          />
          <div class="acc-hint">
            选产品时鼠标悬停会显示这段描述；加进报价单后它默认成为「设备参数」一栏（可在单里改）。
          </div>
        </el-form-item>

        <div class="grid-3">
          <el-form-item label="参考单价">
            <el-input-number
              v-model="form.reference_price"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="成本价">
            <el-input-number
              v-model="form.cost_price"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <!-- 安装调试费改为在报价单里**手动填写**（2026-10-05）：
               设备库不再按设备维护这个字段 -->
          <el-form-item v-if="false" label="安装单价">
            <el-input-number
              v-model="form.install_unit_price"
              :min="0"
              :precision="2"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="单位辅材费">
            <el-input-number v-model="form.auxiliary_unit_price" :min="0" :precision="4" :controls="false" />
            <span class="text-muted" style="margin-left: 8px">
              辅材口径选「按工程量」时按 Σ(数量 × 本值) 计（桥架按米、网线按箱）
            </span>
          </el-form-item>
          <el-form-item label="底价">
            <el-input-number v-model="form.min_price" :min="0" :precision="2" :controls="false" />
            <span class="text-muted" style="margin-left: 8px">
              低于底价报价需要经理授权 + 理由（0 = 不设底价）
            </span>
          </el-form-item>
          <el-form-item label="阶梯价">
            <div style="width: 100%">
              <el-table :data="tierRows" size="small" border>
                <el-table-column label="从(台)">
                  <template #default="{ row }">
                    <el-input-number v-model="row.min_qty" :min="0" :controls="false" size="small" />
                  </template>
                </el-table-column>
                <el-table-column label="到(台)">
                  <template #default="{ row }">
                    <el-input-number v-model="row.max_qty" :min="0" :controls="false" size="small"
                      placeholder="留空=以上" />
                  </template>
                </el-table-column>
                <el-table-column label="单价">
                  <template #default="{ row }">
                    <el-input-number v-model="row.unit_price" :min="0" :precision="2" :controls="false" size="small" />
                  </template>
                </el-table-column>
                <el-table-column label="备注">
                  <template #default="{ row }">
                    <el-input v-model="row.note" size="small" placeholder="选填" />
                  </template>
                </el-table-column>
                <el-table-column width="60">
                  <template #default="{ $index }">
                    <el-button link type="danger" size="small" @click="removeTier($index)">删</el-button>
                  </template>
                </el-table-column>
              </el-table>
              <el-button link type="primary" size="small" @click="addTier">+ 加一档</el-button>
              <span class="text-muted" style="margin-left: 8px">
                留空不填 = 不用阶梯价；区间不能重叠（后端会拒）
              </span>
            </div>
          </el-form-item>
        </div>

        <el-divider content-position="left">供货状态（停产闭环）</el-divider>

        <div class="grid-2">
          <el-form-item label="状态">
            <el-select v-model="form.status" style="width: 100%">
              <el-option
                v-for="o in meta.options('device_status')"
                :key="o.value"
                :label="o.label"
                :value="o.value"
              />
            </el-select>
          </el-form-item>
          <el-form-item label="停产日期">
            <el-date-picker
              v-model="form.eol_at"
              type="date"
              value-format="YYYY-MM-DD"
              placeholder="选填"
              style="width: 100%"
            />
          </el-form-item>
        </div>

        <el-form-item label="替代型号">
          <el-select
            v-model="form.replacement_id"
            clearable
            filterable
            placeholder="同分类在售型号"
            style="width: 100%"
          >
            <el-option
              v-for="d in replacementOptions"
              :key="d.id"
              :label="`${d.code} ${d.name} ${d.model || ''}`"
              :value="d.id"
            />
          </el-select>
        </el-form-item>

        <el-alert
          v-if="needsReplacementWarning"
          type="warning"
          :closable="false"
          show-icon
          class="mb"
          title="标记为停产/临近停产但未指定替代型号"
        >
          <template #default>
            没有替代型号，报价里出现该型号时无法自动换型，需要人工逐个处理。
          </template>
        </el-alert>

        <div class="grid-3">
          <el-form-item label="质保年限">
            <el-input-number
              v-model="form.warranty_years"
              :min="0"
              :max="20"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="最小起订量">
            <el-input-number
              v-model="form.moq"
              :min="1"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
          <el-form-item label="标记">
            <el-checkbox v-model="form.is_common">常用</el-checkbox>
            <el-checkbox v-model="form.is_owner_supplied">甲供</el-checkbox>
          </el-form-item>
        </div>
        <el-divider content-position="left">配件 / 授权</el-divider>
        <div v-if="dialogMode === 'create'" class="acc-hint">
          配件要挂在已保存的设备上：先点「保存」，再回来配置配件（如防火墙的授权）。
        </div>
        <div v-else>
          <div v-if="!accGroups.length" class="acc-hint">
            还没有配件。例如「买防火墙必须选一个授权」：加一个槽叫「授权」，
            再把可选的不同授权（1 年 / 3 年）加进去 —— 授权本身也是产品库里的设备。
          </div>
          <div v-for="(g, gi) in accGroups" :key="gi" class="acc-group">
            <div class="acc-row">
              <el-input v-model="g.label" size="small" placeholder="槽名，如 授权"
                        style="width: 130px" />
              <el-checkbox v-model="g.is_required" size="small">必配（配单时提示）</el-checkbox>
              <el-select v-model="g.select_mode" size="small" style="width: 92px">
                <el-option label="单选" value="one" />
                <el-option label="多选" value="many" />
              </el-select>
              <el-select v-model="g.quantity_mode" size="small" style="width: 128px">
                <el-option label="随主设备数量" value="per_unit" />
                <el-option label="固定数量" value="fixed" />
              </el-select>
              <el-input-number v-if="g.quantity_mode === 'fixed'"
                               v-model="g.default_quantity" :min="0" size="small"
                               style="width: 110px" />
              <el-button link type="danger" size="small"
                         @click="accGroups.splice(gi, 1)">删除槽</el-button>
            </div>
            <div v-for="(o, oi) in g.options" :key="oi" class="acc-row acc-sub">
              <span class="acc-name">
                {{ o.code ? `${o.code} ` : '' }}{{ o.name || '（未选配件）' }}
              </span>
              <el-input v-model="o.note" size="small" placeholder="说明（选填）"
                        style="width: 150px" />
              <el-checkbox v-model="o.is_default" size="small">默认选它</el-checkbox>
              <el-button link type="danger" size="small"
                         @click="g.options.splice(oi, 1)">移除</el-button>
            </div>
            <!-- 加候选走「选产品」弹窗：可搜索、可一次勾多条（2026-10-05 用户反馈） -->
            <el-button size="small" type="primary" plain @click="openAccPicker(g)">
              + 添加候选配件（可多选）
            </el-button>
          </div>
          <div class="acc-row">
            <el-button size="small" type="primary" plain @click="addAccGroup">
              + 添加配件槽
            </el-button>
            <el-button size="small" type="success" :loading="accSaving"
                       @click="saveAccessories">保存配件</el-button>
          </div>
        </div>
      </el-form>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 批量调价（原型第 5 页工具栏）：先预览，确认后才写入 -->
    <!-- 选配件：复用「选产品」弹窗（搜索 + 多选） -->
    <DevicePickerDialog v-model="accPickVisible" mode="add" @pick="onAccPicked" />

    <el-dialog v-model="bulkVisible" title="批量调价" width="860px" top="6vh">
      <el-alert
        type="warning"
        show-icon
        :closable="false"
        class="mb"
        title="价格是钱：先「预览变更」，核对无误再「确认调价」——预览不会写入数据库。"
      />
      <el-form inline>
        <el-form-item label="调价字段">
          <el-select v-model="bulk.field" style="width: 150px" @change="bulkPreviewed = false">
            <el-option label="参考单价" value="reference_price" />
            <el-option label="成本价" value="cost_price" />
          </el-select>
        </el-form-item>
        <el-form-item label="方式">
          <el-radio-group v-model="bulk.mode" @change="bulkPreviewed = false">
            <el-radio-button value="percent">按比例（%）</el-radio-button>
            <el-radio-button value="amount">按金额（元）</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="bulk.mode === 'percent' ? '幅度' : '增减额'">
          <el-input-number
            v-model="bulk.value"
            :precision="2"
            :step="bulk.mode === 'percent' ? 1 : 10"
            style="width: 130px"
            @change="bulkPreviewed = false"
          />
          <span class="text-muted ml">{{ bulk.mode === 'percent' ? '%' : '元' }}</span>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="bulk.round_yuan" @change="bulkPreviewed = false">取整到元</el-checkbox>
        </el-form-item>
      </el-form>
      <div class="text-muted mb">
        将对勾选的 <b>{{ selectedRows.length }}</b> 台设备生效；负价会被夹到 0 并标注。
      </div>

      <template v-if="bulkResult">
        <el-table :data="bulkResult.changes" border size="small" height="300">
          <el-table-column prop="code" label="编码" width="110" />
          <el-table-column prop="name" label="设备名称" min-width="180" show-overflow-tooltip />
          <el-table-column label="原价" width="110" align="right">
            <template #default="{ row }"><span class="num">{{ money(row.before) }}</span></template>
          </el-table-column>
          <el-table-column label="新价" width="110" align="right">
            <template #default="{ row }"><span class="num strong">{{ money(row.after) }}</span></template>
          </el-table-column>
          <el-table-column label="差额" width="110" align="right">
            <template #default="{ row }">
              <span class="num" :class="row.delta > 0 ? 'warn-text' : row.delta < 0 ? 'ok-text' : ''">
                {{ row.delta > 0 ? '+' : '' }}{{ money(row.delta) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="提示" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.clamped" size="small" type="danger" effect="plain">已夹到 0</el-tag>
              <span v-else class="text-muted">—</span>
            </template>
          </el-table-column>
        </el-table>
        <div class="sum-line">
          {{ bulkResult.dry_run ? '预览' : '已应用' }} {{ bulkResult.count }} 行 ·
          合计 <b class="num">{{ money(bulkResult.total_before) }}</b> →
          <b class="num text-accent">{{ money(bulkResult.total_after) }}</b>
          <span v-if="bulkResult.missing.length" class="danger-text">
            · {{ bulkResult.missing.length }} 个 id 未找到
          </span>
        </div>
      </template>

      <template #footer>
        <el-button @click="bulkVisible = false">取消</el-button>
        <el-button :loading="bulkBusy" @click="runBulk(true)">预览变更</el-button>
        <el-button
          type="primary"
          :loading="bulkBusy"
          :disabled="!bulkPreviewed"
          @click="runBulk(false)"
        >
          确认调价{{ bulkResult ? `（${bulkResult.count} 行）` : '' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 场景一：Excel 导入（原型第 6 页：3 步向导 = 上传文件 / 字段映射与校验 / 确认导入） -->
    <el-dialog
      v-model="importVisible"
      width="1180px"
      top="4vh"
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="imp-head">
          <span class="imp-title">批量导入 · 场景一：导入自有设备库</span>
          <span class="text-muted imp-sub">用 Excel 维护企业设备库，一次导入几百条设备</span>
          <div class="spacer" />
          <span class="text-muted">
            {{ importStep === 'done' ? '已写入正式库' : '尚未写入正式库' }}
          </span>
        </div>
      </template>

      <div class="imp-steps">
        <el-steps
          :active="importStep === 'upload' ? 0 : importStep === 'preview' ? 1 : 2"
          simple
          class="imp-steps-bar"
        >
          <el-step title="上传文件" />
          <el-step title="字段映射与校验" />
          <el-step title="确认导入" />
        </el-steps>
        <span class="text-muted imp-hint">导入过程中不写入正式库，确认后才提交；可随时取消</span>
      </div>

      <!-- 第 1 步：拖拽区（未选文件时） -->
      <el-upload
        v-if="importStep === 'upload' && !importFile"
        ref="uploadRef"
        drag
        :auto-upload="false"
        :limit="1"
        accept=".xlsx,.xls"
        :on-change="onFileChange"
        :on-exceed="onFileExceed"
        :show-file-list="false"
      >
        <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
        <div class="el-upload__text">把 Excel 拖到这里，或<em>点击选择</em></div>
        <template #tip>
          <div class="el-upload__tip">支持 .xlsx / .xls；表头支持同义词识别（型号 / 规格型号、名称 / 品名 等）</div>
        </template>
      </el-upload>
      <el-upload
        v-else
        ref="uploadRef"
        class="imp-hidden-upload"
        :auto-upload="false"
        :limit="1"
        accept=".xlsx,.xls"
        :on-change="onFileChange"
        :on-exceed="onFileExceed"
        :show-file-list="false"
      />

      <!-- 文件条 + 导入模式 + 编码规则（原型 y123 那一行） -->
      <div v-if="importFile" class="imp-file-row">
        <el-icon><Document /></el-icon>
        <span class="imp-file-name">{{ importFile.name }}</span>
        <span class="text-muted">{{ importFileSize }}</span>
        <el-icon class="imp-x" title="移除文件" @click="clearImportFile"><Close /></el-icon>
        <el-divider direction="vertical" />
        <span class="imp-mode-label">导入模式</span>
        <el-radio-group v-model="importPolicy" size="small" :disabled="importStep !== 'upload'">
          <el-radio-button value="overwrite">新增或更新已有型号</el-radio-button>
          <el-radio-button value="skip">跳过已存在</el-radio-button>
          <el-radio-button value="new">作为新型号新增</el-radio-button>
        </el-radio-group>
        <el-button link :disabled="importStep !== 'upload'" @click="pickAgain">重新选择文件</el-button>
        <div class="spacer" />
        <span class="text-muted">编码规则：按分类自动生成，如 V1001（视频监控第 1 条）</span>
      </div>

      <!-- 第 2 步：字段映射（左） + 数据预览（右） -->
      <div v-if="importPreview" class="imp-split">
        <div class="imp-map">
          <div class="imp-block-head">
            字段映射 Excel 列 → 系统字段
            <span class="text-muted">
              已自动识别 {{ (importPreview.mapping ?? []).length }} /
              {{ (importPreview.mapping ?? []).length + (importPreview.unmapped ?? []).length }} 列
            </span>
          </div>
          <el-table :data="importPreview.mapping ?? []" border size="small" height="252">
            <el-table-column label="Excel 列" min-width="150">
              <template #default="{ row }">
                <span class="mono">{{ colLetter(row.excel_index) }} 列</span>
                <span class="text-muted"> {{ row.excel_header }}</span>
              </template>
            </el-table-column>
            <el-table-column label="映射到系统字段" min-width="130">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ fieldLabel(row.system_field) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="sample" label="示例值" min-width="150" show-overflow-tooltip />
          </el-table>
          <div class="text-muted imp-note">
            列名不需要与系统完全一致：系统按同义词库识别（规格 / 型号 / Model 均可映射到「型号规格」）。
            未映射的列会被忽略，不会报错。
          </div>
          <el-alert
            v-if="(importPreview.unmapped ?? []).length"
            type="warning"
            :closable="false"
            show-icon
            class="mt"
            :title="`未识别的列：${(importPreview.unmapped ?? []).join('、')}`"
          />
        </div>

        <div class="imp-prev">
          <div class="imp-block-head">
            数据预览（前 {{ importPreview.preview.length }} 行 / 共 {{ importPreview.sheet_rows }} 行）
            <span class="text-muted">校验在后台逐行执行，不写入正式库</span>
          </div>
          <el-table :data="importPreview.preview" border size="small" height="252">
            <el-table-column prop="row" label="行" width="52" />
            <el-table-column label="校验" width="76">
              <template #default="{ row }">
                <el-tag size="small" :type="levelTag(row.level)" effect="plain">
                  {{ actionLabel(row.level) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="name" label="设备名称" min-width="150" show-overflow-tooltip />
            <el-table-column prop="brand" label="品牌" width="90" show-overflow-tooltip />
            <el-table-column prop="model" label="型号规格" width="130" show-overflow-tooltip />
            <el-table-column prop="unit" label="单位" width="56" />
            <el-table-column label="参考单价" width="96" align="right">
              <template #default="{ row }">
                <span class="num">{{ row.reference_price == null ? '—' : money(row.reference_price) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="问题" min-width="170">
              <template #default="{ row }">
                <span v-if="!row.issues.length" class="text-muted">—</span>
                <ul v-else class="issues">
                  <li v-for="(i, idx) in row.issues" :key="idx">{{ i }}</li>
                </ul>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>

      <!-- 校验结果 + 导入选项 -->
      <div v-if="importPreview" class="imp-verify">
        <div class="imp-block-head">
          校验结果
          <span class="text-muted">共扫描 {{ importPreview.sheet_rows }} 行</span>
        </div>
        <div class="imp-cards">
          <div v-for="c in verifyCards" :key="c.label" class="imp-card" :class="`imp-card-${c.tone}`">
            <div class="imp-card-label">{{ c.label }}</div>
            <div class="imp-card-value">{{ c.value }} 行</div>
          </div>
          <div class="imp-card imp-card-plain">
            <div class="imp-card-label">表格行数 / 重复</div>
            <div class="imp-card-value">
              {{ importPreview.sheet_rows }} / {{ importPreview.stat.duplicate ?? 0 }}
            </div>
          </div>
        </div>
      </div>

      <el-result
        v-if="importStep === 'done' && importResult"
        icon="success"
        title="导入完成"
        :sub-title="`新增 ${importResult.created} · 覆盖 ${importResult.updated} · 跳过 ${importResult.skipped} · 报错 ${importResult.errors}`"
      />

      <template #footer>
        <el-button @click="importVisible = false">
          {{ importStep === 'done' ? '关闭' : '取消' }}
        </el-button>
        <el-button v-if="importStep === 'preview'" @click="importStep = 'upload'">上一步</el-button>
        <el-button v-if="importStep === 'upload'" type="primary" :loading="importBusy" :disabled="!importFile" @click="doPreview">
          下一步：字段映射与校验
        </el-button>
        <template v-else-if="importStep === 'preview'">
          <el-button :loading="importBusy" @click="doPreview">重新校验</el-button>
          <el-button type="primary" :loading="importBusy" @click="doCommit">
            确认导入（{{ importPreview?.stat.ok ?? 0 }} 行可导入）
          </el-button>
        </template>
        <el-button v-else type="primary" @click="importVisible = false">完成</el-button>
      </template>
    </el-dialog>
  </div>
    <!-- 导入记录（原型 #IM-xxx）：这一批导的什么文件、成败多少、哪几行失败 -->
    <el-drawer v-model="batchDrawer" title="导入记录" size="72%" @open="loadBatches">
      <el-form inline class="mb" @submit.prevent>
        <el-form-item label="类型">
          <el-select v-model="batchKind" clearable style="width: 140px" @change="loadBatches">
            <el-option value="device" label="设备库导入" />
            <el-option value="inquiry" label="甲方清单导入" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="batchLoading" @click="loadBatches">
            <el-icon><Search /></el-icon>&nbsp;刷新
          </el-button>
        </el-form-item>
        <el-form-item>
          <span class="text-muted">批次号可直接引用（如 IM-20261004-001）</span>
        </el-form-item>
      </el-form>

      <el-table
        v-loading="batchLoading"
        :data="batches"
        border
        size="small"
        height="420"
        @row-click="openBatchDetail"
      >
        <el-table-column prop="batch_no" label="批次号" width="160">
          <template #default="{ row }"><span class="mono">{{ row.batch_no }}</span></template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">{{ row.kind === 'device' ? '设备库' : '甲方清单' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="96">
          <template #default="{ row }">
            <el-tag
              size="small"
              effect="plain"
              :type="row.status === 'success' ? 'success' : row.status === 'partial' ? 'warning' : 'danger'"
            >
              {{ row.status === 'success' ? '全部成功' : row.status === 'partial' ? '部分成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="total_rows" label="总行" width="66" />
        <el-table-column prop="created_rows" label="新增" width="66" />
        <el-table-column prop="updated_rows" label="更新" width="66" />
        <el-table-column prop="skipped_rows" label="跳过" width="66" />
        <el-table-column label="失败" width="66">
          <template #default="{ row }">
            <span :class="row.error_rows ? 'danger-text' : ''">{{ row.error_rows }}</span>
          </template>
        </el-table-column>
        <el-table-column label="文件" min-width="170" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.filename || '—' }}</span></template>
        </el-table-column>
        <el-table-column prop="created_by_name" label="操作人" width="106" />
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ row.created_at ? dateTime(row.created_at) : '—' }}</template>
        </el-table-column>
        <template #empty>
          <el-empty description="还没有导入记录" :image-size="60" />
        </template>
      </el-table>

      <el-dialog
        v-model="batchDetailOpen"
        :title="`批次明细 ${batchDetail?.batch_no ?? ''}`"
        width="620"
      >
        <template v-if="batchDetail">
          <el-descriptions :column="2" border size="small" class="mb">
            <el-descriptions-item label="文件">{{ batchDetail.filename || '—' }}</el-descriptions-item>
            <el-descriptions-item label="大小">
              {{ Math.round(batchDetail.file_size / 1024) }} KB
            </el-descriptions-item>
            <el-descriptions-item label="总行数">{{ batchDetail.total_rows }}</el-descriptions-item>
            <el-descriptions-item label="新增/更新/跳过">
              {{ batchDetail.created_rows }} / {{ batchDetail.updated_rows }} /
              {{ batchDetail.skipped_rows }}
            </el-descriptions-item>
            <el-descriptions-item label="操作人">
              {{ batchDetail.created_by_name || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="时间">
              {{ batchDetail.created_at ? dateTime(batchDetail.created_at) : '—' }}
            </el-descriptions-item>
          </el-descriptions>

          <template v-if="batchDetail.errors.length">
            <div class="mb"><b>失败明细</b>（行号对应 Excel 里的行）</div>
            <el-table :data="batchDetail.errors" border size="small" max-height="240">
              <el-table-column prop="row" label="行号" width="80" />
              <el-table-column prop="message" label="原因" />
            </el-table>
          </template>
          <el-alert v-else type="success" :closable="false" show-icon title="这一批没有失败行" />
        </template>
      </el-dialog>
    </el-drawer>

</template>

<style scoped>
/* 配件/授权编辑区（2026-10-05） */
.acc-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
  margin-bottom: 6px;
}
.acc-group {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 8px 10px;
  margin-bottom: 8px;
}
.acc-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}
.acc-sub {
  padding-left: 14px;
}
/* 已选配件：只读显示（选/换走弹窗，避免内联选择器搜不到） */
.acc-name {
  min-width: 300px;
  font-size: 13px;
  color: var(--el-text-color-primary);
}
/* ---------- 场景一导入向导（原型第 6 页） ---------- */
.imp-head {
  display: flex;
  align-items: baseline;
  gap: 12px;
}

.imp-title {
  font-size: 15.5px;
  font-weight: 700;
}

.imp-sub {
  font-size: 12.5px;
}

.imp-steps {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-bottom: 12px;
}

.imp-steps-bar {
  flex: 1 1 auto;
}

.imp-hint {
  font-size: 11.5px;
}

.imp-hidden-upload {
  display: none;
}

.imp-file-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 7px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-lighter);
  font-size: 12.5px;
}

.imp-file-name {
  font-weight: 600;
}

.imp-x {
  cursor: pointer;
  color: var(--el-text-color-secondary);
}

.imp-x:hover {
  color: var(--el-color-danger);
}

.imp-mode-label {
  color: var(--el-text-color-secondary);
}

.imp-split {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  margin-top: 10px;
}

.imp-map,
.imp-prev {
  flex: 1 1 0;
  min-width: 0;
}

.imp-block-head {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 6px;
}

.imp-block-head .text-muted {
  font-weight: 400;
  font-size: 11.5px;
}

.imp-note {
  font-size: 11.5px;
  line-height: 1.6;
  margin-top: 6px;
}

.imp-verify {
  margin-top: 12px;
}

.imp-cards {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.imp-card {
  flex: 1 1 130px;
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--el-border-color-lighter);
}

.imp-card-label {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
}

.imp-card-value {
  font-size: 16px;
  font-weight: 700;
  margin-top: 2px;
}

.imp-card-ok {
  border-color: var(--el-color-success-light-5);
  background: var(--el-color-success-light-9);
}

.imp-card-warn {
  border-color: var(--el-color-warning-light-5);
  background: var(--el-color-warning-light-9);
}

.imp-card-error {
  border-color: var(--el-color-danger-light-5);
  background: var(--el-color-danger-light-9);
}

.mb {
  margin-bottom: 10px;
}

.mt {
  margin-top: 12px;
}

.stat-row {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.stat-card {
  flex: 1;
  min-width: 120px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  background: #fff;
  border: 1px solid var(--c-border);
  border-radius: 6px;
}

.stat-num {
  font-size: 20px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--c-navy);
}

.stat-num.ok {
  color: var(--c-green);
}

.stat-num.warn {
  color: var(--c-accent);
}

.stat-num.danger {
  color: #dc2626;
}

.stat-label {
  font-size: 12px;
  color: var(--c-muted);
}

.ok-text {
  color: var(--c-green);
}

.danger-text {
  color: #dc2626;
  font-weight: 700;
}

.mono {
  font-family: Consolas, 'Courier New', monospace;
  font-size: 11.5px;
}

.pager {
  margin-top: 14px;
  justify-content: flex-end;
}

.import-body {
  margin-top: 16px;
}

.preview-block {
  margin-top: 14px;
}

.issues {
  margin: 0;
  padding-left: 16px;
  color: #b45309;
  font-size: 12px;
}

/* ---------- 工具栏与统计文字（原型第 5 页） ---------- */
.dev-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 8px 12px;
  margin-bottom: 10px;
}

.dev-stats {
  font-size: 12.5px;
  color: var(--el-text-color-secondary);
}

/* ---------- 明细表 + 右侧设备详情 ---------- */
.dev-split {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.dev-table-col {
  flex: 1 1 auto;
  min-width: 0;
}

.dev-detail {
  width: 264px;
  flex: 0 0 264px;
  padding: 10px 12px;
}

.dd-head {
  font-weight: 600;
  font-size: 13.5px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 8px;
}

.dd-name {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 6px;
}

.dd-row {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12.5px;
  padding: 3px 0;
}

.dd-row > span:first-child {
  color: var(--el-text-color-secondary);
  flex: 0 0 auto;
}

.dd-row > span:last-child {
  text-align: right;
  word-break: break-all;
}

.dd-actions {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter);
}

/* ---------- 分类树 + 列表的两列布局 ---------- */
.lib-layout {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.tree-panel {
  width: 248px;
  flex: 0 0 248px;
  /* 树长起来时自己滚动，不把整页撑高 */
  max-height: calc(100vh - 240px);
  overflow: auto;
  padding: 10px 12px;
}

.lib-main {
  flex: 1 1 auto;
  min-width: 0;
}

.tree-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 8px;
}

.tree-node {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding-right: 4px;
}

.tree-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 操作图标平时淡一点，悬停到节点上才明显，避免树上到处是小图标 */
.tree-ops {
  display: none;
  gap: 6px;
  color: var(--el-color-primary);
}

.tree-node:hover .tree-ops {
  display: inline-flex;
}

.tree-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
  padding-top: 6px;
  border-top: 1px solid var(--el-border-color-lighter);
  font-size: 12px;
}
</style>










