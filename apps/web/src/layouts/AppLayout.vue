<script setup lang="ts">
/**
 * 应用外壳（2026-10-03 按原型第 1 页 ① 区改造：左侧深色栏 → 顶部导航）。
 *
 * 版式依据：`原型第1页_版式蓝图.md` / `原型页面/proto-1.png`：
 *   品牌 + 系统名 | 主导航 | 用户胶囊
 *
 * 两点取舍：
 *   1. 导航项**按权限过滤**（沿用后端 `/auth/me/permissions` 的 resources），前端不硬编码角色；
 *   2. 原型里的 `点位模板 / 报表中心 / 系统设置` 后端尚未实现 —— 按"融合"策略**置灰展示**
 *      （让人知道"设计在这儿、还没做"），而不是假装有。
 *
 * ⚠ 打印样式 `styles/print.css` 依赖 `.app-header` 这个类名来隐藏头部，改名会回归。
 */
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { authApi, notificationApi } from '@/api'
import type { AppNotification } from '@/api/types'
import { dateTime } from '@/utils/format'
import { useAuthStore } from '@/stores/auth'
import AiAssistantPanel from '@/components/AiAssistantPanel.vue'
import { useMetaStore } from '@/stores/meta'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const meta = useMetaStore()

const appTitle = import.meta.env.VITE_APP_TITLE ?? '智能化弱电报价系统'

/** 主导航：`ready: false` 的是原型已设计、后端未实现的项（置灰）。 */
const navs = computed(() => {
  const all = [
    { key: 'customers', path: '/customers', title: '客户档案', icon: 'User', resource: 'customer', ready: true },
    { key: 'projects', path: '/projects', title: '项目与报价', icon: 'Folder', resource: 'project', ready: true },
    { key: 'devices', path: '/devices', title: '设备库', icon: 'Box', resource: 'device_library', ready: true },
    { key: 'labor', path: '/labor', title: '人工费与定额', icon: 'Coin', resource: 'labor_rate', ready: true },
    { key: 'points', path: '/point-templates', title: '点位模板', icon: 'Grid', resource: 'point_template', ready: true },
    { key: 'reports', path: '/reports', title: '报表中心', icon: 'DataAnalysis', resource: 'project', ready: true },
    { key: 'settings', path: '/settings', title: '系统设置', icon: 'Setting', resources: ['setting', 'user'], ready: true },
  ]
  // 导航项要么给单个 resource，要么给 resources[]（任一命中即可见）。
  // 「系统设置」用后者：里面既有设置（admin/manager/estimator）也有用户管理（admin），
  // 只按 setting 过滤的话，将来某个只有 user 权限的角色就看不到入口了。
  return all.filter((m) => {
    if (!m.ready) return true
    const need = 'resources' in m ? (m.resources as string[]) : [m.resource as string]
    return need.some((r) => auth.hasResource(r))
  })
})

/**
 * 别名：页面挂在**别的导航项**下。
 *   · `/users` → 用户管理已并入系统设置（路由也做了重定向）；
 *   · `/suppliers` → 供应商库从「设备库」工具栏进入，没有独立导航项。
 * 加页面时通常只需加导航项本身；只有当它属于另一个入口时才需要在这里登记。
 */
const NAV_ALIASES: Record<string, string> = {
  '/users': 'settings',
  '/suppliers': 'devices',
}

/**
 * 当前高亮哪个导航项。
 *
 * 以前这里是一串写死的 `if`，末尾 `return 'projects'` 兜底 ——
 * 结果是**任何没被列进去的新页面都会错误地点亮「项目与报价」**
 * （「客户档案」就这么中招的：点它亮的是项目与报价）。
 * 现在改成按导航项自己的 path 做最长前缀匹配，加页面不用再回来补一行。
 */
const activeNav = computed(() => {
  const p = route.path
  for (const [alias, key] of Object.entries(NAV_ALIASES)) {
    if (p === alias || p.startsWith(`${alias}/`)) return key
  }
  const hit = navs.value
    .filter((n) => n.path)
    .slice()
    .sort((a, b) => b.path.length - a.path.length)
    .find((n) => p === n.path || p.startsWith(`${n.path}/`))
  // 匹配不到就不高亮：宁可全都不亮，也不要错误地亮在别的入口上
  return hit?.key ?? ''
})

const roleLabel = computed(() => meta.label('role', auth.user?.role))

onMounted(() => {
  void meta.load()
})

function onNav(key: string, ready: boolean): void {
  if (!ready) return
  const item = navs.value.find((n) => n.key === key)
  if (item?.path) void router.push(item.path)
}

async function handleCommand(cmd: string): Promise<void> {
  if (cmd === 'logout') {
    await ElMessageBox.confirm('确认退出登录？', '提示', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
      .then(() => {
        // 先让后端记一条登出留痕，再清本地态（失败也不拦着用户走）
        void authApi.logout()
        auth.logout()
        void router.replace({ name: 'login' })
      })
      .catch(() => undefined)
  }
}

// ---------- 站内通知（2026-10-04）----------
const unread = ref(0)
const notifOpen = ref(false)
const notifLoading = ref(false)
const notifRows = ref<AppNotification[]>([])
let notifTimer: number | null = null

async function refreshUnread(): Promise<void> {
  if (!auth.token) return
  try {
    unread.value = await notificationApi.unreadCount()
  } catch {
    // 顶栏红点失败不该打扰用户（网络抖一下而已）
  }
}

async function openNotifications(): Promise<void> {
  notifOpen.value = true
  notifLoading.value = true
  try {
    const data = await notificationApi.list(true)
    notifRows.value = data.items
    unread.value = data.unread
  } catch {
    notifRows.value = []
  } finally {
    notifLoading.value = false
  }
}

async function openNotification(row: AppNotification): Promise<void> {
  try {
    await notificationApi.read(row.id)
  } catch {
    // 已读标记失败不影响跳转
  }
  notifOpen.value = false
  await refreshUnread()
  if (row.link) void router.push(row.link)
}

async function readAllNotifications(): Promise<void> {
  const n = await notificationApi.readAll()
  ElMessage.success(n ? `已全部标记已读（${n} 条）` : '没有未读通知')
  notifRows.value = []
  await refreshUnread()
}

onMounted(() => {
  void refreshUnread()
  // 60 秒轮询：站内信不是实时系统，一分钟粒度足够，也不给后端压力
  notifTimer = window.setInterval(() => void refreshUnread(), 60000)
})

onBeforeUnmount(() => {
  if (notifTimer !== null) window.clearInterval(notifTimer)
})
</script>

<template>
  <el-container class="app-shell">
    <!-- ① 顶栏：品牌 + 主导航 + 用户胶囊（原型第 1 页顶部） -->
    <el-header class="app-header" height="52px">
      <div class="brand">
        <span class="brand-mark">弱</span>
        <span class="brand-text">{{ appTitle }}</span>
      </div>

      <nav class="top-nav">
        <button
          v-for="m in navs"
          :key="m.key"
          class="nav-item"
          :class="{ active: m.ready && activeNav === m.key, disabled: !m.ready }"
          :title="m.ready ? m.title : `${m.title}（原型已设计，后端未实现）`"
          @click="onNav(m.key, m.ready)"
        >
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.title }}</span>
        </button>
      </nav>

      <div class="spacer" />

      <el-tag v-if="roleLabel" size="small" type="info" effect="plain">{{ roleLabel }}</el-tag>
      <!-- 站内通知（2026-10-04）：待审批 / 审批结果 -->
      <el-popover
        :visible="notifOpen"
        placement="bottom-end"
        :width="380"
        trigger="click"
        @show="openNotifications"
        @hide="notifOpen = false"
      >
        <template #reference>
          <el-badge :value="unread" :hidden="unread === 0" class="notif-badge">
            <el-button text @click="notifOpen = !notifOpen">
              <el-icon :size="18"><Bell /></el-icon>
            </el-button>
          </el-badge>
        </template>

        <div class="notif-head">
          <b>通知</b>
          <el-button link type="primary" size="small" @click="readAllNotifications">
            全部已读
          </el-button>
        </div>
        <div v-loading="notifLoading" class="notif-list">
          <el-empty v-if="!notifRows.length" description="没有未读通知" :image-size="50" />
          <div
            v-for="row in notifRows"
            :key="row.id"
            class="notif-item"
            @click="openNotification(row)"
          >
            <div class="notif-title">{{ row.title }}</div>
            <div class="notif-body">{{ row.body || '' }}</div>
            <div class="notif-time">{{ row.created_at ? dateTime(row.created_at) : '' }}</div>
          </div>
        </div>
      </el-popover>
      <el-dropdown @command="handleCommand">
        <span class="user-chip">
          <el-icon><UserFilled /></el-icon>
          {{ auth.displayName }}
          <el-icon class="caret"><ArrowDown /></el-icon>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item disabled>{{ auth.user?.email }}</el-dropdown-item>
            <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </el-header>

    <el-main class="app-main">
      <router-view v-slot="{ Component }">
        <keep-alive :include="['ProjectListView', 'DeviceLibraryView']">
          <component :is="Component" />
        </keep-alive>
      </router-view>
    </el-main>

    <el-footer height="26px" class="app-footer">
      © 2026 XX科技有限公司
    </el-footer>

    <!-- AI 助理悬浮窗（2026-10-04）：必须挂在**根节点下**。
         之前误插进通知气泡（el-popover）内部 —— 气泡内容是懒渲染的，
         结果就是"开关打开了但悬浮框永远不出现"。 -->
    <AiAssistantPanel v-if="auth.can('ai', 'use')" />
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100vh;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 14px;
  height: 52px;
  padding: 0 16px;
  background: #fff;
  border-bottom: 1px solid var(--c-border, #e5e7eb);
}

/* 品牌区（原型 x18「弱」+ x50 系统名） */
.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  flex: 0 0 auto;
  padding-right: 6px;
}

.brand-mark {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  background: var(--c-navy, #14243d);
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}

.brand-text {
  font-size: 14.5px;
  font-weight: 700;
  letter-spacing: 0.3px;
  white-space: nowrap;
}

/* 主导航（原型是文字导航等距排布；这里做成带 hover 的按钮） */
.top-nav {
  display: flex;
  align-items: center;
  gap: 2px;
  overflow-x: auto;
}

.nav-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 32px;
  padding: 0 12px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: #475569;
  font-size: 13.5px;
  white-space: nowrap;
  cursor: pointer;
}

.nav-item:hover {
  background: #f1f5f9;
  color: #1f2937;
}

.nav-item.active {
  background: #eef2ff;
  color: #2f5aa8;
  font-weight: 600;
}

/* 置灰项：原型有、后端没有 —— 保持可见但不可点，鼠标悬停有说明 */
.nav-item.disabled {
  color: #b6bec9;
  cursor: not-allowed;
}

.nav-item.disabled:hover {
  background: transparent;
  color: #b6bec9;
}

.spacer {
  flex: 1 1 auto;
}

.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  outline: none;
  font-size: 13.5px;
}

.caret {
  font-size: 12px;
  color: #94a3b8;
}

.app-main {
  background: #f4f6f9;
  padding: 12px 16px;
  overflow: auto;
}

.app-footer {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fff;
  border-top: 1px solid var(--c-border, #e5e7eb);
  color: #94a3b8;
  font-size: 12px;
}

.notif-badge {
  margin-right: 12px;
}
.notif-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}
.notif-list {
  max-height: 380px;
  overflow-y: auto;
}
.notif-item {
  padding: 8px 4px;
  border-bottom: 1px dashed var(--el-border-color-lighter);
  cursor: pointer;
}
.notif-item:hover {
  background: var(--el-fill-color-light);
}
.notif-title {
  font-size: 13px;
  font-weight: 600;
}
.notif-body {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.notif-time {
  font-size: 11px;
  color: var(--el-text-color-placeholder);
}
</style>





