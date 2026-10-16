import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// 直接執行現有 hook 函式，避免為本票新增瀏覽器測試相依套件。
function writeHarness() {
  const source = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8')
  const start = source.indexOf('function useWrite(')
  const end = source.indexOf('\n// 下架／恢復', start)
  assert.ok(start >= 0 && end > start, '應找到實際 useWrite 定義')
  const cells = []
  const calls = []
  const switchEpoch = { current: 0 }
  let cursor = 0
  let ownedCredential = null
  let draft = { dirty: false, pending: false }

  // 每次渲染按 Hook 順序保存 state；更新後需再次 render 才取得新畫面狀態。
  function useState(initial) {
    const slot = cursor++
    if (!(slot in cells)) cells[slot] = initial
    return [cells[slot], (value) => { cells[slot] = typeof value === 'function' ? value(cells[slot]) : value }]
  }
  // ref 穿越渲染保存目前請求身分。
  function useRef(initial) {
    const slot = cursor++
    if (!(slot in cells)) cells[slot] = { current: initial }
    return cells[slot]
  }
  // 依賴改變時模擬 effect 清理與執行，重現憑證失效後的收尾。
  function useEffect(callback, deps) {
    const slot = cursor++
    const previous = cells[slot]
    if (previous && deps.every((value, index) => Object.is(value, previous.deps[index]))) return
    previous?.cleanup?.()
    cells[slot] = { deps, cleanup: callback() }
  }
  // callback 依賴未變時保留同一身分，避免測試替身製造額外 effect。
  function useCallback(callback, deps) {
    const slot = cursor++
    const previous = cells[slot]
    if (previous && deps.every((value, index) => Object.is(value, previous.deps[index]))) return previous.callback
    cells[slot] = { deps, callback }
    return callback
  }
  // 寫入延後結束，讓測試精確控制 401 與新登入的先後順序。
  function request() {
    let resolve
    let reject
    const promise = new Promise((yes, no) => { resolve = yes; reject = no })
    calls.push({ resolve, reject })
    return promise
  }
  // 同一份記憶體憑證代表目前 session；過期時舊 POST 即失去擁有權。
  function ownsSession(_storage, _backend, credential) { return credential === ownedCredential }
  // 後端明確拒絕時保留欄位訊息，與實際失敗 envelope 的消費方式相同。
  function failure(body) { return { code: body?.error_code, message: body?.message, errors: body?.errors } }
  // 模擬 App 的全頁草稿標記，檢查舊回應不會提前解除新寫入的離頁保護。
  function onDraftChange(dirty, pending) { draft = { dirty, pending } }
  const useWrite = new Function('useState', 'useRef', 'useEffect', 'useCallback', 'request', 'ownsSession', 'failure', 'sessionStorage', `${source.slice(start, end)}\nreturn useWrite`)(useState, useRef, useEffect, useCallback, request, ownsSession, failure, {})
  // 模擬元件在原頁以新憑證重新渲染，保留同一組 hook state。
  function render(credential) {
    cursor = 0
    return useWrite('backend', credential, () => {}, switchEpoch, onDraftChange)
  }
  // GET 401 或重新登入只更動目前 session，不替 POST 推定結果。
  function setCredential(credential) { ownedCredential = credential }
  // 呼叫端送出前設為在途，查證後的新請求亦應重新保護離頁。
  function markPending() { draft = { dirty: true, pending: true } }
  return { calls, render, setCredential, markPending, draft: () => draft }
}

// 401 先撤銷 JWT；舊 POST 後來成功或失敗都不能留下 pending 或清新寫入。
for (const oldOutcome of ['success', 'failure']) test(`expired write settles unknown before late ${oldOutcome}`, async () => {
  const harness = writeHarness()
  const firstCredential = { accessToken: 'old' }
  harness.setCredential(firstCredential)
  let write = harness.render(firstCredential)
  harness.markPending()
  const first = write.send('/api/articles/7/comments', 'POST', { body: '草稿' }, 201)
  assert.equal(harness.render(firstCredential).pending, true)

  // 並行 GET 的 401 先清掉原 JWT，原頁表單仍掛載等待同帳號重登。
  harness.setCredential(null)
  write = harness.render(null)
  write = harness.render(null)
  assert.equal(write.pending, false)
  assert.equal(write.uncertain, true)
  assert.match(write.error.message, /查證/)
  assert.deepEqual(harness.draft(), { dirty: true, pending: false })

  // 新憑證重驗後仍須人工查證；未解鎖前不能送出第二筆。
  const secondCredential = { accessToken: 'same-account-new' }
  harness.setCredential(secondCredential)
  write = harness.render(secondCredential)
  assert.equal((await write.send('/api/articles/7/comments', 'POST', { body: '草稿' }, 201)).ok, false)
  assert.equal(harness.calls.length, 1)
  write.checked()
  write = harness.render(secondCredential)
  harness.markPending()
  const second = write.send('/api/articles/7/comments', 'POST', { body: '人工決定重送' }, 201)
  assert.equal(harness.calls.length, 2)

  // 舊回應較晚抵達時，不能結束較新請求的等待狀態。
  if (oldOutcome === 'success') harness.calls[0].resolve({ status: 201, body: { success: true, message: 'OK', data: {} } })
  else harness.calls[0].reject(new Error('連線中斷'))
  const oldResult = await first
  assert.equal(oldResult.ok, false)
  assert.equal(oldResult.obsolete, true)
  assert.equal(harness.render(secondCredential).pending, true)
  assert.deepEqual(harness.draft(), { dirty: true, pending: true })
  assert.equal((await harness.render(secondCredential).send('/api/articles/7/comments', 'POST', { body: '第三筆' }, 201)).ok, false)
  assert.equal(harness.calls.length, 2)
  harness.calls[1].resolve({ status: 201, body: { success: true, message: 'OK', data: {} } })
  assert.equal((await second).ok, true)
  assert.equal(harness.render(secondCredential).pending, false)
})

// 一般已知拒絕要回傳欄位錯誤，不能誤當未知結果永久鎖住表單。
test('known write failure still settles and allows correction', async () => {
  const harness = writeHarness()
  const credential = { accessToken: 'current' }
  harness.setCredential(credential)
  const write = harness.render(credential)
  const result = write.send('/api/articles/7/comments', 'POST', { body: '' }, 201)
  harness.calls[0].resolve({ status: 422, body: { success: false, error_code: 'E0001', message: '欄位資料不正確', errors: { body: ['必填'] } } })
  assert.equal((await result).ok, false)
  const settled = harness.render(credential)
  assert.equal(settled.pending, false)
  assert.equal(settled.uncertain, false)
  assert.deepEqual(settled.error.errors, { body: ['必填'] })
})

// POST 自己先收到明確 401 時，仍以後端拒絕處理，不能改判成已寫入結果不明。
test('known write 401 remains a rejection after session invalidation', async () => {
  const harness = writeHarness()
  const credential = { accessToken: 'expired' }
  harness.setCredential(credential)
  const write = harness.render(credential)
  const result = write.send('/api/articles/7/comments', 'POST', { body: '草稿' }, 201)
  harness.calls[0].resolve({ status: 401, body: { success: false, error_code: 'E0002', message: '登入已逾期' } })
  assert.equal((await result).problem.code, 'E0002')
  harness.setCredential(null)
  const settled = harness.render(null)
  assert.equal(settled.pending, false)
  assert.equal(settled.uncertain, false)
})
