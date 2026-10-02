import assert from 'node:assert/strict'
import { test } from 'node:test'
import { apiRequest, readList } from '../utils/contracts.js'
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
