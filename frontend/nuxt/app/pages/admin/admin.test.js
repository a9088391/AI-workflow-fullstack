import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as contracts from '../../utils/contracts.js'
import * as admin from '../../utils/admin.js'

const administrator = { id: 1, email: 'admin@example.test', nickname: '管理員', role: 'admin', status: 'active', created_at: '2026-09-24 09:00:00' }
const member = { id: 2, email: 'member@example.test', nickname: '阿明', role: 'member', status: 'active', created_at: '2026-09-24 09:00:00' }
const settings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 }

// 從真實頁面 script 執行 Vue watch，只將 HTTP 與 Nuxt 注入換成可控邊界。
function page(file, exposed, { kind = 'members', role = 'admin', query = {}, id = '002' } = {}) {
  const requests = []
  let cleanup
  const site = { ready: vue.ref(true), account: vue.ref(role === 'admin' ? administrator : role === 'member' ? member : null), token: vue.ref(role ? 'token' : null), backend: vue.ref(contracts.API_BASE), leaving: vue.ref(false), dirty: vue.ref(false), adminPending: vue.ref(false),
    // 模擬正式共用狀態在認證失效時撤銷帳號與憑證。
    forget(message) { this.account.value = null; this.token.value = null; this.authError = message },
  }
  // 查詢物件維持 Vue Router 的 reactive 行為，才能重現同頁查詢變更。
  const route = { path: `/admin/${kind}`, query: vue.reactive(query), params: { id } }
  const context = {
    ...vue, ...contracts, ...admin, AbortController, URLSearchParams,
    // 頁面標題由框架處理，本測試只看實際請求和狀態。
    useHead() {},
    // 固定頁面種類供清單元件讀取，不模擬 Nuxt 的其他編譯流程。
    defineProps() { return { kind } },
    // 共用同一條路由，重現 watcher 觸發順序。
    useRoute() { return route },
    // 共用同一份登入狀態，避免每個元件各自建立權限。
    useSite() { return site },
    // 保留頁面的清理程序，測試結束時撤銷未完成請求。
    onBeforeUnmount(callback) { cleanup = callback },
    // 各請求由案例明確完成，能檢查未知寫入與讀回順序。
    apiRequest(path, options) { const deferred = Promise.withResolvers(); requests.push({ path, ...options, ...deferred }); return deferred.promise },
    window: {
      // 測試案例採使用者確認後的狀態變更路徑。
      confirm() { return true },
      location: {
        // 測試不真的離開頁面，導覽影響由既有切換測試覆蓋。
        assign() {},
      },
    },
  }
  const script = parse(readFileSync(new URL(file, import.meta.url), 'utf8')).descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const scope = vue.effectScope()
  const state = scope.run(
    // 在獨立 reactive scope 執行正式頁面 script，結束時可移除所有 watch。
    () => runInNewContext(`${script}\n;(${exposed})`, context),
  )
  return { state, site, route, requests,
    // 各案例清理 watcher 與正在讀取的請求，避免跨例污染。
    close() { cleanup(); scope.stop() },
  }
}

// 依 API envelope 完成單一請求，等待 Vue 更新後再檢查。
async function respond(request, status, data, errorCode = '') {
  request.resolve({ status, body: errorCode ? { success: false, error_code: errorCode, log_id: 'trace-1' } : { success: true, message: 'OK', data } })
  await setImmediate()
}

// 未驗證管理員不呼叫私有 API；會員搜尋只使用字面 keyword 和原頁碼。
test('admin list gates private reads and uses approved member search', async (t) => {
  const instance = page('../../components/AdminList.vue', '({ data, load })', { role: 'member', query: { page: '3', keyword: '阿' } })
  t.after(instance.close)
  assert.equal(instance.requests.length, 0)
  instance.site.account.value = administrator
  await vue.nextTick()
  assert.equal(instance.requests[0].path, '/api/members?page=3&per_page=20&filters%5Bkeyword%5D=%E9%98%BF')
  await respond(instance.requests[0], 200, { list: [], pagination: { page: 3, per_page: 20, total: 1, total_pages: 1 } })
  assert.equal(instance.state.data.value.pagination.total, 1)
  instance.site.backend.value = contracts.BACKENDS.symfony
  await vue.nextTick()
  assert.equal(instance.state.data.value, null)
  assert.equal(instance.requests[1].path, instance.requests[0].path)
})

// 無效網址關鍵字改為合法值後，成功清單不得留下前次欄位錯誤。
test('admin list clears stale keyword validation on route query change', async (t) => {
  const invalidKeyword = '字'.repeat(255)
  const instance = page('../../components/AdminList.vue', '({ data, error, fieldError })', { query: { keyword: invalidKeyword } })
  t.after(instance.close)
  assert.equal(instance.requests.length, 0)
  assert.match(instance.state.fieldError.value, /最多 254 個字/)
  instance.route.query.keyword = '阿'
  await vue.nextTick()
  assert.equal(instance.requests[0].path, '/api/members?page=1&per_page=20&filters%5Bkeyword%5D=%E9%98%BF')
  await respond(instance.requests[0], 200, { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } })
  assert.equal(instance.state.data.value.pagination.total, 0)
  assert.equal(instance.state.error.value, '')
  assert.equal(instance.state.fieldError.value, '')
  instance.route.query.keyword = invalidKeyword
  await vue.nextTick()
  assert.equal(instance.requests.length, 1)
  assert.match(instance.state.fieldError.value, /最多 254 個字/)
})

// 直接開啟會員詳情或設定時，一般會員不得因已知網址取得管理 API 資料。
test('member and settings deep links do not read private data for nonadmins', (t) => {
  for (const [file, exposed] of [['./members/[id].vue', '({ member })'], ['./settings/login-lockout.vue', '({ current })']]) {
    const instance = page(file, exposed, { role: 'member' })
    t.after(instance.close)
    assert.equal(instance.requests.length, 0)
  }
})

// 會員狀態 5xx 不重送；人工重讀後仍保留未知提示，後續成功依伺服器值呈現。
test('member status unknown requires readback before another PUT', async (t) => {
  const instance = page('./members/[id].vue', '({ member, pending, unknown, needsReadback, changeStatus, load })')
  t.after(instance.close)
  await respond(instance.requests[0], 200, member)
  const first = instance.state.changeStatus()
  assert.equal(instance.requests[1].path, '/api/members/2/status')
  assert.deepEqual(JSON.parse(JSON.stringify(instance.requests[1].body)), { status: 'disabled' })
  assert.equal(instance.site.adminPending.value, true)
  await respond(instance.requests[1], 500, null, 'E9999')
  await first
  assert.equal(instance.state.unknown.value, true)
  assert.equal(instance.state.needsReadback.value, true)
  await instance.state.changeStatus()
  assert.equal(instance.requests.length, 2)
  const reread = instance.state.load()
  await respond(instance.requests[2], 200, { ...member, status: 'disabled' })
  await reread
  assert.equal(instance.state.needsReadback.value, false)
  assert.equal(instance.state.unknown.value, true)
  const second = instance.state.changeStatus()
  await respond(instance.requests[3], 200, {})
  await respond(instance.requests[4], 200, member)
  await second
  assert.equal(instance.state.member.value.status, 'active')
  assert.equal(instance.site.adminPending.value, false)
})

// 不同管理員重登不得繼承前一帳號的未知狀態；新帳號在途寫入晚到也不可回寫。
test('different administrator clears unknown member status and aborts old write', async (t) => {
  const instance = page('./members/[id].vue', '({ member, pending, unknown, needsReadback, notice, changeStatus })')
  t.after(instance.close)
  await respond(instance.requests[0], 200, member)
  const uncertain = instance.state.changeStatus()
  await respond(instance.requests[1], 500, null, 'E9999')
  await uncertain
  assert.equal(instance.state.unknown.value, true)
  instance.site.account.value = null
  instance.site.token.value = null
  await vue.nextTick()
  instance.site.account.value = { ...administrator, id: 3, email: 'admin3@example.test' }
  instance.site.token.value = 'second-token'
  await vue.nextTick()
  assert.equal(instance.state.unknown.value, false)
  assert.equal(instance.state.needsReadback.value, false)
  assert.equal(instance.site.dirty.value, false)
  assert.equal(instance.requests[2].token, 'second-token')
  await respond(instance.requests[2], 200, member)
  const pending = instance.state.changeStatus()
  assert.equal(instance.requests[3].method, 'PUT')
  instance.site.account.value = { ...administrator, id: 4, email: 'admin4@example.test' }
  instance.site.token.value = 'third-token'
  await vue.nextTick()
  assert.equal(instance.requests[3].signal.aborted, true)
  assert.equal(instance.state.pending.value, false)
  assert.equal(instance.site.adminPending.value, false)
  await respond(instance.requests[3], 200, {})
  await pending
  assert.equal(instance.state.unknown.value, false)
  assert.equal(instance.state.notice.value, '')
})

// PUT 成功後的舊讀回若遇換後端或換管理員，不得覆寫新來源的會員與提示。
for (const change of ['backend', 'account']) {
  // 共用操作順序但分開建立狀態，確認兩種來源變動都有新的有效讀回。
  test(`member status readback ignores late ${change} response`, async (t) => {
    const instance = page('./members/[id].vue', '({ member, notice, changeStatus })')
    t.after(instance.close)
    await respond(instance.requests[0], 200, member)
    const updating = instance.state.changeStatus()
    await respond(instance.requests[1], 200, {})
    assert.equal(instance.requests[2].path, '/api/members/2')
    assert.equal(instance.state.notice.value, '更新已送達；正在重新讀取會員狀態。')
    if (change === 'backend') instance.site.backend.value = contracts.BACKENDS.symfony
    else { instance.site.account.value = { ...administrator, id: 3 }; instance.site.token.value = 'new-token' }
    await vue.nextTick()
    assert.equal(instance.requests[2].signal.aborted, true)
    assert.equal(instance.requests[3].path, '/api/members/2')
    await respond(instance.requests[3], 200, member)
    await respond(instance.requests[2], 200, { ...member, status: 'disabled' })
    await updating
    assert.equal(instance.state.member.value.status, 'active')
    assert.equal(instance.state.notice.value, '')
    assert.equal(instance.site.adminPending.value, false)
  })
}

// 設定先驗證四欄，再送完整 PUT；成功回應沒有值，必須等 GET 才更新畫面。
test('lockout settings validate draft and display confirmed readback', async (t) => {
  const instance = page('./settings/login-lockout.vue', '({ current, draft, errors, save })')
  t.after(instance.close)
  await respond(instance.requests[0], 200, settings)
  instance.state.draft.value.window_minutes = 0
  await instance.state.save()
  assert.equal(instance.requests.length, 1)
  assert.ok(instance.state.errors.value.window_minutes)
  instance.state.draft.value.window_minutes = 6
  await vue.nextTick()
  assert.equal(instance.site.dirty.value, true)
  const rejected = instance.state.save()
  assert.equal(instance.requests[1].path, '/api/settings/login-lockout')
  assert.deepEqual(JSON.parse(JSON.stringify(instance.requests[1].body)), { ...settings, window_minutes: 6 })
  instance.requests[1].resolve({ status: 422, body: { success: false, error_code: 'E0001', errors: { window_minutes: ['欄位格式或範圍不正確'] } } })
  await rejected
  assert.deepEqual([...instance.state.errors.value.window_minutes], ['欄位格式或範圍不正確'])
  assert.equal(instance.state.draft.value.window_minutes, 6)
  const save = instance.state.save()
  await respond(instance.requests[2], 200, {})
  assert.equal(instance.state.current.value, null)
  await respond(instance.requests[3], 200, { ...settings, window_minutes: 6 })
  await save
  assert.equal(instance.state.current.value.window_minutes, 6)
  assert.equal(instance.state.draft.value.window_minutes, 6)
  await vue.nextTick()
  assert.equal(instance.site.dirty.value, false)
})

// 422 後手動重讀成功須清除舊欄位錯誤，同時保留使用者尚未送出的草稿。
test('lockout settings clear stale field errors after readback', async (t) => {
  const instance = page('./settings/login-lockout.vue', '({ current, draft, errors, save, load })')
  t.after(instance.close)
  await respond(instance.requests[0], 200, settings)
  instance.state.draft.value.window_minutes = 6
  const rejected = instance.state.save()
  instance.requests[1].resolve({ status: 422, body: { success: false, error_code: 'E0001', errors: { window_minutes: ['欄位格式或範圍不正確'] } } })
  await rejected
  assert.ok(instance.state.errors.value.window_minutes)
  const readback = instance.state.load()
  await respond(instance.requests[2], 200, settings)
  await readback
  assert.equal(instance.state.current.value.window_minutes, 5)
  assert.equal(instance.state.draft.value.window_minutes, 6)
  assert.deepEqual(Object.keys(instance.state.errors.value), [])
})

// 重讀暫時移除可見值或讀取失敗時，未送出草稿仍須觸發離頁捨棄確認。
test('lockout draft remains dirty through pending and failed rereads', async (t) => {
  const instance = page('./settings/login-lockout.vue', '({ current, draft, load })')
  t.after(instance.close)
  await respond(instance.requests[0], 200, settings)
  instance.state.draft.value.window_minutes = 6
  await vue.nextTick()
  assert.equal(instance.site.dirty.value, true)
  const failed = instance.state.load()
  await vue.nextTick()
  assert.equal(instance.state.current.value, null)
  assert.equal(instance.site.dirty.value, true)
  await respond(instance.requests[1], 500, null, 'E9999')
  await failed
  assert.equal(instance.site.dirty.value, true)
  const recovered = instance.state.load()
  await respond(instance.requests[2], 200, settings)
  await recovered
  assert.equal(instance.state.draft.value.window_minutes, 6)
  assert.equal(instance.site.dirty.value, true)
})

// 原管理員重登可恢復草稿；換管理員或後端則清除舊草稿及比較基準。
test('lockout draft baseline belongs only to its admin and backend', async (t) => {
  const instance = page('./settings/login-lockout.vue', '({ current, confirmed, draft, load })')
  t.after(instance.close)
  await respond(instance.requests[0], 200, settings)
  instance.state.draft.value.window_minutes = 6
  await vue.nextTick()
  instance.site.account.value = null
  instance.site.token.value = null
  await vue.nextTick()
  assert.equal(instance.state.draft.value.window_minutes, 6)
  assert.equal(instance.state.confirmed.value.window_minutes, 5)
  assert.equal(instance.site.dirty.value, true)
  instance.site.account.value = administrator
  instance.site.token.value = 'restored-token'
  await vue.nextTick()
  await respond(instance.requests[1], 200, settings)
  assert.equal(instance.state.draft.value.window_minutes, 6)
  assert.equal(instance.site.dirty.value, true)
  instance.site.account.value = { ...administrator, id: 3 }
  instance.site.token.value = 'new-token'
  await vue.nextTick()
  assert.equal(instance.state.draft.value, null)
  assert.equal(instance.state.confirmed.value, null)
  assert.equal(instance.site.dirty.value, false)
  await respond(instance.requests[2], 200, settings)
  assert.equal(instance.state.draft.value.window_minutes, 5)
  instance.state.draft.value.window_minutes = 7
  await vue.nextTick()
  assert.equal(instance.site.dirty.value, true)
  instance.site.backend.value = contracts.BACKENDS.symfony
  await vue.nextTick()
  assert.equal(instance.state.draft.value, null)
  assert.equal(instance.state.confirmed.value, null)
  assert.equal(instance.site.dirty.value, false)
})

// 設定更新 5xx 後需手動重讀；草稿與未知提示仍在，不得直接重送。
test('lockout unknown write keeps draft and blocks automatic retry', async (t) => {
  const instance = page('./settings/login-lockout.vue', '({ current, draft, unknown, needsReadback, save, load })')
  t.after(instance.close)
  await respond(instance.requests[0], 200, settings)
  instance.state.draft.value.lock_seconds = 600
  const write = instance.state.save()
  await respond(instance.requests[1], 500, null, 'E9999')
  await write
  assert.equal(instance.state.unknown.value, true)
  assert.equal(instance.state.needsReadback.value, true)
  await instance.state.save()
  assert.equal(instance.requests.length, 2)
  const readback = instance.state.load()
  await respond(instance.requests[2], 200, settings)
  await readback
  assert.equal(instance.state.current.value.lock_seconds, 300)
  assert.equal(instance.state.draft.value.lock_seconds, 600)
  assert.equal(instance.state.unknown.value, true)
  assert.equal(instance.state.needsReadback.value, false)
})
