// PDF 页数/每页画面的"眼睛"（2026-10-05）：本机没有 pdftoppm/gs/mutool，
// 所以借 Chrome 自己把 PDF 渲染成 PNG —— 供人核对"打印所得"每页长什么样。
// 用法：node pdf-shot.mjs <in.pdf> <outPrefix>
import puppeteer from 'puppeteer-core'
import { pathToFileURL } from 'node:url'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const [inPdf, outPrefix] = process.argv.slice(2)
if (!inPdf || !outPrefix) {
  console.error('参数不足：node pdf-shot.mjs <in.pdf> <outPrefix>')
  process.exit(2)
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-dev-shm-usage', '--font-render-hinting=none'],
})
try {
  const page = await browser.newPage()
  await page.setViewport({ width: 1400, height: 2400, deviceScaleFactor: 1 })
  const url = pathToFileURL(inPdf).href
  const resp = await page.goto(url, { waitUntil: 'load', timeout: 60000 })
  const ct = resp ? resp.headers()['content-type'] ?? '' : ''
  console.log('GOTO ' + JSON.stringify({ url, contentType: ct, status: resp ? resp.status() : null }))
  await new Promise((r) => setTimeout(r, 2500))
  const shot = outPrefix + '.png'
  await page.screenshot({ path: shot, fullPage: true })
  console.log('OK png=' + shot)
} finally {
  await browser.close()
}
