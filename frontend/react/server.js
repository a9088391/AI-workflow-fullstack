import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'

const root = resolve('dist')
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.webp': 'image/webp' }

// 只提供 React 基底下的唯讀資源；合法深層路徑回 SPA 入口供直接刷新。
createServer(async (request, response) => {
  // 限制方法與來源路徑，避免靜態服務誤成為業務 API。
  let pathname
  try { pathname = new URL(request.url, 'http://localhost').pathname } catch { response.writeHead(400).end(); return }
  if (!pathname.startsWith('/react/') || !['GET', 'HEAD'].includes(request.method)) { response.writeHead(404).end(); return }

  // 解碼後再檢查檔案邊界，阻止編碼穿越讀取 dist 以外內容。
  let relative
  try { relative = decodeURIComponent(pathname.slice('/react/'.length)) } catch { response.writeHead(400).end(); return }
  const path = resolve(root, relative)
  if (path !== root && !path.startsWith(root + sep)) { response.writeHead(404).end(); return }

  // 缺少靜態檔時只替頁面路徑回入口；缺少 JS/CSS 應維持 404。
  try {
    const file = await readFile(path)
    response.writeHead(200, { 'Content-Type': types[extname(path)] || 'application/octet-stream' })
    response.end(request.method === 'HEAD' ? undefined : file)
  } catch {
    if (extname(path)) { response.writeHead(404).end(); return }
    const file = await readFile(resolve(root, 'index.html'))
    response.writeHead(200, { 'Content-Type': types['.html'] })
    response.end(request.method === 'HEAD' ? undefined : file)
  }
}).listen(4173, '0.0.0.0')
