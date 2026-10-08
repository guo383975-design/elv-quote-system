/**
 * 「保存并重算」成功后的提示文案（2026-10-08，全站**单一来源**）。
 *
 * 为什么单独一个文件：`POST /quotes/{id}/compute` 有**三个**人主动点的入口 ——
 * 工作台「保存并重算」、报价编制页「保存并重算」、人工费设置「保存」✓。
 * 语义变了（现在会落一版快照 ✓），三处提示必须口径一致 ✓；
 * 各自拼字符串迟早会漂移（一个说存了、一个不说 ✗），所以只留这一份 ✓。
 *
 * 口径：
 *   · 真落了版 → 「已保存版本（时间，共 N 个版本）· <尾句>」✓
 *   · 因"内容没变"被去重跳过 → 「内容未变化，未新增版本 · <尾句>」✓
 *     —— **不能**假装存了一版 ✗（用户会去版本历史里找一个不存在的版本 ✗）
 */
import { dateTimeSec } from './format'

/** compute 接口返回里与版本有关的字段（见后端 ComputeResult）。 */
export interface ComputeVersionInfo {
  revision_saved?: boolean
  revision_created_at?: string | null
  revision_count?: number
}

/**
 * 拼出提示文本。
 *
 * @param res  compute 接口的响应（用它的 revision_* 三个字段）
 * @param tail 这次动作的业务描述，如「已按当前费率重算」「人工费已重算」
 */
export function computeSavedMessage(res: ComputeVersionInfo, tail: string): string {
  if (res?.revision_saved) {
    const n = res.revision_count ?? 0
    const at = dateTimeSec(res.revision_created_at)
    const count = n > 0 ? `，共 ${n} 个版本` : ''
    return `已保存版本（${at}${count}）· ${tail}`
  }
  return `内容未变化，未新增版本 · ${tail}`
}
