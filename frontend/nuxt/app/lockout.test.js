import assert from 'node:assert/strict'
import { test } from 'node:test'
import { runInNewContext } from 'node:vm'
import * as vue from 'vue'
import * as contracts from './utils/contracts.js'
import { loadScriptSetup, renderSfcTemplate } from './support/component-fixtures.js'

const lockMessage = '登入失敗次數過多，請稍後再試'
const account = { id: 1, email: 'reader@example.test', nickname: '讀者', role: 'member', status: 'active', created_at: '2026-09-24 10:00:00' }
const login = { success: true, message: 'OK', data: { access_token: 'original-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account } }

// 執行兩個正式表單的 script 與 template；只控制 HTTP、時間及父層登入接收邊界。
function loginForm(kind) {
  const file = new URL(kind === 'login' ? './pages/login.vue' : './components/ReauthForm.vue', import.meta.url)
  const requests = []
  const accepted = []
  const events = []
  const redirects = []
  const timers = new Map()
  const lifecycle = []
  let now = 1000
  let nextTimer = 0
  let closed = false
  const site = { backend: vue.ref(contracts.BACKENDS.rails), leaving: vue.ref(false), checking: vue.ref(false), dirty: vue.ref(false),
    // 登入成功只交回原 token 與期限，這個表單測試不替代 useSite 的儲存驗證。
    acceptLogin(data) { accepted.push(data); return true },
    // 保留停用時的撤權入口，避免一般登入失敗被測試當成登入成功。
    forget(message) { events.push(['forgotten', message]) },
  }
  // 記錄恢復事件，確認原頁重登由父層接續而不是自行送出內容。
  function emit(event) { events.push(event) }
  const context = { ...vue, ...contracts, AbortController,
    // API 回應由每個案例決定，已取消的請求也能晚到以驗證正式 guard。
    apiRequest(path, options) { const response = Promise.withResolvers(); requests.push({ path, ...options, ...response }); return response.promise },
    // 時鐘由案例推進到到期前與相等邊界，不使用真實等待或改寫正式倒數公式。
    remainingSeconds(deadline) { return contracts.remainingSeconds(deadline, now) },
    Date: {
      // 正式 startCountdown 使用同一可控時鐘建立截止時間。
      now() { return now },
    },
    // 只保存正式 timer callback，讓到期與卸載清理可觀察。
    setInterval(callback) { const id = ++nextTimer; timers.set(id, callback); return id },
    // 正式到期與離頁都必須回收相同計時器。
    clearInterval(id) { timers.delete(id) },
    // 兩表單共用目前選取的 Rails 與頁面離開狀態。
    useSite() { return site },
    // 登入成功須回原安全文章網址，不能因鎖定或重試換目標。
    useRoute() { return { query: { return_to: '/nuxt/articles/7' } } },
    // 標題不影響本次鎖定摘要與表單控制驗收。
    useHead() {},
    // 原頁重登只恢復相同帳號，事件交給原父表單處理。
    defineProps() { return { ownerId: account.id } },
    // 不替換事件邏輯，只記錄元件真正送出的事件名稱。
    defineEmits() { return emit },
    // 保存正式清理 callback，模擬離頁或父層卸載恢復表單。
    onBeforeUnmount(callback) { lifecycle.push(callback) },
    window: { location: {
      // 導覽只記錄目的地，避免測試產生瀏覽器或外部效果。
      assign(path) { redirects.push(path) },
    } },
  }
  const exposed = kind === 'login'
    ? '{ email, password, errors, summary, logId, pending, secondsLeft, retryReady, countdown, summaryElement, emailElement, passwordElement, registered, returnTo, site, submit, focusFirstError, clearRetryReady }'
    : '{ email, password, errors, error, logId, pending, secondsLeft, retryReady, summaryElement, emailElement, passwordElement, site, submit, focusFirstError, formatCountdown }'
  const scope = vue.effectScope()
  // 真正的 Vue watchers 保留同步取消語意，不能由測試重作後端／離頁 guard。
  function evaluate() { return runInNewContext(`${loadScriptSetup(file)}\n;(${exposed})`, context) }
  const form = scope.run(evaluate)
  form.email.value = account.email
  form.password.value = 'Abcd1234'
  return { form, site, requests, accepted, events, redirects, timers,
    // 使用真實 template 斷言摘要、倒數及 disabled；不把這個 SSR 測試當成瀏覽器 Enter。
    render() { return renderSfcTemplate(file, form) },
    // 時間只改一次再呼叫現存 timer，涵蓋背景分頁節流下的正式到期計算。
    advance(milliseconds) { now += milliseconds; for (const callback of [...timers.values()]) callback() },
    // 元件卸載後需取消 HTTP 與 timer，並停止案例自己的 watchers。
    close() { if (closed) return; closed = true; for (const callback of lifecycle) callback(); scope.stop() },
  }
}

for (const kind of ['login', 'reauth']) {
  // 共同錯誤摘要不能被倒數文案取代；到期只恢復人工重試，成功才交付登入結果。
  test(`${kind} lockout preserves the common message and expires into a manual successful retry`, async (t) => {
    const app = loginForm(kind)
    t.after(app.close)
    assert.doesNotMatch(await app.render(), /可再次嘗試登入/)
    const attempt = app.form.submit()
    assert.equal(app.requests[0].path, '/api/auth/login')
    app.requests[0].resolve({ status: 429, body: { success: false, error_code: 'E0105', log_id: 'lock-trace' }, retryAfter: '2' })
    await attempt
    const locked = await app.render()
    assert.match(locked, new RegExp(`${lockMessage}.*log_id：lock-trace`))
    assert.match(locked, /登入暫時鎖定，約剩 (?:<strong>)?00:02/)
    assert.match(locked, new RegExp(`id="${kind}-email"[^>]*disabled`))
    assert.match(locked, new RegExp(`id="${kind}-password"[^>]*disabled`))
    assert.match(locked, /<button[^>]*disabled[^>]*>/)
    assert.doesNotMatch(locked, /可再次嘗試登入/)
    // 表單按鈕與 Enter 使用同一 submit handler，鎖定時不能再建立請求。
    await app.form.submit()
    assert.equal(app.requests.length, 1)
    app.advance(1999)
    assert.equal(app.form.secondsLeft.value, 1)
    assert.doesNotMatch(await app.render(), /可再次嘗試登入/)
    app.advance(1)
    assert.equal(app.form.secondsLeft.value, 0)
    assert.equal(app.timers.size, 0)
    assert.equal(app.requests.length, 1)
    assert.equal(app.accepted.length, 0)
    const ready = await app.render()
    assert.match(ready, /role="status">可再次嘗試登入。/)
    assert.doesNotMatch(ready, /登入暫時鎖定|登入失敗次數過多|lock-trace|<(?:input|button)\b[^>]*\bdisabled/)
    // 到期後仍由使用者明確提交；新成功不能沿用上一筆錯誤或延長伺服器期限。
    const retry = app.form.submit()
    assert.doesNotMatch(await app.render(), /可再次嘗試登入/)
    app.requests[1].resolve({ status: 200, body: login })
    await retry
    assert.equal(app.accepted.length, 1)
    assert.equal(app.accepted[0].access_token, 'original-token')
    assert.equal(app.accepted[0].expires_at, '2026-09-24 18:00:00')
    assert.equal(app.form.pending.value, false)
    assert.deepEqual(app.redirects, kind === 'login' ? ['/nuxt/articles/7'] : [])
    assert.deepEqual(app.events, kind === 'reauth' ? ['restored'] : [])
  })

  // 沒有有效 Retry-After 只能保留共同摘要與人工重試，不能捏造秒數或停用控制。
  test(`${kind} lockout without valid retry seconds keeps the common message without a timer`, async (t) => {
    for (const retryAfter of [undefined, '0', '01', 'tomorrow']) {
      const app = loginForm(kind)
      t.after(app.close)
      const attempt = app.form.submit()
      app.requests[0].resolve({ status: 429, body: { success: false, error_code: 'E0105', log_id: 'missing-seconds-trace' }, retryAfter })
      await attempt
      const html = await app.render()
      assert.match(html, new RegExp(lockMessage))
      assert.match(html, /無法取得有效倒數秒數/)
      assert.match(html, /log_id：missing-seconds-trace/)
      assert.doesNotMatch(html, /登入暫時鎖定|可再次嘗試登入|<(?:input|button)\b[^>]*\bdisabled/)
      assert.equal(app.form.secondsLeft.value, 0)
      assert.equal(app.timers.size, 0)
      assert.equal(app.requests.length, 1)
    }
  })

  // 離頁或父層因帳號切換卸載時，已啟動的倒數也須回收而非只取消在途登入。
  test(`${kind} unmount clears an active lockout timer`, async (t) => {
    const app = loginForm(kind)
    t.after(app.close)
    const attempt = app.form.submit()
    app.requests[0].resolve({ status: 429, body: { success: false, error_code: 'E0105' }, retryAfter: '2' })
    await attempt
    assert.equal(app.timers.size, 1)
    app.close()
    assert.equal(app.timers.size, 0)
    app.advance(2000)
    assert.equal(app.form.secondsLeft.value, 2)
    assert.doesNotMatch(await app.render(), /可再次嘗試登入/)
    assert.equal(app.requests.length, 1)
  })

  // 相鄰的密碼錯誤仍使用共同 E0101 摘要，不能被鎖定處理誤造 timer。
  test(`${kind} non-lockout login failure retains its own summary and trace`, async (t) => {
    const app = loginForm(kind)
    t.after(app.close)
    const attempt = app.form.submit()
    app.requests[0].resolve({ status: 401, body: { success: false, error_code: 'E0101', log_id: 'password-trace' } })
    await attempt
    const html = await app.render()
    assert.match(html, /帳號或密碼錯誤.*log_id：password-trace/)
    assert.doesNotMatch(html, /登入暫時鎖定|可再次嘗試登入/)
    assert.equal(app.timers.size, 0)
    assert.equal(app.form.pending.value, false)
    assert.equal(app.form.email.value, account.email)
    assert.equal(app.form.password.value, 'Abcd1234')
  })

  // 換後端、離開前端或父層卸載後，晚到的鎖定與成功回應都不能污染新表單。
  test(`${kind} ignores late login responses after backend change, leaving or unmount`, async (t) => {
    for (const boundary of ['backend', 'leaving', 'unmount']) {
      for (const status of [429, 200]) {
        const app = loginForm(kind)
        t.after(app.close)
        const attempt = app.form.submit()
        if (boundary === 'backend') app.site.backend.value = contracts.BACKENDS.laravel
        else if (boundary === 'leaving') app.site.leaving.value = true
        else app.close()
        assert.equal(app.requests[0].signal.aborted, true)
        app.requests[0].resolve({ status, body: status === 200 ? login : { success: false, error_code: 'E0105', log_id: 'stale-trace' }, retryAfter: '2' })
        await attempt
        const html = await app.render()
        assert.doesNotMatch(html, /登入失敗次數過多|登入暫時鎖定|可再次嘗試登入|stale-trace/)
        assert.equal(app.timers.size, 0)
        assert.equal(app.accepted.length, 0)
        assert.deepEqual(app.events, [])
        assert.deepEqual(app.redirects, [])
      }
    }
  })
}

// 到期提示只屬原帳密嘗試；兩個可編輯欄位任一變更都須清除，不建立登入請求。
test('reauth editing either credential clears the expired-lockout readiness message', async (t) => {
  for (const field of ['email', 'password']) {
    const app = loginForm('reauth')
    t.after(app.close)
    // 先從正式 429 流程進入到期狀態，避免直接指定旗標而漏測提示來源。
    const attempt = app.form.submit()
    app.requests[0].resolve({ status: 429, body: { success: false, error_code: 'E0105' }, retryAfter: '2' })
    await attempt
    app.advance(2000)
    assert.match(await app.render(), /role="status">可再次嘗試登入。/)
    // 透過真正的 reactive 欄位變更觸發 watcher，檢查畫面與禁止自動請求的終態。
    app.form[field].value = field === 'email' ? 'other@example.test' : 'Next1234'
    await vue.nextTick()
    assert.doesNotMatch(await app.render(), /可再次嘗試登入/)
    assert.equal(app.requests.length, 1)
    assert.equal(app.timers.size, 0)
  }
})

// 人工重試先清前次提示，新的鎖定只依新標頭倒數；缺漏秒數也不能沿用已到期提示。
test('reauth manual retry replaces readiness with a fresh countdown or missing-seconds guidance', async (t) => {
  const app = loginForm('reauth')
  t.after(app.close)
  // 首次鎖定到期只顯示可重試，不能自行再次登入。
  const first = app.form.submit()
  app.requests[0].resolve({ status: 429, body: { success: false, error_code: 'E0105' }, retryAfter: '2' })
  await first
  app.advance(2000)
  assert.match(await app.render(), /可再次嘗試登入/)
  // 第二次人工提交立刻清掉舊提示；後端仍鎖定時使用新的剩餘秒數。
  const retry = app.form.submit()
  assert.doesNotMatch(await app.render(), /可再次嘗試登入/)
  assert.equal(app.requests.length, 2)
  app.requests[1].resolve({ status: 429, body: { success: false, error_code: 'E0105', log_id: 'relocked-trace' }, retryAfter: '3' })
  await retry
  const relocked = await app.render()
  assert.match(relocked, /登入失敗次數過多.*log_id：relocked-trace/)
  assert.match(relocked, /登入暫時鎖定，約剩 00:03/)
  assert.match(relocked, /id="reauth-email"[^>]*disabled/)
  assert.match(relocked, /id="reauth-password"[^>]*disabled/)
  assert.match(relocked, /<button[^>]*disabled[^>]*>/)
  assert.doesNotMatch(relocked, /可再次嘗試登入/)
  // 新倒數再次到期仍不送出請求，接續人工重試的缺漏標頭不能保留舊狀態。
  app.advance(3000)
  assert.match(await app.render(), /可再次嘗試登入/)
  assert.equal(app.requests.length, 2)
  const missingSeconds = app.form.submit()
  app.requests[2].resolve({ status: 429, body: { success: false, error_code: 'E0105', log_id: 'missing-retry-trace' } })
  await missingSeconds
  const manual = await app.render()
  assert.match(manual, /無法取得有效倒數秒數.*log_id：missing-retry-trace/)
  assert.doesNotMatch(manual, /可再次嘗試登入|登入暫時鎖定|<(?:input|button)\b[^>]*\bdisabled/)
  assert.equal(app.timers.size, 0)
  assert.equal(app.accepted.length, 0)
})

// 後端、前端或元件生命週期改變會終止舊提示；未到期與已到期都不能跨邊界殘留。
test('reauth clears readiness across backend change, leaving and unmount boundaries', async (t) => {
  for (const expired of [false, true]) {
    for (const boundary of ['backend', 'leaving', 'unmount']) {
      const app = loginForm('reauth')
      t.after(app.close)
      // 同一 429 流程分別停在到期前與到期後，覆蓋兩種生命週期退出。
      const attempt = app.form.submit()
      app.requests[0].resolve({ status: 429, body: { success: false, error_code: 'E0105' }, retryAfter: '2' })
      await attempt
      if (expired) {
        app.advance(2000)
        assert.match(await app.render(), /可再次嘗試登入/)
      }
      // 操作正式切換 watcher 或卸載 callback，再推進時鐘確認舊 timer 不會帶回提示。
      if (boundary === 'backend') app.site.backend.value = contracts.BACKENDS.laravel
      else if (boundary === 'leaving') app.site.leaving.value = true
      else app.close()
      app.advance(2000)
      assert.doesNotMatch(await app.render(), /可再次嘗試登入/)
      assert.equal(app.timers.size, 0)
      assert.equal(app.requests.length, 1)
      assert.equal(app.accepted.length, 0)
    }
  }
})
