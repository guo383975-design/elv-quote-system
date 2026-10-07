/**
 * Markdown 渲染（2026-10-05）。
 *
 * AI 助理的回答本来就是 Markdown（`**粗体**`、表格、列表、代码块），以前按纯文本显示，
 * 用户看到的是满屏星号和竖线。这里把它渲染出来。
 *
 * ## 安全（重要）
 *
 * 渲染的是**模型输出**，而模型输出里可能夹带文件内容（用户上传的报价单）——
 * 也就是说这条链路上有"别处来的文本"进入 DOM。所以：
 *
 *   · `html: false` —— 原始 HTML **一律转义**，`<script>`、`<img onerror=...>`
 *     都只会以文字形式出现，不会被执行（这是最关键的一条）；
 *   · `linkify` 生成的链接走 markdown-it 自带的 `validateLink`，`javascript:` 被拦；
 *   · 外链统一加 `target="_blank" rel="noopener noreferrer"`，避免
 *     `window.opener` 被反向控制。
 *
 * 若哪天要开 `html: true`（比如允许模型输出 `<br>`），**必须**同时接入
 * DOMPurify 之类的消毒库 —— 只开 html 不消毒等于开后门。
 */
import MarkdownIt from 'markdown-it'

const md: MarkdownIt = new MarkdownIt({
  html: false, // ← 不允许原始 HTML（见文件头说明）
  linkify: true, // 裸 URL 自动成链接
  breaks: true, // 单个换行也换行（模型爱用单换行分行）
  typographer: false,
})

// 外链加 rel/target（默认渲染出来的 <a> 是在当前页打开的）
const defaultLinkOpen =
  md.renderer.rules.link_open ??
  ((tokens, idx, options, _env, self) => self.renderToken(tokens, idx, options))

md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen(tokens, idx, options, env, self)
}

/** 把 Markdown 文本渲染成 HTML（已转义原始 HTML，可安全 v-html）。 */
export function renderMarkdown(text: string): string {
  if (!text) return ''
  try {
    return md.render(text)
  } catch {
    // 渲染失败就退回纯文本（并转义），宁可少样式也不要白屏
    return `<p>${escapeHtml(text)}</p>`
  }
}

/** 极简转义（仅在兜底路径用） */
function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 是否含 Markdown 特征（用于决定要不要走渲染，纯文本消息省一层开销） */
export function looksLikeMarkdown(text: string): boolean {
  return /(\*\*|^#{1,6}\s|^\s*[-*+]\s|^\s*\d+\.\s|`|\|.*\|)/m.test(text || '')
}
