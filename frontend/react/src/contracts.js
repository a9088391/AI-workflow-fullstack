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
// 從唯一固定清單取出合法 API 基底，儲存資料不得擴大目的地。
const bases = Object.values(BACKENDS).map((entry) => entry[1])
const timestamp = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/
const blank = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u
const errorMessages = { E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入', E0003: '沒有權限使用此功能', E0004: '找不到指定資料或 API', E0101: '帳號或密碼錯誤', E0102: '此帳號已停用，請聯絡系統管理員', E0104: '後台認證設定異常，請聯絡系統管理員', E0105: '登入失敗次數過多，請稍後再試' }

// 只讀取已核准的固定後端位址，避免損壞儲存值讓 JWT 傳往任意來源。
export function readBackend(storage) {
  try { const value = storage.getItem(BACKEND_KEY); return bases.includes(value) ? value : BACKENDS.laravel[1] } catch { return BACKENDS.laravel[1] }
}

// 切換後端先刪憑證再存新選擇，防止跨後端錯用舊 JWT。
export function selectBackend(storage, next) {
  if (!bases.includes(next)) return false
  storage.removeItem(SESSION_KEY)
  storage.setItem(BACKEND_KEY, next)
  return true
}

// 只接受同分頁三欄 schema，刷新後仍要向 /api/me 重新驗證。
export function readSession(storage) {
  try {
    const raw = storage.getItem(SESSION_KEY)
    if (!raw) return null
    const value = JSON.parse(raw)
    const storedBackend = storage.getItem(BACKEND_KEY)
    if (storedBackend !== null && !bases.includes(storedBackend)) { storage.removeItem(SESSION_KEY); return null }
    if (value && Object.keys(value).length === 3 && value.backend === readBackend(storage) && typeof value.accessToken === 'string' && value.accessToken.length > 0 && typeof value.expiresAt === 'string' && timestamp.test(value.expiresAt)) return value
    storage.removeItem(SESSION_KEY)
  } catch { try { storage.removeItem(SESSION_KEY) } catch { /* 儲存被拒時保守視為訪客。 */ } }
  return null
}

// 保存後端簽發的原到期時間；跨前端載入與 me 驗證不會延長它。
export function saveSession(storage, backend, accessToken, expiresAt) {
  if (!bases.includes(backend) || typeof accessToken !== 'string' || !accessToken || !timestamp.test(expiresAt)) return false
  storage.setItem(BACKEND_KEY, backend)
  storage.setItem(SESSION_KEY, JSON.stringify({ backend, accessToken, expiresAt }))
  return true
}

// 只移除已確認失效或登出成功的憑證，暫時連線失敗仍可重試。
export function clearSession(storage) { storage.removeItem(SESSION_KEY) }

// 非同步回應只可改動發請求時的後端與憑證；讀取不清理儲存，避免舊回應傷及新登入。
export function ownsSession(storage, backend, credential) {
  try {
    const selected = storage.getItem(BACKEND_KEY)
    if ((selected ?? BACKENDS.laravel[1]) !== backend) return false
    const raw = storage.getItem(SESSION_KEY)
    if (!credential) return raw === null
    if (!raw) return false
    const current = JSON.parse(raw)
    return Boolean(current && Object.keys(current).length === 3 && credential.backend === backend && current.backend === backend
      && current.accessToken === credential.accessToken && current.expiresAt === credential.expiresAt)
  } catch { return false }
}

// 僅在請求仍擁有目前 JWT 時清除它，避免舊 401 或登出刪除新登入。
export function clearSessionIfOwned(storage, backend, credential) {
  if (!ownsSession(storage, backend, credential)) return false
  clearSession(storage)
  return true
}

// URL 僅允許安全範圍的十進位 ID；保留合法前導零供跨前端對照。
export function positiveId(value) {
  if (typeof value !== 'string' || !/^[0-9]+$/.test(value)) return null
  const number = Number(value)
  return Number.isSafeInteger(number) && number > 0 ? number : null
}

// 只允許本票已實作頁面作為登入返回目標，不接受外站或未完成頁面。
export function validReturn(path) {
  return path === '/react/' || Boolean(typeof path === 'string' && /^\/react\/articles\/([0-9]+)$/.test(path) && positiveId(path.slice('/react/articles/'.length)) !== null)
}

// 前端切換只搬移目前頁面語意與文章 ID，頁碼及表單內容不跨版本搬運。
export function frontendTarget(path, frontend, returnTo = '/react/') {
  if (!Object.hasOwn(FRONTENDS, frontend)) return null
  if (path === '/react/login' && returnTo === '/react/') return `/${frontend}/login`
  const current = path === '/react/login' && validReturn(returnTo) ? returnTo : path
  if (current === '/react/' || current === '/react/register' || current === '/react/login' || validReturn(current)) {
    const target = current.replace(/^\/react/, `/${frontend}`)
    if (path === '/react/login' && current !== '/react/') return `/${frontend}/login?return_to=${encodeURIComponent(target)}`
    return target
  }
  return `/${frontend}/`
}

// Email 僅修剪 ASCII 空格、折 ASCII 大寫，避免與後端規則分歧。
export function normalizeEmail(value) {
  if (typeof value !== 'string') return ''
  return value.replace(/^ +| +$/g, '').replace(/[A-Z]/g,
    // 已限定 A–Z，避免 Unicode 特殊大小寫映射。
    (letter) => letter.toLowerCase())
}

// 逐段檢查核准的 ASCII Email，瀏覽器較寬鬆的原生檢查不可取代此規則。
export function validEmail(value) {
  if (value.length > 254 || !/^[\x00-\x7f]+$/.test(value)) return false
  const parts = value.split('@')
  if (parts.length !== 2) return false
  const [local, domain] = parts
  if (local.length < 1 || local.length > 64 || !/^[a-z0-9._%+-]+$/.test(local) || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return false
  const labels = domain.split('.')
  // 網域標籤不得以連字號起迄，末段須為 2–63 個字母。
  return labels.length >= 2 && labels.every(
    // 每段最多 63 個 ASCII 英數／內部連字號。
    (label) => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label),
  ) && /^[a-z]{2,63}$/.test(labels.at(-1))
}

// 在發請求前顯示和伺服器一致的欄位邊界，避免無效登入累計鎖定。
export function validateAccount(input, register = false) {
  const email = normalizeEmail(input.email)
  const errors = {}
  if (!validEmail(email)) errors.email = ['請輸入符合格式的 Email']
  if (!/^[A-Za-z0-9]{4,16}$/.test(input.password)) errors.password = ['密碼須為 4–16 碼英文字母或數字']
  if (register) {
    const nickname = input.nickname
    if (typeof nickname !== 'string' || [...nickname].length < 1 || [...nickname].length > 30 || blank.test(nickname) || nickname.includes('\0')) errors.nickname = ['暱稱須為 1–30 個字，且不可全為空白或含不支援字元']
  }
  return { email, errors }
}

// 成功登入必須有完整 Account 與原期限，避免不完整回應被當成已授權。
export function validAccount(value) {
  return value && typeof value === 'object' && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.email === 'string' && value.email === normalizeEmail(value.email) && validEmail(value.email)
    && typeof value.nickname === 'string' && [...value.nickname].length >= 1 && [...value.nickname].length <= 30
    && (value.role === 'member' || value.role === 'admin') && value.status === 'active' && timestamp.test(value.created_at)
}

// 文章與留言只顯示共同契約的完整純文字資源，避免殘缺資料被當成成功。
export function validContent(item, kind = 'article') {
  return item && typeof item === 'object' && !Array.isArray(item) && Number.isSafeInteger(item.id) && item.id > 0
    && typeof item.body === 'string' && item.body.length > 0
    && (kind === 'comment' ? Number.isSafeInteger(item.article_id) && item.article_id > 0 : typeof item.title === 'string' && item.title.length > 0)
    && item.author && Number.isSafeInteger(item.author.id) && item.author.id > 0 && typeof item.author.nickname === 'string' && item.author.nickname.length > 0
    && Number.isSafeInteger(item.version) && item.version > 0 && Object.hasOwn(BACKENDS, item.created_backend)
    && timestamp.test(item.created_at) && timestamp.test(item.updated_at)
    && ['visible', 'hidden'].includes(item.visibility) && typeof item.can_restore === 'boolean'
}

// 分頁回應保留伺服器頁碼與總數，區分真空列表和合法超頁。
export function validList(data, page, kind = 'article') {
  const p = data?.pagination
  return Array.isArray(data?.list) && p && p.page === page && p.per_page === 20
    && Number.isSafeInteger(p.total) && p.total >= 0 && p.total_pages === Math.ceil(p.total / 20)
    && data.list.every(
      // 每筆都需是完整文章或留言，不能只檢查外層 envelope。
      (item) => validContent(item, kind),
    )
}

// 首頁零留言與單頁無須導覽；任何超頁都需提供返回第一頁入口。
export function showCommentPagination(pagination) {
  return Boolean(pagination && (pagination.total_pages > 1 || pagination.page > 1))
}

// 只有真正首頁且總數為零才說尚無留言；指定超頁先回報此頁無資料。
export function emptyCommentMessage(pagination) {
  return pagination.page > 1 || pagination.total > 0 ? '此頁沒有留言。' : '目前尚無留言。'
}

// 嚴格解析 Retry-After 的正整數秒，不以日期或浮點值猜測鎖定期限。
export function retryAfterSeconds(value) {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value)) return null
  const seconds = Number(value)
  return Number.isSafeInteger(seconds) && seconds <= Number.MAX_SAFE_INTEGER / 1000 ? seconds : null
}

// 依實際經過時間重新計算，背景分頁節流不會延長倒數。
export function remainingSeconds(deadline, now) { return Math.max(0, Math.ceil((deadline - now) / 1000)) }

// 非同步帳號回應只屬於原表單嘗試與原前後端世代，切換後不可套用結果。
export function ownsAttempt(signal, sequence, latest, startedEpoch, currentEpoch) {
  return !signal.aborted && sequence === latest && startedEpoch === currentEpoch
}

// 固定兩位數顯示分秒；至少一小時才加上時數。
export function formatCountdown(seconds) {
  // 顯示固定寬度的兩位數，避免倒數跳動時版面位移。
  const two = (value) => String(value).padStart(2, '0')
  const hours = Math.floor(seconds / 3600)
  return hours ? `${two(hours)}:${two(Math.floor(seconds % 3600 / 60))}:${two(seconds % 60)}` : `${two(Math.floor(seconds / 60))}:${two(seconds % 60)}`
}

// 從共同錯誤 envelope 擷取可顯示資料，不把 debug 或非文字注入畫面。
export function failure(body) {
  const code = body?.success === false && typeof body.error_code === 'string' ? body.error_code : ''
  const errors = {}
  if (code === 'E0001' && body.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
    for (const [key, value] of Object.entries(body.errors)) {
      // 每欄只保留文字訊息，其他錯誤仍可在摘要顯示。
      if (Array.isArray(value)) errors[key] = value.filter(
        // 不信任服務端回傳的任意型態。
        (item) => typeof item === 'string',
      )
    }
  }
  return { code, errors, message: errorMessages[code] || (typeof body?.message === 'string' ? body.message : '伺服器回應不完整，請稍後手動重試。'), logId: typeof body?.log_id === 'string' ? body.log_id : '' }
}

// 所有業務請求直連目前後端固定基底，讀取共同 envelope 與 Retry-After。
export async function request(backend, path, options = {}) {
  const response = await fetch(`${backend}${path}`, { ...options, headers: { Accept: 'application/json', ...options.headers } })
  let body
  try { body = await response.json() } catch { body = null }
  return { status: response.status, body, retryAfter: response.headers.get('Retry-After') }
}
