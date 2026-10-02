import assert from 'node:assert/strict'
import { test } from 'node:test'
import { API_BASE, BACKENDS, apiRequest, readBackend, readList, saveSession, selectBackend } from '../utils/contracts.js'
import { useSite } from './useSite.js'

// 即使瀏覽器拒絕取得 sessionStorage 屬性，公開頁仍須轉成訪客並可讀 API。
test('storage getter denial settles guest state before public reading', async (t) => {
  const originalUseState = globalThis.useState
  const originalComputed = globalThis.computed
  const originalFetch = globalThis.fetch
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const states = new Map()

  // 模擬同一 Nuxt 頁面共用的 useState 欄位。
  globalThis.useState = (key, initial) => {
    if (!states.has(key)) states.set(key, { value: initial() })
    return states.get(key)
  }
  // 保留 computed 對狀態的即時讀取，避免測試使用陳舊 token。
  globalThis.computed = (getter) => Object.defineProperty({}, 'value', { get: getter })
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    // 瀏覽器可在取得屬性時就拒絕，readSession 本身尚未進入 try。
    get() { throw new Error('storage denied') },
  })
  t.after(
    // 還原測試所用的全域物件，避免影響其他測試。
    () => {
      globalThis.useState = originalUseState
      globalThis.computed = originalComputed
      globalThis.fetch = originalFetch
      if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage)
      else delete globalThis.sessionStorage
    },
  )

  const site = useSite()
  await site.initialize()
  assert.equal(site.ready.value, true)
  assert.equal(site.account.value, null)
  assert.equal(site.credential.value, null)
  assert.match(site.authError.value, /訪客/)

  // ready 解鎖公開頁後，即使沒有憑證也要能直連文章清單。
  let requested = false
  // 攔截公開 GET，確認 storage 拒絕時仍以訪客送到預設後端。
  globalThis.fetch = async (url, options) => {
    requested = true
    assert.equal(url, 'http://localhost:8081/api/articles?page=1&per_page=20')
    assert.equal(options.headers.Authorization, undefined)
    return new Response(JSON.stringify({ success: true, message: 'OK', data: { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } } }), { status: 200 })
  }
  const response = await apiRequest('/api/articles?page=1&per_page=20', { token: site.token.value })
  assert.equal(requested, true)
  assert.equal(readList(response.status, response.body, 1)?.pagination.total, 0)
})

// Vue 留下的同分頁 Symfony JWT 要向 Symfony 重驗；切換後舊 /me 成功也不能恢復授權。
test('shared Symfony session verifies exact token and stale me cannot restore it', async (t) => {
  const originalUseState = globalThis.useState
  const originalComputed = globalThis.computed
  const originalFetch = globalThis.fetch
  const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const states = new Map()
  const items = new Map()
  const storage = {
    // 以同一份記憶體模擬兩版共用的單分頁 Storage。
    getItem(key) { return items.get(key) ?? null },
    // 保存本次選擇與原 JWT 到期時間。
    setItem(key, value) { items.set(key, value) },
    // 切換後端時移除舊 JWT。
    removeItem(key) { items.delete(key) },
  }
  selectBackend(storage, BACKENDS.symfony)
  saveSession(storage, 'vue-token', '2026-09-24 18:00:00')
  // 相同 key 對應相同 ref，模擬 app 和頁面共用 Nuxt useState。
  globalThis.useState = (key, initial) => {
    if (!states.has(key)) states.set(key, { value: initial() })
    return states.get(key)
  }
  // token getter 每次讀取目前認證狀態，避免測試使用舊身分。
  globalThis.computed = (getter) => Object.defineProperty({}, 'value', { get: getter })
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: storage })
  const requests = []
  // 延遲 /me 的回應，檢查切換後舊結果是否仍能寫回。
  globalThis.fetch = (url, options) => {
    const response = Promise.withResolvers()
    requests.push({ url, options, ...response })
    return response.promise
  }
  t.after(
    // 還原 Node 全域測試邊界，避免影響其他元件測試。
    () => {
      globalThis.useState = originalUseState
      globalThis.computed = originalComputed
      globalThis.fetch = originalFetch
      if (originalStorage) Object.defineProperty(globalThis, 'sessionStorage', originalStorage)
      else delete globalThis.sessionStorage
    },
  )
  const account = { id: 1, email: 'reader@example.test', nickname: '林', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }
  const site = useSite()
  const initial = site.initialize()
  assert.equal(requests[0].url, `${BACKENDS.symfony}/api/me`)
  assert.equal(requests[0].options.headers.Authorization, 'Bearer vue-token')
  requests[0].resolve(new Response(JSON.stringify({ success: true, message: 'OK', data: account }), { status: 200 }))
  await initial
  assert.equal(site.account.value.id, 1)
  assert.equal(site.credential.value.expiresAt, '2026-09-24 18:00:00')
  const delayed = site.verify()
  assert.equal(requests[1].url, `${BACKENDS.symfony}/api/me`)
  assert.equal(site.switchBackend(API_BASE), true)
  assert.equal(readBackend(storage), API_BASE)
  requests[1].resolve(new Response(JSON.stringify({ success: true, message: 'OK', data: account }), { status: 200 }))
  await delayed
  assert.equal(site.account.value, null)
  assert.equal(site.credential.value, null)
})
