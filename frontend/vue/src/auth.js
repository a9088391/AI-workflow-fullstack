import { normalizeEmail, validEmail } from './registration.js'

export const API_BASE = 'http://localhost:8081'
export const BACKEND_KEY = 'article-collection.backend.v1'
export const BACKENDS = { laravel: API_BASE, symfony: 'http://localhost:8082', aspnet: 'http://localhost:8083', django: 'http://localhost:8084', flask: 'http://localhost:8085', fastapi: 'http://localhost:8086', gin: 'http://localhost:8087' }
// 六版前端共用此 key／schema：只存目前後端、JWT 與原到期時間，絕不快取帳號資料。
export const SESSION_KEY = 'article-collection.auth.v1'

// 登入沿用註冊的 Email 與密碼邊界，避免格式錯誤送到後端累計鎖定。
export function validateLogin(input) {
  const email = normalizeEmail(input.email)
  const errors = {}
  if (!validEmail(email)) errors.email = ['請輸入符合格式的 Email']
  if (!/^[A-Za-z0-9]{4,16}$/.test(input.password)) errors.password = ['密碼須為 4–16 碼英文字母或數字']
  return { email, errors }
}

// 只接受正整數秒數；HTTP 日期或寬鬆數字會讓前端猜錯鎖定時間。
export function retryAfterSeconds(value) {
  if (typeof value !== 'string' || !/^[1-9][0-9]*$/.test(value)) return null
  const seconds = Number(value)
  return Number.isSafeInteger(seconds) && seconds <= Number.MAX_SAFE_INTEGER / 1000 ? seconds : null
}

// 用實際經過時間計算剩餘秒數，背景分頁節流也不會把倒數延長。
export function remainingSeconds(deadline, now) {
  return Math.max(0, Math.ceil((deadline - now) / 1000))
}

// 超過一小時才顯示時數；秒數歸零後不再顯示鎖定倒數。
export function formatCountdown(seconds) {
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  // 固定兩位數，避免分鐘／秒數跳動時改變畫面寬度。
  const two = (value) => String(value).padStart(2, '0')
  return hours ? `${two(hours)}:${two(minutes)}:${two(rest)}` : `${two(minutes)}:${two(rest)}`
}

// 驗證伺服器 Account 最低必要契約，避免不完整回應顯示為已授權狀態。
export function isAccount(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.email === 'string' && value.email === normalizeEmail(value.email) && validEmail(value.email)
    && typeof value.nickname === 'string' && [...value.nickname].length >= 1 && [...value.nickname].length <= 30
    && (value.role === 'member' || value.role === 'admin')
    && value.status === 'active'
    && typeof value.created_at === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value.created_at)
}

// 無憑證時仍保留同分頁選擇；未知或損壞值一律回首次預設 Laravel。
export function readBackend(storage) {
  try {
    const value = storage.getItem(BACKEND_KEY)
    return Object.values(BACKENDS).includes(value) ? value : API_BASE
  } catch { return API_BASE }
}

// 先清除舊憑證再保存新選擇；清除失敗時不能留下新後端配舊 JWT。
export function selectBackend(storage, backend) {
  if (!Object.values(BACKENDS).includes(backend)) return false
  storage.removeItem(SESSION_KEY)
  storage.setItem(BACKEND_KEY, backend)
  return true
}

// 儲存格式損壞或後端不符時拒絕沿用，重新整理仍需 GET /api/me 驗證身分。
export function readSession(storage) {
  try {
    const raw = storage.getItem(SESSION_KEY)
    if (!raw) return null
    const value = JSON.parse(raw)
    const selected = storage.getItem(BACKEND_KEY)
    // 選擇 key 若遭破壞，不以 Laravel 預設值重新授權另一筆舊 JWT。
    if (selected !== null && !Object.values(BACKENDS).includes(selected)) { storage.removeItem(SESSION_KEY); return null }
    if (value?.backend === readBackend(storage) && Object.keys(value).length === 3 && typeof value.accessToken === 'string' && value.accessToken.length > 0 && typeof value.expiresAt === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value.expiresAt)) return value
    storage.removeItem(SESSION_KEY)
  } catch {
    // 儲存不可用時只能視為訪客；不把損壞資料當成有效憑證。
    try { storage.removeItem(SESSION_KEY) } catch { /* 儲存權限遭拒時無法清除，仍不授權。 */ }
  }
  return null
}

// 只保存跨同分頁前端所需的原始憑證與到期時間；密碼及 Account 留在記憶體。
export function saveSession(storage, accessToken, expiresAt) {
  const backend = readBackend(storage)
  // 登入成功時修復壞的選擇 key，避免本頁已登入卻在下一次刷新被拒絕。
  storage.setItem(BACKEND_KEY, backend)
  storage.setItem(SESSION_KEY, JSON.stringify({ backend, accessToken, expiresAt }))
}

// 已確認失效或伺服器撤銷成功才清除保存的憑證。
export function clearSession(storage) {
  storage.removeItem(SESSION_KEY)
}

// API 錯誤只取契約欄位，將欄位明細與 log_id 留給畫面修正及查證。
function failure(status, body) {
  const code = body?.success === false && typeof body.error_code === 'string' ? body.error_code : ''
  const known = { E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入', E0101: '帳號或密碼錯誤', E0102: '此帳號已停用，請聯絡系統管理員', E0104: '後台認證設定異常，請聯絡系統管理員', E0105: '登入失敗次數過多，請稍後再試', E9999: '系統暫時無法處理，請稍後再試' }
  const errors = {}
  const extra = []
  if (code === 'E0001' && body.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
    for (const [name, values] of Object.entries(body.errors)) {
      if (!Array.isArray(values)) continue
      // API 明細只允許文字；其他欄位的訊息仍須留在摘要中。
      const messages = values.filter((item) => typeof item === 'string')
      if (name === 'email' || name === 'password') errors[name] = messages
      else extra.push(...messages)
    }
  }
  const base = known[code] || (body?.success === false && typeof body.message === 'string' ? body.message : '伺服器回應不完整，請稍後手動重試。')
  const detail = extra.length ? `${base}：${extra.join('；')}` : base
  return {
    ok: false,
    code,
    message: status >= 500 ? `${detail}；請稍後手動重試。` : detail,
    errors,
    logId: typeof body?.log_id === 'string' ? body.log_id : '',
  }
}

// 登入只接受完整 200 envelope；429 額外帶出嚴格解析的 Retry-After。
export function interpretLogin(status, body, retryAfter) {
  if (status === 200 && body?.success === true && body.message === 'OK' && body.data?.token_type === 'Bearer'
    && typeof body.data.access_token === 'string' && body.data.access_token.length > 0
    && typeof body.data.expires_at === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(body.data.expires_at)
    && isAccount(body.data.user)) return { ok: true, accessToken: body.data.access_token, expiresAt: body.data.expires_at, account: body.data.user }
  const result = failure(status, body)
  if (status === 429 && result.code === 'E0105') result.retryAfter = retryAfterSeconds(retryAfter)
  return result
}

// /api/me 的 data 直接是 Account，不接受登入回應中的巢狀 user 形式。
export function interpretMe(status, body) {
  return status === 200 && body?.success === true && body.message === 'OK' && isAccount(body.data)
    ? { ok: true, account: body.data }
    : failure(status, body)
}

// 只有伺服器回覆完整的空 data 成功 envelope，才能宣稱已登出。
export function interpretLogout(status, body) {
  return status === 200 && body?.success === true && body.message === 'OK' && body.data && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0
    ? { ok: true }
    : failure(status, body)
}
