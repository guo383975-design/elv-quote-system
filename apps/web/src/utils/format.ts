/**
 * 金额 / 比率的归一化与展示。
 *
 * 后端所有 Decimal 字段序列化后可能是字符串（"58480.00"）也可能是数字，
 * 所以**任何算术都必须先过 toNum()**，不要直接相加。
 */
import type { Decimalish } from '@/api/types'

/** 安全转数字。null/undefined/''/非数字一律回 0（而不是 NaN）。 */
export function toNum(v: Decimalish | null | undefined): number {
  if (v === null || v === undefined || v === '') return 0
  if (typeof v === 'number') return Number.isFinite(v) ? v : 0
  const n = Number(v)
  return Number.isFinite(n) ? n : 0
}

/** 千分位金额，默认 2 位小数。 */
export function money(v: Decimalish | null | undefined, digits = 2): string {
  return toNum(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** 带 ¥ 前缀的金额。 */
export function yuan(v: Decimalish | null | undefined, digits = 2): string {
  return `¥${money(v, digits)}`
}

/** 万元展示，用于大额汇总。 */
export function wan(v: Decimalish | null | undefined, digits = 2): string {
  return `${(toNum(v) / 10000).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })} 万`
}

/** 比率（0.06）转百分比字符串（"6.00%"）。 */
export function percent(v: Decimalish | null | undefined, digits = 2): string {
  return `${(toNum(v) * 100).toFixed(digits)}%`
}

/** 比率字段回填输入框时用：0.06 -> 6（因为界面按「%」录入更符合造价习惯）。 */
export function ratioToPercentInput(v: Decimalish | null | undefined): number {
  return Number((toNum(v) * 100).toFixed(4))
}

/** 输入框的「%」值转回比率：6 -> 0.06。 */
export function percentInputToRatio(v: number | null | undefined): number {
  return Number(((v ?? 0) / 100).toFixed(6))
}

/** ISO 时间 -> YYYY-MM-DD HH:mm。 */
export function dateTime(v: string | null | undefined): string {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/**
 * ISO 时间 -> YYYY-MM-DD HH:mm:ss（**版本标记专用**，2026-10-08 用户要求精确到秒）。
 *
 * 为什么版本标记要秒级：用户的口径是「每次保存并重算 = 一个版本，版本号以时间为标记」✓，
 * 而连续保存两次往往落在同一分钟里 —— 分钟粒度下两个版本会显示成**一模一样**的字符串 ✗，
 * 用户就没法用它区分两版了 ✓。
 *
 * ⚠ 只给版本标记用 ✓；操作留痕之类继续用 `dateTime()`（分钟）—— 免得整页时间都变长更难读。
 */
export function dateTimeSec(v: string | null | undefined): string {
  if (!v) return '—'
  const d = new Date(v)
  if (Number.isNaN(d.getTime())) return v
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

/** ISO 时间 -> YYYY-MM-DD。 */
export function dateOnly(v: string | null | undefined): string {
  if (!v) return '—'
  return v.slice(0, 10)
}

/** 数量展示：最多 3 位小数，去掉无意义的尾零（12.000 -> 12）。 */
export function qty(v: Decimalish | null | undefined): string {
  const n = toNum(v)
  return n.toLocaleString('zh-CN', { maximumFractionDigits: 3 })
}
