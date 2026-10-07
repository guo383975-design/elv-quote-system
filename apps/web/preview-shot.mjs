// 客户版预览：截图 + 打印成 PDF（与 Ctrl+P 完全一致）—— 2026-10-05
// 用法：node preview-shot.mjs <url> <token> <outPdf> <outPng>
// 说明：--print-to-pdf 这条路与浏览器打印对话框同一套分页引擎 ✓（预览=打印 ✓）。
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const [url, token, outPdf, outPng] = process.argv.slice(2)
if (!url || !token) {
  console.error('参数不足：node preview-shot.mjs <url> <token> <outPdf> <outPng>')
  process.exit(2)
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--font-render-hinting=none'],
})
try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1680, height: 2400, deviceScaleFactor: 1 })
  // 先把登录令牌塞进 localStorage ✓（键名来自 apps/web/src/api/client.ts：quote.token ✓）
  await page.evaluateOnNewDocument((t) => {
    try {
      localStorage.setItem('quote.token', t)
    } catch (e) {
      /* ignore */
    }
  }, token)

  await page.goto(url, { waitUntil: 'networkidle2', timeout: 90000 })
  await page.waitForSelector('.qp-sheet', { timeout: 60000 })
  await new Promise((r) => setTimeout(r, 2000))   // 等字体/布局稳定 ✓

  // 读屏幕上的关键数据（用于与真实打印页数对比 ✓）
  const info = await page.evaluate(() => {
    const sheet = document.querySelector('.qp-sheet')
    // 有纸盒就报纸盒的页脚 ✓（.qp-sheet 里的那份是量尺纸的，会先被 querySelector 抓走 ✗）
    const foot = document.querySelector('.qp-page .doc-foot') ?? document.querySelector('.doc-foot')
    const bottom = document.querySelector('.doc-bottom')
    const cs = sheet ? getComputedStyle(sheet) : null
    const pages = Array.from(document.querySelectorAll('.qp-page'))
    return {
      screenFooter: foot ? foot.textContent.replace(/\s+/g, ' ').trim() : '(无)',
      sheetScrollHeight: sheet ? sheet.scrollHeight : 0,
      sheetClientWidth: sheet ? sheet.clientWidth : 0,
      sheetClass: sheet ? sheet.className : '',
      paddingTop: cs ? cs.paddingTop : '',
      pageH: 0,
      bottomTop: bottom ? bottom.offsetTop - (sheet ? sheet.offsetTop : 0) : -1,
      breakBefore: bottom ? bottom.className.includes('qp-break-before') : false,
      // A 步骤②：纸盒自检 ✓（几张纸 / 每张的页码 / 每张多高 / 每张几行明细 ✓）
      pageBoxCount: pages.length,
      pageFooters: pages.map((p) => {
        const f = p.querySelector('.doc-foot')
        return f ? f.textContent.replace(/\s+/g, ' ').trim() : '(无)'
      }),
      pageHeights: pages.map((p) => Math.round(p.getBoundingClientRect().height)),
      pageRowCounts: pages.map((p) => p.querySelectorAll('.doc-table tbody tr').length),
      pageHasTail: pages.map((p) => !!p.querySelector('.doc-sign')),
      // 量尺纸里的行数：与所有纸盒行数之和对照 ✓（少一行就是静默裁掉 ✗）
      sheetRowCount: document.querySelectorAll('.qp-sheet .doc-table tbody tr').length,
    }
  })
  console.log('SCREEN ' + JSON.stringify(info))

  // 把页面自检的数字也读出来 ✓（我加的 __qpDebug ✓）
  const dbg = await page.evaluate(() => (window).__qpDebug ?? null)
  console.log('DEBUG ' + JSON.stringify(dbg))

  // 稳定性探针 ✓（2026-10-05 A 步骤② ✓）：量尺纸里那段人工空白若在**抖动** ✓，
  //   说明"量一次 → 加空白 → 再量"成了自反馈环 ✗（量尺纸藏起来之后症状只是白烧 CPU ✓，
  //   但分页计划会跟着一次次重算 → 整叠纸反复重渲染 ✓）。取 4 个样本，值必须完全一致 ✓。
  const gapSamples = []
  for (let i = 0; i < 4; i++) {
    gapSamples.push(
      await page.evaluate(() => {
        const s = document.querySelector('.qp-break-spacer')
        return s ? Math.round(s.getBoundingClientRect().height * 100) / 100 : 0
      }),
    )
    await new Promise((r) => setTimeout(r, 700))
  }
  console.log('STABLE ' + JSON.stringify({ gapSamples, stable: new Set(gapSamples).size === 1 }))
  await page.screenshot({ path: outPng, fullPage: true })
  // ★ 这一句就是"打印" ✓：Chrome 的打印分页引擎，与 Ctrl+P 完全同源 ✓
  await page.pdf({
    path: outPdf,
    format: 'A4',
    landscape: true,
    printBackground: true,
    preferCSSPageSize: true,
    margin: { top: '12mm', right: '12mm', bottom: '12mm', left: '12mm' },
  })
  // PDF 实际几页 ✓（"屏幕 N 页 = 打印 N 页"这句话的**直接证据** ✓；
  //   Chrome 的 PDF 里 /Type /Page 是明文 ✓，直接数就行 ✓）
  try {
    const { readFileSync } = await import('node:fs')
    const raw = readFileSync(outPdf).toString('latin1')
    const count = (raw.match(/\/Type\s*\/Page[^s]/g) ?? []).length
    const declared = raw.match(/\/Type\s*\/Pages[^>]*?\/Count\s+(\d+)/)
    console.log('PDFPAGES ' + JSON.stringify({ actual: count, declared: declared ? Number(declared[1]) : null }))
  } catch (e) {
    console.log('PDFPAGES ' + JSON.stringify({ error: String(e) }))
  }
  console.log('OK pdf=' + outPdf + ' png=' + outPng)
} finally {
  await browser.close()
}
