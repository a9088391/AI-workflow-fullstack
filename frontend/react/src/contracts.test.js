import assert from 'node:assert/strict'
import test from 'node:test'
import { BACKENDS, BACKEND_KEY, SESSION_KEY, canEditContent, canHideContent, canRestoreContent, clearSession, clearSessionIfOwned, emptyCommentMessage, failure, formatCountdown, frontendTarget, ownsAttempt, ownsRead, ownsSession, positiveId, readBackend, readSession, remainingSeconds, retryAfterSeconds, saveSession, selectBackend, showCommentPagination, supportsAngularRoute, supportsNextRoute, validContent, validList, validLockout, validMember, validReturn, validateAccount, validateContent, validateLockout } from './contracts.js'

// 模擬同一分頁的 sessionStorage，以確實檢查跨框架共享格式與清除順序。
function storage() {
  const values = new Map()
  // 測試替身維持瀏覽器的缺值 null 語意，讓讀取契約與實際環境一致。
  function getItem(key) { return values.get(key) ?? null }
  // 記錄共用 key 的原值，供跨前端 schema 斷言。
  function setItem(key, value) { values.set(key, value) }
  // 移除 JWT 不應順便清掉目前後端選擇。
  function removeItem(key) { values.delete(key) }
  return { values, getItem, setItem, removeItem }
}

// 必須使用 Vue／Nuxt 同樣的 key、三欄值與原期限，切後端先清除憑證。
test('shared session schema and backend switch', () => {
  const s = storage()
  assert.equal(readBackend(s), BACKENDS.laravel[1])
  assert.equal(saveSession(s, BACKENDS.laravel[1], 'jwt', '2026-10-16 18:00:00'), true)
  assert.equal(s.values.get(BACKEND_KEY), BACKENDS.laravel[1])
  assert.deepEqual(JSON.parse(s.values.get(SESSION_KEY)), { backend: BACKENDS.laravel[1], accessToken: 'jwt', expiresAt: '2026-10-16 18:00:00' })
  assert.deepEqual(readSession(s), { backend: BACKENDS.laravel[1], accessToken: 'jwt', expiresAt: '2026-10-16 18:00:00' })
  // 前端網址映射本身不可動共用儲存，原 JWT 與到期時間供新前端重驗。
  assert.equal(frontendTarget('/react/articles/7', 'next'), '/next/articles/7')
  assert.deepEqual(readSession(s), { backend: BACKENDS.laravel[1], accessToken: 'jwt', expiresAt: '2026-10-16 18:00:00' })
  assert.equal(selectBackend(s, BACKENDS.nestjs[1]), true)
  assert.equal(readSession(s), null)
  assert.equal(readBackend(s), BACKENDS.nestjs[1])
  assert.equal(saveSession(s, BACKENDS.nestjs[1], 'new', '2026-10-16 18:00:00'), true)
  clearSession(s)
  assert.equal(readBackend(s), BACKENDS.nestjs[1])
  assert.equal(readSession(s), null)
})

// 損壞的選擇或額外 session 欄位不可被視為已登入，亦不能接受外部 API URL。
test('corrupt session is rejected', () => {
  const s = storage()
  s.setItem(BACKEND_KEY, 'https://example.test')
  s.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.laravel[1], accessToken: 'jwt', expiresAt: '2026-10-16 18:00:00' }))
  assert.equal(readSession(s), null)
  assert.equal(s.getItem(SESSION_KEY), null)
  assert.equal(selectBackend(s, 'https://example.test'), false)
  s.setItem(BACKEND_KEY, BACKENDS.laravel[1])
  s.setItem(SESSION_KEY, JSON.stringify({ backend: BACKENDS.laravel[1], accessToken: 'jwt', expiresAt: '2026-10-16 18:00:00', email: 'secret' }))
  assert.equal(readSession(s), null)
})

// 返回與切換只接受本票實作的本地語意頁及安全 ID。
test('return target and frontend mapping', () => {
  assert.equal(validReturn('/react/'), true)
  assert.equal(validReturn('/react/articles/0007'), true)
  assert.equal(frontendTarget('/react/articles/0007', 'vue'), '/vue/articles/0007')
  assert.equal(frontendTarget('/react/login', 'nuxt', '/react/articles/7'), '/nuxt/login?return_to=%2Fnuxt%2Farticles%2F7')
  assert.equal(frontendTarget('/react/login', 'vue'), '/vue/login')
  assert.equal(frontendTarget('/react/', '__proto__'), null)
  for (const path of ['/react/articles/new', '/react/articles/7/edit', '/react/me/articles', '/react/me/comments', '/react/comments/009', '/react/comments/9/edit']) assert.equal(validReturn(path), true, path)
  assert.equal(frontendTarget('/react/comments/009/edit', 'nuxt'), '/nuxt/comments/009/edit')
  assert.equal(frontendTarget('/react/login', 'vue', '/react/articles/new'), '/vue/login?return_to=%2Fvue%2Farticles%2Fnew')
  for (const path of ['/react/admin/members', '/react/admin/members/0007', '/react/admin/articles', '/react/admin/comments', '/react/admin/settings/login-lockout']) assert.equal(validReturn(path), true, path)
  assert.equal(frontendTarget('/react/admin/members/0007', 'vue'), '/vue/admin/members/0007')
  assert.equal(frontendTarget('/react/login', 'nuxt', '/react/admin/settings/login-lockout'), '/nuxt/login?return_to=%2Fnuxt%2Fadmin%2Fsettings%2Flogin-lockout')
  for (const path of ['//evil.test', '/react/admin/members/0', '/react/admin/members/-1', '/react/admin/members/9007199254740992', '/react/admin/settings/nope', '/react/articles/0', '/react/articles/-1', '/react/articles/9007199254740992', '/react/comments/0/edit', '/react/articles/new/edit']) assert.equal(validReturn(path), false)
  assert.equal(positiveId('07'), 7)
})

// Next 已完成五種管理語意；合法路由可互切，無效 ID 仍不得成為返回目標。
test('Next menu availability and safe frontend targets', () => {
  for (const [path, target] of [['/react/', '/next/'], ['/react/register', '/next/register'], ['/react/login', '/next/login'], ['/react/articles/0007', '/next/articles/0007'], ['/react/articles/new', '/next/articles/new'], ['/react/articles/007/edit', '/next/articles/007/edit'], ['/react/me/articles', '/next/me/articles'], ['/react/me/comments', '/next/me/comments'], ['/react/comments/009', '/next/comments/009'], ['/react/comments/009/edit', '/next/comments/009/edit'], ['/react/admin/members', '/next/admin/members'], ['/react/admin/members/0007', '/next/admin/members/0007'], ['/react/admin/articles', '/next/admin/articles'], ['/react/admin/comments', '/next/admin/comments'], ['/react/admin/settings/login-lockout', '/next/admin/settings/login-lockout']]) {
    assert.equal(supportsNextRoute(path), true, path)
    assert.equal(frontendTarget(path, 'next'), target, path)
  }
  for (const path of ['/react/admin/members/0', '/react/admin/members/-1', '/react/admin/members/9007199254740992', '/react/admin/settings/nope', '/react/articles/0', '/react/articles/9007199254740992', '/react/comments/0/edit', '/react/comments/9007199254740992', '/react/articles/new/edit', '/react/unknown', '//evil.test']) {
    assert.equal(supportsNextRoute(path), false, path)
    assert.equal(frontendTarget(path, 'next'), null, path)
  }
  assert.equal(frontendTarget('/react/login', 'next', '/react/articles/0007'), '/next/login?return_to=%2Fnext%2Farticles%2F0007')
  assert.equal(frontendTarget('/react/login', 'next', '/react/articles/new'), '/next/login?return_to=%2Fnext%2Farticles%2Fnew')
  assert.equal(frontendTarget('/react/login', 'next', '/react/comments/009/edit'), '/next/login?return_to=%2Fnext%2Fcomments%2F009%2Fedit')
  for (const path of ['/react/admin/members', '/react/admin/members/0007', '/react/admin/articles', '/react/admin/comments', '/react/admin/settings/login-lockout']) {
    assert.equal(frontendTarget('/react/login', 'next', path), `/next/login?return_to=${encodeURIComponent(path.replace('/react/', '/next/'))}`)
  }
  assert.equal(frontendTarget('/react/login', 'next', '/react/admin/members/0'), '/next/login')
  assert.equal(frontendTarget('/react/login', 'next', '//evil.test'), '/next/login')
})

// Angular 已提供作者頁，切換及登入返回保留合法 ID；管理頁與無效 ID 仍不可進入。
test('Angular targets preserve author routes and reject unavailable or invalid routes', () => {
  for (const [path, target] of [['/react/', '/angular/'], ['/react/login', '/angular/login'], ['/react/register', '/angular/register'], ['/react/articles/0007', '/angular/articles/0007'], ['/react/articles/new', '/angular/articles/new'], ['/react/articles/007/edit', '/angular/articles/007/edit'], ['/react/me/articles', '/angular/me/articles'], ['/react/me/comments', '/angular/me/comments'], ['/react/comments/009', '/angular/comments/009'], ['/react/comments/009/edit', '/angular/comments/009/edit']]) {
    assert.equal(supportsAngularRoute(path), true, path)
    assert.equal(frontendTarget(path, 'angular'), target, path)
    assert.equal(frontendTarget('/react/login', 'angular', path), ['/react/', '/react/login', '/react/register'].includes(path) ? '/angular/login' : `/angular/login?return_to=${encodeURIComponent(target)}`, path)
  }
  for (const path of ['/react/admin/members', '/react/admin/members/7', '/react/admin/articles', '/react/admin/comments', '/react/admin/settings/login-lockout', '/react/articles/0', '/react/articles/9007199254740992', '/react/articles/0/edit', '/react/comments/0', '/react/comments/9007199254740992/edit', '/react/articles/new?draft=1', '//evil.test']) {
    assert.equal(supportsAngularRoute(path), false, path)
    assert.equal(frontendTarget(path, 'angular'), null, path)
    assert.equal(frontendTarget('/react/login', 'angular', path), '/angular/login', path)
  }
})

// 管理回應只有完整一般會員與四欄設定可呈現；錯誤型態不得視為空列表。
test('admin member and lockout envelopes reject incomplete or wrong-role data', () => {
  const member = { id: 7, email: 'demo@example.test', nickname: '陳', role: 'member', status: 'disabled', created_at: '2026-10-16 10:00:00' }
  const list = { list: [member], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }
  assert.equal(validMember(member), true)
  assert.equal(validList(list, 1, 'member'), true)
  assert.equal(validList({ ...list, list: [{ ...member, role: 'admin' }] }, 1, 'member'), false)
  assert.equal(validList({ list: [], pagination: { page: 4, per_page: 20, total: 1, total_pages: 1 } }, 4, 'member'), true)
  assert.equal(validMember({ ...member, status: 'unknown' }), false)
  assert.equal(validLockout({ enabled: false, window_minutes: 1, failure_threshold: 100, lock_seconds: 86400 }), true)
  assert.equal(validLockout({ enabled: 'false', window_minutes: 1, failure_threshold: 100, lock_seconds: 86400 }), false)
  assert.deepEqual(validateLockout({ window_minutes: '1', failure_threshold: '100', lock_seconds: '86400' }), {})
  assert.deepEqual(Object.keys(validateLockout({ window_minutes: '0', failure_threshold: '101', lock_seconds: '1.5' })).sort(), ['failure_threshold', 'lock_seconds', 'window_minutes'])
})

// 同時測試 Unicode 碼點、純空白與原文字保留；UTF-16 長度不能代替契約。
test('author content validates exact plain-text bounds without rewriting input', () => {
  assert.deepEqual(validateContent({ title: '😀', body: '第一行\r\n第二行' }), {})
  assert.deepEqual(validateContent({ body: '😀'.repeat(2000) }, 'comment'), {})
  assert.deepEqual(validateContent({ title: '  ', body: '內容' }), { title: ['欄位格式或範圍不正確'] })
  assert.deepEqual(validateContent({ title: '標題', body: '內容\0' }), { body: ['欄位格式或範圍不正確'] })
  assert.deepEqual(validateContent({ body: '😀'.repeat(2001) }, 'comment'), { body: ['欄位格式或範圍不正確'] })
})

// 管理員自己的下架內容不享有接管捷徑，且父文章不可見時不能編修留言。
test('author and administrator actions keep ownership and parent visibility boundaries', () => {
  const own = { author: { id: 4 }, visibility: 'hidden', can_restore: false }
  const other = { author: { id: 5 }, visibility: 'hidden', can_restore: true }
  const admin = { id: 4, role: 'admin' }
  assert.equal(canEditContent(own, admin), true)
  assert.equal(canEditContent(other, admin), false)
  assert.equal(canEditContent(own, admin, false), false)
  assert.equal(canHideContent(own, admin), false)
  assert.equal(canHideContent(other, admin), true)
  assert.equal(canRestoreContent(own, admin), false)
  assert.equal(canRestoreContent(other, admin), true)
  assert.equal(canHideContent(other, { id: 6, role: 'member' }), false)
})

// 已知 ID、JWT 或重讀世代變更時，舊 GET 不能在新畫面上短暫顯示。
test('private read result belongs to current credential and request generation', () => {
  const first = { backend: BACKENDS.laravel[1], credential: { accessToken: 'old' }, path: '/api/comments/7', currentPage: null, enabled: true, kind: 'comment', expectedId: '7', revision: 0 }
  assert.equal(ownsRead(first, first), true)
  assert.equal(ownsRead(first, { ...first, credential: { accessToken: 'new' } }), false)
  assert.equal(ownsRead(first, { ...first, expectedId: '8', path: '/api/comments/8' }), false)
  assert.equal(ownsRead(first, { ...first, revision: 1 }), false)
  assert.equal(ownsRead(first, { ...first, enabled: false }), false)
})

// 無效帳密與暱稱必須先擋下，不能發請求造成錯誤鎖定計數。
test('registration and login validation follows shared boundaries', () => {
  assert.deepEqual(validateAccount({ email: ' DEMO+X@EXAMPLE.TEST ', password: 'abcd', nickname: '陳' }, true), { email: 'demo+x@example.test', errors: {} })
  assert.ok(validateAccount({ email: 'bad@example.test', password: 'abc', nickname: '\0' }, true).errors.password)
  assert.ok(validateAccount({ email: 'bad@example.test', password: 'abcd', nickname: '\0' }, true).errors.nickname)
  assert.ok(validateAccount({ email: '用戶@example.test', password: 'abcd' }).errors.email)
  assert.ok(validateAccount({ email: 'bad@example.test', password: 'abcd!' }).errors.password)
})

// 空列表與合法超頁仍保留不同的 total/page；殘缺資源不得被當作成功。
test('article and comment envelopes retain all fields and pagination', () => {
  const article = { id: 7, title: '標題', body: '第一行\n第二行', author: { id: 2, nickname: '作者' }, version: 1, created_backend: 'laravel', created_at: '2026-10-16 10:00:00', updated_at: '2026-10-16 10:00:00', visibility: 'visible', can_restore: false }
  const comment = { id: 8, article_id: 7, body: '留言', author: article.author, version: 1, created_backend: 'gin', created_at: article.created_at, updated_at: article.updated_at, visibility: 'visible', can_restore: false }
  assert.equal(validContent(article), true)
  assert.equal(validContent(comment, 'comment'), true)
  assert.equal(validList({ list: [article], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }, 1), true)
  assert.equal(validList({ list: [comment], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }, 1, 'comment'), true)
  assert.equal(validList({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }, 1), true)
  assert.equal(validList({ list: [], pagination: { page: 9, per_page: 20, total: 4, total_pages: 1 } }, 9), true)
  assert.equal(validList({ list: [{ ...article, created_backend: 'unknown' }], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }, 1), false)
  assert.equal(validList({ list: [{ ...comment, article_id: '7' }], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }, 1, 'comment'), false)
})

// 登入錯誤碼必須分辨帳密錯誤、失效與停用，保留 422 欄位訊息。
test('API failure envelope keeps code, field errors and log id', () => {
  assert.equal(failure({ success: false, error_code: 'E0101' }).message, '帳號或密碼錯誤')
  assert.equal(failure({ success: false, error_code: 'E0002' }).message, '登入已逾期，請重新登入')
  assert.equal(failure({ success: false, error_code: 'E0102' }).message, '此帳號已停用，請聯絡系統管理員')
  assert.deepEqual(failure({ success: false, error_code: 'E0001', errors: { email: ['此 Email 已註冊'] }, log_id: 'log-1' }), { code: 'E0001', errors: { email: ['此 Email 已註冊'] }, message: '欄位資料不正確，請確認後再送出', logId: 'log-1' })
})

// 鎖定僅接受十進位整數秒，按時鐘差而非計時器觸發次數歸零。
test('lock countdown is strict and ends without submission', () => {
  assert.equal(retryAfterSeconds('61'), 61)
  for (const value of ['0', '01', '1.5', 'Fri, 16 Oct 2026 10:00:00 GMT', '-1', null]) assert.equal(retryAfterSeconds(value), null)
  assert.equal(remainingSeconds(61_000, 1_500), 60)
  assert.equal(remainingSeconds(61_000, 61_000), 0)
  assert.equal(formatCountdown(59), '00:59')
  assert.equal(formatCountdown(3661), '01:01:01')
})

// 確認切換前的成功回應即使未卸載且未中止，也不能保存登入或轉向。
test('old account attempt loses ownership immediately after switch', () => {
  const controller = new AbortController()
  const sequence = 1
  let epoch = 0
  const startedEpoch = epoch
  assert.equal(ownsAttempt(controller.signal, sequence, sequence, startedEpoch, epoch), true)
  epoch++
  assert.equal(ownsAttempt(controller.signal, sequence, sequence, startedEpoch, epoch), false)
  // 使用者取消切換時不增加世代，原請求仍可正常完成。
  const canceledStart = epoch
  assert.equal(ownsAttempt(controller.signal, sequence, sequence, canceledStart, epoch), true)
  controller.abort()
  assert.equal(ownsAttempt(controller.signal, sequence, sequence, canceledStart, epoch), false)
})

// 舊 me／登出／公開讀取的回應只能改動發請求時的同一份 JWT。
test('old request cannot own a newly saved session or backend', () => {
  const s = storage()
  assert.equal(saveSession(s, BACKENDS.laravel[1], 'old', '2026-10-16 18:00:00'), true)
  const old = readSession(s)
  assert.equal(ownsSession(s, BACKENDS.laravel[1], old), true)
  assert.equal(saveSession(s, BACKENDS.laravel[1], 'new', '2026-10-16 19:00:00'), true)
  assert.equal(ownsSession(s, BACKENDS.laravel[1], old), false)
  assert.equal(ownsSession(s, BACKENDS.laravel[1], readSession(s)), true)
  assert.equal(selectBackend(s, BACKENDS.gin[1]), true)
  assert.equal(ownsSession(s, BACKENDS.laravel[1], old), false)
  assert.equal(ownsSession(s, BACKENDS.gin[1], null), true)
  assert.equal(saveSession(s, BACKENDS.gin[1], 'gin-token', '2026-10-16 19:00:00'), true)
  assert.equal(ownsSession(s, BACKENDS.gin[1], null), false)
})

// 延遲的舊拒絕在新登入成功後才抵達，不可刪除新 JWT；目前 JWT 的拒絕仍須清除。
test('delayed rejection clears only its own session', async () => {
  const s = storage()
  saveSession(s, BACKENDS.laravel[1], 'old', '2026-10-16 18:00:00')
  const old = readSession(s)
  let release
  const delayed = new Promise(
    // 手動釋放回應以固定新登入和舊拒絕的先後順序。
    (resolve) => { release = resolve },
  ).then(
    // 模擬 me／登出／公開讀取共同使用的有條件清除入口。
    () => clearSessionIfOwned(s, BACKENDS.laravel[1], old),
  )
  saveSession(s, BACKENDS.laravel[1], 'new', '2026-10-16 19:00:00')
  release()
  assert.equal(await delayed, false)
  assert.equal(readSession(s).accessToken, 'new')
  assert.equal(clearSessionIfOwned(s, BACKENDS.laravel[1], readSession(s)), true)
  assert.equal(readSession(s), null)
})

// 首頁零留言與單頁不顯示無效導覽；零總數或有資料的超頁均可回第一頁。
test('comment pagination distinguishes empty, single and overpage states', () => {
  assert.equal(showCommentPagination({ page: 1, total: 0, total_pages: 0 }), false)
  assert.equal(showCommentPagination({ page: 1, total: 3, total_pages: 1 }), false)
  assert.equal(showCommentPagination({ page: 1, total: 21, total_pages: 2 }), true)
  assert.equal(showCommentPagination({ page: 9, total: 3, total_pages: 1 }), true)
  const zeroOverpage = { page: 99, per_page: 20, total: 0, total_pages: 0 }
  assert.equal(validList({ list: [], pagination: zeroOverpage }, 99, 'comment'), true)
  assert.equal(showCommentPagination(zeroOverpage), true)
  assert.equal(emptyCommentMessage({ page: 1, total: 0 }), '目前尚無留言。')
  assert.equal(emptyCommentMessage(zeroOverpage), '此頁沒有留言。')
})
