import assert from 'node:assert/strict'
import { test } from 'node:test'
import { API_BASE, BACKEND_KEY, BACKENDS, SESSION_KEY, apiFailure, apiRequest, emptySuccess, formatCountdown, isAccount, loginData, loginHref, positiveUrlInteger, readArticle, readBackend, readList, readSession, remainingSeconds, retryAfterSeconds, saveSession, selectBackend, validReturn, validateCredentials, vueFrontendTarget } from './contracts.js'

const account = { id: 1, email: 'reader@example.test', nickname: '林', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }
const article = { id: 101, title: '一篇文章', body: '第一行\n第二行', author: { id: 1, nickname: '林' }, version: 1, created_backend: 'laravel', created_at: '2026-09-24 10:00:00', updated_at: '2026-09-24 10:00:00', visibility: 'visible', can_restore: false }

// 模擬同分頁儲存，檢查 Nuxt 與 Vue 的共同 key/schema。
function storage() {
  const items = new Map()
  return {
    // 讀出指定 key 以模擬 sessionStorage。
    getItem(key) { return items.get(key) ?? null },
    // 保存同一分頁的登入資料。
    setItem(key, value) { items.set(key, value) },
    // 刪除失效或損壞的登入資料。
    removeItem(key) { items.delete(key) },
  }
}

// 儲存只含原憑證和到期時間，錯誤後端或多餘欄位不能沿用。
test('session format matches Vue and rejects damaged data', () => {
  const s = storage()
  saveSession(s, 'same-token', '2026-09-24 18:00:00')
  assert.equal(s.getItem(SESSION_KEY), JSON.stringify({ backend: API_BASE, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s).expiresAt, '2026-09-24 18:00:00')
  s.setItem(SESSION_KEY, JSON.stringify({ backend: 'http://localhost:8082', accessToken: 'wrong', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s), null)
  assert.equal(s.getItem(SESSION_KEY), null)
})

// Nuxt 讀取 Vue 留下的 Symfony 選擇與原到期時間，反向切回時拒絕舊 token。
test('shared backend selection and token remain bound in both directions', () => {
  const s = storage()
  selectBackend(s, BACKENDS.symfony)
  saveSession(s, 'same-jwt', '2026-09-24 18:00:00')
  assert.equal(readBackend(s), BACKENDS.symfony)
  assert.deepEqual(readSession(s), { backend: BACKENDS.symfony, accessToken: 'same-jwt', expiresAt: '2026-09-24 18:00:00' })
  selectBackend(s, BACKENDS.laravel)
  assert.equal(readSession(s), null)
  assert.equal(readBackend(s), BACKENDS.laravel)
  s.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.symfony, accessToken: 'old', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s), null)
  saveSession(s, 'laravel-jwt', '2026-09-24 18:00:00')
  s.setItem(BACKEND_KEY, 'broken')
  assert.equal(readSession(s), null)
  saveSession(s, 'new-login', '2026-09-24 18:00:00')
  assert.equal(readSession(s).accessToken, 'new-login')
})

// 清除 JWT 若遭瀏覽器拒絕，新後端選擇不能已寫入儲存。
test('backend switch keeps original pairing when storage removal fails', () => {
  const s = storage()
  saveSession(s, 'old-token', '2026-09-24 18:00:00')
  const denied = {
    // 仍可讀出目前配對以觀察失敗後狀態。
    getItem(key) { return s.getItem(key) },
    // 若程式先寫後清，此函式會暴露錯誤順序。
    setItem(key, value) { s.setItem(key, value) },
    // 刻意拒絕清除 token。
    removeItem() { throw new Error('storage denied') },
  }
  assert.throws(() => selectBackend(denied, BACKENDS.symfony), /storage denied/)
  assert.equal(readBackend(s), API_BASE)
  assert.equal(readSession(s).accessToken, 'old-token')
})

// 返回目標只能是已實作的 Nuxt 頁面；輸入仍遵守共同驗證規則。
test('route and credential boundaries', () => {
  assert.equal(validReturn('/nuxt/'), true)
  assert.equal(validReturn('/nuxt/articles/101'), true)
  assert.equal(validReturn('/nuxt/articles/001'), true)
  assert.equal(positiveUrlInteger('001'), 1)
  assert.equal(positiveUrlInteger('002'), 2)
  assert.equal(positiveUrlInteger('000'), null)
  assert.equal(positiveUrlInteger('9007199254740992'), null)
  assert.equal(positiveUrlInteger('+1'), null)
  assert.equal(positiveUrlInteger('1.0'), null)
  for (const path of ['https://evil.test', '//evil.test', '/nuxt/login', '/nuxt/articles/0', '/nuxt/articles/9007199254740992', '/nuxt/articles/1?x=1']) assert.equal(validReturn(path), false)
  const articleReturn = new URL(loginHref('/articles/001'), 'http://localhost:8080').searchParams.get('return_to')
  assert.equal(articleReturn, '/nuxt/articles/001')
  assert.equal(validReturn(articleReturn), true)
  assert.equal(new URL(loginHref('/unknown'), 'http://localhost:8080').searchParams.get('return_to'), '/nuxt/')
  assert.equal(vueFrontendTarget('/nuxt/articles/001'), '/vue/articles/001')
  assert.equal(vueFrontendTarget('/nuxt/register'), '/vue/register')
  for (const path of ['/nuxt/articles/new', '/nuxt/admin/members', '/nuxt/articles/0', '//evil.test']) assert.equal(vueFrontendTarget(path), null)
  assert.deepEqual(validateCredentials({ email: ' Reader@Example.Test ', password: 'abcd' }), { email: 'reader@example.test', errors: {} })
  assert.ok(validateCredentials({ email: 'bad', password: 'a', nickname: '　' }, true).errors.nickname)
})

// 鎖定只接受安全秒數，並按真實時鐘推算，不因計時器延遲拉長。
test('Retry-After countdown contract', () => {
  assert.equal(retryAfterSeconds('300'), 300)
  for (const value of ['0', '01', '-1', 'Fri, 02 Oct 2026 00:00:00 GMT', '9007199254741', null]) assert.equal(retryAfterSeconds(value), null)
  assert.equal(remainingSeconds(60_000, 1001), 59)
  assert.equal(formatCountdown(299), '04:59')
  assert.equal(formatCountdown(3600), '01:00:00')
})

// 成功與錯誤 envelope 逐項核對，避免不完整資料誤顯為登入或讀取成功。
test('API response parsing distinguishes success, validation, and hidden content', () => {
  assert.ok(isAccount(account))
  const body = { success: true, message: 'OK', data: { access_token: 'jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } }
  assert.equal(loginData(200, body).access_token, 'jwt')
  assert.equal(loginData(200, { ...body, data: { ...body.data, token_type: 'Basic' } }), null)
  assert.equal(emptySuccess(201, { success: true, message: 'OK', data: {} }, 201), true)
  const invalid = apiFailure(422, { success: false, error_code: 'E0001', errors: { email: ['此 Email 已註冊'], unknown: ['其他錯誤'] }, log_id: 'test-log' }, ['email'])
  assert.deepEqual(invalid.errors.email, ['此 Email 已註冊'])
  assert.match(invalid.message, /其他錯誤/)
  assert.equal(invalid.logId, 'test-log')
  assert.equal(readArticle(200, { success: true, message: 'OK', data: article }, 101)?.id, 101)
  assert.equal(readArticle(200, { success: true, message: 'OK', data: article }, 102), null)
  assert.equal(readList(200, { success: true, message: 'OK', data: { list: [], pagination: { page: 3, per_page: 20, total: 21, total_pages: 2 } } }, 3).pagination.total, 21)
})

// 請求必須直連業務後端，且只有已驗證狀態才由呼叫端傳 JWT。
test('API request keeps base path and headers', async (t) => {
  const original = globalThis.fetch
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const selected = storage()
  let request
  // 攔截單次網路請求，以檢查固定基底位址與驗證標頭。
  globalThis.fetch = async (url, options) => {
    request = { url, options }
    return new Response(JSON.stringify({ success: true, message: 'OK', data: {} }), { status: 200, headers: { 'Retry-After': '5' } })
  }
  t.after(
    // 測試結束恢復全域 fetch，避免污染其他案例。
    () => {
      globalThis.fetch = original
      if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage)
      else delete globalThis.sessionStorage
    },
  )
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: selected })
  const response = await apiRequest('/api/me', { token: 'jwt' })
  assert.equal(request.url, `${API_BASE}/api/me`)
  assert.equal(request.options.headers.Authorization, 'Bearer jwt')
  assert.equal(response.retryAfter, '5')
  // 切換後公開註冊與登入也須直連 Symfony，不能沿用 Laravel 固定網址。
  selectBackend(selected, BACKENDS.symfony)
  await apiRequest('/api/auth/register', { method: 'POST', body: { email: 'x@example.test' } })
  assert.equal(request.url, `${BACKENDS.symfony}/api/auth/register`)
  assert.equal(request.options.headers.Authorization, undefined)
  await apiRequest('/api/auth/login', { method: 'POST', body: { email: 'x@example.test' } })
  assert.equal(request.url, `${BACKENDS.symfony}/api/auth/login`)
})
