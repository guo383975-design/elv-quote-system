/**
 * 报价有效期「倒计时」的纯计算（2026-10-07 用户要求）。
 *
 * 口径与后端保持一致（apps/api/app/services/export_data.py 的
 * `_quote_date` / `_valid_until` / `_export_settings`）：
 *
 *   有效期至 = 报价日期 + 有效天数
 *     · 报价日期 = `quote.created_at` 取**本地时区**那一天；
 *     · 有效天数取值链（务必按此顺序）：
 *         ① terms_override.valid_days   每单条款覆盖
 *         ② valid_days_override         旧字段，兼容
 *         ③ 系统设置 quote.valid_days   与后端 quote_terms.build_variables 同源
 *         ④ 兜底 30 天
 *
 * 放在这里而不是写在视图里，是为了让这段"日期算术"能被单独跑一遍验证
 * （跨月/跨年、今天到期、已过期各一条），视图只负责渲染。
 */
import type { Quote } from '@/api/types'

/** 兜底有效期（天）：与后端 QUOTE_VALID_DAYS / DEFAULT_TERMS 一致。 */
export const FALLBACK_VALID_DAYS = 30

/** normal = >7 天；soon = ≤7 天；expired = 已过期。 */
export type ValidityLevel = 'normal' | 'soon' | 'expired'

export interface ValidityInfo {
  /** 本单生效的有效期天数 */
  days: number
  /** 有效期至那一天（本地时区 00:00） */
  validUntil: Date
  /** 剩余天数：>0 还有；=0 今天到期；<0 已过期 |remaining| 天 */
  remaining: number
  level: ValidityLevel
  /** 直接可显示的文字 */
  text: string
}

const DAY_MS = 86400000

/** 宽松转数字：null / undefined / '' / 非数字 → null（不要静默当成 0 天）。 */
function num(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** 本地时区那一天的 00:00 —— 不用 UTC 计算，否则东八区的单子会差一天。 */
export function localMidnight(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

/** YYYY-MM-DD（本地日期；不经 toISOString，避免时区偏移把日期挪走）。 */
export function formatLocalDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** 本单生效的有效期天数：条款覆盖 → 旧字段 → 系统设置 → 30 天。 */
export function effectiveValidDays(
  quote: Pick<Quote, 'terms_override' | 'valid_days_override'> | null | undefined,
  settingsValidDays?: number | null,
): number {
  const own = num(quote?.terms_override?.valid_days)
  if (own !== null && own > 0) return Math.trunc(own)
  const legacy = num(quote?.valid_days_override)
  if (legacy !== null && legacy > 0) return Math.trunc(legacy)
  const fromSettings = num(settingsValidDays)
  if (fromSettings !== null && fromSettings > 0) return Math.trunc(fromSettings)
  return FALLBACK_VALID_DAYS
}

/**
 * 倒计时。`created_at` 解析不出来时返回 null（界面显示「—」，
 * 而不是拿一个凭空的日期去骗用户）。
 */
export function validityInfo(
  createdAt: string | null | undefined,
  validDays: number,
  now: Date = new Date(),
): ValidityInfo | null {
  if (!createdAt) return null
  const created = new Date(createdAt)
  if (Number.isNaN(created.getTime())) return null
  const days = Math.max(0, Math.trunc(validDays))
  // ⚠️ 起点口径必须与**后端导出 PDF**一致：export_data._quote_date 取的是
  //   「创建时间在服务器本地的日历日」，而服务器时区实测为 **UTC** ✓
  //   → 所以这里用 UTC 日历日 ✗ 不能用浏览器本地日（否则本地凌晨建的单会差一天 ✗，
  //     项目列表那一列原先是本地日 → 与工作台/PDF 不一致，2026-10-08 统一过来 ✓）。
  const start = new Date(Date.UTC(created.getUTCFullYear(), created.getUTCMonth(), created.getUTCDate()))
  const validUntil = new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate() + days))
  const remaining = Math.round(
    (validUntil.getTime() - localMidnight(now).getTime()) / DAY_MS,
  )

  const level: ValidityLevel = remaining < 0 ? 'expired' : remaining <= 7 ? 'soon' : 'normal'
  const text =
    remaining > 0 ? `剩余 ${remaining} 天` : remaining === 0 ? '今天到期' : `已过期 ${-remaining} 天`
  return { days, validUntil, remaining, level, text }
}

/** 悬浮说明：把算式摆出来（便于与报价日期核对，也方便排查"数字不对"）。 */
export function validityHint(
  createdAt: string | null | undefined,
  info: ValidityInfo,
): string {
  const created = createdAt ? new Date(createdAt) : null
  const from =
    created && !Number.isNaN(created.getTime()) ? formatLocalDate(localMidnight(created)) : '—'
  return `报价日期 ${from} + 有效天数 ${info.days} 天 = 有效期至 ${formatLocalDate(info.validUntil)}`
}

/** el-tag 的类型：>7 天普通（灰）/ ≤7 天橙 / 已过期红。 */
export function validityTagType(level: ValidityLevel): 'info' | 'warning' | 'danger' {
  if (level === 'expired') return 'danger'
  if (level === 'soon') return 'warning'
  return 'info'
}
