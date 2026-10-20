export const BACKEND_KEY = 'article-collection.backend.v1'
export const SESSION_KEY = 'article-collection.auth.v1'
export const BACKENDS = {
  laravel: ['Laravel', 'http://localhost:8081'], symfony: ['Symfony', 'http://localhost:8082'],
  'aspnet-core': ['ASP.NET Core Controllers', 'http://localhost:8083'], django: ['Django', 'http://localhost:8084'],
  flask: ['Flask', 'http://localhost:8085'], fastapi: ['FastAPI', 'http://localhost:8086'],
  gin: ['Gin', 'http://localhost:8087'], echo: ['Echo', 'http://localhost:8088'],
  fiber: ['Fiber', 'http://localhost:8089'], express: ['Express', 'http://localhost:8090'],
  fastify: ['Fastify', 'http://localhost:8091'], nestjs: ['NestJS', 'http://localhost:8092'],
}
export const FRONTENDS = { vue: 'Vue 3＋Vite', nuxt: 'Nuxt', react: 'React＋Vite', next: 'Next.js', angular: 'Angular', sveltekit: 'SvelteKit' }
// 前端切換只接受固定的六版與十二個 API 來源，避免不明目的地接收共用登入。
export function canSwitchFrontend(frontend, backend) {
  return Object.hasOwn(FRONTENDS, frontend) && bases.includes(backend)
}
// 固定基底位址清單供儲存與送出驗證，阻止任意來源接收 JWT。
const bases = Object.values(BACKENDS).map((entry) => entry[1])
const timestamp = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/
const blank = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u
const errorMessages = {
  E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入',
  E0003: '沒有權限使用此功能', E0004: '找不到指定資料或 API',
  E0101: '帳號或密碼錯誤', E0102: '此帳號已停用，請聯絡系統管理員',
  E0104: '後台認證設定異常，請聯絡系統管理員', E0105: '登入失敗次數過多，請稍後再試',
  E9999: '系統暫時無法處理，請稍後再試',
}

// 只讀取核准的固定 API 位址，避免損壞儲存值使憑證流向任意來源。
export function readBackend(storage) {
  try { const value = storage.getItem(BACKEND_KEY); return bases.includes(value) ? value : BACKENDS.laravel[1] } catch { return BACKENDS.laravel[1] }
}

// 換後端前先刪除舊 JWT，避免來源已切換但舊登入仍被當成有效。
export function selectBackend(storage, next) {
  if (!bases.includes(next)) return false
  storage.removeItem(SESSION_KEY)
  storage.setItem(BACKEND_KEY, next)
  return true
}

// 僅接受六版共用的三欄儲存格式；讀取後仍須向 /api/me 驗證。
export function readSession(storage) {
  try {
    const raw = storage.getItem(SESSION_KEY)
    if (!raw) return null
    const value = JSON.parse(raw)
    const selected = storage.getItem(BACKEND_KEY)
    if (selected !== null && !bases.includes(selected)) { storage.removeItem(SESSION_KEY); return null }
    if (value && Object.keys(value).length === 3 && value.backend === readBackend(storage)
      && typeof value.accessToken === 'string' && value.accessToken && typeof value.expiresAt === 'string' && timestamp.test(value.expiresAt)) return value
    storage.removeItem(SESSION_KEY)
  } catch { try { storage.removeItem(SESSION_KEY) } catch { /* 儲存被拒時視為訪客。 */ } }
  return null
}

// 保存後端簽發的原期限，跨前端驗證不得重簽或延長登入。
export function saveSession(storage, backend, token, expiresAt) {
  if (!bases.includes(backend) || typeof token !== 'string' || !token || typeof expiresAt !== 'string' || !timestamp.test(expiresAt)) return false
  storage.setItem(BACKEND_KEY, backend)
  storage.setItem(SESSION_KEY, JSON.stringify({ backend, accessToken: token, expiresAt }))
  return true
}

// 比對原請求的後端與憑證，舊 401 或登出回應不得動到新登入。
export function ownsSession(storage, backend, credential) {
  try {
    if (readBackend(storage) !== backend) return false
    const raw = storage.getItem(SESSION_KEY)
    if (!raw) return false
    const current = JSON.parse(raw)
    return Boolean(current && credential && Object.keys(current).length === 3 && current.backend === backend
      && current.accessToken === credential.accessToken && current.expiresAt === credential.expiresAt)
  } catch { return false }
}

// 只清理由原請求持有的登入資料，保留後端選擇與其他新登入。
export function clearSessionIfOwned(storage, backend, credential) {
  if (!ownsSession(storage, backend, credential)) return false
  storage.removeItem(SESSION_KEY)
  return true
}

// URL 的 ID 只接受安全正整數，保留合法前導零的原路徑。
export function positiveId(value) {
  if (typeof value !== 'string' || !/^[0-9]+$/.test(value)) return null
  const number = Number(value)
  return Number.isSafeInteger(number) && number > 0 ? number : null
}

// 登入返回只接受已完成的作者／管理語意頁與合法 ID，避免開放式轉址。
export function validReturn(path) {
  if (['/next/', '/next', '/next/articles/new', '/next/me/articles', '/next/me/comments', '/next/admin/members', '/next/admin/articles', '/next/admin/comments', '/next/admin/settings/login-lockout'].includes(path)) return true
  const match = typeof path === 'string' && path.match(/^\/next\/(?:(?:articles|comments)\/([0-9]+)(?:\/edit)?|admin\/members\/([0-9]+))$/)
  return Boolean(match && positiveId(match[1] || match[2]) !== null)
}

// Angular 已提供全部語意頁；仍只允許合法路由，避免切換到未知目標。
function supportsAngularRoute(path) {
  return path === '/next/login' || path === '/next/register' || validReturn(path)
}

// SvelteKit 承接白名單中的公開、作者與管理頁，拒絕未知或不安全 ID。
function svelteTarget(path) {
  if (path === '/next/' || path === '/next') return '/sveltekit/'
  if (path === '/next/register') return '/sveltekit/register'
  return validReturn(path) ? path.replace(/^\/next/, '/sveltekit') : null
}

// 切換版本只搬移已完成的頁面語意與既有 ID，登入目標另安全映射。
export function frontendTarget(path, frontend, returnTo = '/next/') {
  if (!Object.hasOwn(FRONTENDS, frontend)) return null
  if (frontend === 'angular' && !supportsAngularRoute(path)) return null
  if (frontend === 'sveltekit') {
    // 登入返回限列表或文章詳情；未實作及不安全目標不能回退成可切換狀態。
    if (path === '/next/login') {
      if (!validReturn(returnTo)) return null
      const target = svelteTarget(returnTo)
      return target ? `/sveltekit/login${target === '/sveltekit/' ? '' : `?return_to=${encodeURIComponent(target)}`}` : null
    }
    return svelteTarget(path)
  }
  if (path === '/next/login') {
    // 返回頁若不是目標版本可用的合法路由，就以文章列表為預設目標。
    const target = (validReturn(returnTo) && frontendTarget(returnTo, frontend)) || `/${frontend}/`
    return `/${frontend}/login${target !== `/${frontend}/` ? `?return_to=${encodeURIComponent(target)}` : ''}`
  }
  if (path === '/next/' || path === '/next') return `/${frontend}/`
  if (path === '/next/register') return `/${frontend}/register`
  if (validReturn(path)) return path.replace(/^\/next/, `/${frontend}`)
  return `/${frontend}/`
}

// 列表頁碼無效時回第一頁，不把不合法 query 交給 API。
export function pageNumber(value) {
  return typeof value === 'string' && /^[1-9][0-9]*$/.test(value) && Number.isSafeInteger(Number(value)) ? Number(value) : 1
}

// Email 只修剪 ASCII 空格並折 ASCII 大寫，與後端共同規則一致。
export function normalizeEmail(value) {
  if (typeof value !== 'string') return ''
  return value.replace(/^ +| +$/g, '').replace(/[A-Z]/g,
    // 限定 ASCII 大寫，避免 Unicode 特殊折字改變合法性。
    (letter) => letter.toLowerCase())
}

// 逐段檢查核准的 ASCII Email，瀏覽器原生寬鬆規則不足以取代。
export function validEmail(value) {
  if (value.length > 254 || !/^[\x00-\x7f]+$/.test(value)) return false
  const parts = value.split('@')
  if (parts.length !== 2) return false
  const [local, domain] = parts
  if (local.length < 1 || local.length > 64 || !/^[a-z0-9._%+-]+$/.test(local) || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return false
  const labels = domain.split('.')
  return labels.length >= 2 && labels.every(
    // 每個網域段只能用 ASCII 英數與內部連字號。
    (label) => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label),
  ) && /^[a-z]{2,63}$/.test(labels.at(-1))
}

// 先回饋可確定的帳號格式錯誤，後端仍負責最終驗證與唯一性。
export function validateAccount(input, register = false) {
  const email = normalizeEmail(input.email)
  const errors = {}
  if (!validEmail(email)) errors.email = ['請輸入符合格式的 Email']
  if (!/^[A-Za-z0-9]{4,16}$/.test(input.password)) errors.password = ['密碼須為 4–16 碼英文字母或數字']
  if (register && (typeof input.nickname !== 'string' || [...input.nickname].length < 1 || [...input.nickname].length > 30 || blank.test(input.nickname) || input.nickname.includes('\0'))) errors.nickname = ['暱稱須為 1–30 個字，且不可全為空白或含不支援字元']
  return { email, errors }
}

// Retry-After 僅允許安全正整數秒，不能用 HTTP 日期猜鎖定期限。
export function retryAfterSeconds(value) {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value)) return null
  const seconds = Number(value)
  return Number.isSafeInteger(seconds) && seconds <= Number.MAX_SAFE_INTEGER / 1000 ? seconds : null
}

// 用真實經過時間估計剩餘秒數，背景分頁節流不會延長倒數。
export function remainingSeconds(deadline, now = Date.now()) { return Math.max(0, Math.ceil((deadline - now) / 1000)) }

// 固定兩位數呈現分秒，超過一小時才加上時數。
export function formatCountdown(seconds) {
  // 固定寬度避免倒數數字變化時晃動版面。
  // 補零讓倒數格式與視覺寬度保持穩定。
  const two = (number) => String(number).padStart(2, '0')
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor(seconds % 3600 / 60)
  return hours ? `${two(hours)}:${two(minutes)}:${two(seconds % 60)}` : `${two(minutes)}:${two(seconds % 60)}`
}

// 只接受完整且啟用的本人資料，不由不完整回應推測已登入。
export function validAccount(value) {
  return value && typeof value === 'object' && !Array.isArray(value) && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.email === 'string' && value.email === normalizeEmail(value.email) && validEmail(value.email)
    && typeof value.nickname === 'string' && [...value.nickname].length >= 1 && [...value.nickname].length <= 30
    && (value.role === 'member' || value.role === 'admin') && value.status === 'active' && timestamp.test(value.created_at)
}

// 管理列表須容納停用的一般會員，但不得把管理員或不完整資料當成會員。
export function validMember(value) {
  return value && typeof value === 'object' && !Array.isArray(value) && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.email === 'string' && value.email === normalizeEmail(value.email) && validEmail(value.email)
    && typeof value.nickname === 'string' && [...value.nickname].length >= 1 && [...value.nickname].length <= 30 && !blank.test(value.nickname)
    && value.role === 'member' && ['active', 'disabled'].includes(value.status) && timestamp.test(value.created_at)
}

// 設定讀回只接受四個核准欄位與界限，避免用損壞回應覆蓋表單。
export function validLockoutSettings(value) {
  return value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 4
    && typeof value.enabled === 'boolean' && Number.isInteger(value.window_minutes) && value.window_minutes >= 1 && value.window_minutes <= 60
    && Number.isInteger(value.failure_threshold) && value.failure_threshold >= 1 && value.failure_threshold <= 100
    && Number.isInteger(value.lock_seconds) && value.lock_seconds >= 1 && value.lock_seconds <= 86400
}

// 表單原字串先驗證十進位與上限，再組成 API 要求的完整四欄數值。
export function parseLockoutDraft(draft) {
  const errors = {}
  const limits = { window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 }
  for (const [name, max] of Object.entries(limits)) {
    // 空字串、指數與小數不可先經 Number 轉型而誤成合法整數。
    if (typeof draft?.[name] !== 'string' || !/^[0-9]+$/.test(draft[name]) || Number(draft[name]) < 1 || Number(draft[name]) > max) errors[name] = [`請輸入 1–${max} 的整數`]
  }
  if (Object.keys(errors).length) return { body: null, errors }
  const body = { enabled: draft.enabled, window_minutes: Number(draft.window_minutes), failure_threshold: Number(draft.failure_threshold), lock_seconds: Number(draft.lock_seconds) }
  return validLockoutSettings(body) ? { body, errors } : { body: null, errors: { enabled: ['設定欄位不完整'] } }
}

// 搜尋只修剪 U+0020；過長值由介面攔下，不交給後端產生可避免的 422。
export function memberKeyword(value) {
  if (typeof value !== 'string') return null
  const keyword = value.replace(/^ +| +$/g, '')
  return [...keyword].length <= 254 ? keyword : null
}

// 文章與留言的共用欄位必須完整，才可顯示伺服器文字。
export function validContent(item, kind = 'article') {
  return item && typeof item === 'object' && !Array.isArray(item) && Number.isSafeInteger(item.id) && item.id > 0
    && typeof item.body === 'string' && [...item.body].length >= 1 && [...item.body].length <= (kind === 'comment' ? 2000 : 20000) && !blank.test(item.body)
    && (kind === 'comment' ? Number.isSafeInteger(item.article_id) && item.article_id > 0 : typeof item.title === 'string' && [...item.title].length >= 1 && [...item.title].length <= 200 && !blank.test(item.title))
    && item.author && typeof item.author === 'object' && Object.keys(item.author).length === 2
    && Number.isSafeInteger(item.author.id) && item.author.id > 0
    && typeof item.author.nickname === 'string' && [...item.author.nickname].length >= 1 && [...item.author.nickname].length <= 30 && !blank.test(item.author.nickname)
    && Number.isSafeInteger(item.version) && item.version > 0 && Object.hasOwn(BACKENDS, item.created_backend)
    && timestamp.test(item.created_at) && timestamp.test(item.updated_at)
    && ['visible', 'hidden'].includes(item.visibility) && typeof item.can_restore === 'boolean'
}

// 純文字按共同 Unicode 碼點與固定空白集合檢查，原值仍交後端最終驗證。
export function validateContent(input, kind = 'article') {
  const errors = {}
  const fields = kind === 'comment' ? { body: 2000 } : { title: 200, body: 20000 }
  // 不修剪或轉換文字，避免前端默默變更使用者的內容。
  for (const [name, max] of Object.entries(fields)) {
    const value = input[name]
    if (typeof value !== 'string' || !value.length || [...value].length > max || blank.test(value) || value.includes('\0')) errors[name] = ['欄位格式或範圍不正確']
  }
  return errors
}

// 管理員自己的內容仍按作者規則編修，留言內文另需父文章確定公開。
export function canEditContent(item, account, parentVisible = true) {
  return Boolean(item && account && item.author.id === account.id && parentVisible)
}

// 管理員可以下架別人的內容；自己的內容仍只按作者權限處理。
export function canHideContent(item, account) {
  return Boolean(item && account && (item.author.id === account.id || account.role === 'admin')
    && (item.visibility === 'visible' || account.role === 'admin' && item.author.id !== account.id))
}

// 恢復入口採伺服器的 can_restore，但實際權限仍由寫入端點重驗。
export function canRestoreContent(item, account) {
  return Boolean(item && account && (item.author.id === account.id || account.role === 'admin') && item.visibility === 'hidden' && item.can_restore)
}

// 分頁資料須維持伺服器頁碼與總數，才能區分真空與合法超頁。
export function validList(data, page, kind = 'article', articleId = null) {
  const p = data?.pagination
  return Array.isArray(data?.list) && p && p.page === page && p.per_page === 20
    && Number.isSafeInteger(p.total) && p.total >= 0 && p.total_pages === Math.ceil(p.total / 20)
    && data.list.every(
      // 每筆都須是契約資源；留言另須屬於目前文章。
      (item) => (kind === 'member' ? validMember(item) : validContent(item, kind)) && (articleId === null || item.article_id === articleId),
    )
}

// 以完整成功 envelope 和預期型態判斷，錯誤資料不能冒充空結果。
export function successData(status, body, kind, expected, articleId = null) {
  if (status !== 200 || body?.success !== true || body.message !== 'OK') return null
  if (kind === 'account') return validAccount(body.data) ? body.data : null
  if (kind === 'member') return validMember(body.data) && body.data.id === expected ? body.data : null
  if (kind === 'lockout-settings') return validLockoutSettings(body.data) ? body.data : null
  if (kind === 'article') return validContent(body.data) && body.data.id === expected ? body.data : null
  if (kind === 'comment') return validContent(body.data, 'comment') && body.data.id === expected ? body.data : null
  return validList(body.data, expected, kind === 'members' ? 'member' : ['comments', 'my-comments', 'managed-comments'].includes(kind) ? 'comment' : 'article', articleId) ? body.data : null
}

// 登入必須同時取得 JWT、原期限和完整身分資料，才可保存共用登入。
export function loginData(status, body) {
  const data = body?.data
  return status === 200 && body?.success === true && body.message === 'OK' && data?.token_type === 'Bearer'
    && typeof data.access_token === 'string' && data.access_token && typeof data.expires_at === 'string'
    && timestamp.test(data.expires_at) && validAccount(data.user) ? data : null
}

// 註冊與登出只接受指定狀態的空物件成功回應，避免誤報已完成。
export function emptySuccess(status, body, expectedStatus) {
  return status === expectedStatus && body?.success === true && body.message === 'OK' && body.data
    && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0
}

// 轉成可顯示的固定錯誤碼、欄位明細與追蹤編號，不遺失未知欄位。
export function apiFailure(status, body, fields = []) {
  const code = body?.success === false && typeof body.error_code === 'string' ? body.error_code : ''
  const errors = {}
  const extra = []
  if (code === 'E0001' && body.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
    // 已知欄位靠近輸入顯示，其餘保留在摘要。
    for (const [name, values] of Object.entries(body.errors)) {
      if (!Array.isArray(values)) continue
      const messages = values.filter(
        // 非字串不能作為使用者可讀錯誤。
        (value) => typeof value === 'string',
      )
      if (fields.includes(name)) errors[name] = messages
      else extra.push(...messages)
    }
  }
  const message = errorMessages[code] || (body?.success === false && typeof body.message === 'string' ? body.message : '伺服器回應不完整，請稍後手動重試。')
  return { status, code, message: extra.length ? `${message}：${extra.join('；')}` : message, errors, logId: typeof body?.log_id === 'string' ? body.log_id : '' }
}

// 業務 API 由瀏覽器直連固定後端，前端伺服器不承載憑證或代理請求。
export async function apiRequest(backend, path, { method = 'GET', body, token, signal } = {}) {
  if (!bases.includes(backend) || !path.startsWith('/api/')) throw new Error('Invalid API destination')
  const headers = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(`${backend}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal })
  let data
  try { data = await response.json() } catch { data = null }
  return { status: response.status, body: data, retryAfter: response.headers.get('Retry-After') }
}
