import assert from 'node:assert/strict'
import test from 'node:test'
import { apiFailure, apiRequest, BACKEND_KEY, BACKENDS, canEditContent, canHideContent, canRestoreContent, canSwitchFrontend, clearSessionIfOwned, emptySuccess, formatCountdown, frontendTarget, loginData, memberKeyword, pageNumber, parseLockoutDraft, positiveId, readBackend, readSession, remainingSeconds, retryAfterSeconds, saveSession, selectBackend, SESSION_KEY, successData, validReturn, validateAccount, validateContent, validLockoutSettings, validMember } from './contracts.js'

const member = { id: 2, email: 'member@example.test', nickname: '會員', role: 'member', status: 'disabled', created_at: '2026-10-17 09:00:00' }

// 管理資料與登入本人不同，停用會員仍須可顯示，管理員目標不得洩漏。
test('management responses validate disabled members, original pagination, and exact settings', () => {
  assert.equal(validMember(member), true)
  assert.equal(validMember({ ...member, role: 'admin' }), false)
  assert.deepEqual(successData(200, { success: true, message: 'OK', data: member }, 'member', 2), member)
  assert.equal(successData(200, { success: true, message: 'OK', data: { ...member, role: 'admin' } }, 'member', 2), null)
  const page = { list: [], pagination: { page: 4, per_page: 20, total: 2, total_pages: 1 } }
  assert.deepEqual(successData(200, { success: true, message: 'OK', data: page }, 'members', 4), page)
  assert.equal(successData(200, { success: true, message: 'OK', data: page }, 'members', 1), null)
  const settings = { enabled: false, window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 }
  assert.equal(validLockoutSettings(settings), true)
  assert.deepEqual(successData(200, { success: true, message: 'OK', data: settings }, 'lockout-settings'), settings)
  assert.equal(validLockoutSettings({ ...settings, lock_seconds: 86401 }), false)
  assert.equal(validLockoutSettings({ ...settings, version: 1 }), false)
})

// ASCII 空格以外的碼點不被前端改寫，過長搜尋留在表單供修正。
test('member keyword keeps literal symbols and Unicode while bounding code points', () => {
  assert.equal(memberKeyword('  A_%陳  '), 'A_%陳')
  assert.equal(memberKeyword('   '), '')
  assert.equal(memberKeyword('　'), '　')
  assert.equal(memberKeyword('😀'.repeat(254)), '😀'.repeat(254))
  assert.equal(memberKeyword('😀'.repeat(255)), null)
})

// 寫入只傳完整四欄；格式錯誤維持原草稿，改正後才可送出合法範圍。
test('lockout draft rejects lexical and boundary errors before corrected full update', () => {
  const draft = { enabled: false, window_minutes: '0', failure_threshold: '1e2', lock_seconds: '86401' }
  const invalid = parseLockoutDraft(draft)
  assert.equal(invalid.body, null)
  assert.deepEqual(Object.keys(invalid.errors), ['window_minutes', 'failure_threshold', 'lock_seconds'])
  assert.deepEqual(draft, { enabled: false, window_minutes: '0', failure_threshold: '1e2', lock_seconds: '86401' })
  assert.deepEqual(parseLockoutDraft({ enabled: false, window_minutes: '1', failure_threshold: '100', lock_seconds: '86400' }), {
    body: { enabled: false, window_minutes: 1, failure_threshold: 100, lock_seconds: 86400 }, errors: {},
  })
  assert.deepEqual(parseLockoutDraft({ enabled: 'false', window_minutes: '5', failure_threshold: '5', lock_seconds: '300' }).errors, { enabled: ['設定欄位不完整'] })
})

// 狀態寫入沒有新資源資料，需以完整 GET 讀回停用狀態才可更新畫面。
test('member status empty write response is distinct from disabled member readback', () => {
  const write = { success: true, message: 'OK', data: {} }
  assert.equal(emptySuccess(200, write, 200), true)
  assert.equal(successData(200, write, 'member', 2), null)
  assert.deepEqual(successData(200, { success: true, message: 'OK', data: member }, 'member', 2), member)
  assert.equal(successData(404, { success: false, error_code: 'E0004' }, 'member', 2), null)
})

// 模擬同分頁儲存，核對跨前端共用 schema 與後端切換順序。
function storage() {
  const values = new Map()
  return {
    // Map 保留瀏覽器 getItem 對缺值回 null 的行為。
    getItem(key) { return values.has(key) ? values.get(key) : null },
    // 儲存值一律為字串，與 sessionStorage 一致。
    setItem(key, value) { values.set(key, String(value)) },
    // 刪除舊憑證時不影響後端選擇。
    removeItem(key) { values.delete(key) },
  }
}

// 切換後端必須清 JWT，舊回應不得清掉新登入或恢復舊來源。
test('shared session keeps original expiry across frontend, but backend change revokes local credential', () => {
  const tab = storage()
  const laravel = BACKENDS.laravel[1]
  const symfony = BACKENDS.symfony[1]
  assert.equal(readBackend(tab), laravel)
  assert.equal(saveSession(tab, laravel, 'original-token', '2026-10-17 20:00:00'), true)
  const old = readSession(tab)
  assert.deepEqual(old, { backend: laravel, accessToken: 'original-token', expiresAt: '2026-10-17 20:00:00' })
  assert.equal(selectBackend(tab, symfony), true)
  assert.equal(tab.getItem(SESSION_KEY), null)
  assert.equal(readBackend(tab), symfony)
  assert.equal(saveSession(tab, symfony, 'new-token', '2026-10-17 21:00:00'), true)
  assert.equal(clearSessionIfOwned(tab, laravel, old), false)
  assert.equal(readSession(tab).accessToken, 'new-token')
})

// 損壞後端鍵不能讓 Laravel 預設值錯收舊 JWT；未知 API 位址也不能被選取。
test('invalid backend and session data fail closed', () => {
  const tab = storage()
  tab.setItem(BACKEND_KEY, 'https://attacker.invalid')
  tab.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.laravel[1], accessToken: 'old', expiresAt: '2026-10-17 20:00:00' }))
  assert.equal(readSession(tab), null)
  assert.equal(tab.getItem(SESSION_KEY), null)
  assert.equal(selectBackend(tab, 'https://attacker.invalid'), false)
})

// 導覽只接受已完成的語意頁與合法 ID，不可把登入後返回目標改成外站。
test('route and frontend targets preserve only approved completed pages', () => {
  assert.equal(validReturn('/next/articles/0009'), true)
  assert.equal(validReturn('https://attacker.invalid'), false)
  assert.equal(validReturn('/next/articles/0'), false)
  for (const path of ['/next/articles/new', '/next/articles/007/edit', '/next/me/articles', '/next/me/comments', '/next/comments/009', '/next/comments/009/edit']) assert.equal(validReturn(path), true, path)
  for (const path of ['/next/articles/0/edit', '/next/comments/9007199254740992', '/next/admin/members/0', '/next/admin/members/9007199254740992', '//evil.test']) assert.equal(validReturn(path), false, path)
  for (const path of ['/next/admin/members', '/next/admin/members/007', '/next/admin/articles', '/next/admin/comments', '/next/admin/settings/login-lockout']) {
    assert.equal(validReturn(path), true, path)
    assert.equal(frontendTarget(path, 'react'), path.replace('/next/', '/react/'))
  }
  assert.equal(frontendTarget('/next/login', 'vue', '/next/admin/members/007'), '/vue/login?return_to=%2Fvue%2Fadmin%2Fmembers%2F007')
  assert.equal(positiveId('9007199254740992'), null)
  assert.equal(frontendTarget('/next/articles/0009', 'vue'), '/vue/articles/0009')
  assert.equal(frontendTarget('/next/comments/009/edit', 'vue'), '/vue/comments/009/edit')
  assert.equal(frontendTarget('/next/login', 'react', '/next/me/comments'), '/react/login?return_to=%2Freact%2Fme%2Fcomments')
  assert.equal(frontendTarget('/next/login', 'react', '/next/articles/9'), '/react/login?return_to=%2Freact%2Farticles%2F9')
  assert.equal(frontendTarget('/next/login', 'react', '//attacker.invalid'), '/react/login')
  assert.equal(frontendTarget('/next/register', 'nuxt'), '/nuxt/register')
  assert.equal(frontendTarget('/next', 'react'), '/react/')
  assert.equal(frontendTarget('/next/', 'react'), '/react/')
  assert.equal(frontendTarget('/next/login', 'react', '/next'), '/react/login')
  assert.equal(pageNumber('0'), 1)
  assert.equal(pageNumber('5'), 5)
})

// Angular 的作者與管理頁都須保留語意及合法 ID，登入後也要回到相同目標。
test('Angular frontend targets preserve completed routes and reject invalid routes', () => {
  for (const [path, target] of [['/next', '/angular/'], ['/next/', '/angular/'], ['/next/login', '/angular/login'], ['/next/register', '/angular/register'], ['/next/articles/0009', '/angular/articles/0009'], ['/next/articles/new', '/angular/articles/new'], ['/next/articles/009/edit', '/angular/articles/009/edit'], ['/next/me/articles', '/angular/me/articles'], ['/next/me/comments', '/angular/me/comments'], ['/next/comments/009', '/angular/comments/009'], ['/next/comments/009/edit', '/angular/comments/009/edit'], ['/next/admin/members', '/angular/admin/members'], ['/next/admin/members/007', '/angular/admin/members/007'], ['/next/admin/articles', '/angular/admin/articles'], ['/next/admin/comments', '/angular/admin/comments'], ['/next/admin/settings/login-lockout', '/angular/admin/settings/login-lockout']]) {
    assert.equal(frontendTarget(path, 'angular'), target, path)
    assert.equal(frontendTarget('/next/login', 'angular', path), ['/next', '/next/', '/next/login', '/next/register'].includes(path) ? '/angular/login' : `/angular/login?return_to=${encodeURIComponent(target)}`, path)
  }
  for (const path of ['/next/admin/members/0', '/next/admin/members/9007199254740992', '/next/admin/unknown', '/next/admin/settings/other', '/next/articles/0', '/next/articles/9007199254740992', '/next/articles/0/edit', '/next/comments/0', '/next/comments/9007199254740992/edit', '/next/articles/new?draft=1', '//evil.test']) {
    assert.equal(frontendTarget(path, 'angular'), null, path)
    assert.equal(frontendTarget('/next/login', 'angular', path), '/angular/login', path)
  }
})

// SvelteKit 承接已完成的公開、作者與管理頁；跨版保留既有 JWT 原期限。
test('SvelteKit targets preserve completed public, author, and admin routes with the shared session', () => {
  const tab = storage()
  saveSession(tab, BACKENDS.nestjs[1], 'same-token', '2026-10-18 18:00:00')
  // 切換保留已完成頁面的語意與既有資源 ID，包括五種管理路徑。
  const routes = [
    ['/next', '/sveltekit/'], ['/next/', '/sveltekit/'], ['/next/register', '/sveltekit/register'], ['/next/login', '/sveltekit/login'],
    ['/next/articles/001', '/sveltekit/articles/001'], ['/next/articles/9007199254740991', '/sveltekit/articles/9007199254740991'],
    ['/next/articles/new', '/sveltekit/articles/new'], ['/next/me/articles', '/sveltekit/me/articles'], ['/next/me/comments', '/sveltekit/me/comments'],
    ['/next/articles/007/edit', '/sveltekit/articles/007/edit'], ['/next/comments/009', '/sveltekit/comments/009'], ['/next/comments/009/edit', '/sveltekit/comments/009/edit'],
    ['/next/admin/members', '/sveltekit/admin/members'], ['/next/admin/members/007', '/sveltekit/admin/members/007'],
    ['/next/admin/members/9007199254740991', '/sveltekit/admin/members/9007199254740991'],
    ['/next/admin/articles', '/sveltekit/admin/articles'], ['/next/admin/comments', '/sveltekit/admin/comments'],
    ['/next/admin/settings/login-lockout', '/sveltekit/admin/settings/login-lockout'],
  ]
  for (const [path, target] of routes) {
    assert.equal(frontendTarget(path, 'sveltekit'), target, path)
  }
  // 登入返回映射到目標端支援的本機頁面，包含會員詳情與管理設定。
  assert.equal(frontendTarget('/next/login', 'sveltekit', '/next/articles/001'), '/sveltekit/login?return_to=%2Fsveltekit%2Farticles%2F001')
  for (const [path, target] of routes) {
    if (['/next', '/next/', '/next/register', '/next/login'].includes(path)) continue
    assert.equal(frontendTarget('/next/login', 'sveltekit', path), `/sveltekit/login?return_to=${encodeURIComponent(target)}`, path)
  }
  // 跨版只延續同一份登入資料，不延長原期限。
  assert.deepEqual(readSession(tab), { backend: BACKENDS.nestjs[1], accessToken: 'same-token', expiresAt: '2026-10-18 18:00:00' })
})

// 非法 ID、未知管理路徑與不安全登入返回均拒絕，避免錯導或外站跳轉。
test('SvelteKit targets reject unfinished routes and unsafe login returns', () => {
  // 不安全 ID、未知管理頁及含 query 的路徑不應啟用切換。
  for (const path of ['/next/admin/members/0', '/next/admin/members/-1', '/next/admin/members/9007199254740992', '/next/admin/unknown', '/next/admin/settings/other', '/next/admin/lockout-settings', '/next/admin/members?draft=1', '/next/articles/0', '/next/articles/0/edit', '/next/articles/9007199254740992', '/next/comments/9007199254740992/edit', '/next/unknown', '//evil.test', 'https://attacker.invalid']) {
    assert.equal(frontendTarget(path, 'sveltekit'), null, path)
  }
  // 外站、非法 ID 或未知管理頁不能成為登入返回目標。
  for (const path of ['/next/articles/0', '/next/articles/9007199254740992', '/next/admin/members/0', '/next/admin/members/9007199254740992', '/next/admin/unknown', '/next/admin/comments?draft=1', '/next/register', '//evil.test', 'https://evil.test']) {
    assert.equal(frontendTarget('/next/login', 'sveltekit', path), null, path)
  }
})

// 內容檢查、作者編修與恢復入口依核准欄位和權限處理。
test('author content validation and controls use actual authorship and server restore flag', () => {
  assert.deepEqual(validateContent({ title: '😀', body: '第一行\r\n第二行' }), {})
  assert.deepEqual(validateContent({ body: '😀'.repeat(2000) }, 'comment'), {})
  assert.deepEqual(validateContent({ title: '  ', body: '內容' }), { title: ['欄位格式或範圍不正確'] })
  assert.deepEqual(validateContent({ title: '標題', body: '含\0字元' }), { body: ['欄位格式或範圍不正確'] })
  assert.deepEqual(validateContent({ body: '😀'.repeat(2001) }, 'comment'), { body: ['欄位格式或範圍不正確'] })
  const own = { id: 2, role: 'admin' }
  const other = { id: 3, role: 'member' }
  const item = { author: { id: 2 }, visibility: 'hidden', can_restore: false }
  assert.equal(canEditContent(item, own), true)
  assert.equal(canEditContent(item, own, false), false)
  assert.equal(canEditContent(item, other), false)
  assert.equal(canHideContent(item, own), false)
  assert.equal(canRestoreContent(item, own), false)
  assert.equal(canRestoreContent({ ...item, can_restore: true }, own), true)
  assert.equal(canRestoreContent({ ...item, can_restore: true }, other), false)
})

// Nuxt 可承接十二個固定後端；切換前端只搬移合法路由，不更動來源 JWT 或期限。
test('frontend availability preserves the selected backend and session', () => {
  for (const backend of Object.values(BACKENDS)) assert.equal(canSwitchFrontend('nuxt', backend[1]), true)
  const tab = storage()
  saveSession(tab, BACKENDS.nestjs[1], 'original-token', '2026-10-17 20:00:00')
  assert.equal(canSwitchFrontend('nuxt', readBackend(tab)), true)
  assert.deepEqual(readSession(tab), { backend: BACKENDS.nestjs[1], accessToken: 'original-token', expiresAt: '2026-10-17 20:00:00' })
  assert.equal(frontendTarget('/next/articles/9', 'nuxt'), '/nuxt/articles/9')
  assert.equal(frontendTarget('/next/login', 'nuxt', '/next/admin/members/9'), '/nuxt/login?return_to=%2Fnuxt%2Fadmin%2Fmembers%2F9')
  assert.equal(frontendTarget('/next/login', 'nuxt', '//attacker.invalid'), '/nuxt/login')
  assert.equal(canSwitchFrontend('react', BACKENDS.nestjs[1]), true)
  saveSession(tab, BACKENDS.django[1], 'supported-token', '2026-10-17 21:00:00')
  assert.equal(canSwitchFrontend('nuxt', readBackend(tab)), true)
  assert.equal(readSession(tab).expiresAt, '2026-10-17 21:00:00')
  assert.equal(canSwitchFrontend('unknown', BACKENDS.laravel[1]), false)
  assert.equal(canSwitchFrontend('nuxt', 'https://attacker.invalid'), false)
  assert.deepEqual(readSession(tab), { backend: BACKENDS.django[1], accessToken: 'supported-token', expiresAt: '2026-10-17 21:00:00' })
})

// 前端格式檢查避免無效登入累計鎖定，密碼不 trim 或改寫。
test('account validation keeps approved ASCII boundaries', () => {
  assert.deepEqual(validateAccount({ email: '  USER@EXAMPLE.TEST  ', password: 'abcd' }), { email: 'user@example.test', errors: {} })
  assert.ok(validateAccount({ email: 'user@example.test', password: 'abc' }).errors.password)
  assert.ok(validateAccount({ email: 'user@example.test', password: 'abcd ', nickname: 'A' }, true).errors.password)
  assert.ok(validateAccount({ email: 'user@example.test', password: 'abcd', nickname: '\0' }, true).errors.nickname)
})

// 429 只接受十進位秒數；倒數按實際時鐘歸零，不自動產生登入請求。
test('Retry-After countdown boundaries', () => {
  assert.equal(retryAfterSeconds('300'), 300)
  assert.equal(retryAfterSeconds('Sat, 17 Oct 2026 10:00:00 GMT'), null)
  assert.equal(retryAfterSeconds('0'), null)
  assert.equal(retryAfterSeconds('1.5'), null)
  assert.equal(remainingSeconds(1500, 0), 2)
  assert.equal(remainingSeconds(1500, 1500), 0)
  assert.equal(formatCountdown(3599), '59:59')
  assert.equal(formatCountdown(3600), '01:00:00')
})

// 公開回應必須有完整欄位、正確頁碼與父文章；合法超頁不能顯示為總數零。
test('article and comment response validation distinguishes invalid data from overpage', () => {
  const article = { id: 8, title: '一篇文章', body: '內容', author: { id: 2, nickname: '作者' }, version: 1,
    created_backend: 'laravel', created_at: '2026-10-17 10:00:00', updated_at: '2026-10-17 10:00:00', visibility: 'visible', can_restore: false }
  // 用契約成功封套包裝不同資料，測試只改變資源內容。
  const envelope = (data) => ({ success: true, message: 'OK', data })
  const overpage = { list: [], pagination: { page: 3, per_page: 20, total: 1, total_pages: 1 } }
  assert.deepEqual(successData(200, envelope(overpage), 'articles', 3), overpage)
  assert.equal(successData(200, envelope(overpage), 'articles', 2), null)
  assert.deepEqual(successData(200, envelope(article), 'article', 8), article)
  assert.equal(successData(200, envelope(article), 'article', 9), null)
  const comment = { ...article, article_id: 8 }
  delete comment.title
  const comments = { list: [comment], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }
  assert.deepEqual(successData(200, envelope(comments), 'comments', 1, 8), comments)
  assert.equal(successData(200, envelope(comments), 'comments', 1, 9), null)
  assert.deepEqual(successData(200, envelope(comments), 'my-comments', 1), comments)
  assert.deepEqual(successData(200, envelope(comment), 'comment', 8), comment)
  assert.equal(successData(200, envelope(comment), 'comment', 9), null)
})

// 登入與寫入只接受指定成功 envelope；欄位明細和追蹤碼完整保留。
test('auth envelopes and field errors follow the API contract', () => {
  const user = { id: 1, email: 'user@example.test', nickname: '用戶', role: 'member', status: 'active', created_at: '2026-10-17 10:00:00' }
  const body = { success: true, message: 'OK', data: { access_token: 'jwt', token_type: 'Bearer', expires_at: '2026-10-17 20:00:00', user } }
  assert.equal(loginData(200, body).access_token, 'jwt')
  assert.equal(loginData(200, { ...body, data: { ...body.data, token_type: 'Other' } }), null)
  assert.equal(emptySuccess(201, { success: true, message: 'OK', data: {} }, 201), true)
  const failure = apiFailure(422, { success: false, error_code: 'E0001', errors: { email: ['已註冊'], odd: ['其他明細'] }, log_id: 'trace-1' }, ['email'])
  assert.deepEqual(failure.errors.email, ['已註冊'])
  assert.match(failure.message, /其他明細/)
  assert.equal(failure.logId, 'trace-1')
})

// 業務 API 直接送往固定後端，Authorization 與 Retry-After 依契約傳遞。
test('request uses selected fixed backend and exposes Retry-After', async () => {
  const original = globalThis.fetch
  let requested
  // 攔截固定 API 呼叫，確認直連目的地與標頭並回傳鎖定資訊。
  globalThis.fetch = async (...args) => {
    requested = args
    return { status: 429, headers: {
      // 模擬後端鎖定秒數，驗證呼叫端能取得 Retry-After。
      get: () => '12',
    },
    // 保持 JSON 解析非同步，貼近實際 fetch 回應。
    json: async () => ({ success: false, error_code: 'E0105' }) }
  }
  try {
    const result = await apiRequest(BACKENDS.fastapi[1], '/api/auth/login', { method: 'POST', body: { email: 'x', password: 'y' }, token: 'jwt' })
    assert.equal(requested[0], 'http://localhost:8086/api/auth/login')
    assert.equal(requested[1].headers.Authorization, 'Bearer jwt')
    assert.equal(requested[1].headers['Content-Type'], 'application/json')
    assert.equal(result.retryAfter, '12')
    await assert.rejects(apiRequest('https://attacker.invalid', '/api/me'))
  } finally { globalThis.fetch = original }
})
