import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, resolve, sep } from 'node:path'

const root = resolve('dist')
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml' }

// 只提供 /vue/ 下的靜態檔；未知前端路徑回 SPA 入口，讓重新整理仍能顯示正確頁面。
createServer(async (request, response) => {
  // 先解析 URL 並限制唯讀方法與 Vue 基底路徑，不處理業務 API。
  let pathname
  try {
    pathname = new URL(request.url, 'http://localhost').pathname
  } catch {
    response.writeHead(400)
    response.end()
    return
  }
  if (!pathname.startsWith('/vue/') || !['GET', 'HEAD'].includes(request.method)) {
    response.writeHead(404)
    response.end()
    return
  }

  // 解碼後再次檢查目錄邊界，避免編碼路徑讀取 dist 外的檔案。
  let relative
  try {
    relative = decodeURIComponent(pathname.slice('/vue/'.length))
  } catch {
    response.writeHead(400)
    response.end()
    return
  }
  const path = resolve(root, relative)
  if (path !== root && !path.startsWith(root + sep)) {
    response.writeHead(404)
    response.end()
    return
  }

  // 已建置的資源直接送出；其餘 Vue 頁面交給客戶端顯示入口或找不到頁面。
  try {
    const file = await readFile(path)
    response.writeHead(200, { 'Content-Type': types[extname(path)] || 'application/octet-stream' })
    response.end(request.method === 'HEAD' ? undefined : file)
  } catch {
    const file = await readFile(resolve(root, 'index.html'))
    response.writeHead(200, { 'Content-Type': types['.html'] })
    response.end(request.method === 'HEAD' ? undefined : file)
  }
}).listen(4173, '0.0.0.0')
