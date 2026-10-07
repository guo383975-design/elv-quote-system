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
    const foot = document.querySelector('.doc-foot')
    const bottom = document.querySelector('.doc-bottom')
    const cs = sheet ? getComputedStyle(sheet) : null
    return {
      screenFooter: foot ? foot.textContent.replace(/\s+/g, ' ').trim() : '(无)',
      sheetScrollHeight: sheet ? sheet.scrollHeight : 0,
      sheetClientWidth: sheet ? sheet.clientWidth : 0,
      sheetClass: sheet ? sheet.className : '',
      paddingTop: cs ? cs.paddingTop : '',
      pageH: 0,
      bottomTop: bottom ? bottom.offsetTop - (sheet ? sheet.offsetTop : 0) : -1,
      breakBefore: bottom ? bottom.className.includes('qp-break-before') : false,
    }
  })
  console.log('SCREEN ' + JSON.stringify(info))

  // 把页面自检的数字也读出来 ✓（我加的 __qpDebug ✓）
  const dbg = await page.evaluate(() => (window).__qpDebug ?? null)
  console.log('DEBUG ' + JSON.stringify(dbg))
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
  console.log('OK pdf=' + outPdf + ' png=' + outPng)
} finally {
  await browser.close()
}
