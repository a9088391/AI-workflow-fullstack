export const API_BASE = 'http://localhost:8081'
export const BACKEND_KEY = 'article-collection.backend.v1'
export const BACKENDS = { laravel: API_BASE, symfony: 'http://localhost:8082', aspnet: 'http://localhost:8083', django: 'http://localhost:8084' }
export const SESSION_KEY = 'article-collection.auth.v1'
const timestamp = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/
const backends = { laravel: 'Laravel', symfony: 'Symfony', 'aspnet-core': 'ASP.NET Core', django: 'Django', flask: 'Flask', fastapi: 'FastAPI', gin: 'Gin', echo: 'Echo', fiber: 'Fiber', express: 'Express', fastify: 'Fastify', nestjs: 'NestJS' }
const blank = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u

// 固定後端顯示名稱與 API 識別值，讓建立來源和目前選擇可讀。
export function backendName(id) { return backends[id] || id }

// URL 整數可有前導零，但必須是範圍內的 ASCII 十進位正整數。
export function positiveUrlInteger(value) {
  if (typeof value !== 'string' || !/^[0-9]+$/.test(value)) return null
  const number = Number(value)
  return Number.isSafeInteger(number) && number > 0 ? number : null
}

// 只允許已實作的語意頁與安全正整數 ID，避免登入後導向外站或未實作頁。
export function validReturn(path) {
  if (['/nuxt/', '/nuxt/articles/new', '/nuxt/me/articles', '/nuxt/me/comments', '/nuxt/admin/members', '/nuxt/admin/articles', '/nuxt/admin/comments', '/nuxt/admin/settings/login-lockout'].includes(path)) return true
  const match = typeof path === 'string' && path.match(/^\/nuxt\/(?:articles\/([0-9]+)(?:\/edit)?|comments\/([0-9]+)(?:\/edit)?|admin\/members\/([0-9]+))$/)
  return Boolean(match && positiveUrlInteger(match[1] || match[2] || match[3]) !== null)
}

// Vue Router 的 route.path 不含 /nuxt 基底，產生登入網址前須補回並驗證目標。
export function loginHref(routePath) {
  let target = '/nuxt/'
  if (typeof routePath === 'string' && routePath.startsWith('/')) {
    const candidate = routePath.startsWith('/nuxt/') ? routePath : `/nuxt${routePath}`
    if (validReturn(candidate)) target = candidate
  }
  return `/nuxt/login?return_to=${encodeURIComponent(target)}`
}

// 只把兩版都已完成的頁面映成同一語意路徑，保留網址中的原始資源 ID。
export function vueFrontendTarget(path) {
  if (path === '/nuxt/' || path === '/nuxt/login' || path === '/nuxt/register' || validReturn(path)) return path.replace(/^\/nuxt/, '/vue')
  return null
}

// 只映射 React 已完成的公開、帳號、作者與管理頁，並拒絕非法 ID 或外站返回目標。
export function reactFrontendTarget(path, returnTo = null) {
  if (['/nuxt/', '/nuxt/register', '/nuxt/articles/new', '/nuxt/me/articles', '/nuxt/me/comments', '/nuxt/admin/members', '/nuxt/admin/articles', '/nuxt/admin/comments', '/nuxt/admin/settings/login-lockout'].includes(path)) return path.replace(/^\/nuxt/, '/react')
  if (path === '/nuxt/login') {
    const target = returnTo !== null && validReturn(returnTo) ? reactFrontendTarget(returnTo) : null
    if (returnTo !== null && !target) return null
    return `/react/login${target && target !== '/react/' ? `?return_to=${encodeURIComponent(target)}` : ''}`
  }
  const match = typeof path === 'string' && path.match(/^\/nuxt\/(?:(?:articles|comments)\/([0-9]+)(?:\/edit)?|admin\/members\/([0-9]+))$/)
  return match && positiveUrlInteger(match[1] || match[2]) !== null ? path.replace(/^\/nuxt/, '/react') : null
}

// Next.js 接收同組已完成的語意頁；登入返回重用安全路徑檢查並保留原始 ID。
export function nextFrontendTarget(path, returnTo = null) {
  // 已知頁面共用正整數 ID 驗證；註冊頁只允許直接切換，不作登入返回。
  if (path === '/nuxt/register' || validReturn(path)) return path.replace(/^\/nuxt/, '/next')
  if (path === '/nuxt/login') {
    // 返回頁重用同一白名單，但不能再次導向登入或註冊造成循環。
    if (returnTo === null || returnTo === undefined) return '/next/login'
    const target = nextFrontendTarget(returnTo)
    return target && target !== '/next/login' && target !== '/next/register'
      ? `/next/login${target === '/next/' ? '' : `?return_to=${encodeURIComponent(target)}`}` : null
  }
  return null
}

// 內容欄位依共同 Unicode 碼點與固定空白集合驗證，不改寫作者輸入。
export function validateContent(input, kind = 'article') {
  const errors = {}
  if (kind === 'article' && (typeof input.title !== 'string' || [...input.title].length < 1 || [...input.title].length > 200 || blank.test(input.title) || input.title.includes('\0'))) errors.title = ['欄位格式或範圍不正確']
  const maximum = kind === 'article' ? 20000 : 2000
  if (typeof input.body !== 'string' || [...input.body].length < 1 || [...input.body].length > maximum || blank.test(input.body) || input.body.includes('\0')) errors.body = ['欄位格式或範圍不正確']
  return errors
}

// 同源分頁在沒有 JWT 時仍須記得目前後端；壞資料只回首次預設。
export function readBackend(storage) {
  try {
    const value = storage.getItem(BACKEND_KEY)
    return Object.values(BACKENDS).includes(value) ? value : API_BASE
  } catch { return API_BASE }
}

// 先清除舊 JWT 再固定新來源；清除失敗時不能把舊 JWT 留給新後端。
export function selectBackend(storage, backend) {
  if (!Object.values(BACKENDS).includes(backend)) return false
  storage.removeItem(SESSION_KEY)
  storage.setItem(BACKEND_KEY, backend)
  return true
}

// 與 Vue 使用同一 sessionStorage 格式，損壞或錯誤後端資料不能用來授權。
export function readSession(storage) {
  try {
    const raw = storage.getItem(SESSION_KEY)
    if (!raw) return null
    const value = JSON.parse(raw)
    const selected = storage.getItem(BACKEND_KEY)
    // 損壞的後端選擇不可退回 Laravel 後順勢授權舊憑證。
    if (selected !== null && !Object.values(BACKENDS).includes(selected)) { storage.removeItem(SESSION_KEY); return null }
    if (value && Object.keys(value).length === 3 && value.backend === readBackend(storage) && typeof value.accessToken === 'string' && value.accessToken.length > 0 && typeof value.expiresAt === 'string' && timestamp.test(value.expiresAt)) return value
    storage.removeItem(SESSION_KEY)
  } catch {
    // 儲存權限遭拒時保守視為訪客，清理失敗也不可放行。
    try { storage.removeItem(SESSION_KEY) } catch { /* 瀏覽器拒絕儲存存取。 */ }
  }
  return null
}

// 原到期時間直接保存，不因跨前端重驗而重新簽發或延長。
export function saveSession(storage, token, expiresAt) {
  const backend = readBackend(storage)
  // 完整登入成功才修復選擇 key，避免重整後把新 JWT 當成錯後端憑證。
  storage.setItem(BACKEND_KEY, backend)
  storage.setItem(SESSION_KEY, JSON.stringify({ backend, accessToken: token, expiresAt }))
}

// 只在憑證失效、停用或登出成功等已知結果下清除共用憑證。
export function clearSession(storage) { storage.removeItem(SESSION_KEY) }

// 註冊與登入只修剪 ASCII 空格並折 ASCII 大寫，避免 K 等 Unicode 字元變成合法 Email。
export function normalizeEmail(value) {
  if (typeof value !== 'string') return ''
  return value.replace(/^ +| +$/g, '').replace(/[A-Z]/g,
    // 已由正規式限縮為 A–Z，這個轉換不會改動非 ASCII 字元。
    (letter) => letter.toLowerCase())
}

// 逐段驗證已核准的 ASCII Email，不依賴較寬鬆的瀏覽器規則。
export function validEmail(value) {
  if (value.length > 254 || !/^[\x00-\x7f]+$/.test(value)) return false
  const parts = value.split('@')
  if (parts.length !== 2) return false
  const [local, domain] = parts
  if (local.length < 1 || local.length > 64 || !/^[a-z0-9._%+-]+$/.test(local) || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return false
  const labels = domain.split('.')
  // 網域逐段檢查，避免連字號在首尾或只靠瀏覽器容錯。
  return labels.length >= 2 && labels.every(
    // 各 label 只能用 ASCII 英數與內部連字號。
    (label) => label.length >= 1 && label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label),
  ) && /^[a-z]{2,63}$/.test(labels.at(-1))
}

// 在送出前回饋已知格式錯誤；後端仍負責最終驗證與唯一性。
export function validateCredentials(input, registration = false) {
  const email = normalizeEmail(input.email)
  const errors = {}
  if (!validEmail(email)) errors.email = ['請輸入符合格式的 Email']
  if (!/^[A-Za-z0-9]{4,16}$/.test(input.password)) errors.password = ['密碼須為 4–16 碼英文字母或數字']
  if (registration) {
    const nickname = input.nickname
    if (typeof nickname !== 'string' || [...nickname].length < 1 || [...nickname].length > 30 || blank.test(nickname) || nickname.includes('\0')) errors.nickname = ['暱稱須為 1–30 個字、不可全空白或含不支援字元']
  }
  return { email, errors }
}

// 只接受契約的正整數秒數，避免 HTTP 日期或溢位造成虛假的倒數。
export function retryAfterSeconds(value) {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value)) return null
  const seconds = Number(value)
  return Number.isSafeInteger(seconds) && seconds <= Number.MAX_SAFE_INTEGER / 1000 ? seconds : null
}

// 以實際時鐘推算剩餘秒數，背景分頁被節流也不延長畫面倒數。
export function remainingSeconds(deadline, now = Date.now()) { return Math.max(0, Math.ceil((deadline - now) / 1000)) }

// 固定兩位數顯示鎖定時間，一小時以上包含時數。
export function formatCountdown(seconds) {
  // 倒數各欄固定兩位數，避免數字跳動造成版面位移。
  const two = (n) => String(n).padStart(2, '0')
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor(seconds % 3600 / 60)
  return hours ? `${two(hours)}:${two(minutes)}:${two(seconds % 60)}` : `${two(minutes)}:${two(seconds % 60)}`
}

// 只接受完整的 active Account，防止不完整身分資料被顯示為已驗證。
export function isAccount(value) {
  return value && typeof value === 'object' && !Array.isArray(value) && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.email === 'string' && value.email === normalizeEmail(value.email) && validEmail(value.email)
    && typeof value.nickname === 'string' && [...value.nickname].length >= 1 && [...value.nickname].length <= 30
    && (value.role === 'member' || value.role === 'admin') && value.status === 'active'
    && typeof value.created_at === 'string' && timestamp.test(value.created_at)
}

// 檢查 JWT 結果的原到期時間與身分資料，錯誤 envelope 不可冒充成功。
export function loginData(status, body) {
  const data = body?.data
  return status === 200 && body?.success === true && body.message === 'OK' && data?.token_type === 'Bearer'
    && typeof data.access_token === 'string' && data.access_token.length > 0
    && typeof data.expires_at === 'string' && timestamp.test(data.expires_at) && isAccount(data.user) ? data : null
}

// 一般寫入只接受空 data 的完整成功 envelope。
export function emptySuccess(status, body, expectedStatus) {
  return status === expectedStatus && body?.success === true && body.message === 'OK' && body.data && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0
}

// 解析固定錯誤碼與可顯示明細，保留 log_id 供使用者追查。
export function apiFailure(status, body, fields = []) {
  const code = body?.success === false && typeof body.error_code === 'string' ? body.error_code : ''
  const known = { E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入', E0003: '沒有權限使用此功能', E0004: '找不到指定資料或 API', E0101: '帳號或密碼錯誤', E0102: '此帳號已停用，請聯絡系統管理員', E0104: '後台認證設定異常，請聯絡系統管理員', E0105: '登入失敗次數過多，請稍後再試', E0201: '內容已更新，請確認最新內容後再送出', E9999: '系統暫時無法處理，請稍後再試' }
  const errors = {}
  const extra = []
  if (code === 'E0001' && body.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
    // 未對應欄位仍放在摘要中，不遺失伺服器說明。
    for (const [name, values] of Object.entries(body.errors)) {
      if (!Array.isArray(values)) continue
      const messages = values.filter(
        // 非文字項目不插入畫面。
        (item) => typeof item === 'string',
      )
      if (fields.includes(name)) errors[name] = messages
      else extra.push(...messages)
    }
  }
  const message = known[code] || (body?.success === false && typeof body.message === 'string' ? body.message : '伺服器回應不完整，請稍後手動重試。')
  return { status, code, message: extra.length ? `${message}：${extra.join('；')}` : message, errors, logId: typeof body?.log_id === 'string' ? body.log_id : '' }
}

// 公開作者只允許 ID 與暱稱；不得從錯誤結構推斷私有帳號資訊。
export function isAuthor(value) {
  return value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 2
    && Number.isSafeInteger(value.id) && value.id > 0 && typeof value.nickname === 'string' && [...value.nickname].length >= 1 && [...value.nickname].length <= 30 && !blank.test(value.nickname)
}

// 文章／留言共同欄位必須完整，避免不完整 API 回應被當成可讀內容。
export function isContent(value, maxBody) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Number.isSafeInteger(value.id) && value.id > 0 && Number.isSafeInteger(value.version) && value.version > 0
    && typeof value.body === 'string' && [...value.body].length >= 1 && [...value.body].length <= maxBody && !blank.test(value.body)
    && isAuthor(value.author) && Object.hasOwn(backends, value.created_backend)
    && typeof value.created_at === 'string' && timestamp.test(value.created_at)
    && typeof value.updated_at === 'string' && timestamp.test(value.updated_at)
    && (value.visibility === 'visible' || value.visibility === 'hidden') && typeof value.can_restore === 'boolean'
}

// 文章另有必要標題，保留純文字與完整閱讀入口。
export function isArticle(value) { return isContent(value, 20000) && typeof value.title === 'string' && [...value.title].length >= 1 && [...value.title].length <= 200 && !blank.test(value.title) }

// 留言必須屬於可識別的父文章，避免混入另一篇文章的清單。
export function isComment(value) { return isContent(value, 2000) && Number.isSafeInteger(value.article_id) && value.article_id > 0 }

// 列表沿用伺服器原頁碼與 total；合法超頁依然是空清單。
export function readList(status, body, page, kind = 'article', articleId = null) {
  const data = body?.data
  const p = data?.pagination
  const valid = kind === 'article' ? isArticle : isComment
  if (status !== 200 || body?.success !== true || body.message !== 'OK' || !Array.isArray(data?.list) || !data.list.every(valid)
    || !p || p.page !== page || p.per_page !== 20 || !Number.isSafeInteger(p.total) || p.total < 0 || p.total_pages !== Math.ceil(p.total / 20)) return null
  if (articleId !== null && data.list.some(
    // 列表不得包含其他文章的留言。
    (item) => item.article_id !== articleId,
  )) return null
  return data
}

// 單篇文章必須和網址 ID 相符，避免快取或錯誤回應顯示其他資源。
export function readArticle(status, body, id) { return status === 200 && body?.success === true && body.message === 'OK' && isArticle(body.data) && body.data.id === id ? body.data : null }

// 單則留言必須對上網址 ID；父文章可讀性另外驗證，不能從留言推斷。
export function readComment(status, body, id) { return status === 200 && body?.success === true && body.message === 'OK' && isComment(body.data) && body.data.id === id ? body.data : null }

// 業務 API 直接呼叫同分頁目前後端，Nuxt 伺服器僅提供頁面外殼。
export async function apiRequest(path, { method = 'GET', body, token, signal } = {}) {
  const headers = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`
  // 瀏覽器可能連 sessionStorage 屬性都拒絕讀取；公開請求仍可退回首次預設。
  let backend = API_BASE
  try { backend = readBackend(globalThis.sessionStorage) } catch { /* 無法取得同源選擇時使用 Laravel。 */ }
  const response = await fetch(`${backend}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal })
  let data
  try { data = await response.json() } catch { data = null }
  return { status: response.status, body: data, retryAfter: response.headers.get('Retry-After') }
}
