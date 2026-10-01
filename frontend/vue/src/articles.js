// 只接受本票已實作的本機頁面，防止登入返回目標成為任意網址。
export function localPage(path) {
  if (path === '/vue/' || path === '/vue/login' || path === '/vue/register' || path === '/vue/articles/new' || path === '/vue/me/articles' || path === '/vue/me/comments' || path === '/vue/admin/members' || path === '/vue/admin/articles' || path === '/vue/admin/comments' || path === '/vue/admin/settings/login-lockout') return true
  const member = path.match(/^\/vue\/admin\/members\/([0-9]+)$/)
  if (member) return Number.isSafeInteger(Number(member[1])) && Number(member[1]) > 0
  const match = path.match(/^\/vue\/(?:articles|comments)\/([0-9]+)(?:\/edit)?$/)
  return Boolean(match && Number.isSafeInteger(Number(match[1])) && Number(match[1]) > 0)
}

// 靜態 new 優先於 ID；未知路徑維持找不到頁面。
export function articlePage(path) {
  if (path === '/vue/') return 'articles'
  if (path === '/vue/register') return 'register'
  if (path === '/vue/login') return 'login'
  if (path === '/vue/articles/new') return 'article-new'
  if (path === '/vue/me/articles') return 'my-articles'
  if (path === '/vue/me/comments') return 'my-comments'
  if (path === '/vue/admin/members') return 'members'
  if (path === '/vue/admin/articles') return 'managed-articles'
  if (path === '/vue/admin/comments') return 'managed-comments'
  if (path === '/vue/admin/settings/login-lockout') return 'lockout-settings'
  if (/^\/vue\/admin\/members\/[0-9]+$/.test(path) && localPage(path)) return 'member'
  if (!localPage(path)) return 'missing'
  const resource = path.startsWith('/vue/comments/') ? 'comment' : 'article'
  return path.endsWith('/edit') ? `${resource}-edit` : resource
}

const blank = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u

// Unicode 碼點與契約空白集合一致；原文字及 CR/LF 原樣送出，拒絕無法儲存的 U+0000。
export function validateArticle(input) {
  const errors = {}
  if (typeof input.title !== 'string' || [...input.title].length < 1 || [...input.title].length > 200 || blank.test(input.title) || input.title.includes('\u0000')) errors.title = ['欄位格式或範圍不正確']
  if (typeof input.body !== 'string' || [...input.body].length < 1 || [...input.body].length > 20000 || blank.test(input.body) || input.body.includes('\u0000')) errors.body = ['欄位格式或範圍不正確']
  return errors
}

const backends = { laravel: 'Laravel', symfony: 'Symfony', 'aspnet-core': 'ASP.NET Core', django: 'Django', flask: 'Flask', fastapi: 'FastAPI', gin: 'Gin', echo: 'Echo', fiber: 'Fiber', express: 'Express', fastify: 'Fastify', nestjs: 'NestJS' }
const timestamp = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/
// 公開 ID／版本都須是安全正整數，避免大數在 JavaScript 中失真。
const id = (value) => Number.isSafeInteger(value) && value > 0

// 文章與留言共享作者、來源與狀態契約；只接受完整資料，避免誤顯示授權內容。
export function isContent(value, maximumBody) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && id(value.id) && id(value.version)
    && typeof value.body === 'string' && [...value.body].length >= 1 && [...value.body].length <= maximumBody && !blank.test(value.body)
    && value.author && typeof value.author === 'object' && !Array.isArray(value.author)
    && Object.keys(value.author).length === 2 && id(value.author.id) && typeof value.author.nickname === 'string' && [...value.author.nickname].length >= 1 && [...value.author.nickname].length <= 30 && !blank.test(value.author.nickname)
    && Object.hasOwn(backends, value.created_backend) && typeof value.created_at === 'string' && timestamp.test(value.created_at)
    && typeof value.updated_at === 'string' && timestamp.test(value.updated_at)
    && (value.visibility === 'visible' || value.visibility === 'hidden') && typeof value.can_restore === 'boolean'
}

// 文章另檢查標題，保留既有完整閱讀契約。
export function isArticle(value) {
  return isContent(value, 20000) && typeof value.title === 'string' && [...value.title].length >= 1 && [...value.title].length <= 200 && !blank.test(value.title)
}

// 來源識別由後端填入；前端只映射顯示名稱。
export function backendName(value) { return backends[value] || value }

// 分頁使用後端原頁碼與總數；合法超頁仍是空清單而非零筆資料。
export function readArticleList(status, body, requestedPage, resource = isArticle) {
  const data = body?.data
  const p = data?.pagination
  if (status !== 200 || body?.success !== true || body.message !== 'OK' || !Array.isArray(data?.list) || !data.list.every(resource)
    || !p || p.page !== requestedPage || p.per_page !== 20 || !Number.isSafeInteger(p.total) || p.total < 0
    || p.total_pages !== Math.ceil(p.total / 20)) return null
  return data
}

// 單篇回應須對上所請 ID，避免錯誤資源出現在網址下。
export function readArticle(status, body, requestedId) {
  return status === 200 && body?.success === true && body.message === 'OK' && isArticle(body.data) && body.data.id === requestedId ? body.data : null
}

// 發文成功只接受空 data；新 ID 不能從回應猜測。
export function articleCreated(status, body) {
  return status === 201 && body?.success === true && body.message === 'OK' && body.data && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0
}

// 錯誤依 code 控制流程，欄位明細與未知欄位訊息保留供修正。
export function articleFailure(body) {
  const code = body?.success === false && typeof body.error_code === 'string' ? body.error_code : ''
  const known = { E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入', E0003: '沒有權限使用此功能', E0004: '找不到指定資料或 API', E0102: '此帳號已停用，請聯絡系統管理員', E0201: '內容已更新，請確認最新內容後再送出', E9999: '系統暫時無法處理，請稍後再試' }
  const errors = {}
  const extra = []
  if (code === 'E0001' && body.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
    for (const [name, values] of Object.entries(body.errors)) {
      if (!Array.isArray(values)) continue
      // 非文字明細不能放進畫面，未知欄位仍列在摘要中。
      const messages = values.filter((item) => typeof item === 'string')
      if (name === 'title' || name === 'body') errors[name] = messages
      else extra.push(...messages)
    }
  }
  const message = known[code] || (body?.success === false && typeof body.message === 'string' ? body.message : '伺服器回應不完整，請稍後手動重試。')
  return { code, errors, message: extra.length ? `${message}：${extra.join('；')}` : message, logId: typeof body?.log_id === 'string' ? body.log_id : '' }
}

// 已知資源更新只接受 200 空 data；成功後仍須 GET 同一 ID 確認伺服器值。
export function contentUpdated(status, body) {
  return status === 200 && body?.success === true && body.message === 'OK' && body.data && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0
}
