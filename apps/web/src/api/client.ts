/**
 * axios 实例 + JWT 注入 + 统一错误提示 + 401 自动登出。
 *
 * 后端错误体有两种形状：
 *   - HTTPException          -> { detail: "人类可读的中文" }
 *   - RequestValidationError -> { error, detail: [{loc, msg, type}], path }
 * 所以抽取 message 时要同时兼容字符串与数组。
 */
import axios, { AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

export const TOKEN_KEY = 'quote.token'
export const USER_KEY = 'quote.user'

/** 401 时由 router 注册的回调，避免 api 层直接依赖 router 造成循环引用。 */
let onUnauthorized: (() => void) | null = null
export function registerUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn
}

export const http: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE ?? '/api/v1',
  timeout: 60_000,
})

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

interface ValidationIssue {
  loc?: (string | number)[]
  msg?: string
}

/** 把后端的各种错误体压成一句可展示的中文。 */
export function extractErrorMessage(err: unknown): string {
  const ax = err as AxiosError<{ detail?: string | ValidationIssue[] | { message?: string; error?: string }; error?: string }>
  if (!ax?.response) {
    if (ax?.code === 'ECONNABORTED') return '请求超时，请稍后重试'
    return '无法连接后端服务，请确认 API 已启动'
  }
  const data = ax.response.data
  if (!data) return `请求失败 (HTTP ${ax.response.status})`
  if (typeof data.detail === 'string' && data.detail) return data.detail
  if (Array.isArray(data.detail)) {
    const msgs = data.detail
      .map((d) => {
        const where = (d.loc ?? []).filter((p) => p !== 'body').join('.')
        return where ? `${where}: ${d.msg ?? ''}` : (d.msg ?? '')
      })
      .filter(Boolean)
    if (msgs.length) return `参数校验失败 — ${msgs.join('；')}`
  }
  if (typeof data.error === 'string' && data.error) return data.error
  return `请求失败 (HTTP ${ax.response.status})`
}

/**
 * 把「二进制下载接口」的错误体还原成 JSON。
 *
 * `responseType: 'blob'` 的请求失败时，后端的 `{detail: "中文原因"}` 会被
 * 包成一个 Blob，`extractErrorMessage` 读不到 `detail`，于是用户只看到
 * 「请求失败 (HTTP 403)」，真正的「你没有成本权限」被吞掉了。
 * 这里在弹提示之前先把 Blob 读成文本并解析回对象。
 */
async function normalizeBlobError(err: AxiosError): Promise<void> {
  const resp = err.response
  const data = resp?.data as unknown
  if (!resp || !(data instanceof Blob)) return
  try {
    const text = await data.text()
    // 只有确实是 JSON 才替换；HTML 错误页之类保持原样（否则会误导）
    resp.data = JSON.parse(text)
  } catch {
    /* 不是 JSON：维持原样，让 extractErrorMessage 走通用分支 */
  }
}

/**
 * 令牌刷新（2026-10-04，H-8）：401 时先用当前令牌换一张新的再重试一次。
 *
 * 只重试一次、只对"非刷新接口本身"生效，避免打转。
 * 后端 `/auth/refresh` 要求当前令牌**仍然有效**，所以真过期或被强制下线时
 * 这条路也会失败，那就按原来的逻辑清态回登录页。
 */
let refreshing: Promise<string | null> | null = null

async function tryRefreshToken(): Promise<string | null> {
  if (!refreshing) {
    refreshing = (async () => {
      const token = localStorage.getItem(TOKEN_KEY)
      if (!token) return null
      try {
        const resp = await axios.post(
          `${http.defaults.baseURL ?? '/api/v1'}/auth/refresh`,
          {},
          { headers: { Authorization: `Bearer ${token}` } },
        )
        const next = (resp.data as { access_token?: string }).access_token
        if (next) {
          localStorage.setItem(TOKEN_KEY, next)
          return next
        }
        return null
      } catch {
        return null
      } finally {
        // 让并发请求共用同一次刷新，但下一次 401 允许再刷
        setTimeout(() => {
          refreshing = null
        }, 0)
      }
    })()
  }
  return refreshing
}

http.interceptors.response.use(
  (resp) => resp,
  async (err: AxiosError) => {
    await normalizeBlobError(err)
    const status = err.response?.status
    const cfg = err.config as (InternalAxiosRequestConfig & { _retried?: boolean }) | undefined
    const isRefreshCall = (cfg?.url ?? '').includes('/auth/refresh')

    if (status === 401 && cfg && !cfg._retried && !isRefreshCall) {
      const fresh = await tryRefreshToken()
      if (fresh) {
        cfg._retried = true
        cfg.headers.Authorization = `Bearer ${fresh}`
        return http.request(cfg)
      }
    }

    if (status === 401) {
      // 令牌过期/无效：清掉本地态并回登录页。这里不弹 message，由登录页提示。
      localStorage.removeItem(TOKEN_KEY)
      localStorage.removeItem(USER_KEY)
      onUnauthorized?.()
    } else if (status === 403) {
      ElMessage.warning(extractErrorMessage(err))
    } else {
      ElMessage.error(extractErrorMessage(err))
    }
    return Promise.reject(err)
  },
)

