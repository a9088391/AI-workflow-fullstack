import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { setImmediate } from 'node:timers/promises'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
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
  const page = scope.run(() => runInNewContext(`${script}\n;({ articleTitle, articleBody, articleList, articleData, account, credential, backend, hasDraft, reLogin, disabledAccount, contentPending, contentReady, writeUnknown, summary, summaryCode, published, listPage, submitArticle, submitRegistration, submitLogin, submitLogout, verifyAccount, loadContent, onPageHide, chooseFrontend, chooseBackend, email, password, nickname })`, context))
  lifecycle.mount()
  return { page, requests, redirects, confirmations, sessionStorage,
    // 停止正式生命週期與 Vue watcher，避免測試殘留計時器。
    close() { lifecycle.unmount(); scope.stop() },
  }
}

// 等待回應及 Vue 微任務，觀察穩定的使用者可見狀態。
async function respond(request, status, body) {
  request.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
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
