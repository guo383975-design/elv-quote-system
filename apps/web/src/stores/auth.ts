/**
 * 认证状态 + 当前用户权限。
 *
 * 角色能力判断**只用后端 /auth/me/permissions 返回的 actions 矩阵**，
 * 不在前端硬编码权限表，避免前后端规则漂移
 * （唯一事实源是后端 app/core/permissions.py）。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { authApi } from '@/api'
import { TOKEN_KEY, USER_KEY } from '@/api/client'
import type { Role, User } from '@/api/types'

function readCachedUser(): User | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as User
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
// 是否必须先改口令（2026-10-05 ✓）：登录响应带过来 ✓，持久化以挺过刷新 ✓；
// 真正的事实来源在后端数据库 ✓，这里只是"提示与阻断" ✓。
const MUST_KEY = 'quote.mustChangePwd'
const mustChangePassword = ref<boolean>(localStorage.getItem(MUST_KEY) === '1')

function setMustChangePassword(v: boolean): void {
  mustChangePassword.value = v
  if (v) localStorage.setItem(MUST_KEY, '1')
  else localStorage.removeItem(MUST_KEY)
}
  const user = ref<User | null>(readCachedUser())
  const resources = ref<string[]>([])
  const actions = ref<Record<string, string[]>>({})
  const ready = ref(false)

  const isLoggedIn = computed(() => Boolean(token.value))
  const role = computed<Role | null>(() => user.value?.role ?? null)
  const displayName = computed(() => user.value?.full_name || user.value?.username || '')

  // 字段级可见性（2026-10-05）：**一律用后端返回的标志位**。
  // 以前这里硬编码了角色名单（还带着 estimator，与新口径相反），
  // 报表页又抄了一份 —— 两份口径迟早打架（这次就打了）。
  const serverCanSeeCost = ref(false)
  const serverCanSeeProfit = ref(false)

  /** 能否看成本/进价（后端 core/visibility 判定，系统设置里可按角色改）。 */
  const canSeeCost = computed(
    () => Boolean(user.value?.is_superuser) || serverCanSeeCost.value,
  )

  /** 能否看利润（毛利/利润看板）—— 与成本是**两档**：财务看得到、采购看不到。 */
  const canSeeProfit = computed(
    () => Boolean(user.value?.is_superuser) || serverCanSeeProfit.value,
  )

  /** 是否可编辑报价（对齐后端 quote.update；采购/经理/财务/只读均不可写）。 */
  const canWrite = computed(() => {
    if (!user.value) return false
    if (user.value.is_superuser) return true
    return (actions.value['quote'] ?? []).includes('update')
  })

  /** 细粒度判断：后端说这个角色对 resource.action 有没有权限。 */
  function can(resource: string, action: string): boolean {
    if (user.value?.is_superuser) return true
    return (actions.value[resource] ?? []).includes(action)
  }

  function hasResource(name: string): boolean {
    return resources.value.includes(name)
  }

  function persist(): void {
    if (token.value) localStorage.setItem(TOKEN_KEY, token.value)
    else localStorage.removeItem(TOKEN_KEY)
    if (user.value) localStorage.setItem(USER_KEY, JSON.stringify(user.value))
    else localStorage.removeItem(USER_KEY)
  }

  function clear(): void {
    token.value = null
    user.value = null
    resources.value = []
    actions.value = {}
    persist()
  }

  /** 拉当前用户 + 权限矩阵。失败就清态并把异常抛给调用方（守卫会跳登录页）。 */
  async function loadMe(): Promise<void> {
    const me = await authApi.me()
    const perms = await authApi.permissions()
    user.value = me
    resources.value = perms.resources ?? []
    actions.value = perms.actions ?? {}
    serverCanSeeCost.value = Boolean(perms.can_see_cost)
    serverCanSeeProfit.value = Boolean(perms.can_see_profit)
    persist()
    ready.value = true
  }

  async function login(username: string, password: string): Promise<void> {
    const resp = await authApi.login(username, password)
    token.value = resp.access_token
    setMustChangePassword(Boolean((resp as { must_change_password?: boolean }).must_change_password))
    persist()
    try {
      await loadMe()
    } catch (err) {
      // 登录本身成功了，但 me/permissions 失败。不清理的话会留下
      // 「token 已持久化、isLoggedIn=true、resources 为空」的半程态：
      // 界面表现是已登录但没有菜单、按什么都是无权限，刷新才回登录页。
      clear()
      throw err
    }
  }

  /** 应用启动时恢复会话：有 token 就先验一次，避免拿过期 token 进主页。 */
  async function bootstrap(): Promise<void> {
    if (!token.value) {
      ready.value = true
      return
    }
    try {
      await loadMe()
    } catch {
      clear()
    }
  }

  function logout(): void {
    clear()
  }

  return {
    token,
    user,
    resources,
    actions,
    ready,
    isLoggedIn,
    role,
    displayName,
    mustChangePassword,
    setMustChangePassword,
    canSeeCost,
    canSeeProfit,
    canWrite,
    can,
    hasResource,
    login,
    loadMe,
    bootstrap,
    logout,
  }
})
