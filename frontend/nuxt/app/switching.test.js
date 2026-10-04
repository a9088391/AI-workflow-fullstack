import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as contracts from './utils/contracts.js'
import { BACKEND_KEY, BACKENDS, SESSION_KEY, readSession } from './utils/contracts.js'
import { useSite } from './composables/useSite.js'

const account = { id: 1, email: 'reader@example.test', nickname: '林', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }

// 執行真實 Nuxt app 與表單 script；只替換瀏覽器、Nuxt 注入及延遲 HTTP 邊界。
function switchingPage(page, withSession = false, selectedBackend = BACKENDS.symfony) {
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
  const route = { query: page === 'login' ? { return_to: '/nuxt/articles/001' } : {}, path: page === 'login' ? '/login' : page === 'register' ? '/register' : '/' }
  const pathname = page === 'index' ? '/nuxt/' : `/nuxt/${page}`
  const window = {
    location: { pathname,
      // 留在 VM 時仍記錄會發生的同分頁導向，不讓測試進入網路。
      assign(path) { redirects.push(path) },
      // 註冊成功的舊回應不應取代前端切換導向。
      replace(path) { redirects.push(path) },
      // 記錄 BFCache 回來後是否真的要求重建 Nuxt 文件。
      reload() { reloads.push(pathname) },
    },
    // 草稿情境確認捨棄；登入請求可能已送出但不得回寫。
    confirm() { return true },
    // 保留 pageshow 監聽，才能模擬同一份文件從 BFCache 回來。
    addEventListener(name, callback) { listeners.set(name, callback) },
    // 卸載後不應再收到舊文件事件。
    removeEventListener(name) { listeners.delete(name) },
  }
  const context = { ...vue, ...contracts, AbortController, window, useSite, useRoute,
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
  const app = scope.run(() => run('./app.vue', '({ site, chooseFrontend, chooseBackend, submitLogout })'))
  // 表單直接執行真正的 submit，保留 AbortController 與 watcher 時序。
  const form = page === 'index' ? null : scope.run(() => run(`./pages/${page}.vue`, page === 'login' ? '({ email, password, pending, submit })' : '({ email, password, nickname, created, pending, submit })'))
  for (const callback of callbacks.mounted) callback()
  return { app, form, storage, requests, redirects, reloads,
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
