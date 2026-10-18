import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, resolve, sep } from 'node:path';

const root = resolve('dist/article-collection-angular/browser');
const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml' };

// 只服務 /angular/ 下的唯讀頁面與資源，深連結交回 Angular 客戶端判斷。
createServer(async (request, response) => {
  // 先限制方法、基底路徑與解碼後的目錄邊界，避免讀到建置目錄外。
  let pathname;
  try { pathname = new URL(request.url, 'http://localhost').pathname; }
  catch { response.writeHead(400).end(); return; }
  if (!['GET', 'HEAD'].includes(request.method) || !pathname.startsWith('/angular/')) { response.writeHead(404).end(); return; }
  let relative;
  try { relative = decodeURIComponent(pathname.slice('/angular/'.length)); }
  catch { response.writeHead(400).end(); return; }
  const path = resolve(root, relative || 'index.html');
  if (path !== root && !path.startsWith(root + sep)) { response.writeHead(404).end(); return; }

  // 已建置資源直接回傳；無副檔名的應用路徑回入口，缺失資源保留 404。
  try {
    const file = await readFile(path);
    response.writeHead(200, { 'Content-Type': types[extname(path)] || 'application/octet-stream' });
    response.end(request.method === 'HEAD' ? undefined : file);
  } catch {
    if (extname(path)) { response.writeHead(404).end(); return; }
    const file = await readFile(resolve(root, 'index.html'));
    response.writeHead(200, { 'Content-Type': types['.html'] });
    response.end(request.method === 'HEAD' ? undefined : file);
  }
}).listen(4173, '0.0.0.0');
