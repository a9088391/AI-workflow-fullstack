import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as contracts from './utils/contracts.js'
import * as admin from './utils/admin.js'
import { BACKEND_KEY, BACKENDS, SESSION_KEY, readSession } from './utils/contracts.js'
import { useSite } from './composables/useSite.js'

const account = { id: 1, email: 'reader@example.test', nickname: '林', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }
const administrator = { ...account, role: 'admin' }
const settings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 }

// 執行真實 Nuxt app 與表單 script；只替換瀏覽器、Nuxt 注入及延遲 HTTP 邊界。
function switchingPage(page, withSession = false, selectedBackend = BACKENDS.symfony, confirmResult = true, loginReturn = '/nuxt/articles/001') {
  const original = { useState: globalThis.useState, computed: globalThis.computed, fetch: globalThis.fetch, storage: Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage') }
  const states = new Map()
  const values = new Map()
  const requests = []
  const redirects = []
  const reloads = []
  const listeners = new Map()
  const callbacks = { mounted: [], unmount: [] }
  const storage = {
    // 還原同分頁 Web Storage 缺值的 null 語意。
    getItem(key) { return values.get(key) ?? null },
    // 保留切換前後的原憑證與到期時間供斷言。
    setItem(key, value) { values.set(key, value) },
    // 後端切換或失效時依正式流程撤銷憑證。
    removeItem(key) { values.delete(key) },
  }
  // 直接放入 Vue 留下的原始儲存資料，避免透過 Nuxt 寫入函式掩蓋跨前端不相容。
  storage.setItem(BACKEND_KEY, selectedBackend)
  if (withSession) storage.setItem(SESSION_KEY, JSON.stringify({ backend: selectedBackend, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' }))
  // 每個 Nuxt 狀態 key 只建立一次 ref，讓 app 與頁面共用離頁旗標。
  globalThis.useState = (key, initial) => {
    if (!states.has(key)) states.set(key, vue.ref(initial()))
    return states.get(key)
  }
  globalThis.computed = vue.computed
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage })
  // 延後 HTTP 回應至選單動作之後，以檢查來源請求是否已作廢。
  globalThis.fetch = (url, options) => {
    const response = Promise.withResolvers()
    requests.push({ url, options, ...response })
    return response.promise
  }
  const route = { query: page === 'login' ? { return_to: loginReturn } : {}, path: page === 'login' ? '/login' : page === 'register' ? '/register' : page === 'articles/new' ? '/nuxt/articles/new' : page === 'admin/settings/login-lockout' ? '/admin/settings/login-lockout' : '/' }
  const pathname = page === 'index' ? '/nuxt/' : `/nuxt/${page}`
  const confirmations = []
  const window = {
    location: { pathname,
      // 留在 VM 時仍記錄會發生的同分頁導向，不讓測試進入網路。
      assign(path) { redirects.push(path) },
      // 註冊成功的舊回應不應取代前端切換導向。
      replace(path) { redirects.push(path) },
      // 記錄 BFCache 回來後是否真的要求重建 Nuxt 文件。
      reload() { reloads.push(pathname) },
    },
    // 可控制草稿捨棄決定，確認取消時不應開始導覽或清除來源文字。
    confirm(message) { confirmations.push(message); return confirmResult },
    // 保留 pageshow 監聽，才能模擬同一份文件從 BFCache 回來。
    addEventListener(name, callback) { listeners.set(name, callback) },
    // 卸載後不應再收到舊文件事件。
    removeEventListener(name) { listeners.delete(name) },
  }
  const context = { ...vue, ...contracts, ...admin, AbortController, window, useSite, useRoute,
    // 收集掛載與清理，順序與 Nuxt app、頁面載入一致。
    onMounted(callback) { callbacks.mounted.push(callback) },
    // 同分頁離開前，測試仍可觀察已取消的舊請求。
    onBeforeUnmount(callback) { callbacks.unmount.push(callback) },
    // 標題只影響 document，非本次切換判斷。
    useHead() {},
  }
  // 各元件取得同一條路由與共用 useState，但在自己的 script 作用域執行。
  function useRoute() { return route }
  // 各 SFC 在獨立 VM 作用域執行，避免頂層變數相撞並保留共用注入。
  function run(file, exposed) {
    const source = parse(readFileSync(new URL(file, import.meta.url), 'utf8')).descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
    // 每個 SFC 各有自己的頂層 const，注入邊界共享但 VM 全域不可共用。
    return runInNewContext(`${source}\n;(${exposed})`, { ...context })
  }
  const scope = vue.effectScope()
  // app 掌握切換與共用身分，頁面在另一 SFC 作用域執行。
  const app = scope.run(() => run('./app.vue', '({ site, reactTarget, nextTarget, angularTarget, chooseFrontend, chooseBackend, submitLogout })'))
  // 有表單的頁面執行正式 submit/save；唯讀文章詳情只需 app 切換狀態。
  const form = page === 'index' || /^articles\/[0-9]+$/.test(page) ? null : scope.run(() => run(`./pages/${page}.vue`, page === 'login' ? '({ email, password, pending, submit })' : page === 'register' ? '({ email, password, nickname, created, pending, submit })' : page === 'admin/settings/login-lockout' ? '({ current, draft, pending, save, load, notice, unknown })' : '({ title, body, pending, submit })'))
  for (const callback of callbacks.mounted) callback()
  return { app, form, storage, storedValues: values, requests, redirects, reloads, confirmations,
    // 分別觸發一般顯示及 BFCache 恢復，檢查只對後者重新載入。
    pageShow(persisted) { listeners.get('pageshow')?.({ persisted }) },
    // 還原全域注入及 watchers，避免其他測試看到本案例的 JWT。
    close() {
      for (const callback of callbacks.unmount) callback()
      scope.stop()
      globalThis.useState = original.useState
      globalThis.computed = original.computed
      globalThis.fetch = original.fetch
      if (original.storage) Object.defineProperty(globalThis, 'sessionStorage', original.storage)
      else delete globalThis.sessionStorage
    },
  }
}

// 依真實 Fetch envelope 完成單一延遲回應，不觸發第二次 API 寫入。
function respond(request, status, body) {
  request.resolve(new Response(JSON.stringify(body), { status }))
}

// 先經正式 /me 與設定 GET 建立管理員畫面，切換測試才會有可編輯草稿。
async function readyAdminSettings(app) {
  respond(app.requests[0], 200, { success: true, message: 'OK', data: administrator })
  await setImmediate()
  assert.equal(app.requests[1].url, `${BACKENDS.symfony}/api/settings/login-lockout`)
  respond(app.requests[1], 200, { success: true, message: 'OK', data: settings })
  await setImmediate()
  assert.equal(app.form.current.value.window_minutes, 5)
}

// 切換前端已選定 Vue 後，舊 Nuxt 登入即使回 200 也不可寫入 JWT 或導回 Nuxt。
test('pending login cannot overwrite frontend switch', async (t) => {
  const app = switchingPage('login')
  t.after(app.close)
  app.form.email.value = 'reader@example.test'
  app.form.password.value = 'abcd'
  await vue.nextTick()
  const pending = app.form.submit()
  assert.equal(app.requests[0].url, `${BACKENDS.symfony}/api/auth/login`)
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.equal(app.requests[0].options.signal.aborted, true)
  assert.deepEqual(app.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])
  respond(app.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'late-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } })
  await pending
  assert.equal(app.storage.getItem(SESSION_KEY), null)
  assert.deepEqual(app.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2F001'])
})

// 舊註冊 POST 可能已在 Symfony 完成，切換後不得用舊 201 導回 Nuxt 或自動重送。
test('pending registration cannot redirect back after frontend switch', async (t) => {
  const app = switchingPage('register')
  t.after(app.close)
  app.form.email.value = 'new@example.test'
  app.form.password.value = 'abcd'
  app.form.nickname.value = '新會員'
  await vue.nextTick()
  const pending = app.form.submit()
  assert.equal(app.requests[0].url, `${BACKENDS.symfony}/api/auth/register`)
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.equal(app.requests[0].options.signal.aborted, true)
  respond(app.requests[0], 201, { success: true, message: 'OK', data: {} })
  await pending
  assert.equal(app.form.created.value, false)
  assert.deepEqual(app.redirects, ['/vue/register'])
  assert.equal(app.requests.length, 1)
})

// 原 JWT 與到期時間保留給 Vue 重新驗證；來源 /me 的晚到回應不可顯示舊身分。
test('frontend switch preserves session and ignores delayed me', async (t) => {
  const app = switchingPage('index', true)
  t.after(app.close)
  assert.equal(app.requests[0].url, `${BACKENDS.symfony}/api/me`)
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.equal(app.requests[0].options.signal.aborted, true)
  respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
  await setImmediate()
  assert.equal(app.app.site.account.value, null)
  assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  assert.deepEqual(app.redirects, ['/vue/'])
})

// Angular 選單只開放已完成頁；切換保留原 JWT／到期時間並作廢來源晚到回應。
test('Angular choice keeps supported route and session while invalidating delayed me', async (t) => {
  const template = parse(readFileSync(new URL('./app.vue', import.meta.url), 'utf8')).descriptor.template.content
  assert.match(template, /<option value="angular" :disabled="!angularTarget">Angular/)
  for (const [page, target, loginReturn] of [
    ['index', '/angular/'], ['register', '/angular/register'],
    ['login', '/angular/login?return_to=%2Fangular%2Farticles%2F001'],
    ['articles/001', '/angular/articles/001'],
  ]) {
    const app = switchingPage(page, true, BACKENDS.symfony, true, loginReturn)
    t.after(app.close)
    assert.equal(app.app.angularTarget.value, target)
    app.app.chooseFrontend({ target: { value: 'angular' } })
    assert.deepEqual(app.redirects, [target])
    assert.equal(app.requests[0].options.signal.aborted, true)
    respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
    await setImmediate()
    assert.equal(app.app.site.account.value, null)
    assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  }
})

// 未完成頁與非法登入返回目標不可切 Angular，手動觸發選單事件也不導覽。
test('Angular choice blocks future routes and unsafe login returns', (t) => {
  for (const [page, loginReturn] of [
    ['articles/new'], ['admin/settings/login-lockout'],
    ['login', '/nuxt/articles/new'], ['login', '/nuxt/articles/0'],
    ['login', '/nuxt/register'], ['login', 'https://evil.test'],
  ]) {
    const app = switchingPage(page, false, BACKENDS.symfony, true, loginReturn)
    t.after(app.close)
    assert.equal(app.app.angularTarget.value, null)
    const choice = { target: { value: 'angular' } }
    app.app.chooseFrontend(choice)
    assert.equal(choice.target.value, 'nuxt')
    assert.deepEqual(app.redirects, [])
  }
})

// Angular 切換取消時保留登入文字；確認後舊登入成功不能寫回 Nuxt 的共用憑證。
test('Angular choice preserves cancelled draft and ignores a late login', async (t) => {
  const cancelled = switchingPage('login', false, BACKENDS.symfony, false)
  t.after(cancelled.close)
  cancelled.form.email.value = 'reader@example.test'
  await vue.nextTick()
  const choice = { target: { value: 'angular' } }
  cancelled.app.chooseFrontend(choice)
  assert.equal(choice.target.value, 'nuxt')
  assert.equal(cancelled.form.email.value, 'reader@example.test')
  assert.deepEqual(cancelled.redirects, [])

  const switched = switchingPage('login')
  t.after(switched.close)
  switched.form.email.value = 'reader@example.test'
  switched.form.password.value = 'abcd'
  await vue.nextTick()
  const pending = switched.form.submit()
  switched.app.chooseFrontend({ target: { value: 'angular' } })
  assert.deepEqual(switched.redirects, ['/angular/login?return_to=%2Fangular%2Farticles%2F001'])
  assert.equal(switched.requests[0].options.signal.aborted, true)
  respond(switched.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'late-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } })
  await pending
  assert.equal(switched.storage.getItem(SESSION_KEY), null)
})

// React 切換只在已完成頁啟用，沿用原 Symfony JWT 與到期時間並作廢晚到 /me。
test('React choice keeps implemented route, shared session and ignores delayed me', async (t) => {
  for (const [page, target] of [['index', '/react/'], ['register', '/react/register'], ['articles/001', '/react/articles/001']]) {
    const app = switchingPage(page, true)
    t.after(app.close)
    assert.equal(app.app.reactTarget.value, target)
    app.app.chooseFrontend({ target: { value: 'react' } })
    assert.deepEqual(app.redirects, [target])
    assert.equal(app.requests[0].options.signal.aborted, true)
    respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
    await setImmediate()
    assert.equal(app.app.site.account.value, null)
    assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  }
})

// 登入返回目標保留原 ID；舊登入成功不能在切到 React 後寫回 Nuxt。
test('React login switch keeps safe return and invalidates late login', async (t) => {
  const app = switchingPage('login')
  t.after(app.close)
  app.form.email.value = 'reader@example.test'
  app.form.password.value = 'abcd'
  await vue.nextTick()
  const pending = app.form.submit()
  app.app.chooseFrontend({ target: { value: 'react' } })
  assert.deepEqual(app.redirects, ['/react/login?return_to=%2Freact%2Farticles%2F001'])
  assert.equal(app.requests[0].options.signal.aborted, true)
  respond(app.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'late-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } })
  await pending
  assert.equal(app.storage.getItem(SESSION_KEY), null)
})

// 取消保留登入文字；註冊已送出後切換只作廢來源結果，不重送或寫入憑證。
test('React choice cancels draft and ignores late registration', async (t) => {
  const cancelled = switchingPage('login', false, BACKENDS.symfony, false)
  t.after(cancelled.close)
  cancelled.form.email.value = 'reader@example.test'
  await vue.nextTick()
  const choice = { target: { value: 'react' } }
  cancelled.app.chooseFrontend(choice)
  assert.equal(choice.target.value, 'nuxt')
  assert.equal(cancelled.form.email.value, 'reader@example.test')
  assert.deepEqual(cancelled.redirects, [])
  const registering = switchingPage('register')
  t.after(registering.close)
  registering.form.email.value = 'new@example.test'
  registering.form.password.value = 'abcd'
  registering.form.nickname.value = '新會員'
  await vue.nextTick()
  const pending = registering.form.submit()
  registering.app.chooseFrontend({ target: { value: 'react' } })
  assert.deepEqual(registering.redirects, ['/react/register'])
  assert.equal(registering.requests[0].options.signal.aborted, true)
  respond(registering.requests[0], 201, { success: true, message: 'OK', data: {} })
  await pending
  assert.equal(registering.form.created.value, false)
  assert.deepEqual(registering.redirects, ['/react/register'])
})

// React 已有發文頁，選單可切換且沿用同分頁 JWT 與原到期時間。
test('React choice reaches Nuxt author page with the original shared session', (t) => {
  const app = switchingPage('articles/new', true)
  t.after(app.close)
  assert.equal(app.app.reactTarget.value, '/react/articles/new')
  const choice = { target: { value: 'react' } }
  app.app.chooseFrontend(choice)
  assert.equal(choice.target.value, 'react')
  assert.deepEqual(app.redirects, ['/react/articles/new'])
  assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
})

// 管理頁已可切到 React；保留目前後端與原登入期限，不重新簽發 JWT。
test('React choice reaches Nuxt admin page with the original shared session', (t) => {
  const app = switchingPage('admin/settings/login-lockout', true)
  t.after(app.close)
  assert.equal(app.app.reactTarget.value, '/react/admin/settings/login-lockout')
  const choice = { target: { value: 'react' } }
  app.app.chooseFrontend(choice)
  assert.equal(choice.target.value, 'react')
  assert.deepEqual(app.redirects, ['/react/admin/settings/login-lockout'])
  assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
})

// Next.js 的公開、作者與管理頁可直接切換；不改目前後端、JWT 或原登入期限。
test('Next.js choice reaches public, author, and admin pages with the original shared session', (t) => {
  // 選單以同一個經驗證的 target 決定 disabled，避免畫面可選卻被處理器拒絕。
  const template = parse(readFileSync(new URL('./app.vue', import.meta.url), 'utf8')).descriptor.template.content
  assert.match(template, /<option value="next" :disabled="!nextTarget">Next\.js/)
  for (const [page, target, loginReturn] of [
    ['index', '/next/'], ['register', '/next/register'], ['login', '/next/login?return_to=%2Fnext%2Farticles%2F001'],
    ['login', '/next/login?return_to=%2Fnext%2Farticles%2Fnew', '/nuxt/articles/new'],
    ['articles/001', '/next/articles/001'], ['articles/new', '/next/articles/new'],
    ['admin/settings/login-lockout', '/next/admin/settings/login-lockout'],
    ['login', '/next/login?return_to=%2Fnext%2Fadmin%2Fmembers%2F001', '/nuxt/admin/members/001'],
  ]) {
    const app = switchingPage(page, true, BACKENDS.symfony, true, loginReturn)
    t.after(app.close)
    assert.equal(app.app.nextTarget.value, target)
    app.app.chooseFrontend({ target: { value: 'next' } })
    assert.deepEqual(app.redirects, [target])
    assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  }
})

// 不安全登入目標仍停用 Next.js 選項，即使手動觸發事件也不得導覽。
test('Next.js choice blocks unsafe login returns', (t) => {
  for (const [page, loginReturn] of [['login', '/nuxt/admin/members/0'], ['login', '/nuxt/articles/0'], ['login', '/nuxt/register'], ['login', '/nuxt/login'], ['login', 'https://evil.test']]) {
    const app = switchingPage(page, false, BACKENDS.symfony, true, loginReturn)
    t.after(app.close)
    assert.equal(app.app.nextTarget.value, null)
    const choice = { target: { value: 'next' } }
    app.app.chooseFrontend(choice)
    assert.equal(choice.target.value, 'nuxt')
    assert.deepEqual(app.redirects, [])
  }
})

// 伺服器尚未確認撤銷 JWT 時，React 選項也必須等待登出結果。
test('React choice waits for pending logout', async (t) => {
  const app = switchingPage('index', true)
  t.after(app.close)
  respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
  await setImmediate()
  const logout = app.app.submitLogout()
  const choice = { target: { value: 'react' } }
  app.app.chooseFrontend(choice)
  assert.equal(choice.target.value, 'nuxt')
  assert.deepEqual(app.redirects, [])
  respond(app.requests[1], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(app.storage.getItem(SESSION_KEY), null)
})

// 作者表單有草稿時，拒絕捨棄必須保留原文字、選單與共用登入狀態。
test('cancelled frontend switch keeps new article draft on Nuxt', async (t) => {
  const app = switchingPage('articles/new', true, BACKENDS.symfony, false)
  t.after(app.close)
  respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
  await setImmediate()
  app.form.title.value = '未儲存標題'
  app.form.body.value = '未儲存內文'
  await vue.nextTick()
  assert.equal(app.app.site.dirty.value, true)
  const choice = { target: { value: 'vue' } }
  app.app.chooseFrontend(choice)
  assert.equal(app.confirmations.length, 1)
  assert.equal(choice.target.value, 'nuxt')
  assert.equal(app.form.body.value, '未儲存內文')
  assert.equal(app.app.site.dirty.value, true)
  assert.equal(app.app.site.leaving.value, false)
  assert.deepEqual(app.redirects, [])
  assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
})

// 明確捨棄後僅導向同類 Vue 頁，草稿不進 URL 或共用登入儲存。
test('confirmed frontend switch leaves new article without copying its draft', async (t) => {
  const app = switchingPage('articles/new', true)
  t.after(app.close)
  respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
  await setImmediate()
  app.form.title.value = '私有標題'
  app.form.body.value = '私有內文'
  await vue.nextTick()
  assert.equal(app.app.site.dirty.value, true)
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.equal(app.confirmations.length, 1)
  assert.equal(app.app.site.leaving.value, true)
  assert.equal(app.app.site.dirty.value, false)
  assert.deepEqual(app.redirects, ['/vue/articles/new'])
  assert.deepEqual([...app.storedValues.keys()].sort(), [BACKEND_KEY, SESSION_KEY].sort())
  assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(app.requests.length, 1)
})

// 管理 PUT 尚在途也能確認捨棄切換；舊成功回應不可觸發讀回或回寫新頁。
for (const target of ['frontend', 'backend']) {
  test(`confirmed ${target} switch aborts pending admin write and ignores late success`, async (t) => {
    const app = switchingPage('admin/settings/login-lockout', true)
    t.after(app.close)
    await readyAdminSettings(app)
    app.form.draft.value.window_minutes = 6
    await vue.nextTick()
    const writing = app.form.save()
    assert.equal(app.requests[2].options.method, 'PUT')
    assert.equal(app.app.site.adminPending.value, true)
    const selection = { target: { value: target === 'frontend' ? 'vue' : BACKENDS.laravel } }
    if (target === 'frontend') app.app.chooseFrontend(selection)
    else app.app.chooseBackend(selection)
    assert.equal(app.confirmations.length, 1)
    assert.match(app.confirmations[0], /已送出.*仍可能/)
    assert.equal(app.requests[2].options.signal.aborted, true)
    assert.deepEqual(app.redirects, [target === 'frontend' ? '/vue/admin/settings/login-lockout' : '/nuxt/login?return_to=%2Fnuxt%2Fadmin%2Fsettings%2Flogin-lockout'])
    respond(app.requests[2], 200, { success: true, message: 'OK', data: {} })
    await writing
    assert.equal(app.requests.length, 3)
    assert.equal(app.form.notice.value, '')
    assert.equal(app.form.unknown.value, false)
    if (target === 'frontend') assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
    else assert.equal(app.storage.getItem(SESSION_KEY), null)
  })

  // 取消時保留原後端、憑證與在途請求，選單回到原值，讓結果正常落在原頁。
  test(`cancelled ${target} switch keeps pending admin write`, async (t) => {
    const app = switchingPage('admin/settings/login-lockout', true, BACKENDS.symfony, false)
    t.after(app.close)
    await readyAdminSettings(app)
    app.form.draft.value.window_minutes = 6
    await vue.nextTick()
    const writing = app.form.save()
    const selection = { target: { value: target === 'frontend' ? 'vue' : BACKENDS.laravel } }
    if (target === 'frontend') app.app.chooseFrontend(selection)
    else app.app.chooseBackend(selection)
    assert.equal(app.confirmations.length, 1)
    assert.equal(selection.target.value, target === 'frontend' ? 'nuxt' : BACKENDS.symfony)
    assert.equal(app.requests[2].options.signal.aborted, false)
    assert.equal(app.app.site.backend.value, BACKENDS.symfony)
    assert.equal(app.app.site.leaving.value, false)
    assert.deepEqual(app.redirects, [])
    respond(app.requests[2], 422, { success: false, error_code: 'E0001', errors: { window_minutes: ['欄位格式或範圍不正確'] } })
    await writing
    assert.equal(app.form.draft.value.window_minutes, 6)
    assert.equal(app.app.site.adminPending.value, false)
  })
}

// PUT 已成功但讀回仍在途時，切換後端也要忽略舊 GET 與舊 save 的後續提示。
test('backend switch invalidates pending admin readback continuation', async (t) => {
  const app = switchingPage('admin/settings/login-lockout', true)
  t.after(app.close)
  await readyAdminSettings(app)
  app.form.draft.value.window_minutes = 6
  await vue.nextTick()
  const writing = app.form.save()
  respond(app.requests[2], 200, { success: true, message: 'OK', data: {} })
  await setImmediate()
  assert.equal(app.requests[3].url, `${BACKENDS.symfony}/api/settings/login-lockout`)
  assert.equal(app.app.site.adminPending.value, true)
  app.app.chooseBackend({ target: { value: BACKENDS.laravel } })
  assert.equal(app.requests[3].options.signal.aborted, true)
  respond(app.requests[3], 200, { success: true, message: 'OK', data: { ...settings, window_minutes: 6 } })
  await writing
  assert.equal(app.form.current.value, null)
  assert.equal(app.form.notice.value, '')
  assert.equal(app.app.site.backend.value, BACKENDS.laravel)
  assert.equal(app.storage.getItem(SESSION_KEY), null)
})

// 草稿重讀失敗後即使畫面暫無目前值，切換語意路由仍須要求捨棄確認。
test('failed settings reread still confirms frontend route switch', async (t) => {
  const app = switchingPage('admin/settings/login-lockout', true)
  t.after(app.close)
  await readyAdminSettings(app)
  app.form.draft.value.window_minutes = 6
  await vue.nextTick()
  const reread = app.form.load()
  await vue.nextTick()
  assert.equal(app.form.current.value, null)
  assert.equal(app.app.site.dirty.value, true)
  respond(app.requests[2], 500, { success: false, error_code: 'E9999', log_id: 'trace-1' })
  await reread
  assert.equal(app.app.site.dirty.value, true)
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.equal(app.confirmations.length, 1)
  assert.deepEqual(app.redirects, ['/vue/admin/settings/login-lockout'])
})

// Vue 選用 ASP.NET Core 後切到 Nuxt 必須沿用原 JWT，切回 Vue 也不能延長到期時間。
test('ASP.NET Core Vue session survives Nuxt verification and reverse frontend switch', async (t) => {
  const backend = 'http://localhost:8083'
  const app = switchingPage('index', true, backend)
  t.after(app.close)
  assert.equal(app.app.site.backend.value, backend)
  assert.equal(app.requests[0].url, `${backend}/api/me`)
  assert.equal(app.requests[0].options.headers.Authorization, 'Bearer same-token')
  respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
  await setImmediate()
  assert.equal(app.app.site.account.value?.id, account.id)
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.deepEqual(app.redirects, ['/vue/'])
  assert.deepEqual(JSON.parse(app.storage.getItem(SESSION_KEY)), { backend, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(app.storage.getItem(BACKEND_KEY), backend)
})

// Vue 選用 Django 後，Nuxt 只重驗既有憑證；切回 Vue 仍保留原到期時間。
test('Django Vue session survives Nuxt me verification and reverse switch', async (t) => {
  const app = switchingPage('index', true, BACKENDS.django)
  t.after(app.close)
  assert.equal(app.app.site.backend.value, BACKENDS.django)
  assert.equal(app.requests[0].url, `${BACKENDS.django}/api/me`)
  assert.equal(app.requests[0].options.headers.Authorization, 'Bearer same-token')
  respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
  await setImmediate()
  assert.equal(app.app.site.account.value?.id, account.id)
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.deepEqual(app.redirects, ['/vue/'])
  assert.deepEqual(JSON.parse(app.storage.getItem(SESSION_KEY)), { backend: BACKENDS.django, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
  assert.equal(app.storage.getItem(BACKEND_KEY), BACKENDS.django)
})

// Nuxt 表單沿用固定 /api 路徑；Django 登入只保存原 JWT 與到期時間。
test('Nuxt registration and login use the selected Django API', async (t) => {
  const registration = switchingPage('register', false, BACKENDS.django)
  t.after(registration.close)
  registration.form.email.value = 'new@example.test'
  registration.form.password.value = 'abcd'
  registration.form.nickname.value = '新會員'
  await vue.nextTick()
  const post = registration.form.submit()
  assert.equal(registration.requests[0].url, `${BACKENDS.django}/api/auth/register`)
  respond(registration.requests[0], 201, { success: true, message: 'OK', data: {} })
  await post
  assert.equal(registration.form.created.value, true)

  const login = switchingPage('login', false, BACKENDS.django)
  t.after(login.close)
  login.form.email.value = 'reader@example.test'
  login.form.password.value = 'abcd'
  await vue.nextTick()
  const signIn = login.form.submit()
  assert.equal(login.requests[0].url, `${BACKENDS.django}/api/auth/login`)
  respond(login.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'django-jwt', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } })
  await signIn
  assert.deepEqual(readSession(login.storage), { backend: BACKENDS.django, accessToken: 'django-jwt', expiresAt: '2026-09-24 18:00:00' })
})

// 登入與註冊的真實 submit 都須擋住 K 地址，修正為 ASCII 後才允許送到 Django。
for (const page of ['login', 'register']) {
  test(`Nuxt ${page} rejects Kelvin sign before Django request`,
    // 同一表單先驗證失敗，再改成合法地址，確認沒有永久卡住送出狀態。
    async (t) => {
      const app = switchingPage(page, false, BACKENDS.django)
      t.after(app.close)
      app.form.email.value = 'K@example.test'
      app.form.password.value = 'Abc1234'
      if (page === 'register') app.form.nickname.value = '讀者'
      await vue.nextTick()
      await app.form.submit()
      assert.equal(app.requests.length, 0)
      assert.equal(app.form.pending.value, false)
      assert.equal(app.storage.getItem(SESSION_KEY), null)

      app.form.email.value = 'K@example.test'
      await vue.nextTick()
      const retry = app.form.submit()
      assert.equal(app.requests.length, 1)
      assert.equal(app.requests[0].url, `${BACKENDS.django}/api/auth/${page}`)
      respond(app.requests[0], 422, { success: false, error_code: 'E0001', message: '欄位資料不正確，請確認後再送出', errors: { email: ['此 Email 已註冊'] } })
      await retry
    })
}

// Django 的登入晚到成功不可覆寫已改選的後端，也不可保留舊憑證。
test('Django pending login cannot overwrite a later backend selection', async (t) => {
  const app = switchingPage('login', false, BACKENDS.django)
  t.after(app.close)
  app.form.email.value = 'reader@example.test'
  app.form.password.value = 'abcd'
  await vue.nextTick()
  const pending = app.form.submit()
  assert.equal(app.requests[0].url, `${BACKENDS.django}/api/auth/login`)
  app.app.chooseBackend({ target: { value: BACKENDS.laravel } })
  assert.equal(app.requests[0].options.signal.aborted, true)
  respond(app.requests[0], 200, { success: true, message: 'OK', data: { access_token: 'late-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } })
  await pending
  assert.equal(app.app.site.backend.value, BACKENDS.laravel)
  assert.equal(app.storage.getItem(SESSION_KEY), null)
  assert.deepEqual(app.redirects, ['/nuxt/login?return_to=%2Fnuxt%2Farticles%2F001'])
})

// 後端改選仍走原本的清除憑證流程；舊登入與註冊成功不能把狀態寫回新後端。
for (const page of ['login', 'register']) {
  test(`pending ${page} cannot overwrite backend switch`,
    // 同一案例重播兩種表單，確認改選後端後各自的舊回應都不能回寫。
    async (t) => {
      const app = switchingPage(page)
      t.after(app.close)
      app.form.email.value = 'reader@example.test'
      app.form.password.value = 'abcd'
      if (page === 'register') app.form.nickname.value = '新會員'
      await vue.nextTick()
      const pending = app.form.submit()
      assert.equal(app.requests.length, 1)
      app.app.chooseBackend({ target: { value: BACKENDS.laravel } })
      assert.equal(app.requests[0].options.signal.aborted, true)
      const result = page === 'login'
        ? { success: true, message: 'OK', data: { access_token: 'late-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } }
        : { success: true, message: 'OK', data: {} }
      respond(app.requests[0], page === 'login' ? 200 : 201, result)
      await pending
      assert.equal(app.app.site.backend.value, BACKENDS.laravel)
      assert.equal(app.storage.getItem(SESSION_KEY), null)
      if (page === 'register') assert.equal(app.form.created.value, false)
      assert.equal(app.redirects.length, 1)
      assert.match(app.redirects[0], /^\/nuxt\/login\?return_to=/)
    })
}

// BFCache 返回會恢復已設 leaving 的同一份文件；重新載入才能從共用 session 重驗。
test('BFCache restoration reloads once while ordinary pageshow does not', (t) => {
  const app = switchingPage('index', true)
  t.after(app.close)
  app.pageShow(false)
  assert.deepEqual(app.reloads, [])
  app.app.chooseFrontend({ target: { value: 'vue' } })
  assert.equal(app.app.site.leaving.value, true)
  app.pageShow(false)
  assert.deepEqual(app.reloads, [])
  app.pageShow(true)
  assert.deepEqual(app.reloads, ['/nuxt/'])
  app.pageShow(false)
  assert.deepEqual(app.reloads, ['/nuxt/'])
  assert.deepEqual(readSession(app.storage), { backend: BACKENDS.symfony, accessToken: 'same-token', expiresAt: '2026-09-24 18:00:00' })
})

// 登出請求已送達時留在原頁等待結果，避免切到 Vue 後同一 JWT 又被伺服器撤銷。
test('frontend choice waits for pending logout outcome', async (t) => {
  const app = switchingPage('index', true)
  t.after(app.close)
  respond(app.requests[0], 200, { success: true, message: 'OK', data: account })
  // fetch/json 與 initialize 的 promise 先完成，才會有可送登出的 verified token。
  await setImmediate()
  assert.equal(app.app.site.account.value?.id, 1)
  const logout = app.app.submitLogout()
  assert.equal(app.requests[1].url, `${BACKENDS.symfony}/api/auth/logout`)
  const selection = { target: { value: 'vue' } }
  app.app.chooseFrontend(selection)
  assert.equal(selection.target.value, 'nuxt')
  assert.deepEqual(app.redirects, [])
  respond(app.requests[1], 200, { success: true, message: 'OK', data: {} })
  await logout
  assert.equal(app.storage.getItem(SESSION_KEY), null)
})
