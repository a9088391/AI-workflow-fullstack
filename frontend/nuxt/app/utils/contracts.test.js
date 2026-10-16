import assert from 'node:assert/strict'
import { test } from 'node:test'
import { API_BASE, BACKEND_KEY, BACKENDS, SESSION_KEY, apiFailure, apiRequest, backendName, emptySuccess, formatCountdown, isAccount, loginData, loginHref, normalizeEmail, positiveUrlInteger, reactFrontendTarget, readArticle, readBackend, readComment, readList, readSession, remainingSeconds, retryAfterSeconds, saveSession, selectBackend, validReturn, validateContent, validateCredentials, vueFrontendTarget } from './contracts.js'
import * as vueAuth from '../../../vue/src/auth.js'

const account = { id: 1, email: 'reader@example.test', nickname: '林', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }
const article = { id: 101, title: '一篇文章', body: '第一行\n第二行', author: { id: 1, nickname: '林' }, version: 1, created_backend: 'laravel', created_at: '2026-09-24 10:00:00', updated_at: '2026-09-24 10:00:00', visibility: 'visible', can_restore: false }

// 僅映射 React 已有的頁面，保留原始合法 ID 與登入返回目標。
test('React frontend targets reject unfinished routes without a misleading fallback', () => {
  for (const [source, target] of [['/nuxt/', '/react/'], ['/nuxt/register', '/react/register'], ['/nuxt/login', '/react/login'], ['/nuxt/articles/001', '/react/articles/001']]) assert.equal(reactFrontendTarget(source), target)
  assert.equal(reactFrontendTarget('/nuxt/login', '/nuxt/articles/001'), '/react/login?return_to=%2Freact%2Farticles%2F001')
  assert.equal(reactFrontendTarget('/nuxt/login', '/nuxt/articles/new'), null)
  assert.equal(reactFrontendTarget('/nuxt/login', '/nuxt/admin/members'), null)
  assert.equal(reactFrontendTarget('/nuxt/login', '/nuxt/articles/0'), null)
  assert.equal(reactFrontendTarget('/nuxt/login', 'https://example.test'), null)
  for (const path of ['/nuxt/articles/new', '/nuxt/articles/001/edit', '/nuxt/admin/members', '/nuxt/articles/0', '/nuxt/articles/9007199254740992', '/nuxt/unknown']) assert.equal(reactFrontendTarget(path), null, path)
})

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

// Nuxt 主動改選 ASP.NET Core 時仍先撤銷舊 JWT，下一次讀取應使用新固定 API 位址。
test('ASP.NET Core backend selection clears the previous session', () => {
  const s = storage()
  saveSession(s, 'old-token', '2026-09-24 18:00:00')
  assert.equal(selectBackend(s, BACKENDS.aspnet), true)
  assert.equal(s.getItem(SESSION_KEY), null)
  assert.equal(readBackend(s), 'http://localhost:8083')
})

// Vue 與 Nuxt 必須讀同一份 Django JWT 與原到期時間，切後端則拒絕舊配對。
test('Django session survives both frontend implementations without reissue', () => {
  const s = storage()
  assert.equal(vueAuth.BACKENDS.django, BACKENDS.django)
  assert.equal(backendName('django'), 'Django')
  vueAuth.selectBackend(s, vueAuth.BACKENDS.django)
  vueAuth.saveSession(s, 'django-jwt', '2026-09-24 18:00:00')
  assert.deepEqual(readSession(s), { backend: BACKENDS.django, accessToken: 'django-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(readBackend(s), BACKENDS.django)
  assert.equal(s.getItem(SESSION_KEY), JSON.stringify({ backend: BACKENDS.django, accessToken: 'django-jwt', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(vueAuth.readSession(s).expiresAt, '2026-09-24 18:00:00')
  assert.equal(selectBackend(s, 'http://localhost:8085'), false)
  assert.equal(readSession(s).accessToken, 'django-jwt')
  saveSession(s, 'nuxt-jwt', '2026-09-24 18:00:00')
  assert.deepEqual(vueAuth.readSession(s), { backend: BACKENDS.django, accessToken: 'nuxt-jwt', expiresAt: '2026-09-24 18:00:00' })
  selectBackend(s, BACKENDS.symfony)
  assert.equal(vueAuth.readSession(s), null)
  assert.equal(readBackend(s), BACKENDS.symfony)
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
  for (const path of ['/nuxt/articles/new', '/nuxt/me/articles', '/nuxt/me/comments', '/nuxt/articles/12/edit', '/nuxt/comments/34', '/nuxt/comments/34/edit']) assert.equal(vueFrontendTarget(path), path.replace('/nuxt/', '/vue/'))
  for (const path of ['/nuxt/admin/members', '/nuxt/admin/members/001', '/nuxt/admin/articles', '/nuxt/admin/comments', '/nuxt/admin/settings/login-lockout']) assert.equal(vueFrontendTarget(path), path.replace('/nuxt/', '/vue/'))
  for (const path of ['/nuxt/admin/members/0', '/nuxt/admin/members/1/edit', '/nuxt/articles/0', '//evil.test']) assert.equal(vueFrontendTarget(path), null)
  assert.equal(new URL(loginHref('/admin/members/001'), 'http://localhost').searchParams.get('return_to'), '/nuxt/admin/members/001')
  assert.deepEqual(validateCredentials({ email: ' Reader@Example.Test ', password: 'abcd' }), { email: 'reader@example.test', errors: {} })
  assert.ok(validateCredentials({ email: 'bad', password: 'a', nickname: '　' }, true).errors.nickname)
})

// 作者頁只接受已知路由與 ID，文字維持原樣並拒絕共同空白及 NUL 邊界。
test('author route and content boundaries', () => {
  for (const path of ['/nuxt/articles/new', '/nuxt/me/articles', '/nuxt/me/comments', '/nuxt/articles/9/edit', '/nuxt/comments/7', '/nuxt/comments/7/edit']) assert.equal(validReturn(path), true)
  for (const path of ['/nuxt/comments/0', '/nuxt/articles/1/edit/extra', '/nuxt/me/other', '/nuxt/comments/1?x=1']) assert.equal(validReturn(path), false)
  assert.equal(new URL(loginHref('/comments/7/edit'), 'http://localhost').searchParams.get('return_to'), '/nuxt/comments/7/edit')
  assert.deepEqual(validateContent({ title: '第\n一篇', body: '原文\r\n保留' }), {})
  assert.deepEqual(validateContent({ title: '　', body: '\u0000' }), { title: ['欄位格式或範圍不正確'], body: ['欄位格式或範圍不正確'] })
  assert.deepEqual(validateContent({ body: '😀'.repeat(2001) }, 'comment'), { body: ['欄位格式或範圍不正確'] })
  assert.equal(readComment(200, { success: true, message: 'OK', data: { ...article, article_id: 9 } }, 101)?.article_id, 9)
  assert.equal(readComment(200, { success: true, message: 'OK', data: { ...article, article_id: 9 } }, 102), null)
  assert.equal(apiFailure(409, { success: false, error_code: 'E0201' }).message, '內容已更新，請確認最新內容後再送出')
})

// SEC01 只折 ASCII 大寫；K 不得在驗證前變成 k 而登入或註冊另一個帳號。
test('non-ASCII Kelvin sign remains invalid after Email normalization', () => {
  assert.equal(normalizeEmail('  K@EXAMPLE.TEST  '), 'K@example.test')
  assert.deepEqual(validateCredentials({ email: 'K@example.test', password: 'Abc1234' }).errors.email, ['請輸入符合格式的 Email'])
  assert.deepEqual(validateCredentials({ email: 'K@example.test', password: 'Abc1234', nickname: '讀者' }, true).errors.email, ['請輸入符合格式的 Email'])
  assert.equal(isAccount({ ...account, email: 'K@example.test' }), false)
  assert.equal(normalizeEmail('  READER@EXAMPLE.TEST  '), 'reader@example.test')
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
  selectBackend(selected, BACKENDS.django)
  await apiRequest('/api/articles?page=1&per_page=20')
  assert.equal(request.url, `${BACKENDS.django}/api/articles?page=1&per_page=20`)
  assert.equal(request.options.headers.Authorization, undefined)
  assert.equal(response.retryAfter, '5')
  // 切換後公開註冊與登入也須直連 Symfony，不能沿用 Laravel 固定網址。
  selectBackend(selected, BACKENDS.symfony)
  await apiRequest('/api/auth/register', { method: 'POST', body: { email: 'x@example.test' } })
  assert.equal(request.url, `${BACKENDS.symfony}/api/auth/register`)
  assert.equal(request.options.headers.Authorization, undefined)
  await apiRequest('/api/auth/login', { method: 'POST', body: { email: 'x@example.test' } })
  assert.equal(request.url, `${BACKENDS.symfony}/api/auth/login`)
})
