import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as contracts from '../utils/contracts.js'

// 執行既有頁面 script 與真實 Vue watch，只替換 Nuxt 邊界及可控制的 HTTP 回應。
function guestPage(file, selectedBackend = contracts.API_BASE) {
  const requests = []
  const lifecycle = {}
  const site = { ready: vue.ref(true), token: vue.ref(null), backend: vue.ref(selectedBackend), leaving: vue.ref(false) }
  const route = { query: {}, params: { id: '001' } }
  const context = {
    ...vue, ...contracts, AbortController,
    // 本測試只需要 Nuxt 的頁面標題宣告，不建立瀏覽器 document。
    useHead() {},
    // 固定目前文章與查詢，讓切換後的唯一差異是後端選擇。
    useRoute() { return route },
    // 公開讀取兩次都沒有 JWT，驗證 backend 本身會觸發取消。
    useSite() { return site },
    // 保留頁面清理函式，測試後撤銷尚未完成的讀取。
    onBeforeUnmount(callback) { lifecycle.unmount = callback },
    // 即使取消仍能完成舊回應，檢查它無法寫入新頁面。
    apiRequest(path, options) {
      const response = Promise.withResolvers()
      requests.push({ path, ...options, ...response })
      return response.promise
    },
  }
  const source = readFileSync(new URL(file, import.meta.url), 'utf8')
  const script = parse(source).descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const visible = file === './index.vue' ? 'data' : 'article'
  const scope = vue.effectScope()
  const page = scope.run(() => runInNewContext(`${script}\n;({ visible: ${visible} })`, context))
  return { site, page, requests,
    // 依元件原本生命週期撤銷請求與 watcher。
    close() { lifecycle.unmount(); scope.stop() },
  }
}

// Django 公開列表與詳情仍使用共同文章、留言路徑，並接受其來源標籤資料。
test('Django guest pages read articles and comments through the common routes', async (t) => {
  const sample = { id: 1, title: '文章', body: '內容', author: { id: 2, nickname: '作者' }, version: 1, created_backend: 'django', created_at: '2026-09-24 10:00:00', updated_at: '2026-09-24 10:00:00', visibility: 'visible', can_restore: false }
  const list = guestPage('./index.vue', contracts.BACKENDS.django)
  t.after(list.close)
  assert.equal(list.requests[0].path, '/api/articles?page=1&per_page=20')
  list.requests[0].resolve({ status: 200, body: { success: true, message: 'OK', data: { list: [sample], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } } } })
  await setImmediate()
  assert.equal(list.page.visible.value?.list[0].created_backend, 'django')

  const detail = guestPage('./articles/[id].vue', contracts.BACKENDS.django)
  t.after(detail.close)
  assert.equal(detail.requests[0].path, '/api/articles/1')
  detail.requests[0].resolve({ status: 200, body: { success: true, message: 'OK', data: sample } })
  await setImmediate()
  assert.equal(detail.page.visible.value?.id, 1)
  assert.equal(detail.requests[1].path, '/api/articles/1/comments?page=1&per_page=20')
  detail.requests[1].resolve({ status: 200, body: { success: true, message: 'OK', data: { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } } } })
  await setImmediate()
})

// 訪客 JWT 一直是 null；改後端仍須取消舊 GET，晚到的舊內容不可顯示。
for (const file of ['./index.vue', './articles/[id].vue']) {
  test(`guest ${file} ignores old backend response`, async (t) => {
    const app = guestPage(file)
    t.after(app.close)
    assert.equal(app.requests.length, 1)
    assert.equal(app.requests[0].token, null)
    app.site.backend.value = contracts.BACKENDS.symfony
    await vue.nextTick()
    assert.equal(app.requests[0].signal.aborted, true)
    assert.equal(app.requests.length, 2)
    assert.equal(app.requests[1].token, null)
    app.requests[0].resolve({ status: 200, body: { success: true, message: 'OK', data: {} } })
    await setImmediate()
    assert.equal(app.page.visible.value, null)
  })
}

// guest token 始終為 null；切前端仍需同步撤銷來源列表與詳情 GET。
for (const file of ['./index.vue', './articles/[id].vue']) {
  test(`guest ${file} aborts read on frontend leave`, async (t) => {
    const app = guestPage(file)
    t.after(app.close)
    assert.equal(app.requests.length, 1)
    app.site.leaving.value = true
    assert.equal(app.requests[0].signal.aborted, true)
    app.requests[0].resolve({ status: 200, body: { success: true, message: 'OK', data: {} } })
    await setImmediate()
    assert.equal(app.page.visible.value, null)
    assert.equal(app.requests.length, 1)
  })
}
