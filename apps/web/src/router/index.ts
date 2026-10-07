import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { registerUnauthorizedHandler } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/layouts/AppLayout.vue'),
    redirect: '/projects',
    children: [
      {
        path: 'projects',
        name: 'projects',
        component: () => import('@/views/ProjectListView.vue'),
        meta: { title: '项目列表', icon: 'Folder' },
      },
      {
        path: 'projects/:projectId/subsystems',
        name: 'project-subsystems',
        component: () => import('@/views/SubsystemEditorView.vue'),
        meta: { title: '子系统管理', icon: 'Connection' },
      },
      {
        path: 'projects/:projectId/quotes/:quoteId',
        name: 'quote-editor',
        component: () => import('@/views/QuoteEditorView.vue'),
        meta: { title: '报价编制', icon: 'Tickets' },
      },
      {
        // 原型第 1 页版式的工作台（2026-10-03 起为默认入口；旧编辑器保留以便回退）
        path: 'projects/:projectId/workbench/:quoteId',
        name: 'quote-workbench',
        component: () => import('@/views/QuoteWorkbenchView.vue'),
        meta: { title: '报价编制', icon: 'Tickets' },
      },
      {
        // 原型第 3 页：报价单预览 · 客户版
        path: 'projects/:projectId/workbench/:quoteId/preview',
        name: 'quote-preview',
        component: () => import('@/views/QuotePreviewView.vue'),
        meta: { title: '客户版预览', icon: 'Document' },
      },
      {
        // 原型第 4 页：报价单级的人工费设置
        path: 'projects/:projectId/workbench/:quoteId/labor',
        name: 'quote-labor',
        component: () => import('@/views/LaborSettingView.vue'),
        meta: { title: '人工费设置', icon: 'Coin' },
      },
      {
        path: 'devices',
        name: 'devices',
        component: () => import('@/views/DeviceLibraryView.vue'),
        meta: { title: '设备库', icon: 'Box' },
      },
      {
        // 原型顶栏导航「系统设置」
        path: 'settings',
        name: 'settings',
        component: () => import('@/views/SystemSettingsView.vue'),
        meta: { title: '系统设置', icon: 'Setting' },
      },
      {
        // 原型顶栏导航「报表中心」
        path: 'reports',
        name: 'reports',
        component: () => import('@/views/ReportCenterView.vue'),
        meta: { title: '报表中心', icon: 'DataAnalysis' },
      },
      {
        // 原型顶栏导航「点位模板」
        path: 'point-templates',
        name: 'point-templates',
        component: () => import('@/views/PointTemplateView.vue'),
        meta: { title: '点位模板', icon: 'Grid' },
      },
      {
        path: 'customers',
        name: 'customers',
        component: () => import('@/views/CustomerArchiveView.vue'),
        meta: { title: '客户档案', icon: 'User' },
      },
      {
        // 原型第 5 页设备库工具栏的「供应商库」入口
        path: 'suppliers',
        name: 'suppliers',
        component: () => import('@/views/SupplierManagementView.vue'),
        meta: { title: '供应商库', icon: 'OfficeBuilding' },
      },
      {
        path: 'labor',
        name: 'labor',
        component: () => import('@/views/LaborRateView.vue'),
        meta: { title: '人工费与定额', icon: 'Coin' },
      },
      {
        // 用户管理已收进「系统设置」页签（2026-10-04）。
        // 保留 name/path 作为旧链接与书签的入口，直接重定向过去。
        path: 'users',
        name: 'users',
        redirect: { name: 'settings', query: { tab: 'users' } },
      },
    ],
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { public: true, title: '页面不存在' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 401 时统一踢回登录页，并记住原本要去的地址。
// 注意：必须**同时清掉 pinia 里的登录态**，否则 store 里 token 还在，
// 守卫会认为已登录并把用户弹回 projects，形成死循环。
registerUnauthorizedHandler(() => {
  useAuthStore().logout()
  const current = router.currentRoute.value
  if (current.name === 'login') return
  void router.replace({ name: 'login', query: { redirect: current.fullPath } })
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 首次进入时先恢复会话，否则刷新页面会被误判为未登录。
  if (!auth.ready) {
    await auth.bootstrap()
  }

  if (to.meta.public) {
    // 已登录还去登录页，直接送回首页
    if (to.name === 'login' && auth.isLoggedIn) return { name: 'projects' }
    return true
  }

  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 设备库需要 device_library 读权限；没有就不让进（菜单本身也会隐藏）
  if (to.name === 'devices' && !auth.hasResource('device_library')) {
    return { name: 'projects' }
  }
  // 用户管理需要 user 读权限（只有 admin）；没有就把 tab 摘掉，仍留在系统设置页，
  // 而不是整页踢走 —— 设置项本身他们是有权限看的。
  if (to.name === 'settings' && to.query.tab === 'users' && !auth.hasResource('user')) {
    return { name: 'settings', query: {} }
  }
  return true
})

router.afterEach((to) => {
  const base = import.meta.env.VITE_APP_TITLE ?? '智能化弱电报价系统'
  const t = to.meta.title as string | undefined
  document.title = t ? `${t} · ${base}` : base
})

export default router






