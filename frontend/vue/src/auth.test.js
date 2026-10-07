import test from 'node:test'
import assert from 'node:assert/strict'
import { API_BASE, BACKEND_KEY, BACKENDS, SESSION_KEY, clearSession, formatCountdown, interpretLogin, interpretLogout, interpretMe, readBackend, readSession, remainingSeconds, retryAfterSeconds, saveSession, selectBackend, validateLogin } from './auth.js'

const account = { id: 1, email: 'demo@example.test', nickname: '林', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }
const login = { success: true, message: 'OK', data: { access_token: 'token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } }

// 模擬同分頁跨前端的 Web Storage，確認只保存目前後端與原到期時間。
function storage() {
  const values = new Map()
  return {
    values,
    // 模擬瀏覽器未找到 key 時的 null，而不是 Map 的 undefined。
    getItem(key) { return values.get(key) ?? null },
    // 保存測試資料以檢查實際 schema。
    setItem(key, value) { values.set(key, value) },
    // 檢查壞資料與登出後的移除行為。
    removeItem(key) { values.delete(key) },
  }
}

// 壞資料與錯後端不得讓頁面跳過 /api/me；正確資料須保留原到期時間。
test('session schema and corruption', () => {
  const s = storage()
  saveSession(s, 'token', '2026-09-24 18:00:00')
  assert.deepEqual(readSession(s), { backend: API_BASE, accessToken: 'token', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(s.values.get(SESSION_KEY).includes('password'), false)
  s.values.set(SESSION_KEY, '{broken')
  assert.equal(readSession(s), null)
  assert.equal(s.values.has(SESSION_KEY), false)
  s.values.set(SESSION_KEY, JSON.stringify({ backend: API_BASE, accessToken: 'token', expiresAt: '2026-09-24 18:00:00', password: 'forbidden' }))
  assert.equal(readSession(s), null)
  s.values.set(SESSION_KEY, JSON.stringify({ backend: 'http://localhost:8082', accessToken: 'token', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s), null)
  assert.equal(s.values.has(SESSION_KEY), false)
  clearSession(s)
})

// 無 JWT 的後端選擇跨刷新保留；逆向切換須拒絕舊 Symfony JWT。
test('backend selection persists without token and rejects previous backend credential', () => {
  const s = storage()
  assert.equal(readBackend(s), API_BASE)
  assert.equal(selectBackend(s, BACKENDS.symfony), true)
  assert.equal(readBackend(s), BACKENDS.symfony)
  assert.equal(readSession(s), null)
  saveSession(s, 'symfony-jwt', '2026-09-24 18:00:00')
  assert.equal(readSession(s).backend, BACKENDS.symfony)
  assert.equal(selectBackend(s, BACKENDS.laravel), true)
  assert.equal(readBackend(s), BACKENDS.laravel)
  assert.equal(readSession(s), null)
  s.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.symfony, accessToken: 'stale', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s), null)
  assert.equal(selectBackend(s, 'https://evil.test'), false)
  assert.equal(readBackend(s), API_BASE)
  saveSession(s, 'laravel-jwt', '2026-09-24 18:00:00')
  s.setItem(BACKEND_KEY, 'broken')
  assert.equal(readSession(s), null)
  saveSession(s, 'new-login', '2026-09-24 18:00:00')
  assert.equal(readSession(s).accessToken, 'new-login')
})

// Django 只接受固定 8084 位址；切入與切出都清除前一後端 JWT，未知值不得改選。
test('Django session selection is exact and fails closed', () => {
  const s = storage()
  saveSession(s, 'laravel-jwt', '2026-09-24 18:00:00')
  assert.equal(selectBackend(s, BACKENDS.django), true)
  assert.equal(BACKENDS.django, 'http://localhost:8084')
  assert.equal(readSession(s), null)
  saveSession(s, 'django-jwt', '2026-09-24 18:00:00')
  assert.deepEqual(readSession(s), { backend: BACKENDS.django, accessToken: 'django-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(selectBackend(s, 'http://localhost:8093'), false)
  assert.equal(readSession(s).accessToken, 'django-jwt')
  assert.equal(selectBackend(s, BACKENDS.laravel), true)
  assert.equal(readSession(s), null)
})

// Flask 只接受固定 8085；切入與切出均須先清除舊後端憑證。
test('Flask session selection keeps backend-bound JWT isolated', () => {
  const s = storage()
  saveSession(s, 'laravel-jwt', '2026-09-24 18:00:00')
  assert.equal(BACKENDS.flask, 'http://localhost:8085')
  assert.equal(selectBackend(s, BACKENDS.flask), true)
  assert.equal(readSession(s), null)
  saveSession(s, 'flask-jwt', '2026-09-24 18:00:00')
  assert.deepEqual(readSession(s), { backend: BACKENDS.flask, accessToken: 'flask-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(selectBackend(s, BACKENDS.laravel), true)
  assert.equal(readSession(s), null)
  s.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.flask, accessToken: 'stale', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s), null)
})

// FastAPI 只接受固定 8086；切入、切出與壞儲存狀態都不能重用其他後端 JWT。
test('FastAPI session selection keeps backend-bound JWT isolated', () => {
  const s = storage()
  saveSession(s, 'laravel-jwt', '2026-09-24 18:00:00')
  assert.equal(BACKENDS.fastapi, 'http://localhost:8086')
  assert.equal(selectBackend(s, BACKENDS.fastapi), true)
  assert.equal(readSession(s), null)
  saveSession(s, 'fastapi-jwt', '2026-09-24 18:00:00')
  assert.deepEqual(readSession(s), { backend: BACKENDS.fastapi, accessToken: 'fastapi-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(selectBackend(s, BACKENDS.flask), true)
  assert.equal(readSession(s), null)
  s.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.fastapi, accessToken: 'stale', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s), null)
})

// Gin 必須使用固定 8087；選擇前後的 JWT 只屬於原後端，不能跨框架沿用。
test('Gin selection preserves only Gin-bound sessions', () => {
  const s = storage()
  saveSession(s, 'laravel-jwt', '2026-09-24 18:00:00')
  assert.equal(BACKENDS.gin, 'http://localhost:8087')
  assert.equal(selectBackend(s, BACKENDS.gin), true)
  assert.equal(readSession(s), null)
  saveSession(s, 'gin-jwt', '2026-09-24 18:00:00')
  assert.deepEqual(readSession(s), { backend: BACKENDS.gin, accessToken: 'gin-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(selectBackend(s, BACKENDS.laravel), true)
  assert.equal(readSession(s), null)
  s.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.gin, accessToken: 'stale', expiresAt: '2026-09-24 18:00:00' }))
  assert.equal(readSession(s), null)
})

// 瀏覽器拒絕清除 JWT 時，失敗切換不可把舊 JWT 配給新後端。
test('backend selection fails closed when token removal throws', () => {
  const s = storage()
  saveSession(s, 'still-old', '2026-09-24 18:00:00')
  const throwing = {
    // 讀取既有選擇及憑證供失敗後確認。
    getItem(key) { return s.getItem(key) },
    // 若錯誤排序先寫入新選擇，此案例會立即留下錯誤配對。
    setItem(key, value) { s.setItem(key, value) },
    // 模擬瀏覽器拒絕清除 JWT；切換必須在寫選擇前失敗。
    removeItem() { throw new Error('storage denied') },
  }
  assert.throws(() => selectBackend(throwing, BACKENDS.symfony), /storage denied/)
  assert.equal(readBackend(s), API_BASE)
  assert.equal(readSession(s).accessToken, 'still-old')
})

// Email 僅折 ASCII／U+0020；密碼不裁切，格式錯誤不應送進鎖定計數。
test('login form validation', () => {
  assert.deepEqual(validateLogin({ email: ' A@EXAMPLE.COM ', password: 'abcd' }), { email: 'a@example.com', errors: {} })
  assert.ok(validateLogin({ email: '\tA@EXAMPLE.COM\t', password: 'abcd' }).errors.email)
  assert.ok(validateLogin({ email: 'a@example.com', password: ' abcd' }).errors.password)
  assert.ok(validateLogin({ email: 'a@example.com', password: 'A'.repeat(17) }).errors.password)
})

// 完整登入與直接 Account /api/me 才能授權；登出需空 data 確認。
test('auth envelopes reject incomplete success', () => {
  assert.equal(interpretLogin(200, login, null).ok, true)
  assert.equal(interpretLogin(200, { ...login, data: { ...login.data, user: { ...account, role: 'owner' } } }, null).ok, false)
  assert.equal(interpretLogin(200, { ...login, data: { ...login.data, token_type: 'Token' } }, null).ok, false)
  assert.equal(interpretMe(200, { success: true, message: 'OK', data: account }).ok, true)
  assert.equal(interpretMe(200, { success: true, message: 'OK', data: { user: account } }).ok, false)
  assert.equal(interpretLogout(200, { success: true, message: 'OK', data: {} }).ok, true)
  assert.equal(interpretLogout(200, { success: true, message: 'OK', data: { revoked: true } }).ok, false)
  assert.equal(interpretLogout(500, { success: false, error_code: 'E9999' }).ok, false)
})

// 429 標頭僅正整數有效；倒數按經過時間計算，歸零才可重新操作。
test('lockout header and elapsed countdown', () => {
  for (const invalid of [null, '0', '-1', '+1', '1.5', ' 5', '5 ', '1e2', 'Wed, 24 Sep 2026 10:00:00 GMT']) assert.equal(retryAfterSeconds(invalid), null)
  assert.equal(retryAfterSeconds('300'), 300)
  assert.equal(interpretLogin(429, { success: false, error_code: 'E0105', log_id: 'trace' }, '300').retryAfter, 300)
  assert.equal(remainingSeconds(300000, 1500), 299)
  assert.equal(remainingSeconds(300000, 300000), 0)
  assert.equal(formatCountdown(299), '04:59')
  assert.equal(formatCountdown(3600), '01:00:00')
})

// 欄位錯誤可修正，錯密碼、停用和失效憑證須保持不同處置代碼。
test('auth failure codes and safe details', () => {
  const invalid = interpretLogin(422, { success: false, error_code: 'E0001', errors: { email: ['請輸入符合格式的 Email'] }, log_id: 'trace' }, null)
  assert.deepEqual(invalid.errors, { email: ['請輸入符合格式的 Email'] })
  assert.equal(invalid.logId, 'trace')
  assert.equal(interpretLogin(401, { success: false, error_code: 'E0101' }, null).code, 'E0101')
  assert.equal(interpretMe(401, { success: false, error_code: 'E0002' }).code, 'E0002')
  assert.equal(interpretMe(403, { success: false, error_code: 'E0102' }).code, 'E0102')
  assert.equal(interpretMe(500, { success: false, error_code: 'E0104' }).code, 'E0104')
  assert.match(interpretMe(500, { success: false, error_code: 'E0104' }).message, /後台認證設定異常/)
  assert.match(interpretLogin(422, { success: false, error_code: 'E0001', errors: { _body: ['Body 須為 JSON object'] } }, null).message, /Body 須為 JSON object/)
})
