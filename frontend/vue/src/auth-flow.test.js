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

// 執行實際 App script 與 Vue reactivity，只替換瀏覽器及 HTTP 邊界以控制回應順序。
function loginPage() {
  const requests = []
  const redirects = []
  const values = new Map()
  const lifecycle = {}
  const sessionStorage = {
    // 保留 Web Storage 的缺值語意，讓正式讀取函式處理保存憑證。
    getItem(key) { return values.get(key) ?? null },
    // 使用記憶體保存資料，避免測試接觸真實瀏覽器憑證。
    setItem(key, value) { values.set(key, value) },
    // 讓失效憑證走正式清除流程。
    removeItem(key) { values.delete(key) },
  }
  auth.saveSession(sessionStorage, 'retained-token', '2026-09-24 18:00:00')
  const context = {
    ...vue, ...auth, ...registration, ...articles, ...comments,
    AbortController, URLSearchParams, performance, console,
    setTimeout, clearTimeout, setInterval, clearInterval, sessionStorage,
    document: {},
    window: {
      sessionStorage,
      location: {
        pathname: '/vue/login', search: '',
        // 記錄導向目的地，避免測試真的離開目前執行環境。
        assign(path) { redirects.push(path) },
      },
      // 不建立 DOM；生命週期仍會執行正式註冊與清理程式。
      addEventListener() {},
      // 此測試沒有真實事件監聽，移除動作不需副作用。
      removeEventListener() {},
    },
    // 保存掛載 callback，稍後用它讀取憑證並啟動正式 /api/me 流程。
    onMounted(callback) { lifecycle.mount = callback },
    // 測試完成時執行正式離頁清理，避免 watcher／timer 洩漏。
    onBeforeUnmount(callback) { lifecycle.unmount = callback },
    // 刻意允許 abort 後回應，驗證 epoch 防線仍會拒絕延遲舊結果。
    fetch(url, options) {
      const response = Promise.withResolvers()
      requests.push({ url, ...options, ...response })
      return response.promise
    },
  }
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const script = descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const scope = vue.effectScope()
  // 僅在測試環境取出模板使用的事件與狀態，不修改正式元件的公開介面。
  const page = scope.run(() => runInNewContext(`${script}\n;({ email, password, pending, authChecking, logoutPending, credential, account, summary, submitLogin, verifyAccount, submitLogout })`, context))
  lifecycle.mount()
  return {
    page, requests, redirects, sessionStorage,
    // 先走正式清理，再停止 effect scope 中建立的 watcher。
    close() { lifecycle.unmount(); scope.stop() },
  }
}

// 回傳真實 Response 並等待 Vue／Promise 完成，使斷言觀察穩定的畫面狀態。
async function respond(request, status, body) {
  request.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
  await setImmediate()
}

// 重現保存憑證驗證失敗後的交錯操作，並追到表單恢復與下一次登入成功。
test('manual verification cannot strand login pending or replace a newer login', async (t) => {
  const app = loginPage()
  const { page, requests } = app
  t.after(app.close)
  const invalidCredential = { success: false, error_code: 'E0002' }
  const wrongPassword = { success: false, error_code: 'E0101' }
  const account = { id: 1, email: 'next@example.test', nickname: '林', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }

  // /me 暫時失敗須保留憑證，使用者此時可選手動驗證或輸入帳密。
  assert.equal(requests[0].url, `${auth.API_BASE}/api/me`)
  await respond(requests[0], 500, { success: false, error_code: 'E9999' })
  assert.equal(page.credential.value.accessToken, 'retained-token')
  assert.equal(page.authChecking.value, false)
  page.email.value = 'demo@example.test'
  page.password.value = 'abcd'
  await vue.nextTick()

  // 登入等待中再次操作手動驗證；舊版會送出 /me 並取消登入，造成 pending 永久卡住。
  const firstLogin = page.submitLogin()
  const loginRequest = requests.at(-1)
  const concurrentVerification = page.verifyAccount()
  if (requests.at(-1) !== loginRequest) await respond(requests.at(-1), 401, invalidCredential)
  await concurrentVerification
  await respond(loginRequest, 401, wrongPassword)
  await firstLogin
  assert.equal(page.pending.value, false, 'completed login must release the form after a concurrent verification attempt')
  assert.equal(loginRequest.signal.aborted, false)
  assert.equal(requests.length, 2)
  assert.match(page.summary.value, /帳號或密碼錯誤/)

  // 登入結束後才允許重驗；失效憑證清除後，表單必須仍可手動送出。
  const verification = page.verifyAccount()
  await respond(requests.at(-1), 401, invalidCredential)
  await verification
  assert.equal(page.credential.value, null)
  assert.equal(page.authChecking.value, false)
  assert.equal(page.pending.value, false)
  assert.equal(auth.readSession(app.sessionStorage), null)
  const retry = page.submitLogin()
  const staleRequest = requests.at(-1)
  assert.equal(staleRequest.url, `${auth.API_BASE}/api/auth/login`)
  assert.equal(requests.length, 4)
  assert.equal(page.pending.value, true)

  // 更改帳號會取消舊請求；下一次登入成功後，延遲的停用回應不得清掉新憑證。
  page.email.value = 'next@example.test'
  await vue.nextTick()
  assert.equal(staleRequest.signal.aborted, true)
  assert.equal(page.pending.value, false)
  const latestLogin = page.submitLogin()
  await respond(requests.at(-1), 200, { success: true, message: 'OK', data: { access_token: 'new-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } })
  await latestLogin
  await respond(staleRequest, 403, { success: false, error_code: 'E0102' })
  await retry
  assert.equal(page.pending.value, false)
  assert.equal(page.account.value.email, 'next@example.test')
  assert.equal(auth.readSession(app.sessionStorage).accessToken, 'new-token')
  assert.equal(page.summary.value, '')
  assert.deepEqual(app.redirects, ['/vue/'])

  // 登出共用同一認證請求；重驗也不得打斷撤銷，失敗後仍可手動重試登出。
  const logout = page.submitLogout()
  const logoutRequest = requests.at(-1)
  await page.verifyAccount()
  assert.equal(requests.at(-1), logoutRequest)
  await respond(logoutRequest, 500, { success: false, error_code: 'E9999' })
  await logout
  assert.equal(page.logoutPending.value, false)
  assert.equal(page.account.value.email, 'next@example.test')
  assert.equal(auth.readSession(app.sessionStorage).accessToken, 'new-token')
  assert.match(page.summary.value, /登出未完成/)
})
