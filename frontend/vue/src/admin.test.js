import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { compile } from '@vue/compiler-dom'
import { renderToString } from '@vue/server-renderer'
import * as vue from 'vue'
import * as auth from './auth.js'
import * as registration from './registration.js'
import * as articles from './articles.js'
import * as comments from './comments.js'
import * as admin from './admin.js'

const administrator = { id: 1, email: 'admin@example.test', nickname: '管理員', role: 'admin', status: 'active', created_at: '2026-09-24 09:00:00' }
const member = { id: 2, email: 'member@example.test', nickname: '阿明', role: 'member', status: 'active', created_at: '2026-09-24 09:00:00' }
const lockout = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 }
// 集中組出契約回應，讓各測試只改本次關注的狀態和資料。
const success = (data) => ({ success: true, message: 'OK', data })
// 分頁總數由測試明確提供，模擬後端合法超頁。
const list = (items, page = 1, total = items.length) => success({ list: items, pagination: { page, per_page: 20, total, total_pages: Math.ceil(total / 20) } })

// 測試真實 App script 與模板，只替換 HTTP、Storage 和導覽來控制交錯回應。
function app(path, search = '', token = 'saved-token', selectedBackend = auth.API_BASE) {
  const requests = []
  const redirects = []
  const focuses = []
  const lifecycle = {}
  const storage = new Map()
  const sessionStorage = {
    // 保留缺值語意，讓正式 session parser 決定是否有憑證。
    getItem(key) { return storage.get(key) ?? null },
    // 保存憑證供重新驗證與登出交錯案例使用。
    setItem(key, value) { storage.set(key, value) },
    // 記錄失效或登出撤銷，避免測試憑證殘留。
    removeItem(key) { storage.delete(key) },
  }
  // 渲染前先寫入同分頁選擇，讓測試涵蓋 Django 與原 Laravel 文案。
  if (selectedBackend !== auth.API_BASE) auth.selectBackend(sessionStorage, selectedBackend)
  if (token) auth.saveSession(sessionStorage, token, '2026-09-24 18:00:00')
  const context = {
    ...vue, ...auth, ...registration, ...articles, ...comments, ...admin,
    AbortController, URLSearchParams, performance, console, setTimeout, clearTimeout, setInterval, clearInterval, sessionStorage,
    document: {
      // 摘要點選需聚焦實際欄位 ID；用可觀察焦點替代真實 DOM。
      getElementById(id) { return {
        // 保存被聚焦的欄位，驗證搜尋錯誤摘要能回到輸入框。
        focus() { focuses.push(id) },
      } },
    },
    window: { sessionStorage, location: { pathname: path, search,
      // 記錄安全本機返回與搜尋導覽，不切離測試。
      assign(value) { redirects.push(value) },
    },
      // 測試直接呼叫掛載，不需瀏覽器事件監聽。
      addEventListener() {},
      // 正式 cleanup 仍可執行，測試環境沒有已註冊事件。
      removeEventListener() {},
    },
    // 保存掛載程序，使測試以正式登入驗證入口開始。
    onMounted(callback) { lifecycle.mount = callback },
    // 保存卸載程序，讓每例結束時撤銷 timer 與請求。
    onBeforeUnmount(callback) { lifecycle.unmount = callback },
    // 即使收到 abort 仍可回應，藉此驗證 epoch 不讓舊帳號資料重現。
    fetch(url, options) { const response = Promise.withResolvers(); requests.push({ url, ...options, ...response }); return response.promise },
  }
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const bindings = Object.keys(compileScript(descriptor, { id: 'admin-test' }).bindings)
  const script = descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const scope = vue.effectScope()
  // 在獨立 effect scope 執行正式 script，測試完成後可以整批清理 reactive watcher。
  const page = scope.run(() => runInNewContext(`${script}\n;({ ${bindings.join(', ')} })`, context))
  lifecycle.mount()
  return { page, requests, redirects, focuses,
    // 先執行元件卸載，再停止 effect scope，避免測試間殘留請求與計時器。
    close() { lifecycle.unmount(); scope.stop() },
  }
}

// 登入及註冊畫面的 Django 說明只承諾已支援閱讀；其他後端維持既有發表說明。
test('Django auth copy withholds unsupported publishing claims', async (t) => {
  const djangoLogin = app('/vue/login', '', null, auth.BACKENDS.django)
  const djangoRegister = app('/vue/register', '', null, auth.BACKENDS.django)
  const laravelLogin = app('/vue/login', '', null)
  const laravelRegister = app('/vue/register', '', null)
  t.after(djangoLogin.close)
  t.after(djangoRegister.close)
  t.after(laravelLogin.close)
  t.after(laravelRegister.close)
  const loginHtml = await rendered(djangoLogin.page)
  assert.match(loginHtml, /Django 目前支援登入、公開文章與留言閱讀；發文與我的文章尚未提供。/)
  assert.doesNotMatch(loginHtml, /登入後即可發表文章/)
  const registrationHtml = await rendered(djangoRegister.page)
  assert.match(registrationHtml, /登入後會顯示在您的帳號旁/)
  assert.doesNotMatch(registrationHtml, /日後發表的內容旁/)
  assert.match(await rendered(laravelLogin.page), /登入後即可發表文章與查看自己的文章/)
  assert.match(await rendered(laravelRegister.page), /會顯示在您日後發表的內容旁/)
})

// 使用 Vue 正式模板條件產生 HTML，驗證提示和導覽是否真的出現在畫面。
async function rendered(state) {
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const code = compile(descriptor.template.content, { mode: 'function', prefixIdentifiers: true }).code
  const render = runInNewContext(`(function () { ${code} })()`, { Vue: vue })
  return renderToString(vue.createSSRApp({
    // 交由 Vue 解包正式 refs，保持實際模板顯示條件。
    setup() { return state }, render,
  }))
}

// 以真實 Response 完成指定請求；Promise settle 後再觀察 reactive state。
async function respond(request, status, body) {
  request.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
  await setImmediate()
}

// 返回目標只接受安全管理路徑，伺服器列表也必須保持原頁碼與會員範圍。
test('admin routes and member envelope keep safe IDs and overpage totals', () => {
  assert.equal(articles.articlePage('/vue/admin/members/2'), 'member')
  assert.equal(articles.articlePage('/vue/admin/members/007'), 'member')
  assert.equal(articles.localPage('/vue/admin/members/007'), true)
  for (const path of ['/vue/admin/members/0', '/vue/admin/members/9007199254740992', '//evil.test']) assert.equal(articles.localPage(path), false)
  assert.equal(articles.localPage('/vue/admin/settings/login-lockout'), true)
  assert.equal(articles.articlePage('/vue/admin/settings/login-lockout'), 'lockout-settings')
  assert.equal(admin.readMember(200, success(member), 2)?.email, member.email)
  assert.equal(admin.readMember(200, success(member), 3), null)
  assert.equal(admin.readMemberList(200, list([], 9, 41), 9)?.pagination.total, 41)
  assert.equal(admin.readMemberList(200, list([administrator]), 1), null)
})

// 設定 envelope 僅接受四欄完整型態與邊界；空字串不可從 number input 混成零。
test('lockout settings contract rejects malformed values and validates form bounds', () => {
  assert.deepEqual(admin.readLockoutSettings(200, success(lockout)), lockout)
  assert.equal(admin.readLockoutSettings(200, success({ ...lockout, lock_seconds: '300' })), null)
  assert.equal(admin.readLockoutSettings(200, success({ ...lockout, extra: 1 })), null)
  assert.equal(admin.readLockoutSettings(200, success({ ...lockout, window_minutes: 61 })), null)
  assert.equal(admin.readLockoutSettings(200, { success: true, message: 'wrong', data: lockout }), null)
  assert.deepEqual(Object.keys(admin.validateLockoutSettings({ ...lockout, failure_threshold: '', lock_seconds: 86401 })), ['failure_threshold', 'lock_seconds'])
})

// 非管理員直達設定頁不得讀取 API 或看到管理導覽；管理員經 /me 驗證後才載入。
test('lockout route is protected and renders the admin form only after authorization', async (t) => {
  const denied = app('/vue/admin/settings/login-lockout')
  t.after(denied.close)
  await respond(denied.requests[0], 200, success({ ...administrator, role: 'member' }))
  assert.equal(denied.requests.length, 1)
  assert.doesNotMatch(await rendered(denied.page), /儲存完整設定/)
  assert.doesNotMatch(await rendered(denied.page), /href="\/vue\/admin\/settings\/login-lockout"/)

  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  assert.match(instance.requests[1].url, /\/api\/settings\/login-lockout$/)
  await respond(instance.requests[1], 200, success(lockout))
  const html = await rendered(instance.page)
  assert.match(html, /累計期間（分鐘）/)
  assert.match(html, /設定預覽/)
  assert.match(html, /href="\/vue\/admin\/settings\/login-lockout"/)
  assert.match(html, /目前伺服器值/)
})

// 422 保留輸入並顯示欄位錯誤；成功只在重新 GET 後更新伺服器顯示值。
test('lockout validation, 422 and success readback preserve the right draft', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.window_minutes = 0
  await instance.page.submitLockoutSettings()
  assert.equal(instance.requests.length, 2)
  assert.match(await rendered(instance.page), /請輸入 1 到 60 的整數/)
  instance.page.lockoutDraft.value.window_minutes = 10
  const invalid = instance.page.submitLockoutSettings()
  assert.deepEqual(JSON.parse(instance.requests[2].body), { ...lockout, window_minutes: 10 })
  await respond(instance.requests[2], 422, { success: false, error_code: 'E0001', errors: { window_minutes: ['後端欄位錯誤'] } })
  await invalid
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 10)
  assert.match(await rendered(instance.page), /後端欄位錯誤/)
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[3], 200, success({}))
  assert.match(instance.requests[4].url, /\/api\/settings\/login-lockout$/)
  await respond(instance.requests[4], 200, success({ ...lockout, window_minutes: 12 }))
  await write
  assert.equal(instance.page.lockoutData.value.window_minutes, 12)
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 12)
  const html = await rendered(instance.page)
  assert.match(html, /已重新讀取並顯示目前伺服器設定/)
  assert.doesNotMatch(html, /後端欄位錯誤|請輸入 1 到 60 的整數/)
  assert.doesNotMatch(html, /id="window_minutes"[^>]*aria-invalid="true"/)
})

// 未知 PUT 不重送；手動重讀僅確認目前值，後續成功也不能證明先前寫入結果。
test('lockout unknown outcome needs readback and remains distinct from later success', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.enabled = false
  const first = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 500, { success: false, error_code: 'E9999' })
  await first
  await instance.page.submitLockoutSettings()
  assert.equal(instance.requests.length, 3)
  assert.equal(instance.page.lockoutDraft.value.enabled, false)
  assert.match(await rendered(instance.page), /請先手動重新讀取目前伺服器設定/)
  const readback = instance.page.loadContent()
  await respond(instance.requests[3], 200, success(lockout))
  await readback
  assert.equal(instance.page.lockoutDraft.value.enabled, false)
  assert.match(await rendered(instance.page), /可能已有其他管理員更新/)
  const second = instance.page.submitLockoutSettings()
  await respond(instance.requests[4], 200, success({}))
  await respond(instance.requests[5], 200, success({ ...lockout, enabled: false }))
  await second
  assert.equal(instance.page.lockoutUnknown.value, true)
  assert.match(await rendered(instance.page), /無法藉此判定先前 PUT 是否成功/)
})

// 待寫入時登出與重讀不應交錯；401 要撤銷已授權畫面並保留手動重新登入入口。
test('lockout pending write blocks logout and 401 removes protected settings', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  const write = instance.page.submitLockoutSettings()
  await instance.page.submitLogout()
  await instance.page.loadContent()
  assert.equal(instance.requests.length, 3)
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(instance.page.lockoutData.value, null)
  assert.match(await rendered(instance.page), /請重新登入後讀取原管理頁/)
})

// 登出先開始時不能送設定 PUT；伺服器取消管理權後不能再顯示可操作表單。
test('lockout write is blocked by logout and 403 removes controls', async (t) => {
  const logoutFirst = app('/vue/admin/settings/login-lockout')
  t.after(logoutFirst.close)
  await respond(logoutFirst.requests[0], 200, success(administrator))
  await respond(logoutFirst.requests[1], 200, success(lockout))
  const logout = logoutFirst.page.submitLogout()
  await logoutFirst.page.submitLockoutSettings()
  assert.equal(logoutFirst.requests.length, 3)
  assert.match(logoutFirst.requests[2].url, /\/api\/auth\/logout$/)
  await respond(logoutFirst.requests[2], 200, success({}))
  await logout

  const forbidden = app('/vue/admin/settings/login-lockout')
  t.after(forbidden.close)
  await respond(forbidden.requests[0], 200, success(administrator))
  await respond(forbidden.requests[1], 403, { success: false, error_code: 'E0003' })
  assert.doesNotMatch(await rendered(forbidden.page), /儲存完整設定/)
  assert.match(await rendered(forbidden.page), /沒有權限使用此功能/)
})

// 200 非法 envelope 屬寫入結果不明；換帳號驗證後不能帶入前一人的草稿。
test('malformed write and account switch keep uncertainty isolated', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.failure_threshold = 8
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 200, success({ saved: true }))
  await write
  assert.equal(instance.page.lockoutNeedsReadback.value, true)
  assert.equal(instance.page.lockoutDraft.value.failure_threshold, 8)
  const verify = instance.page.verifyAccount()
  await respond(instance.requests[3], 200, success({ ...administrator, id: 9 }))
  await verify
  await respond(instance.requests[4], 200, success(lockout))
  assert.equal(instance.page.lockoutDraft.value.failure_threshold, 5)
  assert.equal(instance.page.lockoutUnknown.value, false)
})

// 設定 PUT 401 後換另一位管理員登入，必須在新 GET 前清掉前一人的草稿與查證狀態。
test('lockout 401 then different admin login cannot reuse prior draft', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.window_minutes = 9
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 9)
  instance.page.lockoutUnknown.value = true
  instance.page.lockoutNeedsReadback.value = true
  instance.page.lockoutNotice.value = '先前結果不明'
  instance.page.email.value = 'admin2@example.test'
  instance.page.password.value = 'abc123'
  await vue.nextTick()
  const login = instance.page.submitLogin()
  assert.match(instance.requests[3].url, /\/api\/auth\/login$/)
  await respond(instance.requests[3], 200, success({ access_token: 'other-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: { ...administrator, id: 9, email: 'admin2@example.test' } }))
  await login
  assert.equal(instance.page.account.value?.id, 9, instance.page.summary.value)
  await respond(instance.requests[4], 200, success(lockout))
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 5)
  assert.equal(instance.page.lockoutUnknown.value, false)
  assert.equal(instance.page.lockoutNeedsReadback.value, false)
  assert.equal(instance.page.lockoutNotice.value, '')
  assert.doesNotMatch(await rendered(instance.page), /先前結果不明/)
})

// 同一管理員重新登入仍保留記憶體草稿，GET 只供比較，不替使用者覆蓋未提交內容。
test('lockout 401 then same admin login retains draft for manual save', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.window_minutes = 9
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  instance.page.lockoutUnknown.value = true
  instance.page.lockoutNeedsReadback.value = true
  instance.page.email.value = administrator.email
  instance.page.password.value = 'abc123'
  await vue.nextTick()
  const login = instance.page.submitLogin()
  assert.match(instance.requests[3].url, /\/api\/auth\/login$/)
  await respond(instance.requests[3], 200, success({ access_token: 'same-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: administrator }))
  await login
  assert.equal(instance.page.account.value?.id, administrator.id, instance.page.summary.value)
  await respond(instance.requests[4], 200, success(lockout))
  assert.equal(instance.page.lockoutData.value.window_minutes, 5)
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 9)
  assert.equal(instance.page.lockoutUnknown.value, true)
  assert.equal(instance.page.lockoutNeedsReadback.value, false)
  assert.match(await rendered(instance.page), /啟用：9 分鐘內失敗/)
})

// 一般會員即使直達管理網址，也不能觸發管理 API 或看到管理導覽。
test('nonadmin direct route renders denial without loading management data', async (t) => {
  const instance = app('/vue/admin/members')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator.role === 'admin' ? { ...administrator, role: 'member' } : administrator))
  assert.equal(instance.requests.length, 1)
  const html = await rendered(instance.page)
  assert.match(html, /沒有權限使用此功能/)
  assert.doesNotMatch(html, /href="\/vue\/admin\/members"/)
  assert.doesNotMatch(html, /搜尋會員暱稱或 Email/)
})

// 搜尋保留百分號等字面值，合法超頁分辨原總數並提供回第一頁。
test('admin search sends literal keyword and renders overpage recovery', async (t) => {
  const instance = app('/vue/admin/members', '?keyword=%25&page=9')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  assert.match(instance.requests[1].url, /\/api\/members\?page=9&per_page=20&filters%5Bkeyword%5D=%25$/)
  await respond(instance.requests[1], 200, list([], 9, 41))
  const html = await rendered(instance.page)
  assert.match(html, /此頁沒有會員，總數仍保留/)
  assert.match(html, /回第一頁/)
  instance.page.keyword.value = ' A%_ '
  instance.page.submitMemberSearch()
  assert.deepEqual(instance.redirects, ['/vue/admin/members?keyword=A%25_'])
  const astral = '😀'.repeat(254)
  assert.doesNotMatch(html, /id="member-keyword"[^>]*maxlength=/)
  instance.page.keyword.value = astral
  instance.page.submitMemberSearch()
  assert.equal(instance.redirects.at(-1), `/vue/admin/members?keyword=${encodeURIComponent(astral)}`)
  instance.page.keyword.value = `${astral}😀`
  instance.page.submitMemberSearch()
  assert.equal(instance.redirects.length, 2, '255 Unicode code points must be rejected')
})

// 伺服器與本機 422 均要在搜尋欄旁顯示錯誤；未知欄位仍留在可聚焦摘要。
test('member search renders field and unmatched validation errors with focus target', async (t) => {
  const instance = app('/vue/admin/members', '?keyword=invalid')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 422, { success: false, error_code: 'E0001', errors: { 'filters.keyword': ['關鍵字格式不正確'], unexpected: ['其他參數不正確'] } })
  let html = await rendered(instance.page)
  assert.match(html, /搜尋關鍵字：關鍵字格式不正確/)
  assert.match(html, /id="member-keyword"[^>]*aria-invalid="true"[^>]*aria-describedby="member-keyword-error member-keyword-help"/)
  assert.match(html, /id="member-keyword-error"[^>]*>關鍵字格式不正確<\/p>/)
  assert.match(html, /其他參數不正確/)
  instance.page.focusField('filters.keyword')
  assert.deepEqual(instance.focuses, ['member-keyword'])

  instance.page.keyword.value = '😀'.repeat(255)
  instance.page.submitMemberSearch()
  await vue.nextTick()
  html = await rendered(instance.page)
  assert.match(html, /搜尋關鍵字：搜尋關鍵字最多 254 個字/)
  assert.match(html, /id="member-keyword-error"[^>]*>搜尋關鍵字最多 254 個字/)
  assert.equal(instance.redirects.length, 0)
})

// 直接開啟管理頁先保留安全返回路徑；驗證角色降為會員時，舊管理清單不得重現。
test('direct login return and role revalidation hide stale management data', async (t) => {
  const guest = app('/vue/admin/members/2', '', null)
  t.after(guest.close)
  assert.deepEqual(guest.redirects, ['/vue/login?return_to=%2Fvue%2Fadmin%2Fmembers%2F2'])

  const instance = app('/vue/admin/members')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  const stale = instance.requests[1]
  const verification = instance.page.verifyAccount()
  assert.equal(stale.signal.aborted, true)
  await respond(instance.requests[2], 200, success({ ...administrator, role: 'member' }))
  await verification
  await respond(stale, 200, list([member]))
  assert.equal(instance.page.memberList.value, null)
  const html = await rendered(instance.page)
  assert.match(html, /沒有權限使用此功能/)
  assert.doesNotMatch(html, /member@example.test/)
})

// 管理文章必須明確要求 management 範圍；管理留言使用專屬列表並沿用詳情頁。
test('managed content reads approved endpoints and renders hidden detail links', async (t) => {
  const content = { id: 7, body: '保留的內容', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'laravel', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }
  const articlePage = app('/vue/admin/articles')
  t.after(articlePage.close)
  await respond(articlePage.requests[0], 200, success(administrator))
  assert.match(articlePage.requests[1].url, /\/api\/articles\?page=1&per_page=20&filters%5Bscope%5D=management$/)
  await respond(articlePage.requests[1], 200, list([{ ...content, title: '下架文章' }]))
  let html = await rendered(articlePage.page)
  assert.match(html, /href="\/vue\/articles\/7"/)
  assert.match(html, /已下架/)

  const commentPage = app('/vue/admin/comments')
  t.after(commentPage.close)
  await respond(commentPage.requests[0], 200, success(administrator))
  assert.match(commentPage.requests[1].url, /\/api\/comments\?page=1&per_page=20$/)
  await respond(commentPage.requests[1], 200, list([{ ...content, id: 9, article_id: 7 }]))
  html = await rendered(commentPage.page)
  assert.match(html, /href="\/vue\/comments\/9"/)
  assert.match(html, /已下架/)
})

// 管理清單共用既有詳情；詳情仍須提供返回管理列表的落點，並保留原入口。
test('admin detail pages retain links back to their management lists', async (t) => {
  const content = { id: 7, body: '內容', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'laravel', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }
  const articleDetail = app('/vue/articles/7')
  t.after(articleDetail.close)
  await respond(articleDetail.requests[0], 200, success(administrator))
  await respond(articleDetail.requests[1], 200, success({ ...content, title: '下架文章' }))
  let html = await rendered(articleDetail.page)
  assert.match(html, /<p class="detail-return-links">/)
  assert.match(html, /href="\/vue\/admin\/articles"[^>]*>返回文章管理<\/a>/)
  assert.match(html, /href="\/vue\/"[^>]*>返回文章列表<\/a>/)

  const commentDetail = app('/vue/comments/9')
  t.after(commentDetail.close)
  await respond(commentDetail.requests[0], 200, success(administrator))
  await respond(commentDetail.requests[1], 200, success({ ...content, id: 9, article_id: 7 }))
  html = await rendered(commentDetail.page)
  assert.match(html, /<p class="detail-return-links">/)
  assert.match(html, /href="\/vue\/admin\/comments"[^>]*>返回留言管理<\/a>/)
  assert.match(html, /href="\/vue\/me\/comments"[^>]*>返回我的留言<\/a>/)
})

// 結果不明不自動重送；讀回同一 ID 後仍明示先前操作結果未知，後續成功再依伺服器值顯示。
test('status unknown requires readback and preserves uncertainty after later success', async (t) => {
  const instance = app('/vue/admin/members/2')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(member))
  let html = await rendered(instance.page)
  assert.match(html, /停用會員/)
  const first = instance.page.submitMemberStatus()
  assert.equal(instance.requests[2].method, 'PUT')
  assert.equal(JSON.parse(instance.requests[2].body).status, 'disabled')
  await respond(instance.requests[2], 500, { success: false, error_code: 'E9999' })
  await first
  html = await rendered(instance.page)
  assert.match(html, /先前送出的操作是否完成仍無法判定/)
  assert.match(html, /重新讀取同一會員/)
  await instance.page.submitMemberStatus()
  assert.equal(instance.requests.length, 3, 'unknown write cannot be resent before readback')
  const readback = instance.page.loadContent()
  await respond(instance.requests[3], 200, success({ ...member, status: 'disabled' }))
  await readback
  html = await rendered(instance.page)
  assert.match(html, /先前送出的操作是否完成仍無法判定/)
  assert.match(html, /目前伺服器狀態為停用/)
  const second = instance.page.submitMemberStatus()
  await respond(instance.requests[4], 200, success({}))
  await respond(instance.requests[5], 200, success(member))
  await second
  html = await rendered(instance.page)
  assert.match(html, /目前伺服器狀態為啟用/)
  assert.match(html, /先前送出的操作是否完成仍無法判定/)
})

// 寫入成功但重讀失敗不能顯示過期狀態；404 也不能保留先前可操作會員。
test('status readback failure and not-found hide stale member controls', async (t) => {
  const instance = app('/vue/admin/members/2')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(member))
  const write = instance.page.submitMemberStatus()
  await respond(instance.requests[2], 200, success({}))
  await respond(instance.requests[3], 500, { success: false, error_code: 'E9999' })
  await write
  let html = await rendered(instance.page)
  assert.match(html, /尚未讀到最新會員狀態/)
  assert.doesNotMatch(html, /停用會員/)
  const retry = instance.page.loadContent()
  await respond(instance.requests[4], 404, { success: false, error_code: 'E0004' })
  await retry
  html = await rendered(instance.page)
  assert.match(html, /找不到指定資料或 API/)
  assert.doesNotMatch(html, /停用會員/)
})

// 寫入等待中不能被手動重讀取代；401 撤銷登入時即刻移除受保護資料與等待狀態。
test('pending status blocks read races and 401 removes member data', async (t) => {
  const instance = app('/vue/admin/members/2')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(member))
  const write = instance.page.submitMemberStatus()
  await instance.page.loadContent()
  assert.equal(instance.requests.length, 3)
  assert.equal(instance.requests[2].signal.aborted, false)
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(instance.page.statusPending.value, false)
  assert.equal(instance.page.memberData.value, null)
  const html = await rendered(instance.page)
  assert.match(html, /請重新登入後讀取原管理頁/)
  assert.doesNotMatch(html, /member@example.test/)
})

// 登出已送出時不得再送會員狀態 PUT；反向順序仍由既有待寫入防線阻擋登出。
test('logout and member status cannot overlap in either order', async (t) => {
  const logoutFirst = app('/vue/admin/members/2')
  t.after(logoutFirst.close)
  await respond(logoutFirst.requests[0], 200, success(administrator))
  await respond(logoutFirst.requests[1], 200, success(member))
  const logout = logoutFirst.page.submitLogout()
  assert.match(logoutFirst.requests[2].url, /\/api\/auth\/logout$/)
  await logoutFirst.page.submitMemberStatus()
  assert.equal(logoutFirst.requests.length, 3)
  let html = await rendered(logoutFirst.page)
  assert.match(html, /<button[^>]*disabled[^>]*>停用會員<\/button>/)
  await respond(logoutFirst.requests[2], 200, success({}))
  await logout
  assert.equal(logoutFirst.page.memberData.value, null)
  assert.equal(logoutFirst.requests.length, 3)

  const statusFirst = app('/vue/admin/members/2')
  t.after(statusFirst.close)
  await respond(statusFirst.requests[0], 200, success(administrator))
  await respond(statusFirst.requests[1], 200, success(member))
  const write = statusFirst.page.submitMemberStatus()
  await statusFirst.page.submitLogout()
  assert.equal(statusFirst.requests.length, 3)
  assert.equal(statusFirst.requests[2].method, 'PUT')
  await respond(statusFirst.requests[2], 200, success({}))
  await respond(statusFirst.requests[3], 200, success({ ...member, status: 'disabled' }))
  await write
  assert.equal(statusFirst.page.memberData.value.status, 'disabled')
})
