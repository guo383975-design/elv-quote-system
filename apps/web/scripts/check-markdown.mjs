/**
 * Markdown 渲染自检（2026-10-05）。
 *
 * 跑法（在 apps/web 下）：
 *     node scripts/check-markdown.mjs
 *
 * 为什么要有这个脚本：AI 回答会被 `v-html` 插进 DOM，而回答里可能夹带
 * **用户上传文件的内容** —— 也就是"别处来的文本"进了页面。
 * 所以这里不只是看渲染效果，重点是**确认原始 HTML 被转义**（不能执行）。
 */
import { renderMarkdown, looksLikeMarkdown } from '../src/utils/markdown.ts'

let pass = 0
let fail = 0

function check(name, cond, extra = '') {
  if (cond) {
    pass += 1
    console.log(`  OK  ${name}`)
  } else {
    fail += 1
    console.log(`  XX  ${name}${extra ? ' → ' + extra : ''}`)
  }
}

// ---------- ① 该渲染的要渲染出来 ----------
const bold = renderMarkdown('**粗体** 和 `代码`')
check('粗体渲染成 <strong>', bold.includes('<strong>'), bold)
check('行内代码渲染成 <code>', bold.includes('<code>'), bold)

const table = renderMarkdown('| 型号 | 进价 |\n| --- | --- |\n| A | 100 |')
check('表格渲染成 <table>', table.includes('<table>') && table.includes('<th>'), table.slice(0, 80))

const list = renderMarkdown('- 第一条\n- 第二条')
check('列表渲染成 <ul><li>', list.includes('<ul>') && list.includes('<li>'), list)

const head = renderMarkdown('## 小标题')
check('标题渲染成 <h2>', head.includes('<h2'), head)

const link = renderMarkdown('见 https://example.com/x')
check('裸链接自动成 <a>', link.includes('<a href="https://example.com/x"'), link)
check('外链带 rel=noopener', link.includes('noopener'), link)

// ---------- ② 安全：原始 HTML 必须被转义（不能执行）----------
const script = renderMarkdown('<script>alert(1)</script>')
check('不产生真的 <script> 标签', !script.includes('<script>'), script)
check('<script> 被转义成文字', script.includes('&lt;script&gt;'), script)

const img = renderMarkdown('<img src=x onerror=alert(1)>')
check('不产生真的 <img onerror>', !/<img[^>]*onerror/i.test(img), img)

const js = renderMarkdown('[点我](javascript:alert(1))')
check('javascript: 链接不被渲染成可点链接', !js.includes('href="javascript:'), js)

const iframe = renderMarkdown('<iframe src="http://evil"></iframe>')
check('不产生 <iframe>', !iframe.includes('<iframe'), iframe)

// ---------- ③ 兜底 ----------
check('空文本返回空串', renderMarkdown('') === '')
check('纯文本仍然渲染（包在 <p> 里）', renderMarkdown('普通一句话').includes('<p>'))
check('特征识别：粗体算 Markdown', looksLikeMarkdown('**x**') === true)
check('特征识别：纯中文句子不算', looksLikeMarkdown('今天天气不错') === false)

console.log(`\n通过 ${pass}，失败 ${fail}`)
process.exit(fail === 0 ? 0 : 1)
