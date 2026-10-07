/**
 * 浏览器下载的小工具。
 *
 * 这里集中了几个踩过坑的细节，别在页面里再抄一遍：
 *  1. 游离的 `<a>` 在部分 Firefox 上不触发下载 → 必须先挂进 document 再 click。
 *  2. 立刻 `revokeObjectURL` 会在 WebKit/Safari 上把还没开始的下载掐断 → 延后一拍。
 *  3. 从 `Content-Disposition` 取文件名时，中文名走的是 RFC 5987 的
 *     `filename*=UTF-8''…`，必须 `decodeURIComponent`，否则文件名会是
 *     `%E6%8A%A5%E4%BB%B7%E5%8D%95…` 这种乱码。
 */

/** 触发一次浏览器下载。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * 从 `Content-Disposition` 解析文件名。
 *
 * 优先取 RFC 5987 的 `filename*=UTF-8''…`（中文名在这里）；
 * 没有就退回 ASCII 的 `filename="…"`；都没有就返回 null 由调用方兜底。
 */
export function filenameFromDisposition(header: string | undefined | null, fallback: string): string {
  if (!header) return fallback
  const star = /filename\*\s*=\s*([^;]+)/i.exec(header)
  if (star) {
    // 形如 UTF-8''%E6%8A%A5...；语言标签可以不写，所以按第一个 ' 之后切
    const raw = star[1].trim().replace(/^["']|["']$/g, '')
    const parts = raw.split("''")
    const encoded = parts.length > 1 ? parts[1] : raw
    try {
      const decoded = decodeURIComponent(encoded)
      if (decoded) return decoded
    } catch {
      // 编码坏了就继续往下用 ASCII 兜底，不要因为文件名把整个下载搞失败
    }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header)
  if (plain && plain[1].trim()) return plain[1].trim()
  return fallback
}
