import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { setImmediate } from 'node:timers/promises'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import { renderToString } from '@vue/server-renderer'
import * as auth from './auth.js'
import * as registration from './registration.js'
import * as articles from './articles.js'
import * as comments from './comments.js'
import * as admin from './admin.js'

const sample = { id: 7, title: '標題', body: '第一行\r\n第二行', author: { id: 2, nickname: '作者' }, version: 1, created_backend: 'laravel', created_at: '2026-09-24 10:00:00', updated_at: '2026-09-24 10:00:00', visibility: 'visible', can_restore: false }
const member = { id: 2, email: 'writer@example.test', nickname: '作者', role: 'member', status: 'active', created_at: '2026-09-24 09:00:00' }

// 逐端點檢查文字、路徑與完整回應，避免前端自行猜欄位或新 ID。
test('article contract keeps original text and validates routes and envelopes', () => {
  assert.equal(articles.articlePage('/vue/articles/new'), 'article-new')
  assert.equal(articles.articlePage('/vue/articles/007'), 'article')
  assert.equal(articles.articlePage('/vue/articles/0'), 'missing')
  assert.equal(articles.backendName('django'), 'Django')
  assert.equal(articles.localPage('https://evil.test'), false)
  assert.equal(articles.localPage('//evil.test'), false)
  assert.deepEqual(articles.validateArticle({ title: '😀', body: '\r\n內容' }), {})
  assert.deepEqual(articles.validateArticle({ title: '\u00a0\u3000', body: 'x'.repeat(20001) }), { title: ['欄位格式或範圍不正確'], body: ['欄位格式或範圍不正確'] })
  assert.equal(articles.readArticle(200, { success: true, message: 'OK', data: sample }, 7)?.body, '第一行\r\n第二行')
  assert.equal(articles.readArticle(200, { success: true, message: 'OK', data: sample }, 8), null)
  assert.equal(articles.readArticleList(200, { success: true, message: 'OK', data: { list: [sample], pagination: { page: 3, per_page: 20, total: 41, total_pages: 3 } } }, 3)?.pagination.total, 41)
  assert.equal(articles.readArticleList(200, { success: true, message: 'OK', data: { list: [], pagination: { page: 9, per_page: 20, total: 41, total_pages: 3 } } }, 9)?.list.length, 0)
  assert.equal(articles.articleCreated(201, { success: true, message: 'OK', data: {} }), true)
  assert.equal(articles.articleCreated(201, { success: true, message: 'OK', data: { id: 7 } }), false)
  assert.deepEqual(articles.articleFailure({ success: false, error_code: 'E0001', errors: { title: ['標題錯誤'], unknown: ['額外錯誤'] } }).errors, { title: ['標題錯誤'] })
})

// 真正執行 App script；HTTP 與頁面導向改為可控制的記憶體邊界。
function articleApp(path, savedToken = 'old-token', search = '', confirmResult = false, selectedBackend = auth.API_BASE, simulateBeforeUnload = false) {
  const requests = []
  const redirects = []
  const confirmations = []
  const values = new Map()
  const lifecycle = {}
  const listeners = {}
  const sessionStorage = {
    // 維持 Storage 缺值語意，讓正式 session 函式運作。
    getItem(key) { return values.get(key) ?? null },
    // 保存 JWT 供換帳號及失效情境驗證。
    setItem(key, value) { values.set(key, value) },
    // 失效憑證應由正式流程刪除。
    removeItem(key) { values.delete(key) },
  }
  if (selectedBackend !== auth.API_BASE) auth.selectBackend(sessionStorage, selectedBackend)
  if (savedToken) auth.saveSession(sessionStorage, savedToken, '2026-09-24 18:00:00')
  const context = {
    ...vue, ...auth, ...registration, ...articles, ...comments, ...admin,
    AbortController, URLSearchParams, performance, console, setTimeout, clearTimeout, setInterval, clearInterval, sessionStorage,
    document: {},
    window: {
      sessionStorage,
      // 測試取消捨棄草稿時登出不能發出 API 請求。
      confirm(message) { confirmations.push(message); return confirmResult },
      location: { pathname: path, search,
        // 模擬瀏覽器離頁提示；使用者取消時，已確認的 POST 不能被當成可重送。
        assign(next) {
          if (simulateBeforeUnload) {
            const event = { defaultPrevented: false,
              // 記錄瀏覽器會阻止導頁的原生提示。
              preventDefault() { this.defaultPrevented = true },
            }
            listeners.beforeunload?.(event)
            if (event.defaultPrevented) return
          }
          redirects.push(next)
        },
      },
      // 保存真正的離頁處理器，以測試成功後的瀏覽器提示。
      addEventListener(name, callback) { listeners[name] = callback },
      // 測試結束移除監聽，不留舊頁事件。
      removeEventListener(name) { delete listeners[name] },
    },
    // 由測試手動啟動元件掛載。
    onMounted(callback) { lifecycle.mount = callback },
    // 由測試手動執行清理。
    onBeforeUnmount(callback) { lifecycle.unmount = callback },
    // 即使 abort 仍可回覆，以驗證 epoch 不接受舊回應。
    fetch(url, options) { const result = Promise.withResolvers(); requests.push({ url, ...options, ...result }); return result.promise },
  }
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const script = descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const scope = vue.effectScope()
  // 在既有 Vue effect scope 中執行元件 script，供案例讀取實際後端標籤與事件。
  // 收集頂層綁定，讓渲染檢查與事件測試都使用實際 script setup 狀態。
  const bindings = [...script.matchAll(/^(?:const|let|function|async function) ([A-Za-z_$][\w$]*)/gm)].map(function bindingName(match) { return match[1] })
  const page = scope.run(() => runInNewContext(`${script}\n;({ ${bindings.join(', ')} })`, context))
  lifecycle.mount()
  return { page, requests, redirects, confirmations, sessionStorage,
    // 停止正式生命週期與 Vue watcher，避免測試殘留計時器。
    close() { lifecycle.unmount(); scope.stop() },
  }
}

// 用 Vue 正式編譯與 SSR 渲染目前元件模板，檢查控制與提示實際是否出現在畫面。
async function renderArticleApp(app) {
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const render = vue.compile(descriptor.template.content)
  // 模板使用 script setup 與既有匯入的純函式；測試提供相同的公開綁定。
  function renderBindings() { return { ...auth, ...registration, ...articles, ...comments, ...admin, ...app.page } }
  return renderToString(vue.h({ setup: renderBindings, render }))
}

// 等待回應及 Vue 微任務；可指定 Retry-After 等標頭檢查登入互動。
async function respond(request, status, body, headers = {}) {
  request.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } }))
  await setImmediate()
}

// JWT 逾期後保留原草稿；同帳號重登只能手動再次發表。
test('article draft survives 401 and same-account re-login without automatic retry', async (t) => {
  const app = articleApp('/vue/articles/new')
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleTitle.value = '原標題'
  app.page.articleBody.value = '第一行\r\n第二行'
  const write = app.page.submitArticle()
  assert.deepEqual(JSON.parse(app.requests[1].body), { title: '原標題', body: '第一行\r\n第二行' })
  await respond(app.requests[1], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(app.page.reLogin.value, true)
  assert.equal(app.page.articleBody.value, '第一行\r\n第二行')
  assert.equal(auth.readSession(app.sessionStorage), null)
  app.page.email.value = member.email
  app.page.password.value = 'abcd'
  await vue.nextTick()
  const login = app.page.submitLogin()
  await respond(app.requests[2], 200, { success: true, message: 'OK', data: { access_token: 'new-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await login
  assert.equal(app.page.reLogin.value, false)
  assert.equal(app.page.articleTitle.value, '原標題')
  assert.equal(app.requests.length, 3, 're-login must not resubmit the article')
  const manualWrite = app.page.submitArticle()
  await respond(app.requests[3], 201, { success: true, message: 'OK', data: {} })
  await manualWrite
  assert.deepEqual(app.redirects, ['/vue/me/articles?published=1'])
})

// 成功頁從 URL 顯示通知；第一頁不受前次分頁狀態影響，且一般瀏覽不顯示誤導提示。
test('confirmed article publish opens my articles first page with success notice', async (t) => {
  const published = articleApp('/vue/me/articles', 'old-token', '?published=1')
  const ordinary = articleApp('/vue/me/articles', null)
  t.after(published.close)
  t.after(ordinary.close)
  assert.equal(published.page.account.value, null, 'success notice waits for account verification')
  await respond(published.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(published.requests[1].url, `${auth.API_BASE}/api/me/articles?page=1&per_page=20`)
  await respond(published.requests[1], 200, { success: true, message: 'OK', data: { list: [sample], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  assert.equal(published.page.published, true)
  assert.equal(published.page.listPage, 1)
  assert.equal(published.page.contentReady.value, true)
  assert.equal(ordinary.page.published, false)
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  assert.match(descriptor.template.content, /v-if="published && account"[^>]*role="status"[^>]*>文章已發表/)
})

// 201 若缺少核准的空 data 格式仍視為結果未知，不能顯示成功或重送寫入。
test('incomplete 201 publish response does not announce success', async (t) => {
  const app = articleApp('/vue/articles/new')
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleTitle.value = '標題'
  app.page.articleBody.value = '內文'
  const write = app.page.submitArticle()
  await respond(app.requests[1], 201, { success: true, message: 'OK', data: { id: 7 } })
  await write
  assert.deepEqual(app.redirects, [])
  assert.equal(app.page.writeUnknown.value, true)
  assert.equal(app.requests.length, 2)
})

// 重驗失效後若登入另一帳號，原帳號草稿不得繼承；遲到的舊回應也不得覆蓋新狀態。
test('revalidation 401 then different-account login clears draft', async (t) => {
  const app = articleApp('/vue/articles/new')
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleTitle.value = '私有草稿'
  app.page.articleBody.value = '保留到失效'
  const verify = app.page.verifyAccount()
  await respond(app.requests[1], 401, { success: false, error_code: 'E0002' })
  await verify
  assert.equal(app.page.reLogin.value, true)
  app.page.email.value = 'other@example.test'
  app.page.password.value = 'abcd'
  await vue.nextTick()
  const login = app.page.submitLogin()
  await respond(app.requests[2], 200, { success: true, message: 'OK', data: { access_token: 'other-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: { ...member, id: 3, email: 'other@example.test' } } })
  await login
  assert.equal(app.page.articleTitle.value, '')
  assert.equal(app.page.articleBody.value, '')
  assert.match(app.page.summary.value, /不同帳號/)
  assert.equal(app.requests.length, 3)
})

// 登出前須確認捨棄未送草稿；取消後憑證及後端登入均維持原狀。
test('cancelled logout preserves article draft and session', async (t) => {
  const app = articleApp('/vue/articles/new')
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleBody.value = '未送草稿'
  await app.page.submitLogout()
  assert.equal(app.requests.length, 1)
  assert.equal(app.confirmations.length, 1)
  assert.equal(app.page.articleBody.value, '未送草稿')
  assert.equal(auth.readSession(app.sessionStorage).accessToken, 'old-token')
})

// 同一篇文章換前端保留網址原始 ID、目前後端與原 JWT 到期時間；舊 /me 不可回寫。
test('frontend switch keeps article ID and exact shared session', async (t) => {
  const app = articleApp('/vue/articles/001', 'same-token', '', false, auth.BACKENDS.symfony)
  t.after(app.close)
  app.page.chooseFrontend({ target: { value: 'nuxt' } })
  assert.deepEqual(app.redirects, ['/nuxt/articles/001'])
  assert.equal(auth.readBackend(app.sessionStorage), auth.BACKENDS.symfony)
  assert.deepEqual(auth.readSession(app.sessionStorage), { backend: auth.BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(app.page.account.value, null)
})

// 取消保留文字與憑證；確認後清除憑證並帶合法頁面返回登入，不重送草稿。
test('backend switch cancels or discards draft without retrying and isolates old responses', async (t) => {
  const cancelled = articleApp('/vue/articles/new')
  t.after(cancelled.close)
  await respond(cancelled.requests[0], 200, { success: true, message: 'OK', data: member })
  cancelled.page.articleBody.value = '未儲存文字'
  const cancelledSelect = { target: { value: auth.BACKENDS.symfony } }
  await cancelled.page.chooseBackend(cancelledSelect)
  assert.equal(cancelledSelect.target.value, auth.BACKENDS.laravel)
  assert.equal(cancelled.page.articleBody.value, '未儲存文字')
  assert.equal(auth.readSession(cancelled.sessionStorage).accessToken, 'old-token')
  assert.deepEqual(cancelled.redirects, [])

  const discarded = articleApp('/vue/articles/new', 'old-token', '', true)
  t.after(discarded.close)
  await respond(discarded.requests[0], 200, { success: true, message: 'OK', data: member })
  discarded.page.articleBody.value = '捨棄文字'
  await discarded.page.chooseBackend({ target: { value: auth.BACKENDS.symfony } })
  assert.equal(auth.readBackend(discarded.sessionStorage), auth.BACKENDS.symfony)
  assert.equal(auth.readSession(discarded.sessionStorage), null)
  assert.deepEqual(discarded.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2Fnew'])
  assert.equal(discarded.requests.length, 1)
})

// 取消切換後仍可發文；201 導頁不可被殘留草稿提示擋住，等待中第二次送出也不得新增請求。
test('cancelled backend switch then confirmed publish leaves only one POST', async (t) => {
  const app = articleApp('/vue/articles/new', 'symfony-token', '', false, auth.BACKENDS.symfony, true)
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleTitle.value = '已確認標題'
  app.page.articleBody.value = '已確認內文'
  await app.page.chooseBackend({ target: { value: auth.BACKENDS.laravel } })
  assert.equal(app.page.backend.value, auth.BACKENDS.symfony)
  assert.equal(app.page.articleBody.value, '已確認內文')
  const publish = app.page.submitArticle()
  assert.equal(app.requests[1].url, `${auth.BACKENDS.symfony}/api/articles`)
  assert.equal(app.page.contentPending.value, true)
  await app.page.submitArticle()
  assert.equal(app.requests.length, 2, 'pending POST must prevent duplicate submission')
  await respond(app.requests[1], 201, { success: true, message: 'OK', data: {} })
  await publish
  assert.equal(app.page.hasDraft.value, false)
  assert.deepEqual(app.redirects, ['/vue/me/articles?published=1'])
  assert.equal(app.requests.length, 2)
})

// 逆向切換保留文章 ID；公開註冊／登入皆使用目前 Symfony API，不沿用 Laravel。
test('Symfony switches back to Laravel and public forms use the selected API', async (t) => {
  const reverse = articleApp('/vue/articles/001', 'symfony-token', '', false, auth.BACKENDS.symfony)
  t.after(reverse.close)
  assert.equal(reverse.requests[0].url, `${auth.BACKENDS.symfony}/api/me`)
  await reverse.page.chooseBackend({ target: { value: auth.BACKENDS.laravel } })
  assert.equal(auth.readSession(reverse.sessionStorage), null)
  assert.deepEqual(reverse.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])

  const registration = articleApp('/vue/register', null, '', false, auth.BACKENDS.symfony)
  t.after(registration.close)
  registration.page.email.value = 'new@example.test'
  registration.page.password.value = 'abcd'
  registration.page.nickname.value = '新會員'
  const post = registration.page.submitRegistration()
  assert.equal(registration.requests[0].url, `${auth.BACKENDS.symfony}/api/auth/register`)
  await respond(registration.requests[0], 422, { success: false, error_code: 'E0001', errors: { email: ['此 Email 已註冊'] } })
  await post

  const login = articleApp('/vue/login', null, '', false, auth.BACKENDS.symfony)
  t.after(login.close)
  login.page.email.value = 'new@example.test'
  login.page.password.value = 'abcd'
  const signIn = login.page.submitLogin()
  assert.equal(login.requests[0].url, `${auth.BACKENDS.symfony}/api/auth/login`)
  await respond(login.requests[0], 401, { success: false, error_code: 'E0101' })
  await signIn
})

// Django 回傳 E0105 時表單在倒數內不送第二次登入，歸零後只恢復操作而不自動提交。
test('Django login lockout blocks Enter-style submit until countdown ends', async (t) => {
  const app = articleApp('/vue/login', null, '', false, auth.BACKENDS.django)
  t.after(app.close)
  app.page.email.value = 'new@example.test'
  app.page.password.value = 'abcd'
  await vue.nextTick()
  const attempt = app.page.submitLogin()
  await respond(app.requests[0], 429, { success: false, error_code: 'E0105' }, { 'Retry-After': '60' })
  await attempt
  assert.equal(app.page.locked.value, true)
  assert.match(app.page.lockText.value, /登入暫時鎖定，約剩 01:00/)
  await app.page.submitLogin()
  assert.equal(app.requests.length, 1)
  app.page.lockDeadline.value = performance.now() - 1
  app.page.tickLock()
  assert.equal(app.page.locked.value, false)
  assert.equal(app.requests.length, 1)
  const retry = app.page.submitLogin()
  assert.equal(app.requests[1].url, `${auth.BACKENDS.django}/api/auth/login`)
  await respond(app.requests[1], 401, { success: false, error_code: 'E0101' })
  await retry
})

// 新增的 ASP.NET 選擇須清掉原 JWT；同分頁後續公開請求與帳號表單都指向 8083。
test('ASP.NET backend selection clears prior JWT and routes public and auth requests to 8083', async (t) => {
  const switched = articleApp('/vue/articles/001', 'laravel-token')
  t.after(switched.close)
  await switched.page.chooseBackend({ target: { value: auth.BACKENDS.aspnet } })
  assert.equal(auth.readBackend(switched.sessionStorage), 'http://localhost:8083')
  assert.equal(auth.readSession(switched.sessionStorage), null)
  assert.equal(switched.page.backendLabel.value, 'ASP.NET Core Controllers')
  assert.deepEqual(switched.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])

  const registration = articleApp('/vue/register', null, '', false, auth.BACKENDS.aspnet)
  t.after(registration.close)
  registration.page.email.value = 'new@example.test'
  registration.page.password.value = 'abcd'
  registration.page.nickname.value = '新會員'
  const post = registration.page.submitRegistration()
  assert.equal(registration.requests[0].url, 'http://localhost:8083/api/auth/register')
  await respond(registration.requests[0], 422, { success: false, error_code: 'E0001', errors: { email: ['此 Email 已註冊'] } })
  await post

  const login = articleApp('/vue/login', null, '', false, auth.BACKENDS.aspnet)
  t.after(login.close)
  login.page.email.value = 'new@example.test'
  login.page.password.value = 'abcd'
  const signIn = login.page.submitLogin()
  assert.equal(login.requests[0].url, 'http://localhost:8083/api/auth/login')
  await respond(login.requests[0], 401, { success: false, error_code: 'E0101' })
  await signIn

  const publicList = articleApp('/vue/', null, '', false, auth.BACKENDS.aspnet)
  t.after(publicList.close)
  assert.equal(publicList.page.backendLabel.value, 'ASP.NET Core Controllers')
  assert.equal(publicList.requests[0].url, 'http://localhost:8083/api/articles?page=1&per_page=20')
  assert.equal(publicList.requests[0].headers.Authorization, undefined)
})

// Django 改選後舊 JWT 立即失效；公開閱讀、註冊與登入都直連相同 API 路徑。
test('Django selection clears old JWT and serves public and auth routes', async (t) => {
  const switched = articleApp('/vue/articles/001', 'laravel-token')
  t.after(switched.close)
  await switched.page.chooseBackend({ target: { value: auth.BACKENDS.django } })
  assert.equal(auth.readBackend(switched.sessionStorage), auth.BACKENDS.django)
  assert.equal(auth.readSession(switched.sessionStorage), null)
  assert.equal(switched.page.backendLabel.value, 'Django')
  assert.deepEqual(switched.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])
  await respond(switched.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(switched.page.account.value, null)

  const registration = articleApp('/vue/register', null, '', false, auth.BACKENDS.django)
  t.after(registration.close)
  registration.page.email.value = 'new@example.test'
  registration.page.password.value = 'abcd'
  registration.page.nickname.value = '新會員'
  const post = registration.page.submitRegistration()
  assert.equal(registration.requests[0].url, `${auth.BACKENDS.django}/api/auth/register`)
  await respond(registration.requests[0], 201, { success: true, message: 'OK', data: {} })
  await post

  const login = articleApp('/vue/login', null, '', false, auth.BACKENDS.django)
  t.after(login.close)
  login.page.email.value = 'new@example.test'
  login.page.password.value = 'abcd'
  const signIn = login.page.submitLogin()
  assert.equal(login.requests[0].url, `${auth.BACKENDS.django}/api/auth/login`)
  await respond(login.requests[0], 401, { success: false, error_code: 'E0101' })
  await signIn
  const successfulLogin = login.page.submitLogin()
  assert.equal(login.requests[1].url, `${auth.BACKENDS.django}/api/auth/login`)
  await respond(login.requests[1], 200, { success: true, message: 'OK', data: { access_token: 'django-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await successfulLogin
  assert.deepEqual(auth.readSession(login.sessionStorage), { backend: auth.BACKENDS.django, accessToken: 'django-jwt', expiresAt: '2026-09-24 18:00:00' })

  const authenticated = articleApp('/vue/', 'django-jwt', '', false, auth.BACKENDS.django)
  t.after(authenticated.close)
  assert.equal(authenticated.requests[0].url, `${auth.BACKENDS.django}/api/me`)
  await respond(authenticated.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(authenticated.requests[1].url, `${auth.BACKENDS.django}/api/articles?page=1&per_page=20`)
  await respond(authenticated.requests[1], 200, { success: true, message: 'OK', data: { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } } })
  const logout = authenticated.page.submitLogout()
  assert.equal(authenticated.requests[2].url, `${auth.BACKENDS.django}/api/auth/logout`)
  assert.equal(authenticated.requests[2].headers.Authorization, 'Bearer django-jwt')
  await respond(authenticated.requests[2], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(auth.readSession(authenticated.sessionStorage), null)

  const listing = articleApp('/vue/', null, '', false, auth.BACKENDS.django)
  t.after(listing.close)
  assert.equal(listing.requests[0].url, `${auth.BACKENDS.django}/api/articles?page=1&per_page=20`)
  assert.equal(listing.requests[0].headers.Authorization, undefined)
  await respond(listing.requests[0], 200, { success: true, message: 'OK', data: { list: [sample], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  assert.equal(listing.page.articleList.value?.list[0].id, sample.id)

  const detail = articleApp('/vue/articles/007', null, '', false, auth.BACKENDS.django)
  t.after(detail.close)
  assert.equal(detail.requests[0].url, `${auth.BACKENDS.django}/api/articles/7`)
  await respond(detail.requests[0], 200, { success: true, message: 'OK', data: sample })
  assert.equal(detail.requests[1].url, `${auth.BACKENDS.django}/api/articles/7/comments?page=1&per_page=20`)
  assert.equal(detail.page.canComment.value, false)
  assert.equal(detail.page.canManageVisibility.value, false)
})

// Flask 切換保留合法文章返回目標，並使晚到的 Laravel 驗權回應無法恢復舊登入。
test('Flask selection clears prior JWT and ignores old backend response', async (t) => {
  const switched = articleApp('/vue/articles/001', 'laravel-jwt')
  t.after(switched.close)
  await switched.page.chooseBackend({ target: { value: auth.BACKENDS.flask } })
  assert.equal(auth.readBackend(switched.sessionStorage), 'http://localhost:8085')
  assert.equal(auth.readSession(switched.sessionStorage), null)
  assert.equal(switched.page.backendLabel.value, 'Flask')
  assert.equal(switched.page.canSwitchNuxt.value, false)
  assert.deepEqual(switched.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])
  await respond(switched.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(switched.page.account.value, null)

  // 反向切換也不能把 Flask JWT 配給 Laravel。
  const reverse = articleApp('/vue/articles/001', 'flask-jwt', '', false, auth.BACKENDS.flask)
  t.after(reverse.close)
  await reverse.page.chooseBackend({ target: { value: auth.BACKENDS.laravel } })
  assert.equal(auth.readBackend(reverse.sessionStorage), auth.BACKENDS.laravel)
  assert.equal(auth.readSession(reverse.sessionStorage), null)
  assert.deepEqual(reverse.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])
})

// Nuxt 尚未接受 Flask 後端 key，選單應拒絕切換並保留目前的 Flask 憑證。
test('Flask selection keeps Nuxt switch unavailable', (t) => {
  const app = articleApp('/vue/', 'flask-jwt', '', false, auth.BACKENDS.flask)
  t.after(app.close)
  const choice = { target: { value: 'nuxt' } }
  app.page.chooseFrontend(choice)
  assert.equal(app.page.canSwitchNuxt.value, false)
  assert.equal(choice.target.value, 'vue')
  assert.deepEqual(app.redirects, [])
  assert.equal(auth.readSession(app.sessionStorage).accessToken, 'flask-jwt')
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  assert.match(descriptor.template.content, /flaskBackend \|\| fastapiBackend \|\| ginBackend \|\| echoBackend \? '（此後端尚未提供）'/)
})

// Flask 註冊與登入沿用共用路徑，429 倒數期間阻擋送出，成功後只保存原 JWT 期限。
test('Flask registration and locked login keep approved transitions', async (t) => {
  const registration = articleApp('/vue/register', null, '', false, auth.BACKENDS.flask)
  t.after(registration.close)
  registration.page.email.value = 'new@example.test'
  registration.page.password.value = 'abcd'
  registration.page.nickname.value = '新會員'
  const post = registration.page.submitRegistration()
  assert.equal(registration.requests[0].url, `${auth.BACKENDS.flask}/api/auth/register`)
  assert.equal(registration.requests[0].method, 'POST')
  await respond(registration.requests[0], 201, { success: true, message: 'OK', data: {} })
  await post
  assert.deepEqual(registration.redirects, ['/vue/login?registered=1'])
  assert.equal(auth.readSession(registration.sessionStorage), null)

  const login = articleApp('/vue/login', null, '?return_to=%2Fvue%2Farticles%2F007', false, auth.BACKENDS.flask)
  t.after(login.close)
  login.page.email.value = 'writer@example.test'
  login.page.password.value = 'abcd'
  await vue.nextTick()
  const first = login.page.submitLogin()
  assert.equal(login.requests[0].url, `${auth.BACKENDS.flask}/api/auth/login`)
  assert.equal(login.requests[0].method, 'POST')
  assert.equal(login.requests[0].headers.Authorization, undefined)
  await respond(login.requests[0], 429, { success: false, error_code: 'E0105' }, { 'Retry-After': '60' })
  await first
  assert.equal(login.page.locked.value, true)
  assert.match(login.page.lockText.value, /登入暫時鎖定，約剩 01:00/)
  await login.page.submitLogin()
  assert.equal(login.requests.length, 1, 'Enter and button submission use the same locked guard')
  login.page.lockDeadline.value = performance.now() - 1
  login.page.tickLock()
  assert.equal(login.page.locked.value, false)
  assert.equal(login.requests.length, 1, 'countdown end never submits automatically')
  const second = login.page.submitLogin()
  await respond(login.requests[1], 200, { success: true, message: 'OK', data: { access_token: 'flask-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await second
  assert.deepEqual(auth.readSession(login.sessionStorage), { backend: auth.BACKENDS.flask, accessToken: 'flask-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.deepEqual(login.redirects, ['/vue/articles/007'])
})

// Flask 刷新時先 GET /api/me，再讀公開文章；登出失敗保留同一 JWT，成功才清除。
test('Flask public reading and logout use only approved endpoints', async (t) => {
  const authenticated = articleApp('/vue/', 'flask-jwt', '', false, auth.BACKENDS.flask)
  t.after(authenticated.close)
  assert.equal(authenticated.requests[0].url, `${auth.BACKENDS.flask}/api/me`)
  assert.equal(authenticated.requests[0].headers.Authorization, 'Bearer flask-jwt')
  await respond(authenticated.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(authenticated.requests[1].url, `${auth.BACKENDS.flask}/api/articles?page=1&per_page=20`)
  await respond(authenticated.requests[1], 200, { success: true, message: 'OK', data: { list: [sample], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  assert.equal(authenticated.page.articleList.value?.list[0].id, sample.id)
  const failedLogout = authenticated.page.submitLogout()
  assert.equal(authenticated.requests[2].url, `${auth.BACKENDS.flask}/api/auth/logout`)
  assert.equal(authenticated.requests[2].headers.Authorization, 'Bearer flask-jwt')
  await respond(authenticated.requests[2], 500, { success: false, error_code: 'E9999' })
  await failedLogout
  assert.equal(auth.readSession(authenticated.sessionStorage).accessToken, 'flask-jwt')
  const logout = authenticated.page.submitLogout()
  assert.equal(authenticated.requests[3].headers.Authorization, 'Bearer flask-jwt')
  await respond(authenticated.requests[3], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(auth.readSession(authenticated.sessionStorage), null)
  assert.deepEqual(authenticated.redirects, ['/vue/login'])

  // 訪客直接讀列表、詳情及留言，均沿用共用路徑且不攜帶 JWT。
  const listing = articleApp('/vue/', null, '', false, auth.BACKENDS.flask)
  t.after(listing.close)
  assert.equal(listing.requests[0].url, `${auth.BACKENDS.flask}/api/articles?page=1&per_page=20`)
  assert.equal(listing.requests[0].headers.Authorization, undefined)
  const detail = articleApp('/vue/articles/007', null, '', false, auth.BACKENDS.flask)
  t.after(detail.close)
  assert.equal(detail.requests[0].url, `${auth.BACKENDS.flask}/api/articles/7`)
  await respond(detail.requests[0], 200, { success: true, message: 'OK', data: sample })
  assert.equal(detail.requests[1].url, `${auth.BACKENDS.flask}/api/articles/7/comments?page=1&per_page=20`)
  assert.equal(detail.page.canComment.value, false)
  assert.equal(detail.page.canEdit.value, false)
  assert.equal(detail.page.canManageVisibility.value, false)

  // Flask 作者驗權並讀到自己的公開文章後，可以進入共用編輯、留言與可見性流程。
  const authorDetail = articleApp('/vue/articles/007', 'flask-jwt', '', false, auth.BACKENDS.flask)
  t.after(authorDetail.close)
  await respond(authorDetail.requests[0], 200, { success: true, message: 'OK', data: member })
  await respond(authorDetail.requests[1], 200, { success: true, message: 'OK', data: sample })
  assert.equal(authorDetail.page.canEdit.value, true)
  assert.equal(authorDetail.page.canComment.value, true)
  assert.equal(authorDetail.page.canManageVisibility.value, true)
})

// Flask 作者深連結先依共同權限流程驗證；管理路由與權限由 admin.test.js 覆蓋。
test('Flask author routes verify identity', async (t) => {
  const authorRoutes = new Map([
    ['/vue/articles/new', null],
    ['/vue/articles/7/edit', '/api/articles/7'],
    ['/vue/me/articles', '/api/me/articles?page=1&per_page=20'],
    ['/vue/me/comments', '/api/me/comments?page=1&per_page=20'],
    ['/vue/comments/9', '/api/comments/9'],
    ['/vue/comments/9/edit', '/api/comments/9'],
  ])
  for (const [path, endpoint] of authorRoutes) {
    const app = articleApp(path, null, '', false, auth.BACKENDS.flask)
    t.after(app.close)
    assert.equal(app.requests.length, 0, path)
    assert.deepEqual(app.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    const signedIn = articleApp(path, 'flask-jwt', '', false, auth.BACKENDS.flask)
    t.after(signedIn.close)
    assert.equal(signedIn.requests[0].url, `${auth.BACKENDS.flask}/api/me`, path)
    await respond(signedIn.requests[0], 200, { success: true, message: 'OK', data: member })
    assert.equal(signedIn.requests.length, endpoint ? 2 : 1, path)
    if (endpoint) assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.flask}${endpoint}`, path)
  }
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  assert.match(descriptor.template.content, /<option :value="BACKENDS\.flask">Flask<\/option>/)
  assert.match(descriptor.template.content, /v-if="account && \(account\.id === item\.author\.id \|\| account\.role === 'admin'\)"[^>]*>閱讀留言詳情/)
})

// FastAPI 切換雙向清除舊 JWT，晚到的舊驗權回應不得恢復上一後端帳號。
test('FastAPI selection clears prior JWT and ignores old backend response', async (t) => {
  const switched = articleApp('/vue/articles/001', 'laravel-jwt')
  t.after(switched.close)
  await switched.page.chooseBackend({ target: { value: auth.BACKENDS.fastapi } })
  assert.equal(auth.readBackend(switched.sessionStorage), auth.BACKENDS.fastapi)
  assert.equal(auth.readSession(switched.sessionStorage), null)
  assert.equal(switched.page.backendLabel.value, 'FastAPI')
  assert.deepEqual(switched.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])
  await respond(switched.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(switched.page.account.value, null)

  const reverse = articleApp('/vue/articles/001', 'fastapi-jwt', '', false, auth.BACKENDS.fastapi)
  t.after(reverse.close)
  await reverse.page.chooseBackend({ target: { value: auth.BACKENDS.laravel } })
  assert.equal(auth.readSession(reverse.sessionStorage), null)
  assert.deepEqual(reverse.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])
})

// Nuxt 尚未支援 FastAPI；使用者即使觸發 change 也保留目前 JWT 與 Vue 畫面。
test('FastAPI keeps Nuxt switch unavailable without clearing JWT', (t) => {
  const app = articleApp('/vue/', 'fastapi-jwt', '', false, auth.BACKENDS.fastapi)
  t.after(app.close)
  const choice = { target: { value: 'nuxt' } }
  app.page.chooseFrontend(choice)
  assert.equal(app.page.canSwitchNuxt.value, false)
  assert.equal(choice.target.value, 'vue')
  assert.deepEqual(app.redirects, [])
  assert.equal(auth.readSession(app.sessionStorage).accessToken, 'fastapi-jwt')
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  assert.match(descriptor.template.content, /<option :value="BACKENDS\.fastapi">FastAPI<\/option>/)
  assert.match(descriptor.template.content, /fastapiBackend \? 'Nuxt 尚未支援目前選擇的 FastAPI 後端/)
})

// FastAPI 沿用正式表單、429 倒數與公開 API，登入後只保存 8086 對應的原 JWT 期限。
test('FastAPI registration, lockout, reading and logout use approved endpoints', async (t) => {
  const registration = articleApp('/vue/register', null, '', false, auth.BACKENDS.fastapi)
  t.after(registration.close)
  registration.page.email.value = 'new@example.test'
  registration.page.password.value = 'abcd'
  registration.page.nickname.value = '新會員'
  const post = registration.page.submitRegistration()
  assert.equal(registration.requests[0].url, `${auth.BACKENDS.fastapi}/api/auth/register`)
  await respond(registration.requests[0], 201, { success: true, message: 'OK', data: {} })
  await post
  assert.deepEqual(registration.redirects, ['/vue/login?registered=1'])

  const login = articleApp('/vue/login', null, '?return_to=%2Fvue%2Farticles%2F007', false, auth.BACKENDS.fastapi)
  t.after(login.close)
  login.page.email.value = member.email
  login.page.password.value = 'abcd'
  await vue.nextTick()
  const first = login.page.submitLogin()
  assert.equal(login.requests[0].url, `${auth.BACKENDS.fastapi}/api/auth/login`)
  await respond(login.requests[0], 429, { success: false, error_code: 'E0105' }, { 'Retry-After': '60' })
  await first
  assert.equal(login.page.locked.value, true)
  await login.page.submitLogin()
  assert.equal(login.requests.length, 1)
  login.page.lockDeadline.value = performance.now() - 1
  login.page.tickLock()
  const second = login.page.submitLogin()
  await respond(login.requests[1], 200, { success: true, message: 'OK', data: { access_token: 'fastapi-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await second
  assert.deepEqual(auth.readSession(login.sessionStorage), { backend: auth.BACKENDS.fastapi, accessToken: 'fastapi-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.deepEqual(login.redirects, ['/vue/articles/007'])

  const detail = articleApp('/vue/articles/007', 'fastapi-jwt', '', false, auth.BACKENDS.fastapi)
  t.after(detail.close)
  assert.equal(detail.requests[0].url, `${auth.BACKENDS.fastapi}/api/me`)
  await respond(detail.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(detail.requests[1].url, `${auth.BACKENDS.fastapi}/api/articles/7`)
  await respond(detail.requests[1], 200, { success: true, message: 'OK', data: sample })
  assert.equal(detail.requests[2].url, `${auth.BACKENDS.fastapi}/api/articles/7/comments?page=1&per_page=20`)
  await respond(detail.requests[2], 200, { success: true, message: 'OK', data: { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } } })
  assert.equal(detail.page.commentList.value?.list.length, 0)
  assert.equal(detail.page.canComment.value, true)
  assert.equal(detail.page.canEdit.value, true)
  assert.equal(detail.page.canManageVisibility.value, true)
  const logout = detail.page.submitLogout()
  assert.equal(detail.requests[3].url, `${auth.BACKENDS.fastapi}/api/auth/logout`)
  await respond(detail.requests[3], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(auth.readSession(detail.sessionStorage), null)
  assert.deepEqual(detail.redirects, ['/vue/login'])

  const visitor = articleApp('/vue/', null, '', false, auth.BACKENDS.fastapi)
  t.after(visitor.close)
  assert.equal(visitor.requests[0].url, `${auth.BACKENDS.fastapi}/api/articles?page=1&per_page=20`)
  assert.equal(visitor.requests[0].headers.Authorization, undefined)
})

// Gin 選擇會撤銷舊 JWT；註冊、鎖定登入、重載驗權及閱讀均走固定 8087。
test('Gin uses its own session and ordered public auth and reading requests', async (t) => {
  const switched = articleApp('/vue/', 'laravel-jwt')
  t.after(switched.close)
  const choice = { target: { value: auth.BACKENDS.gin } }
  await switched.page.chooseBackend(choice)
  assert.equal(auth.readSession(switched.sessionStorage), null)
  assert.deepEqual(switched.redirects, ['/vue/login?return_to=%2Fvue%2F'])

  const registration = articleApp('/vue/register', null, '', false, auth.BACKENDS.gin)
  t.after(registration.close)
  // Gin 作者 API 已開放；註冊頁應說明暱稱會顯示在發表內容旁。
  const registrationView = await renderArticleApp(registration)
  assert.match(registrationView, /會顯示在您日後發表的內容旁/)
  registration.page.email.value = 'new@example.test'
  registration.page.password.value = 'abcd'
  registration.page.nickname.value = '新會員'
  const register = registration.page.submitRegistration()
  assert.equal(registration.requests[0].url, `${auth.BACKENDS.gin}/api/auth/register`)
  await respond(registration.requests[0], 201, { success: true, message: 'OK', data: {} })
  await register
  assert.deepEqual(registration.redirects, ['/vue/login?registered=1'])

  const login = articleApp('/vue/login', null, '', false, auth.BACKENDS.gin)
  t.after(login.close)
  // 使用實際渲染結果確認 Gin 登入頁提供作者功能導引。
  const loginView = await renderArticleApp(login)
  assert.match(loginView, /登入後即可發表文章與查看自己的文章/)
  login.page.email.value = member.email
  login.page.password.value = 'abcd'
  await vue.nextTick()
  const first = login.page.submitLogin()
  assert.equal(login.requests[0].url, `${auth.BACKENDS.gin}/api/auth/login`)
  assert.equal(login.requests[0].headers.Authorization, undefined)
  await respond(login.requests[0], 429, { success: false, error_code: 'E0105' }, { 'Retry-After': '60' })
  await first
  assert.equal(login.page.locked.value, true)
  assert.match(await renderArticleApp(login), /登入暫時鎖定，約剩 0[01]:[0-5][0-9]/)
  await login.page.submitLogin()
  assert.equal(login.requests.length, 1)
  login.page.lockDeadline.value = performance.now() - 1
  login.page.tickLock()
  const second = login.page.submitLogin()
  await respond(login.requests[1], 200, { success: true, message: 'OK', data: { access_token: 'gin-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await second
  assert.deepEqual(auth.readSession(login.sessionStorage), { backend: auth.BACKENDS.gin, accessToken: 'gin-jwt', expiresAt: '2026-09-24 18:00:00' })
  assert.deepEqual(login.redirects, ['/vue/'])

  const detail = articleApp('/vue/articles/7', 'gin-jwt', '', false, auth.BACKENDS.gin)
  t.after(detail.close)
  assert.equal(detail.requests[0].url, `${auth.BACKENDS.gin}/api/me`)
  assert.equal(detail.requests[0].headers.Authorization, 'Bearer gin-jwt')
  await respond(detail.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(detail.requests[1].url, `${auth.BACKENDS.gin}/api/articles/7`)
  await respond(detail.requests[1], 200, { success: true, message: 'OK', data: sample })
  assert.equal(detail.requests[2].url, `${auth.BACKENDS.gin}/api/articles/7/comments?page=1&per_page=20`)
  await respond(detail.requests[2], 200, { success: true, message: 'OK', data: { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } } })
  assert.equal(detail.page.canComment.value, true)
  assert.equal(detail.page.canEdit.value, true)
  assert.equal(detail.page.canManageVisibility.value, true)
  const rendered = await renderArticleApp(detail)
  assert.match(rendered, /閱讀文章/)
  assert.match(rendered, /發表留言/)
  assert.match(rendered, /編輯文章/)
  assert.match(rendered, /我的文章/)
  const logout = detail.page.submitLogout()
  assert.equal(detail.requests[3].url, `${auth.BACKENDS.gin}/api/auth/logout`)
  assert.equal(detail.requests[3].headers.Authorization, 'Bearer gin-jwt')
  await respond(detail.requests[3], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(auth.readSession(detail.sessionStorage), null)

  const visitor = articleApp('/vue/', null, '', false, auth.BACKENDS.gin)
  t.after(visitor.close)
  assert.equal(visitor.requests[0].url, `${auth.BACKENDS.gin}/api/articles?page=1&per_page=20`)
  assert.equal(visitor.requests[0].headers.Authorization, undefined)
  await respond(visitor.requests[0], 200, { success: true, message: 'OK', data: { list: [sample], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  assert.match(await renderArticleApp(visitor), /閱讀全文/)

  const guestDetail = articleApp('/vue/articles/7', null, '', false, auth.BACKENDS.gin)
  t.after(guestDetail.close)
  assert.equal(guestDetail.requests[0].url, `${auth.BACKENDS.gin}/api/articles/7`)
  assert.equal(guestDetail.requests[0].headers.Authorization, undefined)
  await respond(guestDetail.requests[0], 200, { success: true, message: 'OK', data: sample })
  assert.equal(guestDetail.requests[1].url, `${auth.BACKENDS.gin}/api/articles/7/comments?page=1&per_page=20`)
  assert.equal(guestDetail.requests[1].headers.Authorization, undefined)
  await respond(guestDetail.requests[1], 200, { success: true, message: 'OK', data: { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } } })
  assert.match(await renderArticleApp(guestDetail), /目前尚無留言/)
})

// Nuxt 尚未支援 Gin 後端；切換控制保留目前 Vue 與原登入憑證。
test('Gin frontend switch keeps the selected backend in Vue', async (t) => {
  const app = articleApp('/vue/', 'gin-jwt', '', false, auth.BACKENDS.gin)
  t.after(app.close)
  assert.equal(app.page.canSwitchNuxt.value, false)
  const choice = { target: { value: 'nuxt' } }
  app.page.chooseFrontend(choice)
  assert.equal(choice.target.value, 'vue')
  app.page.versionsOpen.value = true
  assert.match(await renderArticleApp(app), /Nuxt 尚未支援目前選擇的 Gin 後端/)
})

// Echo 切換先清舊 JWT，再保留合法作者與管理目標供新後端重新驗權。
test('Echo selection and login return preserve protected routes', async (t) => {
  const switched = articleApp('/vue/me/articles', 'laravel-jwt')
  t.after(switched.close)
  await switched.page.chooseBackend({ target: { value: auth.BACKENDS.echo } })
  assert.equal(auth.readSession(switched.sessionStorage), null)
  assert.deepEqual(switched.redirects, ['/vue/login?return_to=%2Fvue%2Fme%2Farticles'])
  await respond(switched.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(switched.requests.length, 1)

  for (const destination of ['/vue/me/articles', '/vue/articles/new', '/vue/me/comments', '/vue/comments/9', '/vue/articles/7/edit', '/vue/comments/9/edit', '/vue/articles/007']) {
    const login = articleApp('/vue/login', null, `?return_to=${encodeURIComponent(destination)}`, false, auth.BACKENDS.echo)
    t.after(login.close)
    login.page.email.value = member.email
    login.page.password.value = 'abcd'
    await vue.nextTick()
    const submit = login.page.submitLogin()
    await respond(login.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'echo-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
    await submit
    assert.deepEqual(login.redirects, [destination])
  }

  const adminSwitch = articleApp('/vue/admin/members', 'laravel-jwt')
  t.after(adminSwitch.close)
  await adminSwitch.page.chooseBackend({ target: { value: auth.BACKENDS.echo } })
  assert.equal(auth.readSession(adminSwitch.sessionStorage), null)
  assert.deepEqual(adminSwitch.redirects, ['/vue/login?return_to=%2Fvue%2Fadmin%2Fmembers'])
  const adminLogin = articleApp('/vue/login', null, '?return_to=%2Fvue%2Fadmin%2Farticles', false, auth.BACKENDS.echo)
  t.after(adminLogin.close)
  adminLogin.page.email.value = member.email
  adminLogin.page.password.value = 'abcd'
  await vue.nextTick()
  const login = adminLogin.page.submitLogin()
  await respond(adminLogin.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'echo-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await login
  assert.deepEqual(adminLogin.redirects, ['/vue/admin/articles'])

  // Nuxt 尚未接入 Echo，切換前端仍保留原憑證和 Vue 畫面。
  const frontend = articleApp('/vue/', 'echo-jwt', '', false, auth.BACKENDS.echo)
  t.after(frontend.close)
  const choice = { target: { value: 'nuxt' } }
  frontend.page.chooseFrontend(choice)
  assert.equal(choice.target.value, 'vue')
  assert.equal(frontend.page.canSwitchNuxt.value, false)
  assert.equal(auth.readSession(frontend.sessionStorage).accessToken, 'echo-jwt')
  assert.deepEqual(frontend.redirects, [])
  frontend.page.versionsOpen.value = true
  assert.match(await renderArticleApp(frontend), /Nuxt 尚未支援目前選擇的 Echo 後端/)

  // 已保存的 Echo JWT 在登入頁重驗後，仍保留合法管理目標供該頁驗權。
  const restored = articleApp('/vue/login', 'echo-jwt', '?return_to=%2Fvue%2Fadmin%2Fmembers', false, auth.BACKENDS.echo)
  t.after(restored.close)
  await respond(restored.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.deepEqual(restored.redirects, ['/vue/admin/members'])
})

// Fiber 作者頁可保留同類返回目標；切換後端仍清除舊 JWT，管理頁改回公開入口。
test('Fiber switch preserves author page and blocks admin return targets', async (t) => {
  const supported = articleApp('/vue/articles/7', 'laravel-jwt')
  t.after(supported.close)
  await supported.page.chooseBackend({ target: { value: auth.BACKENDS.fiber } })
  assert.equal(auth.readSession(supported.sessionStorage), null)
  assert.deepEqual(supported.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F7'])

  const unsupported = articleApp('/vue/me/articles', 'laravel-jwt')
  t.after(unsupported.close)
  await unsupported.page.chooseBackend({ target: { value: auth.BACKENDS.fiber } })
  assert.equal(auth.readSession(unsupported.sessionStorage), null)
  assert.deepEqual(unsupported.redirects, ['/vue/login?return_to=%2Fvue%2Fme%2Farticles'])

  for (const target of ['/vue/articles/7', '/vue/articles/new', '/vue/me/articles', '/vue/me/comments', '/vue/comments/9', '/vue/articles/7/edit', '/vue/comments/9/edit', '/vue/admin/members']) {
    const loginPage = articleApp('/vue/login', null, `?return_to=${encodeURIComponent(target)}`, false, auth.BACKENDS.fiber)
    t.after(loginPage.close)
    loginPage.page.email.value = member.email
    loginPage.page.password.value = 'abcd'
    await vue.nextTick()
    const login = loginPage.page.submitLogin()
    assert.equal(loginPage.requests[0].url, `${auth.BACKENDS.fiber}/api/auth/login`)
    await respond(loginPage.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'fiber-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
    await login
    assert.deepEqual(loginPage.redirects, [target.startsWith('/vue/admin/') ? '/vue/' : target])
  }
})

// Fiber 作者深連結先驗權，再依路徑讀取資源；只有管理頁停在未提供畫面。
test('Fiber author deep links verify account and admin deep links stay unavailable', async (t) => {
  for (const [path, endpoint] of [
    ['/vue/articles/new', null], ['/vue/me/articles', '/api/me/articles?page=1&per_page=20'],
    ['/vue/me/comments', '/api/me/comments?page=1&per_page=20'], ['/vue/comments/9', '/api/comments/9'],
    ['/vue/articles/7/edit', '/api/articles/7'], ['/vue/comments/9/edit', '/api/comments/9'],
  ]) {
    const app = articleApp(path, 'fiber-jwt', '', false, auth.BACKENDS.fiber)
    t.after(app.close)
    assert.equal(app.requests[0].url, `${auth.BACKENDS.fiber}/api/me`, path)
    await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
    assert.equal(app.requests.length, endpoint ? 2 : 1, path)
    if (endpoint) assert.equal(app.requests[1].url, `${auth.BACKENDS.fiber}${endpoint}`, path)
  }
  for (const path of ['/vue/admin/members', '/vue/admin/articles', '/vue/admin/comments', '/vue/admin/settings/login-lockout']) {
    const app = articleApp(path, 'fiber-jwt', '', false, auth.BACKENDS.fiber)
    t.after(app.close)
    assert.equal(app.requests.length, 0, path)
    assert.deepEqual(app.redirects, [], path)
    assert.match(await renderArticleApp(app), /此頁尚未提供/, path)
  }
})

// Fiber 詳情提供作者操作，管理入口仍隱藏；Nuxt 切換與登出維持原契約。
test('Fiber reads public content and presents author controls without admin pages', async (t) => {
  const app = articleApp('/vue/articles/7', 'fiber-jwt', '', false, auth.BACKENDS.fiber)
  t.after(app.close)
  assert.equal(app.page.backendLabel.value, 'Fiber')
  assert.equal(app.requests[0].url, `${auth.BACKENDS.fiber}/api/me`)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: { ...member, role: 'admin' } })
  assert.equal(app.requests[1].url, `${auth.BACKENDS.fiber}/api/articles/7`)
  await respond(app.requests[1], 200, { success: true, message: 'OK', data: { ...sample, created_backend: 'fiber', author: { id: member.id, nickname: member.nickname }, can_restore: true } })
  assert.equal(app.requests[2].url, `${auth.BACKENDS.fiber}/api/articles/7/comments?page=1&per_page=20`)
  await respond(app.requests[2], 200, { success: true, message: 'OK', data: { list: [{ id: 9, article_id: 7, body: '既有留言', author: { id: member.id, nickname: member.nickname }, version: 1, created_backend: 'fiber', created_at: sample.created_at, updated_at: sample.updated_at, visibility: 'visible', can_restore: false }], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  const html = await renderArticleApp(app)
  assert.match(html, /既有留言/)
  assert.match(html, /建立於 Fiber/)
  assert.match(html, /href="\/vue\/comments\/9"/)
  assert.match(html, /href="\/vue\/articles\/new"/)
  assert.match(html, /發表留言|編輯文章|>下架</)
  assert.doesNotMatch(html, /href="\/vue\/admin\//)
  assert.equal(app.page.canSwitchNuxt.value, false)
  assert.equal(app.page.canComment.value, true)
  assert.equal(app.page.canEdit.value, true)
  assert.equal(app.page.canManageVisibility.value, true)
  app.page.versionsOpen.value = true
  assert.match(await renderArticleApp(app), /Nuxt 尚未支援目前選擇的 Fiber 後端/)
  const frontendChoice = { target: { value: 'nuxt' } }
  app.page.chooseFrontend(frontendChoice)
  assert.equal(frontendChoice.target.value, 'vue')
  assert.equal(auth.readSession(app.sessionStorage).accessToken, 'fiber-jwt')
  const logout = app.page.submitLogout()
  assert.equal(app.requests[3].url, `${auth.BACKENDS.fiber}/api/auth/logout`)
  await respond(app.requests[3], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(auth.readSession(app.sessionStorage), null)
})

// Fiber 發文 401 保留同帳號草稿但不自動重送；換帳號時清除原帳號文字。
test('Fiber author draft recovery keeps only the same account input', async (t) => {
  for (const different of [false, true]) {
    const app = articleApp('/vue/articles/new', 'fiber-jwt', '', false, auth.BACKENDS.fiber)
    t.after(app.close)
    await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
    app.page.articleTitle.value = '私有標題'
    app.page.articleBody.value = '私有內文'
    const write = app.page.submitArticle()
    assert.equal(app.requests[1].url, `${auth.BACKENDS.fiber}/api/articles`)
    await respond(app.requests[1], 401, { success: false, error_code: 'E0002' })
    await write
    assert.equal(app.page.reLogin.value, true)
    assert.equal(app.page.articleBody.value, '私有內文')
    app.page.email.value = different ? 'other@example.test' : member.email
    app.page.password.value = 'abcd'
    await vue.nextTick()
    const login = app.page.submitLogin()
    await respond(app.requests[2], 200, { success: true, message: 'OK', data: { access_token: 'next-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: different ? { ...member, id: 3, email: 'other@example.test' } : member } })
    await login
    assert.equal(app.page.articleBody.value, different ? '' : '私有內文')
    assert.equal(app.requests.length, 3, '重登後不得自動重送發文')
  }
})

// Fiber 201 格式不完整時保留文字及查證入口，不能猜新文章 ID 或重送。
test('Fiber unknown publish keeps manual verification guidance', async (t) => {
  const app = articleApp('/vue/articles/new', 'fiber-jwt', '', false, auth.BACKENDS.fiber)
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleTitle.value = '待查證'
  app.page.articleBody.value = '尚未確認寫入'
  const write = app.page.submitArticle()
  await respond(app.requests[1], 201, { success: true, message: 'OK', data: { id: 42 } })
  await write
  assert.equal(app.page.writeUnknown.value, true)
  assert.equal(app.page.articleBody.value, '尚未確認寫入')
  assert.equal(app.requests.length, 2)
  assert.deepEqual(app.redirects, [])
  assert.match(await renderArticleApp(app), /先到我的文章查證/)
})

// Fiber 註冊、鎖定登入與訪客閱讀均走既有契約；失效 JWT 不應被當成訪客靜默略過。
test('Fiber registration lockout and guest reading use shared flows', async (t) => {
  const registrationPage = articleApp('/vue/register', null, '', false, auth.BACKENDS.fiber)
  t.after(registrationPage.close)
  assert.match(await renderArticleApp(registrationPage), /建立帳號後即可登入並發表文章/)
  registrationPage.page.email.value = 'new@example.test'
  registrationPage.page.password.value = 'abcd'
  registrationPage.page.nickname.value = '新會員'
  const registration = registrationPage.page.submitRegistration()
  assert.equal(registrationPage.requests[0].url, `${auth.BACKENDS.fiber}/api/auth/register`)
  await respond(registrationPage.requests[0], 201, { success: true, message: 'OK', data: {} })
  await registration
  assert.deepEqual(registrationPage.redirects, ['/vue/login?registered=1'])

  const locked = articleApp('/vue/login', null, '', false, auth.BACKENDS.fiber)
  t.after(locked.close)
  locked.page.email.value = member.email
  locked.page.password.value = 'abcd'
  await vue.nextTick()
  const first = locked.page.submitLogin()
  await respond(locked.requests[0], 429, { success: false, error_code: 'E0105', message: '登入失敗次數過多，請稍後再試' }, { 'Retry-After': '300' })
  await first
  assert.equal(locked.page.locked.value, true)
  assert.match(await renderArticleApp(locked), /登入暫時鎖定/)
  await locked.page.submitLogin()
  assert.equal(locked.requests.length, 1)

  const guest = articleApp('/vue/', null, '', false, auth.BACKENDS.fiber)
  t.after(guest.close)
  assert.equal(guest.requests[0].url, `${auth.BACKENDS.fiber}/api/articles?page=1&per_page=20`)
  assert.equal(guest.requests[0].headers.Authorization, undefined)
  await respond(guest.requests[0], 200, { success: true, message: 'OK', data: { list: [{ ...sample, created_backend: 'fiber' }], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  assert.match(await renderArticleApp(guest), /建立於 Fiber/)

  const stale = articleApp('/vue/articles/7', 'expired-jwt', '', false, auth.BACKENDS.fiber)
  t.after(stale.close)
  await respond(stale.requests[0], 401, { success: false, error_code: 'E0002', message: '登入已逾期，請重新登入' })
  assert.equal(stale.requests.length, 1)
  assert.equal(auth.readSession(stale.sessionStorage), null)
  assert.match(await renderArticleApp(stale), /重新登入/)
})

// 作者深連結先驗身分；沒有登入的導向與已登入的 API 讀取沿用共用路由。
test('Echo author routes verify identity before protected reads', async (t) => {
  const routes = new Map([
    ['/vue/articles/new', null],
    ['/vue/me/articles', '/api/me/articles?page=1&per_page=20'],
    ['/vue/me/comments', '/api/me/comments?page=1&per_page=20'],
    ['/vue/comments/9', '/api/comments/9'],
    ['/vue/articles/7/edit', '/api/articles/7'],
    ['/vue/comments/9/edit', '/api/comments/9'],
  ])
  for (const [path, endpoint] of routes) {
    const visitor = articleApp(path, null, '', false, auth.BACKENDS.echo)
    t.after(visitor.close)
    assert.equal(visitor.requests.length, 0, path)
    assert.deepEqual(visitor.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    const signedIn = articleApp(path, 'echo-jwt', '', false, auth.BACKENDS.echo)
    t.after(signedIn.close)
    assert.equal(signedIn.requests[0].url, `${auth.BACKENDS.echo}/api/me`, path)
    await respond(signedIn.requests[0], 200, { success: true, message: 'OK', data: member })
    assert.equal(signedIn.requests.length, endpoint ? 2 : 1, path)
    if (endpoint) assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.echo}${endpoint}`, path)
    assert.doesNotMatch(await renderArticleApp(signedIn), /此頁尚未提供/, path)
  }
})

// Echo 沿用完整註冊與鎖定登入，重載先驗 JWT 再讀文章並提供作者入口。
test('Echo auth and public reading expose author controls', async (t) => {
  const registration = articleApp('/vue/register', null, '', false, auth.BACKENDS.echo)
  t.after(registration.close)
  assert.match(await renderArticleApp(registration), /建立帳號後即可登入並發表文章/)
  registration.page.email.value = 'new@example.test'
  registration.page.password.value = 'abcd'
  registration.page.nickname.value = '新會員'
  const register = registration.page.submitRegistration()
  assert.equal(registration.requests[0].url, `${auth.BACKENDS.echo}/api/auth/register`)
  await respond(registration.requests[0], 201, { success: true, message: 'OK', data: {} })
  await register
  assert.deepEqual(registration.redirects, ['/vue/login?registered=1'])

  const locked = articleApp('/vue/login', null, '', false, auth.BACKENDS.echo)
  t.after(locked.close)
  assert.match(await renderArticleApp(locked), /登入後即可發表文章與查看自己的文章/)
  locked.page.email.value = member.email
  locked.page.password.value = 'abcd'
  await vue.nextTick()
  const first = locked.page.submitLogin()
  await respond(locked.requests[0], 429, { success: false, error_code: 'E0105', message: '登入失敗次數過多，請稍後再試' }, { 'Retry-After': '300' })
  await first
  assert.equal(locked.page.locked.value, true)
  assert.match(await renderArticleApp(locked), /登入暫時鎖定/)
  await locked.page.submitLogin()
  assert.equal(locked.requests.length, 1)

  const list = articleApp('/vue/', 'echo-jwt', '', false, auth.BACKENDS.echo)
  t.after(list.close)
  assert.equal(list.requests[0].url, `${auth.BACKENDS.echo}/api/me`)
  await respond(list.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(list.requests[1].url, `${auth.BACKENDS.echo}/api/articles?page=1&per_page=20`)
  await respond(list.requests[1], 200, { success: true, message: 'OK', data: { list: [sample], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  const listView = await renderArticleApp(list)
  assert.match(listView, /標題/)
  assert.match(listView, /href="\/vue\/articles\/new"/)
  assert.match(listView, /href="\/vue\/me\/articles"/)
  assert.match(listView, /href="\/vue\/me\/comments"/)
  assert.doesNotMatch(listView, /href="\/vue\/admin\//)
  const logout = list.page.submitLogout()
  assert.equal(list.requests[2].url, `${auth.BACKENDS.echo}/api/auth/logout`)
  await respond(list.requests[2], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(auth.readSession(list.sessionStorage), null)

  const detail = articleApp('/vue/articles/7', 'echo-jwt', '', false, auth.BACKENDS.echo)
  t.after(detail.close)
  await respond(detail.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(detail.requests[1].url, `${auth.BACKENDS.echo}/api/articles/7`)
  await respond(detail.requests[1], 200, { success: true, message: 'OK', data: { ...sample, author: { id: member.id, nickname: member.nickname }, can_restore: true } })
  assert.equal(detail.requests[2].url, `${auth.BACKENDS.echo}/api/articles/7/comments?page=1&per_page=20`)
  const ownComment = { id: 9, article_id: 7, body: '本人留言', author: { id: member.id, nickname: member.nickname }, version: 1, created_backend: 'echo', created_at: '2026-09-24 10:00:00', updated_at: '2026-09-24 10:00:00', visibility: 'visible', can_restore: false }
  await respond(detail.requests[2], 200, { success: true, message: 'OK', data: { list: [ownComment], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  const detailView = await renderArticleApp(detail)
  assert.match(detailView, /第一行/)
  assert.match(detailView, /本人留言/)
  assert.match(detailView, /href="\/vue\/comments\/9"/)
  assert.match(detailView, /發表留言|編輯文章|>下架</)
  assert.equal(detail.page.canComment.value, true)
  assert.equal(detail.page.canEdit.value, true)
  assert.equal(detail.page.canManageVisibility.value, true)

  // Echo 管理員從內容詳情可返回管理列表；角色仍由目前後端驗證。
  const adminDetail = articleApp('/vue/articles/7', 'echo-admin-jwt', '', false, auth.BACKENDS.echo)
  t.after(adminDetail.close)
  await respond(adminDetail.requests[0], 200, { success: true, message: 'OK', data: { ...member, role: 'admin' } })
  await respond(adminDetail.requests[1], 200, { success: true, message: 'OK', data: sample })
  await respond(adminDetail.requests[2], 200, { success: true, message: 'OK', data: { list: [ownComment], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  const adminView = await renderArticleApp(adminDetail)
  assert.match(adminView, /本人留言/)
  assert.match(adminView, /閱讀留言詳情/)
  assert.match(adminView, /href="\/vue\/admin\/articles"[^>]*>返回文章管理<\/a>/)
})

// Echo 在錯誤或已失效 JWT 時保留重試／重新登入提示。
test('Echo public read errors preserve recovery and reject stale sessions', async (t) => {
  const stale = articleApp('/vue/articles/7', 'stale-jwt', '', false, auth.BACKENDS.echo)
  t.after(stale.close)
  await respond(stale.requests[0], 401, { success: false, error_code: 'E0002', message: '登入已逾期，請重新登入' })
  assert.equal(auth.readSession(stale.sessionStorage), null)
  assert.equal(stale.requests.length, 1)
  assert.match(await renderArticleApp(stale), /重新登入/)

  const hidden = articleApp('/vue/articles/7', null, '', false, auth.BACKENDS.echo)
  t.after(hidden.close)
  await respond(hidden.requests[0], 404, { success: false, error_code: 'E0004', message: '找不到指定資料或 API' })
  assert.match(await renderArticleApp(hidden), /找不到指定資料或 API/)
  assert.equal(hidden.requests.length, 1)

  const failed = articleApp('/vue/', null, '', false, auth.BACKENDS.echo)
  t.after(failed.close)
  await respond(failed.requests[0], 500, { success: false, error_code: 'E9999', message: '系統暫時無法處理，請稍後再試' })
  assert.match(await renderArticleApp(failed), /重新讀取/)
})

// Gin 作者深連結先驗證帳號，合法登入目標才能返回原頁並讀取作者 API。
test('Gin author routes verify identity and preserve safe login return', async (t) => {
  const routes = new Map([
    ['/vue/articles/new', null],
    ['/vue/me/articles', '/api/me/articles?page=1&per_page=20'],
    ['/vue/me/comments', '/api/me/comments?page=1&per_page=20'],
    ['/vue/comments/9', '/api/comments/9'],
    ['/vue/articles/7/edit', '/api/articles/7'],
    ['/vue/comments/9/edit', '/api/comments/9'],
  ])
  for (const [path, endpoint] of routes) {
    const visitor = articleApp(path, null, '', false, auth.BACKENDS.gin)
    t.after(visitor.close)
    assert.equal(visitor.requests.length, 0, path)
    assert.deepEqual(visitor.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    const signedIn = articleApp(path, 'gin-jwt', '', false, auth.BACKENDS.gin)
    t.after(signedIn.close)
    assert.equal(signedIn.requests[0].url, `${auth.BACKENDS.gin}/api/me`, path)
    await respond(signedIn.requests[0], 200, { success: true, message: 'OK', data: member })
    assert.equal(signedIn.requests.length, endpoint ? 2 : 1, path)
    if (endpoint) assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.gin}${endpoint}`, path)
  }
  const login = articleApp('/vue/login', null, '?return_to=%2Fvue%2Fme%2Farticles', false, auth.BACKENDS.gin)
  t.after(login.close)
  login.page.email.value = member.email
  login.page.password.value = 'abcd'
  await vue.nextTick()
  const submit = login.page.submitLogin()
  await respond(login.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'gin-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await submit
  assert.deepEqual(login.redirects, ['/vue/me/articles'])
})

// FastAPI 作者深連結先驗權才讀取；管理入口由獨立管理測試驗證。
test('FastAPI author routes verify identity before protected reads', async (t) => {
  const authorRoutes = new Map([
    ['/vue/articles/new', null],
    ['/vue/me/articles', '/api/me/articles?page=1&per_page=20'],
    ['/vue/me/comments', '/api/me/comments?page=1&per_page=20'],
    ['/vue/comments/9', '/api/comments/9'],
    ['/vue/articles/7/edit', '/api/articles/7'],
    ['/vue/comments/9/edit', '/api/comments/9'],
  ])
  for (const [path, endpoint] of authorRoutes) {
    const visitor = articleApp(path, null, '', false, auth.BACKENDS.fastapi)
    t.after(visitor.close)
    assert.equal(visitor.requests.length, 0, path)
    assert.deepEqual(visitor.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    const signedIn = articleApp(path, 'fastapi-jwt', '', false, auth.BACKENDS.fastapi)
    t.after(signedIn.close)
    assert.equal(signedIn.requests[0].url, `${auth.BACKENDS.fastapi}/api/me`, path)
    assert.equal(signedIn.requests.length, 1, path)
    await respond(signedIn.requests[0], 200, { success: true, message: 'OK', data: member })
    assert.equal(signedIn.requests.length, endpoint ? 2 : 1, path)
    if (endpoint) {
      assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.fastapi}${endpoint}`, path)
      assert.equal(signedIn.requests[1].headers.Authorization, 'Bearer fastapi-jwt', path)
    }
  }
  const login = articleApp('/vue/login', null, '?return_to=%2Fvue%2Fme%2Farticles', false, auth.BACKENDS.fastapi)
  t.after(login.close)
  login.page.email.value = member.email
  login.page.password.value = 'abcd'
  await vue.nextTick()
  const submit = login.page.submitLogin()
  await respond(login.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'fastapi-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member } })
  await submit
  assert.deepEqual(login.redirects, ['/vue/me/articles'])
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  assert.match(descriptor.template.content, /<template v-if="account"><a href="\/vue\/articles\/new"/)
})

// 各作者後端發文使用 201 空 data 與現有導向，不猜新 ID；管理員也能在作者頁發文。
test('author backends publish member and admin articles to the first my articles page', async (t) => {
  for (const selectedBackend of [auth.BACKENDS.flask, auth.BACKENDS.fastapi, auth.BACKENDS.gin, auth.BACKENDS.echo, auth.BACKENDS.fiber]) for (const actor of [member, { ...member, role: 'admin' }]) {
    const app = articleApp('/vue/articles/new', 'author-jwt', '', false, selectedBackend)
    t.after(app.close)
    await respond(app.requests[0], 200, { success: true, message: 'OK', data: actor })
    app.page.articleTitle.value = '作者標題'
    app.page.articleBody.value = '原文\r\n第二行'
    const write = app.page.submitArticle()
    assert.equal(app.requests[1].url, `${selectedBackend}/api/articles`)
    assert.equal(app.requests[1].method, 'POST')
    assert.equal(app.requests[1].headers.Authorization, 'Bearer author-jwt')
    assert.deepEqual(JSON.parse(app.requests[1].body), { title: '作者標題', body: '原文\r\n第二行' })
    await respond(app.requests[1], 201, { success: true, message: 'OK', data: {} })
    await write
    assert.deepEqual(app.redirects, ['/vue/me/articles?published=1'])
  }
})

// Django 作者與管理頁都須先驗證身分，再以同一後端的共同 API 路徑讀取。
test('Django author and management routes verify identity before content requests', async (t) => {
  const authorRoutes = new Map([
    ['/vue/articles/new', null],
    ['/vue/articles/7/edit', '/api/articles/7'],
    ['/vue/me/articles', '/api/me/articles?page=1&per_page=20'],
    ['/vue/me/comments', '/api/me/comments?page=1&per_page=20'],
    ['/vue/comments/9', '/api/comments/9'],
    ['/vue/comments/9/edit', '/api/comments/9'],
  ])
  for (const [path, endpoint] of authorRoutes) {
    const guest = articleApp(path, null, '', false, auth.BACKENDS.django)
    t.after(guest.close)
    assert.equal(guest.requests.length, 0, path)
    assert.deepEqual(guest.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    const signedIn = articleApp(path, 'django-token', '', false, auth.BACKENDS.django)
    t.after(signedIn.close)
    assert.equal(signedIn.requests[0].url, `${auth.BACKENDS.django}/api/me`)
    await respond(signedIn.requests[0], 200, { success: true, message: 'OK', data: member })
    assert.equal(signedIn.requests.length, endpoint ? 2 : 1, path)
    if (endpoint) assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.django}${endpoint}`, path)
  }
  const managementRoutes = new Map([
    ['/vue/admin/members', '/api/members?page=1&per_page=20'],
    ['/vue/admin/members/9', '/api/members/9'],
    ['/vue/admin/articles', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'],
    ['/vue/admin/comments', '/api/comments?page=1&per_page=20'],
    ['/vue/admin/settings/login-lockout', '/api/settings/login-lockout'],
  ])
  for (const [path, endpoint] of managementRoutes) {
    const signedIn = articleApp(path, 'django-token', '', false, auth.BACKENDS.django)
    t.after(signedIn.close)
    await respond(signedIn.requests[0], 200, { success: true, message: 'OK', data: { ...member, role: 'admin' } })
    assert.equal(signedIn.requests.length, 2, path)
    assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.django}${endpoint}`, path)
  }
})

// Django 建立文章沿用 201 空 data 導向自己的列表，不猜新文章 ID。
test('Django article publish reaches my articles without inventing an ID', async (t) => {
  const app = articleApp('/vue/articles/new', 'django-token', '', false, auth.BACKENDS.django)
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleTitle.value = 'Django 標題'
  app.page.articleBody.value = '原文\r\n第二行'
  const write = app.page.submitArticle()
  assert.equal(app.requests[1].url, `${auth.BACKENDS.django}/api/articles`)
  assert.equal(app.requests[1].headers.Authorization, 'Bearer django-token')
  assert.deepEqual(JSON.parse(app.requests[1].body), { title: 'Django 標題', body: '原文\r\n第二行' })
  await respond(app.requests[1], 201, { success: true, message: 'OK', data: {} })
  await write
  assert.deepEqual(app.redirects, ['/vue/me/articles?published=1'])
})

// 已送出的舊後端寫入即使晚到成功回應，也不可觸發發文完成導頁或重送。
test('old write response after backend switch cannot publish on the new selection', async (t) => {
  const app = articleApp('/vue/articles/new', 'old-token', '', true)
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  app.page.articleTitle.value = '切換前標題'
  app.page.articleBody.value = '切換前內文'
  const write = app.page.submitArticle()
  assert.equal(app.requests[1].url, `${auth.API_BASE}/api/articles`)
  await app.page.chooseBackend({ target: { value: auth.BACKENDS.symfony } })
  await respond(app.requests[1], 201, { success: true, message: 'OK', data: {} })
  await write
  assert.equal(app.requests.length, 2)
  assert.deepEqual(app.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2Fnew'])
  assert.equal(auth.readBackend(app.sessionStorage), auth.BACKENDS.symfony)
  assert.equal(auth.readSession(app.sessionStorage), null)
})

// 未知直連路徑只能返回文章列表，不能把任意 path 當登入後導向目標。
test('backend switch from unknown route uses a safe local return target', async (t) => {
  const app = articleApp('/vue/unknown', null)
  t.after(app.close)
  await app.page.chooseBackend({ target: { value: auth.BACKENDS.symfony } })
  assert.deepEqual(app.redirects, ['/vue/login?return_to=%2Fvue%2F'])
})

// 公開列表無 JWT 可讀；離頁後延遲回應不能恢復舊資料。
test('public article request ignores response after page leave', async (t) => {
  const app = articleApp('/vue/', null)
  t.after(app.close)
  assert.equal(app.requests[0].url, `${auth.API_BASE}/api/articles?page=1&per_page=20`)
  assert.equal(app.requests[0].headers.Authorization, undefined)
  app.page.onPageHide()
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: { list: [sample], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } })
  assert.equal(app.page.articleList.value, null)
  assert.equal(app.page.contentReady.value, false)
})

// 公開 API 已帶 JWT 時，401 必須留在逾期狀態，不以訪客身分補讀。
test('stale JWT on public article list does not downgrade to guest request', async (t) => {
  const app = articleApp('/vue/')
  t.after(app.close)
  await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
  assert.equal(app.requests[1].headers.Authorization, 'Bearer old-token')
  await respond(app.requests[1], 401, { success: false, error_code: 'E0002' })
  await app.page.loadContent()
  assert.equal(app.requests.length, 2)
  assert.equal(app.page.articleList.value, null)
  assert.equal(auth.readSession(app.sessionStorage), null)
})

// 掛載時 /api/me 失效須保留錯誤碼；公開列表與詳情不可被手動重讀降為訪客請求。
test('initial account verification 401 blocks guest retry on public pages', async (t) => {
  for (const path of ['/vue/', '/vue/articles/7']) {
    const app = articleApp(path)
    t.after(app.close)
    assert.equal(app.requests[0].url, `${auth.API_BASE}/api/me`)
    await respond(app.requests[0], 401, { success: false, error_code: 'E0002' })
    assert.equal(app.page.summaryCode.value, 'E0002')
    assert.equal(auth.readSession(app.sessionStorage), null)
    await app.page.loadContent()
    assert.equal(app.requests.length, 1)
  }
})

// 公開頁登出回報 JWT 已失效時也要保留登入連結的原頁路徑，且不能以訪客身分補讀。
test('logout 401 blocks guest retry and keeps public login return target', async (t) => {
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const expression = descriptor.template.content.match(/:href="([^"]+)"[^>]*>前往登入<\/a>/)?.[1]
  assert.ok(expression, 'reading page needs a login return link')
  for (const path of ['/vue/', '/vue/articles/7']) {
    const app = articleApp(path)
    t.after(app.close)
    await respond(app.requests[0], 200, { success: true, message: 'OK', data: member })
    const logout = app.page.submitLogout()
    await respond(app.requests.at(-1), 401, { success: false, error_code: 'E0002' })
    await logout
    assert.equal(app.page.summaryCode.value, 'E0002')
    assert.equal(auth.readSession(app.sessionStorage), null)
    assert.equal(runInNewContext(expression, { path, encodeURIComponent }), `/vue/login?return_to=${encodeURIComponent(path)}`)
    await app.page.loadContent()
    assert.equal(app.requests.length, 3)
  }
})
