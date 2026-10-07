import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { compile } from '@vue/compiler-dom'
import { renderToString } from '@vue/server-renderer'
import * as vue from 'vue'
import * as auth from './auth.js'
import * as registration from './registration.js'
import * as articles from './articles.js'
import * as comments from './comments.js'
import * as admin from './admin.js'

const member = { id: 2, email: 'reader@example.test', nickname: '讀者', role: 'member', status: 'active', created_at: '2026-09-24 09:00:00' }
const comment = { id: 9, body: '<script>原文字</script>\r\n😀', article_id: 7, author: { id: 2, nickname: '讀者' }, version: 1, created_backend: 'gin', created_at: '2026-09-24 10:00:00', updated_at: '2026-09-24 10:00:00', visibility: 'hidden', can_restore: true }
const article = { ...comment, id: 7, title: '文章', body: '公開正文', visibility: 'visible' }

// 建立核准 envelope，讓案例只變動本次要驗證的資源與分頁。
function success(data) { return { success: true, message: 'OK', data } }

// 分頁使用原請求 page 與真實 total，不因合法超頁改成零筆。
function list(items, page = 1, total = items.length) { return success({ list: items, pagination: { page, per_page: 20, total, total_pages: Math.ceil(total / 20) } }) }

// 檢查留言完整資源、父文章識別與 Unicode 原文，避免混入其他文章或公開作者的私有欄位。
test('comment routes and response parsing preserve page, parent and public author contracts', () => {
  assert.equal(articles.articlePage('/vue/me/comments'), 'my-comments')
  assert.equal(articles.articlePage('/vue/comments/009'), 'comment')
  for (const path of ['/vue/comments/0', '/vue/comments/9007199254740992', '//evil.test', 'https://evil.test']) assert.equal(articles.localPage(path), false)
  assert.equal(comments.readComment(200, success(comment), 9)?.body, comment.body)
  assert.equal(comments.readComment(200, success(comment), 8), null)
  assert.equal(comments.readComment(200, success({ ...comment, author: { ...comment.author, email: member.email } }), 9), null)
  assert.equal(comments.readComment(200, success({ ...comment, body: '😀'.repeat(2001) }), 9), null)
  assert.equal(comments.readComment(200, success({ ...comment, body: '\u3000' }), 9), null)
  assert.equal(comments.readCommentList(200, list([comment]), 1, 8), null)
  assert.equal(comments.readCommentList(200, list([comment]), 1, 7)?.list[0].visibility, 'hidden')
  assert.equal(comments.readCommentList(200, list([], 9, 41), 9)?.pagination.total, 41)
  assert.equal(comments.readCommentList(200, list([], 9, 41), 8), null)
})

// 執行實際 Vue 狀態與 App script；只替換 HTTP、Storage 與導覽，控制權限和回應先後。
function app(path, token = 'saved-token', search = '', selectedBackend = auth.API_BASE) {
  const requests = []
  const redirects = []
  const confirmations = []
  let approve = false
  const history = []
  const storage = new Map()
  const lifecycle = {}
  const sessionStorage = {
    // 保留缺值語意，讓正式認證函式讀取儲存資料。
    getItem(key) { return storage.get(key) ?? null },
    // 保存登入狀態供失效和暫時錯誤案例使用。
    setItem(key, value) { storage.set(key, value) },
    // 驗證正式失效流程確實刪除憑證。
    removeItem(key) { storage.delete(key) },
  }
  // 每例先選後端再保存憑證，確保 Django 作者請求沿同一登入來源送出。
  if (selectedBackend !== auth.API_BASE) auth.selectBackend(sessionStorage, selectedBackend)
  if (token) auth.saveSession(sessionStorage, token, '2026-09-24 18:00:00')
  const context = {
    ...vue, ...auth, ...registration, ...articles, ...comments, ...admin,
    AbortController, URLSearchParams, performance, console, setTimeout, clearTimeout, setInterval, clearInterval, sessionStorage, document: {},
    window: {
      // 記錄使用者確認；預設取消，避免測試意外捨棄既有草稿。
      confirm(message) { confirmations.push(message); return approve },
      history: {
        // 記錄同頁分頁更新，確認成功未轉向猜測的新 ID。
        replaceState(state, title, url) { history.push(url) },
      },
      sessionStorage, location: { pathname: path, search,
        // 記錄登入返回路徑，不真的離開測試程序。
        assign(value) { redirects.push(value) },
      },
      // 此 harness 無 DOM，事件註冊不需要額外副作用。
      addEventListener() {},
      // 正式清理仍執行，但測試沒有實體事件監聽器。
      removeEventListener() {},
    },
    // 保存正式掛載流程，於狀態建立後啟動認證或公開讀取。
    onMounted(callback) { lifecycle.mount = callback },
    // 保留離頁清理，測試結束時撤銷計時器與請求。
    onBeforeUnmount(callback) { lifecycle.unmount = callback },
    // 刻意允許已 abort 回應完成，確保 epoch 而非瀏覽器取消獨自維護正確性。
    fetch(url, options) { const response = Promise.withResolvers(); requests.push({ url, ...options, ...response }); return response.promise },
  }
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const script = descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const scope = vue.effectScope()
  // 使用 compiler 的正式 binding 清單取出整份狀態，讓模板測試執行真實條件與控制。
  const bindings = Object.keys(compileScript(descriptor, { id: 'comment-test' }).bindings)
  // 在原 effect scope 執行實際 script，沒有另寫一份狀態或顯示判斷。
  const page = scope.run(() => runInNewContext(`${script}\n;({ ${bindings.join(', ')} })`, context))
  lifecycle.mount()
  return { page, requests, redirects, history, sessionStorage, confirmations,
    // 測試明確改變確認選擇，仍透過正式事件入口送出。
    approve(value) { approve = value },
    // 元件清理後再停止 Vue effect，避免 timer 和 watcher 留在下一個案例。
    close() { lifecycle.unmount(); scope.stop() },
  }
}

// 以真實 JSON Response 完成一次可控請求，再等候 Vue 和 Promise 狀態穩定。
async function respond(request, status, body) {
  request.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
  await setImmediate()
}

// 作者留言先驗證登入；父文章 404 和 5xx 均不得撤回自己留言，且可獨立手動重試。
test('own comment survives unreadable parent and independent parent retry', async (t) => {
  const instance = app('/vue/comments/9')
  t.after(instance.close)
  assert.equal(instance.requests.length, 1)
  assert.equal(instance.page.commentData.value, null)
  await respond(instance.requests[0], 200, success(member))
  assert.equal(instance.requests[1].url, `${auth.API_BASE}/api/comments/9`)
  await respond(instance.requests[1], 200, success(comment))
  assert.equal(instance.requests[2].url, `${auth.API_BASE}/api/articles/7`)
  await respond(instance.requests[2], 404, { success: false, error_code: 'E0004', data: { title: '不得顯示', body: '不可洩漏' } })
  assert.equal(instance.page.commentData.value.body, comment.body)
  assert.equal(instance.page.parentArticle.value, null)
  assert.equal(instance.page.relatedError.value, '所屬文章目前無法讀取')
  const retry = instance.page.loadRelated()
  await respond(instance.requests[3], 500, { success: false, error_code: 'E9999' })
  await retry
  assert.equal(instance.page.commentData.value.body, comment.body)
  assert.equal(instance.page.credential.value.accessToken, 'saved-token')
  const recovery = instance.page.loadRelated()
  await respond(instance.requests[4], 200, success(article))
  await recovery
  assert.equal(instance.page.parentArticle.value.title, '文章')
  assert.equal(instance.requests.filter(
    // 獨立父文章重試不應重新讀取或移除留言。
    (request) => request.url.endsWith('/api/comments/9'),
  ).length, 1)
})

// 公開文章留言依 server 頁碼與排序呈現，訪客不攜帶 JWT，也不過濾 server 授權的資源。
test('public comments keep server page and order without guest credentials', async (t) => {
  const instance = app('/vue/articles/7', null, '?page=9')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(article))
  assert.equal(instance.requests[1].url, `${auth.API_BASE}/api/articles/7/comments?page=9&per_page=20`)
  assert.equal(instance.requests[1].headers.Authorization, undefined)
  await respond(instance.requests[1], 200, list([], 9, 41))
  assert.equal(instance.page.commentList.value.pagination.page, 9)
  assert.equal(instance.page.commentList.value.pagination.total, 41)
  const retry = instance.page.loadRelated()
  await respond(instance.requests[2], 200, list([comment, { ...comment, id: 8 }], 9, 41))
  await retry
  assert.deepEqual(Array.from(instance.page.commentList.value.list,
    // 前端保留 server 排序，不能重排或丟棄管理員可讀的 hidden 資料。
    (value) => value.id,
  ), [9, 8])
})

// 認證重驗與離頁撤銷延遲回應，避免舊 parent 或留言資料重新出現。
test('revalidation hides authorized data and ignores stale parent response', async (t) => {
  const instance = app('/vue/comments/9')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(member))
  await respond(instance.requests[1], 200, success(comment))
  const staleParent = instance.requests[2]
  const verify = instance.page.verifyAccount()
  assert.equal(instance.page.commentData.value, null)
  assert.equal(instance.page.contentReady.value, false)
  assert.equal(staleParent.signal.aborted, true)
  await respond(staleParent, 403, { success: false, error_code: 'E0102' })
  assert.equal(instance.page.disabledAccount.value, false)
  await respond(instance.requests[3], 200, success(member))
  await verify
  await respond(instance.requests[4], 200, success(comment))
  const latestParent = instance.requests[5]
  instance.page.onPageHide()
  await respond(latestParent, 200, success(article))
  assert.equal(instance.page.parentArticle.value, null)
})

// 有效 JWT 公開留言讀取失效與停用，皆須清除原授權畫面，不能補送訪客請求。
test('comment read authentication failures clear authorized content and block guest retries', async (t) => {
  for (const [status, code] of [[401, 'E0002'], [403, 'E0102']]) {
    const instance = app('/vue/articles/7')
    t.after(instance.close)
    await respond(instance.requests[0], 200, success(member))
    await respond(instance.requests[1], 200, success(article))
    await respond(instance.requests[2], status, { success: false, error_code: code })
    assert.equal(instance.page.articleData.value, null)
    assert.equal(instance.page.commentList.value, null)
    assert.equal(auth.readSession(instance.sessionStorage), null)
    await instance.page.loadRelated()
    await instance.page.loadContent()
    assert.equal(instance.requests.length, 3)
    assert.equal(instance.page.disabledAccount.value, status === 403)
  }
})

// 我的留言直接開啟需先驗證；無 JWT 返回合法原頁，外站 target 不可被採用。
test('my comments and login returns require verified local routes', async (t) => {
  const mine = app('/vue/me/comments')
  const guest = app('/vue/comments/9', null)
  const malicious = app('/vue/login', null, '?return_to=https%3A%2F%2Fevil.test')
  const local = app('/vue/login', null, '?return_to=%2Fvue%2Fcomments%2F9')
  for (const instance of [mine, guest, malicious, local]) t.after(instance.close)
  assert.equal(mine.page.commentList.value, null)
  await respond(mine.requests[0], 200, success(member))
  assert.equal(mine.requests[1].url, `${auth.API_BASE}/api/me/comments?page=1&per_page=20`)
  await respond(mine.requests[1], 200, list([comment]))
  assert.equal(mine.page.commentList.value.list[0].id, 9)
  assert.deepEqual(guest.redirects, ['/vue/login?return_to=%2Fvue%2Fcomments%2F9'])
  assert.equal(malicious.page.returnTo, '/vue/')
  assert.equal(local.page.returnTo, '/vue/comments/9')
})

// 固定空白集合與 Unicode 長度不能用 UTF-16 或 trim 代替；NUL 拒絕而非移除，原換行留給 API。
test('comment body validation preserves original line endings and rejects NUL and invalid lengths', () => {
  for (const body of ['😀'.repeat(2000), '第一行\r\n第二行\r第三行\n', '\u200b', '字\\u0000']) assert.deepEqual(comments.validateComment(body), {})
  for (const body of ['', '\u0009\u0085\u3000', '😀'.repeat(2001), '字\u0000', null, 1]) assert.deepEqual(comments.validateComment(body), { body: ['欄位格式或範圍不正確'] })
})

// 載入目前登入與公開父文章，讓每個寫入案例從真實可送出的正式狀態開始。
async function writableArticle(t, search = '', selectedBackend = auth.API_BASE) {
  const instance = app('/vue/articles/7', 'saved-token', search, selectedBackend)
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(member))
  await respond(instance.requests[1], 200, success(article))
  const initialPage = search ? 2 : 1
  await respond(instance.requests[2], 200, list([], initialPage))
  return instance
}

// 201 空 data 才清除草稿；由第二頁回第一頁時同步 URL 與後續留言分頁，不猜新 ID。
test('comment creation reloads first page in place and blocks duplicate submission', async (t) => {
  const instance = await writableArticle(t, '?page=2')
  instance.page.commentBody.value = '第一行\r\n第二行\r第三行\n😀'
  const write = instance.page.submitComment()
  const request = instance.requests[3]
  assert.equal(request.url, `${auth.API_BASE}/api/articles/7/comments`)
  assert.deepEqual(JSON.parse(request.body), { body: instance.page.commentBody.value })
  await instance.page.submitComment()
  await instance.page.verifyAccount()
  await instance.page.submitLogout()
  assert.equal(instance.requests.length, 4)
  assert.equal(instance.page.contentPending.value, true)
  await respond(request, 201, success({}))
  await write
  assert.equal(instance.page.commentBody.value, '')
  assert.equal(instance.page.hasDraft.value, false)
  assert.equal(instance.page.commentPosted.value, true)
  assert.equal(instance.page.commentPage.value, 1)
  assert.deepEqual(instance.history, ['/vue/articles/7#comments'])
  assert.deepEqual(instance.redirects, [])
  assert.equal(instance.requests[4].url, `${auth.API_BASE}/api/articles/7/comments?page=1&per_page=20`)
  await respond(instance.requests[4], 200, list([comment]))
  instance.page.commentBody.value = '下一則草稿'
  assert.equal(instance.page.hasDraft.value, true)
})

// Django 留言寫入後重讀原文章留言；自己的單則詳情即使父文章不可讀也保留操作。
test('Django comment creation and own detail use author endpoints', async (t) => {
  const instance = await writableArticle(t, '', auth.BACKENDS.django)
  assert.equal(instance.requests[0].url, `${auth.BACKENDS.django}/api/me`)
  assert.equal(instance.requests[1].url, `${auth.BACKENDS.django}/api/articles/7`)
  assert.equal(instance.page.canComment.value, true)
  assert.match(await renderedPage(instance.page), /發表留言/)
  instance.page.commentBody.value = 'Django 留言'
  const write = instance.page.submitComment()
  assert.equal(instance.requests[3].url, `${auth.BACKENDS.django}/api/articles/7/comments`)
  await respond(instance.requests[3], 201, success({}))
  await write
  assert.equal(instance.requests[4].url, `${auth.BACKENDS.django}/api/articles/7/comments?page=1&per_page=20`)
  await respond(instance.requests[4], 200, list([{ ...comment, visibility: 'visible' }]))
  assert.match(await renderedPage(instance.page), /href="\/vue\/comments\/9"/)

  const own = app('/vue/comments/9', 'saved-token', '', auth.BACKENDS.django)
  t.after(own.close)
  await respond(own.requests[0], 200, success(member))
  assert.equal(own.requests[1].url, `${auth.BACKENDS.django}/api/comments/9`)
  await respond(own.requests[1], 200, success(comment))
  assert.equal(own.requests[2].url, `${auth.BACKENDS.django}/api/articles/7`)
  await respond(own.requests[2], 404, { success: false, error_code: 'E0004' })
  assert.equal(own.page.commentData.value.body, comment.body)
  assert.equal(own.page.relatedError.value, '所屬文章目前無法讀取')
  assert.equal(own.page.canManageVisibility.value, true)
  assert.equal(own.page.canEdit.value, false)
  assert.match(await renderedPage(own.page), />恢復<\/button>/)
})

// Flask、FastAPI 與 Gin 新留言重讀原文章第一頁；父文章不可讀時仍保留自己的留言詳情。
test('Flask, FastAPI and Gin comment creation and own detail preserve author flow', async (t) => {
  for (const selectedBackend of [auth.BACKENDS.flask, auth.BACKENDS.fastapi, auth.BACKENDS.gin]) {
    const instance = await writableArticle(t, '?page=2', selectedBackend)
    assert.equal(instance.page.canComment.value, true)
    assert.match(await renderedPage(instance.page), /發表留言/)
    instance.page.commentBody.value = '作者留言\r\n第二行'
    const write = instance.page.submitComment()
    assert.equal(instance.requests[3].url, `${selectedBackend}/api/articles/7/comments`)
    assert.equal(instance.requests[3].method, 'POST')
    assert.deepEqual(JSON.parse(instance.requests[3].body), { body: '作者留言\r\n第二行' })
    await respond(instance.requests[3], 201, success({}))
    await write
    assert.equal(instance.requests[4].url, `${selectedBackend}/api/articles/7/comments?page=1&per_page=20`)
    assert.deepEqual(instance.history, ['/vue/articles/7#comments'])
    await respond(instance.requests[4], 200, list([{ ...comment, visibility: 'visible' }]))
    assert.match(await renderedPage(instance.page), /href="\/vue\/comments\/9"/)

    const own = app('/vue/comments/9', 'saved-token', '', selectedBackend)
    t.after(own.close)
    await respond(own.requests[0], 200, success(member))
    assert.equal(own.requests[1].url, `${selectedBackend}/api/comments/9`)
    await respond(own.requests[1], 200, success(comment))
    assert.equal(own.requests[2].url, `${selectedBackend}/api/articles/7`)
    await respond(own.requests[2], 404, { success: false, error_code: 'E0004', data: { title: '不得顯示' } })
    assert.equal(own.page.commentData.value.body, comment.body)
    assert.equal(own.page.parentArticle.value, null)
    assert.equal(own.page.canManageVisibility.value, true)
    assert.equal(own.page.canEdit.value, false)
    const html = await renderedPage(own.page)
    assert.match(html, /所屬文章目前無法讀取/)
    assert.doesNotMatch(html, /不得顯示/)
  }
})

// FastAPI 發文表單只在目前帳號已驗證時出現；重驗期間撤下寫入入口。
test('FastAPI article form waits for verified author state', async (t) => {
  const visitor = app('/vue/articles/new', null, '', auth.BACKENDS.fastapi)
  const author = app('/vue/articles/new', 'fastapi-jwt', '', auth.BACKENDS.fastapi)
  t.after(visitor.close)
  t.after(author.close)
  assert.deepEqual(visitor.redirects, ['/vue/login?return_to=%2Fvue%2Farticles%2Fnew'])
  assert.doesNotMatch(await renderedPage(visitor.page), /<form[^>]*><div class="field"><label for="title">/)
  assert.doesNotMatch(await renderedPage(author.page), /<form[^>]*><div class="field"><label for="title">/)
  await respond(author.requests[0], 200, success(member))
  assert.match(await renderedPage(author.page), /<form[^>]*><div class="field"><label for="title">/)
  const recheck = author.page.verifyAccount()
  assert.doesNotMatch(await renderedPage(author.page), /<form[^>]*><div class="field"><label for="title">/)
  await respond(author.requests[1], 200, success(member))
  await recheck
  assert.match(await renderedPage(author.page), /<form[^>]*><div class="field"><label for="title">/)
})

// 格式與後端 422 都保留草稿，欄位明細留在原表單且不產生額外送出。
test('comment validation and server 422 retain text and field errors', async (t) => {
  const instance = await writableArticle(t)
  instance.page.commentBody.value = '字\u0000'
  await instance.page.submitComment()
  assert.equal(instance.requests.length, 3)
  assert.deepEqual(Array.from(instance.page.errors.value.body), ['欄位格式或範圍不正確'])
  instance.page.commentBody.value = '原文\r\n'
  const write = instance.page.submitComment()
  await respond(instance.requests[3], 422, { success: false, error_code: 'E0001', errors: { body: ['欄位格式或範圍不正確'] } })
  await write
  assert.equal(instance.page.commentBody.value, '原文\r\n')
  assert.equal(instance.page.contentPending.value, false)
  assert.equal(instance.page.commentPosted.value, false)
})

// 401 同帳號重登保留原文並僅重讀文章；另一帳號不能接收原草稿，皆不得自動寫入。
test('comment 401 draft survives same-account login and is cleared for different account', async (t) => {
  for (const different of [false, true]) {
    const instance = await writableArticle(t)
    instance.page.commentBody.value = '原帳號私有草稿'
    const write = instance.page.submitComment()
    await respond(instance.requests[3], 401, { success: false, error_code: 'E0002' })
    await write
    assert.equal(instance.page.reLogin.value, true)
    assert.equal(instance.page.commentBody.value, '原帳號私有草稿')
    assert.equal(instance.page.articleData.value, null)
    instance.page.email.value = different ? 'other@example.test' : member.email
    instance.page.password.value = 'abcd'
    await vue.nextTick()
    const login = instance.page.submitLogin()
    const account = different ? { ...member, id: 3, email: 'other@example.test' } : member
    await respond(instance.requests[4], 200, success({ access_token: 'new-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: account }))
    await login
    assert.equal(instance.page.commentBody.value, different ? '' : '原帳號私有草稿')
    assert.equal(instance.page.reLogin.value, false)
    assert.equal(instance.requests.filter(
      // 重新登入只可恢復讀取權限，不重送既有草稿。
      (request) => request.method === 'POST' && request.url.endsWith('/comments'),
    ).length, 1)
    if (!different) {
      await respond(instance.requests[5], 200, success(article))
      await respond(instance.requests[6], 200, list([]))
      assert.equal(instance.page.canComment.value, true)
      const manual = instance.page.submitComment()
      await respond(instance.requests[7], 201, success({}))
      await manual
    } else assert.match(instance.page.summary.value, /不同帳號/)
  }
})

// 5xx、未知 201 與連線錯誤都屬結果不明，保留登入和文字供手動查證，不能假報成功。
test('unknown comment write result retains credential and text without retry', async (t) => {
  for (const result of ['server', 'incomplete', 'network']) {
    const instance = await writableArticle(t)
    instance.page.commentBody.value = '待查證文字'
    const write = instance.page.submitComment()
    if (result === 'network') { instance.requests[3].reject(new Error('connection interrupted')); await setImmediate() }
    else await respond(instance.requests[3], result === 'server' ? 500 : 201, result === 'server' ? { success: false, error_code: 'E9999' } : success({ id: 10 }))
    await write
    assert.equal(instance.page.commentBody.value, '待查證文字')
    assert.equal(instance.page.credential.value.accessToken, 'saved-token')
    assert.equal(instance.page.writeUnknown.value, true)
    assert.equal(instance.page.commentPosted.value, false)
    assert.equal(instance.requests.length, 4)
    assert.deepEqual(instance.history, [])
  }
})

// 下架預覽、停用與離頁後舊回應都不能恢復可送出狀態；捨棄草稿取消時不登出。
test('hidden parent and stale comment writes cannot restore authorization or clear drafts', async (t) => {
  const hidden = app('/vue/articles/7')
  t.after(hidden.close)
  await respond(hidden.requests[0], 200, success(member))
  await respond(hidden.requests[1], 200, success({ ...article, visibility: 'hidden' }))
  hidden.page.commentBody.value = '禁止送出'
  await hidden.page.submitComment()
  assert.equal(hidden.page.canComment.value, false)
  assert.equal(hidden.requests.length, 3)
  const disabled = await writableArticle(t)
  disabled.page.commentBody.value = '停用保留文字'
  await disabled.page.submitLogout()
  assert.equal(disabled.requests.length, 3)
  const write = disabled.page.submitComment()
  await respond(disabled.requests[3], 403, { success: false, error_code: 'E0102' })
  await write
  assert.equal(disabled.page.disabledAccount.value, true)
  assert.equal(disabled.page.reLogin.value, false)
  assert.equal(disabled.page.commentBody.value, '停用保留文字')
  const stale = await writableArticle(t)
  stale.page.commentBody.value = '離頁草稿'
  const pending = stale.page.submitComment()
  stale.page.onPageHide()
  await respond(stale.requests[3], 201, success({}))
  await pending
  assert.equal(stale.page.commentBody.value, '離頁草稿')
  assert.equal(stale.page.commentPosted.value, false)
  assert.deepEqual(stale.history, [])
})

// 留言列表 404 已確認父文章不可讀；撤下舊畫面但保留草稿，須重讀父文章才能恢復留言。
test('article comment 404 clears stale parent until manual parent revalidation', async (t) => {
  const instance = await writableArticle(t)
  instance.page.commentBody.value = '原帳號草稿'
  const retry = instance.page.loadRelated()
  await respond(instance.requests[3], 404, { success: false, error_code: 'E0004', log_id: 'parent-unreadable' })
  await retry
  assert.equal(instance.page.articleData.value, null, 'known unreadable parent must not retain its prior content')
  assert.equal(instance.page.commentList.value, null)
  assert.equal(instance.page.contentReady.value, false)
  assert.equal(instance.page.canComment.value, false)
  assert.equal(instance.page.commentBody.value, '原帳號草稿')
  assert.equal(instance.page.credential.value.accessToken, 'saved-token')
  assert.match(instance.page.summary.value, /找不到指定資料/)
  await instance.page.loadRelated()
  await instance.page.submitComment()
  assert.equal(instance.requests.length, 4, 'only a parent reload may re-establish readability')

  // 作者仍可預覽已下架文章，但父文章重讀成功不代表可新增留言。
  const preview = instance.page.loadContent()
  await respond(instance.requests[4], 200, success({ ...article, visibility: 'hidden' }))
  await preview
  await respond(instance.requests[5], 200, list([]))
  assert.equal(instance.page.contentReady.value, true)
  assert.equal(instance.page.canComment.value, false)
  assert.equal(instance.page.commentBody.value, '原帳號草稿')

  // 後端再次確認公開後才恢復送出；本輪重試只讀取，不自動送出保留文字。
  const restored = instance.page.loadContent()
  await respond(instance.requests[6], 200, success(article))
  await restored
  await respond(instance.requests[7], 200, list([]))
  assert.equal(instance.page.canComment.value, true)
  assert.equal(instance.page.commentBody.value, '原帳號草稿')
  assert.equal(instance.requests.length, 8)
})

// 留言 POST 等待中不能重讀列表；否則重讀的 401 會取消尚未確認結果的寫入並遺失未知結果提示。
test('comment retry cannot interrupt pending write after list failure', async (t) => {
  const instance = await writableArticle(t)
  const failedList = instance.page.loadRelated()
  await respond(instance.requests[3], 500, { success: false, error_code: 'E9999' })
  await failedList
  instance.page.commentBody.value = '等待送出'
  const write = instance.page.submitComment()
  const post = instance.requests[4]
  const retry = instance.page.loadRelated()
  if (instance.requests.length > 5) await respond(instance.requests.at(-1), 401, { success: false, error_code: 'E0002' })
  await retry
  assert.equal(post.signal.aborted, false, 'read retry must not cancel a pending write via credential clearing')
  assert.equal(instance.requests.length, 5)
  assert.equal(instance.page.contentPending.value, true)
  assert.equal(instance.page.credential.value.accessToken, 'saved-token')
  await respond(post, 201, success({}))
  await write
  assert.equal(instance.page.commentPosted.value, true)
  assert.equal(instance.page.contentPending.value, false)
  assert.equal(instance.requests[5].url, `${auth.API_BASE}/api/articles/7/comments?page=1&per_page=20`)
  await respond(instance.requests[5], 200, list([comment]))
  assert.equal(instance.page.commentList.value.list[0].id, 9)

  // 送出前已開始的讀取雖然晚回 401，也必須由原 epoch 擋掉而不影響新 POST。
  const oldRead = instance.page.loadRelated()
  const oldRequest = instance.requests[6]
  instance.page.commentBody.value = '下一則'
  const nextWrite = instance.page.submitComment()
  const nextPost = instance.requests[7]
  assert.equal(oldRequest.signal.aborted, true)
  await respond(oldRequest, 401, { success: false, error_code: 'E0002' })
  await oldRead
  assert.equal(nextPost.signal.aborted, false)
  assert.equal(instance.page.contentPending.value, true)
  await respond(nextPost, 201, success({}))
  await nextWrite
  await respond(instance.requests[8], 200, list([comment]))
  assert.equal(instance.page.commentPosted.value, true)
  assert.equal(instance.page.credential.value.accessToken, 'saved-token')

  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  assert.match(descriptor.template.content, /<button[^>]*:disabled="contentPending"[^>]*@click="loadRelated">重新讀取留言<\/button>/)
})

// 用已安裝的 Vue compiler 與 renderer 產出實際模板 HTML，避免只驗證旗標而漏掉外層 v-if。
async function renderedPage(state) {
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const code = compile(descriptor.template.content, { mode: 'function', prefixIdentifiers: true }).code
  const render = runInNewContext(`(function () { ${code} })()`, { Vue: vue })
  return renderToString(vue.createSSRApp({
    // 交給 Vue 解包正式 refs，保留模板與正式元件相同的顯示語意。
    setup() { return state },
    render,
  }))
}

// 結果不明的查證入口不能依父文章可讀性消失；父 404／5xx／下架時仍保留，但不能開放留言。
test('unknown write guidance stays rendered through unreadable parent and parent retries', async (t) => {
  const instance = await writableArticle(t)
  instance.page.commentBody.value = '待查證草稿'
  const write = instance.page.submitComment()
  await respond(instance.requests[3], 500, { success: false, error_code: 'E9999' })
  await write
  const commentRetry = instance.page.loadRelated()
  await respond(instance.requests[4], 404, { success: false, error_code: 'E0004' })
  await commentRetry
  let html = await renderedPage(instance.page)
  assert.match(html, /留言結果尚未確認/, 'unknown-write guidance must remain rendered after parent readability is denied')
  assert.match(html, /href="\/vue\/me\/comments"[^>]*>到我的留言查證<\/a>/)
  assert.doesNotMatch(html, /<textarea/)
  assert.doesNotMatch(html, /公開正文/)

  // 原文及憑證保持；父文章手動重試期間與 404／5xx 後，查證入口仍是獨立提示。
  for (const status of [404, 500]) {
    const parentRetry = instance.page.loadContent()
    html = await renderedPage(instance.page)
    assert.match(html, /留言結果尚未確認/)
    await respond(instance.requests.at(-1), status, { success: false, error_code: status === 404 ? 'E0004' : 'E9999' })
    await parentRetry
    html = await renderedPage(instance.page)
    assert.match(html, /留言結果尚未確認/)
    assert.match(html, /href="\/vue\/me\/comments"[^>]*>到我的留言查證<\/a>/)
    assert.doesNotMatch(html, /<textarea/)
    assert.equal(instance.page.commentBody.value, '待查證草稿')
    assert.equal(instance.page.credential.value.accessToken, 'saved-token')
  }

  // 下架作者預覽不恢復可寫表單；未知結果提示與查證連結仍可閱讀。
  const preview = instance.page.loadContent()
  await respond(instance.requests.at(-1), 200, success({ ...article, visibility: 'hidden' }))
  await preview
  await respond(instance.requests.at(-1), 200, list([]))
  html = await renderedPage(instance.page)
  assert.match(html, /留言結果尚未確認/)
  assert.match(html, /到我的留言查證/)
  assert.doesNotMatch(html, /<textarea/)
  assert.equal(instance.page.canComment.value, false)
  assert.equal(instance.requests.filter(
    // 所有查證與父文章重讀只能 GET，不能自動重送結果不明的 POST。
    (request) => request.method === 'POST',
  ).length, 1)

  // 重驗 401 保留未知結果與草稿，重新登入表單只能恢復授權，留言文字仍不可送出。
  const verification = instance.page.verifyAccount()
  await respond(instance.requests.at(-1), 401, { success: false, error_code: 'E0002' })
  await verification
  html = await renderedPage(instance.page)
  assert.match(html, /留言結果尚未確認/)
  assert.match(html, /href="\/vue\/me\/comments"[^>]*>到我的留言查證<\/a>/)
  assert.match(html, /<textarea[^>]*id="body"[^>]*disabled/)
  assert.equal(instance.page.reLogin.value, true)
  const requestCount = instance.requests.length
  await instance.page.loadRelated()
  await instance.page.submitComment()
  assert.equal(instance.requests.length, requestCount)
  assert.equal(instance.page.commentBody.value, '待查證草稿')

})

// 驗證合法 edit ID 與固定錯誤文案，拒絕外站／溢位／多餘子路徑與 NUL。
test('editor routes and PUT envelope keep safe IDs and fixed validation contracts', () => {
  for (const [path, page] of [['/vue/articles/007/edit', 'article-edit'], ['/vue/comments/009/edit', 'comment-edit']]) {
    assert.equal(articles.localPage(path), true)
    assert.equal(articles.articlePage(path), page)
  }
  for (const path of ['/vue/articles/0/edit', '/vue/comments/9007199254740992/edit', '/vue/articles/-1/edit', '/vue/articles/new/edit', '/vue/articles/7/edit/extra', '//evil.test', 'https://evil.test']) assert.equal(articles.articlePage(path), 'missing')
  assert.equal(articles.contentUpdated(200, success({})), true)
  assert.equal(articles.contentUpdated(201, success({})), false)
  assert.equal(articles.contentUpdated(200, success({ id: 7 })), false)
  assert.equal(articles.articleFailure({ success: false, error_code: 'E0201' }).message, '內容已更新，請確認最新內容後再送出')
  assert.deepEqual(articles.validateArticle({ title: 'a\u0000', body: 'b\u0000' }), { title: ['欄位格式或範圍不正確'], body: ['欄位格式或範圍不正確'] })
  assert.deepEqual(articles.validateArticle({ title: '\\u0000', body: '<script>純文字</script>\r\n' }), {})
})

// 載入真實 App state；編輯與詳情都先經 /me，不繞過正式權限流程。
async function resourcePage(t, path, resource = path.includes('/comments/') ? comment : article, identity = member, parent = article, selectedBackend = auth.API_BASE) {
  const instance = app(path, 'saved-token', '', selectedBackend)
  instance.approve(true)
  t.after(instance.close)
  assert.equal(instance.page.contentReady.value, false)
  await respond(instance.requests[0], 200, success(identity))
  await respond(instance.requests[1], 200, success(resource))
  if (path.includes('/comments/')) await respond(instance.requests[2], 200, success(parent))
  else if (!path.endsWith('/edit')) await respond(instance.requests[2], 200, list([]))
  return instance
}

// 訪客保留合法原路徑，登入頁接受 edit 返回目標；未知 ID 不發資源請求。
test('direct editor access requires verification and preserves a local login return', async (t) => {
  const guest = app('/vue/comments/009/edit', null)
  const login = app('/vue/login', null, '?return_to=%2Fvue%2Farticles%2F007%2Fedit')
  const invalid = app('/vue/articles/9007199254740992/edit', null)
  for (const instance of [guest, login, invalid]) t.after(instance.close)
  assert.equal(guest.redirects[0], '/vue/login?return_to=%2Fvue%2Fcomments%2F009%2Fedit')
  assert.equal(login.page.returnTo, '/vue/articles/007/edit')
  assert.equal(invalid.requests.length, 0)
  assert.match(await renderedPage(invalid.page), /找不到頁面/)
  const checking = app('/vue/articles/7/edit')
  t.after(checking.close)
  assert.doesNotMatch(await renderedPage(checking.page), /<textarea/)
  await respond(checking.requests[0], 401, { success: false, error_code: 'E0002' })
  assert.equal(checking.requests.length, 1)
  assert.equal(checking.page.reLogin.value, true)
})

// 同值送出保留載入原 CRLF／HTML 字面值；200 後只 GET 同 ID，不猜 ID 或先改資源。
test('no-op article edit sends raw server text and rereads the same ID after empty 200', async (t) => {
  const raw = { ...article, title: '<b>標題</b>', body: '第一行\r\n<script>字面值</script>\r第三行', visibility: 'hidden', can_restore: false }
  const instance = await resourcePage(t, '/vue/articles/007/edit', raw)
  assert.equal(instance.page.canEdit.value, true)
  assert.equal(instance.page.hasDraft.value, false)
  assert.equal(instance.page.articleBody.value, raw.body)
  let html = await renderedPage(instance.page)
  assert.match(html, /&lt;script&gt;字面值&lt;\/script&gt;/)
  assert.match(html, /讀者 · 2026-09-24 10:00:00 · 建立於 Gin/)
  assert.match(html, /已下架/)
  assert.doesNotMatch(html, /<script>/)
  const write = instance.page.submitEdit()
  assert.equal(instance.requests[2].url, `${auth.API_BASE}/api/articles/7`)
  assert.equal(instance.requests[2].method, 'PUT')
  assert.deepEqual(JSON.parse(instance.requests[2].body), { title: raw.title, body: raw.body, version: 1 })
  assert.equal(instance.page.articleData.value.version, 1)
  await respond(instance.requests[2], 200, success({}))
  await write
  assert.equal(instance.requests[3].url, `${auth.API_BASE}/api/articles/7`)
  assert.equal(instance.requests[3].method, undefined)
  await respond(instance.requests[3], 200, success(raw))
  assert.equal(instance.page.articleData.value.visibility, 'hidden')
  assert.equal(instance.page.hasDraft.value, false)
  assert.deepEqual(instance.redirects, [])
  // 原生 textarea 只在真的輸入時改草稿，避免 mount 的換行正規化造成假修改。
  instance.page.editBodyInput({ target: { value: '新文字\n第二行' } })
  assert.equal(instance.page.articleBody.value, '新文字\n第二行')
  assert.equal(instance.page.hasDraft.value, true)
})

// 作者可以改自己下架留言；父文章 404／5xx／下架都關閉編修，且獨立重試不丟草稿。
test('comment editor retains own content and draft until parent is confirmed public', async (t) => {
  const instance = app('/vue/comments/9/edit')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(member))
  await respond(instance.requests[1], 200, success(comment))
  instance.page.commentBody.value = '作者草稿'
  assert.equal(instance.page.canEdit.value, false)
  await respond(instance.requests[2], 404, { success: false, error_code: 'E0004', data: { title: '不可洩漏' } })
  assert.equal(instance.page.commentData.value.id, 9)
  let html = await renderedPage(instance.page)
  assert.match(html, /作者草稿/)
  assert.doesNotMatch(html, /不可洩漏/)
  assert.match(html, /<textarea[^>]*disabled/)
  await instance.page.submitEdit()
  assert.equal(instance.requests.length, 3)
  for (const result of ['failed', 'hidden', 'visible']) {
    const retry = instance.page.loadRelated()
    const request = instance.requests.at(-1)
    if (result === 'failed') await respond(request, 500, { success: false, error_code: 'E9999' })
    else await respond(request, 200, success({ ...article, visibility: result }))
    await retry
    assert.equal(instance.page.canEdit.value, result === 'visible')
    assert.equal(instance.page.commentBody.value, '作者草稿')
  }
  const write = instance.page.submitEdit()
  assert.deepEqual(JSON.parse(instance.requests.at(-1).body), { body: '作者草稿', version: 1 })
  await respond(instance.requests.at(-1), 200, success({}))
  await write
  assert.equal(instance.requests.at(-1).url, `${auth.API_BASE}/api/comments/9`)
  await respond(instance.requests.at(-1), 200, success({ ...comment, body: '作者草稿', version: 2 }))
  await respond(instance.requests.at(-1), 200, success(article))
  assert.equal(instance.page.commentBody.value, '作者草稿')
  assert.equal(instance.page.editVersion.value, 2)
})

// 管理員只有自己是作者才能編輯；詳情管理操作與父文章的公開資格分開。
test('ownership limits editors while administrators retain permitted detail actions', async (t) => {
  const admin = { ...member, id: 3, role: 'admin', nickname: '管理員' }
  const other = await resourcePage(t, '/vue/articles/7/edit', article, admin)
  assert.equal(other.page.canEdit.value, false)
  assert.equal(other.page.editLoaded.value, false)
  assert.doesNotMatch(await renderedPage(other.page), /<textarea/)
  await other.page.submitEdit()
  assert.equal(other.requests.length, 2)
  const own = await resourcePage(t, '/vue/articles/7/edit', article, { ...member, role: 'admin' })
  assert.equal(own.page.canEdit.value, true)
  const ownComment = await resourcePage(t, '/vue/comments/9', comment, member, { ...article, visibility: 'hidden' })
  assert.equal(ownComment.page.canEdit.value, false)
  assert.equal(ownComment.page.canManageVisibility.value, true)
  assert.match(await renderedPage(ownComment.page), />恢復<\/button>/)
  assert.doesNotMatch(await renderedPage(ownComment.page), /href="[^\"]*\/edit"/)
})

// Gin 管理員自己的內容仍走作者操作；詳情同時保留返回管理列表的入口。
test('Gin administrator can edit own content and return to management lists', async (t) => {
  const own = await resourcePage(t, '/vue/articles/7', article, { ...member, role: 'admin' }, article, auth.BACKENDS.gin)
  assert.equal(own.page.canEdit.value, true)
  assert.equal(own.page.canManageVisibility.value, true)
  const html = await renderedPage(own.page)
  assert.match(html, /href="\/vue\/articles\/7\/edit"/)
  assert.match(html, />下架<\/button>/)
  assert.match(html, /href="\/vue\/admin\/articles"/)
  const ownComment = await resourcePage(t, '/vue/comments/9', { ...comment, visibility: 'visible' }, { ...member, role: 'admin' }, article, auth.BACKENDS.gin)
  assert.equal(ownComment.page.canEdit.value, true)
  assert.equal(ownComment.page.canManageVisibility.value, true)
  assert.match(await renderedPage(ownComment.page), /href="\/vue\/admin\/comments"/)
})

// 409 保留文字及舊 version；GET 只提供比較，確認整合／捨棄後才可手動使用新 version。
test('edit conflict requires a fresh comparison and explicit merge or discard before manual PUT', async (t) => {
  const instance = await resourcePage(t, '/vue/articles/7/edit')
  instance.page.articleTitle.value = '我的標題'
  instance.page.articleBody.value = '我的文字'
  const write = instance.page.submitEdit()
  await respond(instance.requests[2], 409, { success: false, error_code: 'E0201' })
  await write
  assert.equal(instance.page.conflict.value, true)
  assert.equal(instance.page.editVersion.value, 1)
  await instance.page.submitEdit()
  assert.equal(instance.requests.length, 3)
  const read = instance.page.loadContent()
  await respond(instance.requests[3], 200, success({ ...article, title: '別人的標題', body: '<em>最新文字</em>', version: 2 }))
  await read
  assert.equal(instance.page.articleBody.value, '我的文字')
  assert.equal(instance.page.editVersion.value, 1)
  const html = await renderedPage(instance.page)
  assert.match(html, /我的文字/)
  assert.match(html, /&lt;em&gt;最新文字&lt;\/em&gt;/)
  assert.match(html, /最新內容（版本 2）/)
  instance.approve(false)
  instance.page.acceptComparison(false)
  assert.equal(instance.page.editVersion.value, 1)
  instance.approve(true)
  instance.page.acceptComparison(false)
  assert.equal(instance.page.editVersion.value, 2)
  assert.equal(instance.page.articleBody.value, '我的文字')
  assert.equal(instance.requests.length, 4)
  const merged = instance.page.submitEdit()
  assert.deepEqual(JSON.parse(instance.requests[4].body), { title: '我的標題', body: '我的文字', version: 2 })
  await respond(instance.requests[4], 409, { success: false, error_code: 'E0201' })
  await merged
  const reread = instance.page.loadContent()
  const latest = { ...article, title: '已更新', body: '第三版\r\n原文', version: 3 }
  await respond(instance.requests[5], 200, success(latest))
  await reread
  instance.page.acceptComparison(true)
  assert.equal(instance.page.articleBody.value, latest.body)
  assert.equal(instance.page.articleTitle.value, latest.title)
  assert.equal(instance.page.editVersion.value, 3)
  assert.equal(instance.page.hasDraft.value, false)
  assert.equal(instance.requests.length, 6)
})

// 各作者後端編輯衝突均保留草稿及原版本；讀取比較後仍須作者確認新版本。
test('author backends keep edit conflict draft until explicit latest-version comparison', async (t) => {
  for (const selectedBackend of [auth.BACKENDS.django, auth.BACKENDS.flask, auth.BACKENDS.fastapi, auth.BACKENDS.gin]) {
    const instance = await resourcePage(t, '/vue/articles/7/edit', article, member, article, selectedBackend)
    instance.page.articleTitle.value = '我的 Django 標題'
    instance.page.articleBody.value = '我的 Django 內文'
    const write = instance.page.submitEdit()
    assert.equal(instance.requests[2].url, `${selectedBackend}/api/articles/7`)
    assert.deepEqual(JSON.parse(instance.requests[2].body), { title: '我的 Django 標題', body: '我的 Django 內文', version: 1 })
    await respond(instance.requests[2], 409, { success: false, error_code: 'E0201' })
    await write
    assert.equal(instance.page.conflict.value, true)
    await instance.page.submitEdit()
    assert.equal(instance.requests.length, 3)
    const read = instance.page.loadContent()
    await respond(instance.requests[3], 200, success({ ...article, title: '最新標題', body: '最新內文', version: 2 }))
    await read
    assert.equal(instance.page.articleTitle.value, '我的 Django 標題')
    assert.equal(instance.page.editVersion.value, 1)
    assert.match(await renderedPage(instance.page), /最新內容（版本 2）/)
    instance.page.acceptComparison(false)
    assert.equal(instance.page.editVersion.value, 2)
    const retry = instance.page.submitEdit()
    assert.deepEqual(JSON.parse(instance.requests[4].body), { title: '我的 Django 標題', body: '我的 Django 內文', version: 2 })
    await respond(instance.requests[4], 200, success({}))
    await retry
    assert.equal(instance.requests[5].url, `${selectedBackend}/api/articles/7`)
    await respond(instance.requests[5], 200, success({ ...article, title: '我的 Django 標題', body: '我的 Django 內文', version: 3 }))
    assert.equal(instance.page.articleData.value.version, 3)
  }
})

// 各作者後端編修留言須先確認父文章公開；成功後依原 ID 重讀。
test('author backends reread own comment detail after edit success', async (t) => {
  for (const selectedBackend of [auth.BACKENDS.django, auth.BACKENDS.flask, auth.BACKENDS.fastapi, auth.BACKENDS.gin]) {
    const instance = await resourcePage(t, '/vue/comments/9/edit', { ...comment, visibility: 'visible' }, member, article, selectedBackend)
    assert.equal(instance.page.canEdit.value, true)
    instance.page.commentBody.value = '編修後留言'
    const write = instance.page.submitEdit()
    assert.equal(instance.requests[3].url, `${selectedBackend}/api/comments/9`)
    assert.deepEqual(JSON.parse(instance.requests[3].body), { body: '編修後留言', version: 1 })
    await respond(instance.requests[3], 200, success({}))
    await write
    assert.equal(instance.requests[4].url, `${selectedBackend}/api/comments/9`)
    await respond(instance.requests[4], 200, success({ ...comment, body: '編修後留言', visibility: 'visible', version: 2 }))
    await respond(instance.requests[5], 200, success(article))
    assert.equal(instance.page.commentData.value.body, '編修後留言')
    assert.equal(instance.page.editVersion.value, 2)
  }
})

// 各作者後端下架、恢復沿作者權限及 server can_restore；成功後重讀目前詳情。
test('author backends change article visibility with version and server readback', async (t) => {
  for (const selectedBackend of [auth.BACKENDS.django, auth.BACKENDS.flask, auth.BACKENDS.fastapi, auth.BACKENDS.gin]) {
    const instance = await resourcePage(t, '/vue/articles/7', article, member, article, selectedBackend)
    assert.equal(instance.page.canManageVisibility.value, true)
    const hide = instance.page.submitVisibility('hidden')
    assert.equal(instance.requests[3].url, `${selectedBackend}/api/articles/7/visibility`)
    assert.deepEqual(JSON.parse(instance.requests[3].body), { visibility: 'hidden', version: 1 })
    await respond(instance.requests[3], 200, success({}))
    await hide
    await respond(instance.requests[4], 200, success({ ...article, visibility: 'hidden', can_restore: true, version: 2 }))
    await respond(instance.requests[5], 200, list([]))
    assert.equal(instance.page.articleData.value.visibility, 'hidden')
    assert.match(await renderedPage(instance.page), />恢復<\/button>/)
    const restore = instance.page.submitVisibility('visible')
    assert.deepEqual(JSON.parse(instance.requests[6].body), { visibility: 'visible', version: 2 })
    await respond(instance.requests[6], 200, success({}))
    await restore
    await respond(instance.requests[7], 200, success({ ...article, visibility: 'visible', can_restore: false, version: 3 }))
    await respond(instance.requests[8], 200, list([]))
    assert.equal(instance.page.articleData.value.visibility, 'visible')
  }
})

// FastAPI 留言恢復依後端 can_restore 與目前版本；無資格時不能發出 PUT。
test('FastAPI own comment visibility uses current version and restore eligibility', async (t) => {
  const instance = await resourcePage(t, '/vue/comments/9', { ...comment, can_restore: false }, member, article, auth.BACKENDS.fastapi)
  await instance.page.submitVisibility('visible')
  assert.equal(instance.requests.length, 3)
  const read = instance.page.loadContent()
  await respond(instance.requests[3], 200, success({ ...comment, can_restore: true, version: 2 }))
  await read
  await respond(instance.requests[4], 200, success(article))
  assert.match(await renderedPage(instance.page), />恢復<\/button>/)
  const restore = instance.page.submitVisibility('visible')
  assert.equal(instance.requests[5].url, `${auth.BACKENDS.fastapi}/api/comments/9/visibility`)
  assert.equal(instance.requests[5].method, 'PUT')
  assert.deepEqual(JSON.parse(instance.requests[5].body), { visibility: 'visible', version: 2 })
  await respond(instance.requests[5], 200, success({}))
  await restore
  assert.equal(instance.requests[6].url, `${auth.BACKENDS.fastapi}/api/comments/9`)
  await respond(instance.requests[6], 200, success({ ...comment, visibility: 'visible', can_restore: false, version: 3 }))
  await respond(instance.requests[7], 200, success(article))
  assert.equal(instance.page.commentData.value.visibility, 'visible')
})

// 管理員接管他人自行下架內容仍送 hidden；自己的下架不可借管理員身分取得接管控制。
test('visibility uses server restoration rights and confirms administrator takeover', async (t) => {
  const admin = { ...member, id: 3, role: 'admin' }
  const hidden = { ...article, visibility: 'hidden', can_restore: true }
  const instance = await resourcePage(t, '/vue/articles/7', hidden, admin)
  const html = await renderedPage(instance.page)
  assert.match(html, /再次下架並接管恢復權/)
  assert.match(html, />恢復<\/button>/)
  assert.doesNotMatch(html, /href="[^\"]*\/edit"/)
  instance.approve(false)
  await instance.page.submitVisibility('hidden')
  assert.equal(instance.requests.length, 3)
  instance.approve(true)
  const write = instance.page.submitVisibility('hidden')
  assert.deepEqual(JSON.parse(instance.requests[3].body), { visibility: 'hidden', version: 1 })
  assert.match(instance.confirmations.at(-1), /管理員控制恢復/)
  await respond(instance.requests[3], 200, success({}))
  await write
  await respond(instance.requests[4], 200, success({ ...hidden, version: 2 }))
  await respond(instance.requests[5], 200, list([]))
  assert.equal(instance.page.articleData.value.version, 2)
  const ownAdmin = await resourcePage(t, '/vue/articles/7', { ...hidden, can_restore: false }, { ...member, role: 'admin' })
  assert.equal(ownAdmin.page.canHide.value, false)
  assert.doesNotMatch(await renderedPage(ownAdmin.page), />恢復<\/button>/)
  await ownAdmin.page.submitVisibility('visible')
  assert.equal(ownAdmin.requests.length, 3)
})

// 狀態衝突先阻止重送；手動 GET 後再次確認操作，使用最新詳情版本而非隱藏自動重試。
test('visibility conflict rereads and reconfirms the operation before sending a new version', async (t) => {
  const instance = await resourcePage(t, '/vue/comments/9', comment)
  const restore = instance.page.submitVisibility('visible')
  await respond(instance.requests[3], 409, { success: false, error_code: 'E0201' })
  await restore
  await instance.page.submitVisibility('visible')
  assert.equal(instance.requests.length, 4)
  assert.equal(instance.confirmations.length, 1)
  const read = instance.page.loadContent()
  await respond(instance.requests[4], 200, success({ ...comment, version: 4 }))
  await read
  await respond(instance.requests[5], 404, { success: false, error_code: 'E0004' })
  instance.approve(false)
  await instance.page.submitVisibility('visible')
  assert.equal(instance.requests.length, 6)
  instance.approve(true)
  const retried = instance.page.submitVisibility('visible')
  assert.deepEqual(JSON.parse(instance.requests[6].body), { visibility: 'visible', version: 4 })
  await respond(instance.requests[6], 200, success({}))
  await retried
  assert.equal(instance.requests[7].url, `${auth.API_BASE}/api/comments/9`)
  await respond(instance.requests[7], 200, success({ ...comment, visibility: 'visible', can_restore: false, version: 5 }))
  await respond(instance.requests[8], 404, { success: false, error_code: 'E0004' })
  assert.equal(instance.page.commentData.value.visibility, 'visible')
  assert.equal(instance.page.canEdit.value, false)
  assert.equal(instance.page.conflict.value, false)
})

// PUT 等待中不可重讀／重驗；送出前的舊父文章回應和離頁後的成功均由 epoch 擋住。
test('read retries, reauthentication and late responses cannot interrupt or replace pending PUT', async (t) => {
  const instance = await resourcePage(t, '/vue/comments/9/edit')
  instance.page.commentBody.value = '等待中的草稿'
  const previous = instance.page.loadRelated()
  const oldParent = instance.requests[3]
  const write = instance.page.submitEdit()
  // 讀取父文章時編修資格未確認，不會送出；等本次公開確認後才能開始 PUT。
  assert.equal(instance.requests.length, 4)
  await respond(oldParent, 200, success(article))
  await previous
  await write
  const stale = instance.page.loadRelated()
  const staleParent = instance.requests[4]
  await respond(staleParent, 200, success(article))
  await stale
  const put = instance.page.submitEdit()
  const request = instance.requests[5]
  await instance.page.loadContent()
  await instance.page.loadRelated()
  await instance.page.verifyAccount()
  await instance.page.submitEdit()
  assert.equal(instance.requests.length, 6)
  assert.equal(request.signal.aborted, false)
  assert.equal(instance.page.contentPending.value, true)
  await respond(request, 200, success({}))
  await put
  await respond(instance.requests[6], 200, success({ ...comment, body: '等待中的草稿', version: 2 }))
  const pendingParent = instance.requests[7]
  const verify = instance.page.verifyAccount()
  assert.equal(pendingParent.signal.aborted, true)
  await respond(pendingParent, 401, { success: false, error_code: 'E0002' })
  assert.equal(instance.page.reLogin.value, false)
  await respond(instance.requests[8], 200, success(member))
  await verify
  await respond(instance.requests[9], 200, success({ ...comment, body: '等待中的草稿', version: 2 }))
  await respond(instance.requests[10], 200, success(article))
  const lateWrite = instance.page.submitEdit()
  instance.page.onPageHide()
  await respond(instance.requests[11], 200, success({}))
  await lateWrite
  assert.equal(instance.requests.length, 12)
  assert.equal(instance.page.editVersion.value, 2)
})

// 403／404 撤下過期權限但保留原稿與 version，父文章隱藏後不得再送留言內文。
test('write permission failures remove stale content while preserving draft and version', async (t) => {
  for (const [status, code] of [[403, 'E0003'], [404, 'E0004']]) {
    const instance = await resourcePage(t, '/vue/comments/9/edit')
    instance.page.commentBody.value = '不得遺失的草稿'
    const write = instance.page.submitEdit()
    await respond(instance.requests[3], status, { success: false, error_code: code, data: { body: '拒絕資料不可洩漏' } })
    await write
    assert.equal(instance.page.commentData.value, null)
    assert.equal(instance.page.parentArticle.value, null)
    assert.equal(instance.page.canEdit.value, false)
    assert.equal(instance.page.commentBody.value, '不得遺失的草稿')
    assert.equal(instance.page.editVersion.value, 1)
    assert.doesNotMatch(await renderedPage(instance.page), /拒絕資料不可洩漏/)
    const read = instance.page.loadContent()
    await respond(instance.requests[4], 200, success(comment))
    await read
    await respond(instance.requests[5], 200, success({ ...article, visibility: 'hidden' }))
    await instance.page.submitEdit()
    assert.equal(instance.requests.length, 6)
    assert.equal(instance.page.commentBody.value, '不得遺失的草稿')
  }
})

// 401 同帳號重登仍保留草稿／舊版本；不同帳號清除文字、版本與比較，不自動重送。
test('same-page relogin preserves only the same account editor draft and conflict version', async (t) => {
  for (const same of [true, false]) {
    const instance = await resourcePage(t, '/vue/articles/7/edit')
    instance.page.articleBody.value = '私有草稿'
    const write = instance.page.submitEdit()
    await respond(instance.requests[2], 409, { success: false, error_code: 'E0201' })
    await write
    const verification = instance.page.verifyAccount()
    await respond(instance.requests[3], 401, { success: false, error_code: 'E0002' })
    await verification
    assert.equal(instance.page.articleData.value, null)
    assert.equal(instance.page.articleBody.value, '私有草稿')
    assert.equal(instance.page.editVersion.value, 1)
    assert.doesNotMatch(await renderedPage(instance.page), /私有草稿/)
    instance.page.email.value = same ? member.email : 'another@example.test'
    instance.page.password.value = 'abc123'
    await vue.nextTick()
    const login = instance.page.submitLogin()
    const identity = same ? member : { ...member, id: 3, email: 'another@example.test' }
    await respond(instance.requests[4], 200, success({ access_token: 'fresh-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: identity }))
    await login
    assert.equal(instance.requests[5].url, `${auth.API_BASE}/api/articles/7`)
    await respond(instance.requests[5], 200, success({ ...article, version: 2 }))
    if (same) {
      assert.equal(instance.page.articleBody.value, '私有草稿')
      assert.equal(instance.page.editVersion.value, 1)
      assert.equal(instance.page.conflict.value, true)
      assert.equal(instance.page.comparison.value.version, 2)
    } else {
      assert.equal(instance.page.articleBody.value, '')
      assert.equal(instance.page.editVersion.value, null)
      assert.equal(instance.page.conflict.value, false)
      assert.equal(instance.page.comparison.value, null)
      assert.equal(instance.page.canEdit.value, false)
    }
    assert.equal(instance.requests.filter(
      // 重登只讀取，不能代替使用者送出任何保留草稿。
      (request) => request.method === 'PUT',
    ).length, 1)
  }
})

// 未知結果提示獨立於目前資源和父文章；404／5xx／重登入仍可依原 ID 手動查證。
test('unknown PUT result guidance survives resource, parent and authentication failures without retry', async (t) => {
  const instance = await resourcePage(t, '/vue/comments/9/edit')
  instance.page.commentBody.value = '待查證草稿'
  const write = instance.page.submitEdit()
  instance.requests[3].reject(new Error('connection interrupted'))
  await write
  assert.equal(instance.page.lifecycleUnknown.value, true)
  assert.equal(instance.page.commentBody.value, '待查證草稿')
  for (const status of [404, 500]) {
    const read = instance.page.loadContent()
    await respond(instance.requests.at(-1), status, { success: false, error_code: status === 404 ? 'E0004' : 'E9999' })
    await read
    const html = await renderedPage(instance.page)
    assert.match(html, /更新結果尚未確認/)
    assert.match(html, /href="\/vue\/comments\/9"[^>]*>依原 ID 開啟詳情查證/)
    assert.doesNotMatch(html, /<textarea/)
  }
  const recovery = instance.page.loadContent()
  await respond(instance.requests.at(-1), 200, success(comment))
  await recovery
  await respond(instance.requests.at(-1), 404, { success: false, error_code: 'E0004' })
  assert.match(await renderedPage(instance.page), /更新結果尚未確認/)
  assert.equal(instance.page.canEdit.value, false)
  const verify = instance.page.verifyAccount()
  await respond(instance.requests.at(-1), 401, { success: false, error_code: 'E0002' })
  await verify
  assert.match(await renderedPage(instance.page), /更新結果尚未確認/)
  assert.equal(instance.page.commentBody.value, '待查證草稿')
  assert.equal(instance.requests.filter(
    // 未知結果的所有查證流程皆不能重送 PUT。
    (request) => request.method === 'PUT',
  ).length, 1)
})

// 停用停止授權流程；422 與未知 200／5xx 保留輸入，不能誤報已更新。
test('edit failure contracts distinguish validation, disabled account and unknown writes', async (t) => {
  for (const [status, body, unknown] of [[422, { success: false, error_code: 'E0001', errors: { body: ['欄位格式或範圍不正確'], version: ['版本資料不正確'] } }, false], [403, { success: false, error_code: 'E0102' }, false], [500, { success: false, error_code: 'E9999' }, true], [200, success({ id: 9 }), true]]) {
    const instance = await resourcePage(t, '/vue/articles/7/edit')
    instance.page.articleBody.value = '保存草稿'
    const write = instance.page.submitEdit()
    await respond(instance.requests[2], status, body)
    await write
    assert.equal(instance.page.articleBody.value, '保存草稿')
    assert.equal(instance.page.editVersion.value, 1)
    assert.equal(instance.page.lifecycleUnknown.value, unknown)
    assert.equal(instance.requests.length, 3)
    assert.equal(instance.page.contentPending.value, false)
    if (status === 422) {
      assert.deepEqual(Array.from(instance.page.errors.value.body), ['欄位格式或範圍不正確'])
      assert.match(instance.page.summary.value, /版本資料不正確/)
    }
    if (status === 403) {
      assert.equal(instance.page.disabledAccount.value, true)
      assert.equal(instance.page.reLogin.value, false)
      await instance.page.verifyAccount()
      await instance.page.submitLogin()
      await instance.page.submitEdit()
      assert.equal(instance.requests.length, 3)
      assert.doesNotMatch(await renderedPage(instance.page), /<textarea/)
    }
  }
})

// PUT 本身回 401 時沿原頁重登，同帳號仍使用原版，不能因重讀自動接受較新內容。
test('PUT 401 preserves raw same-account editor text and only resumes manual submission', async (t) => {
  const instance = await resourcePage(t, '/vue/comments/9/edit')
  instance.page.commentBody.value = '同帳號\r\n私有草稿'
  const write = instance.page.submitEdit()
  await respond(instance.requests[3], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(instance.page.reLogin.value, true)
  assert.equal(instance.page.commentData.value, null)
  assert.equal(instance.page.commentBody.value, '同帳號\r\n私有草稿')
  instance.page.email.value = member.email
  instance.page.password.value = 'abc123'
  await vue.nextTick()
  const login = instance.page.submitLogin()
  await respond(instance.requests[4], 200, success({ access_token: 'fresh-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: member }))
  await login
  await respond(instance.requests[5], 200, success({ ...comment, version: 2 }))
  await respond(instance.requests[6], 200, success(article))
  assert.equal(instance.page.editVersion.value, 1)
  assert.equal(instance.page.commentBody.value, '同帳號\r\n私有草稿')
  assert.equal(instance.requests.length, 7)
  const manual = instance.page.submitEdit()
  assert.deepEqual(JSON.parse(instance.requests[7].body), { body: '同帳號\r\n私有草稿', version: 1 })
  await respond(instance.requests[7], 409, { success: false, error_code: 'E0201' })
  await manual
  assert.equal(instance.page.conflict.value, true)
  assert.equal(instance.page.editVersion.value, 1)
})

// 留言建立結果不明後，即使文章下架成功並重讀，也不能清除留言的查證入口。
test('comment POST unknown remains independently actionable after visibility PUT success', async (t) => {
  const instance = await writableArticle(t)
  instance.page.commentBody.value = '尚待查證的留言'
  const post = instance.page.submitComment()
  instance.requests[3].reject(new Error('connection interrupted'))
  await post
  assert.equal(instance.page.writeUnknown.value, true)
  instance.approve(true)
  const hide = instance.page.submitVisibility('hidden')
  assert.deepEqual(JSON.parse(instance.requests[4].body), { visibility: 'hidden', version: 1 })
  await respond(instance.requests[4], 200, success({}))
  await hide
  await respond(instance.requests[5], 200, success({ ...article, visibility: 'hidden', version: 2 }))
  await respond(instance.requests[6], 200, list([]))
  const html = await renderedPage(instance.page)
  assert.equal(instance.page.writeUnknown.value, true)
  assert.match(html, /留言結果尚未確認/)
  assert.match(html, /href="\/vue\/me\/comments"[^>]*>到我的留言查證<\/a>/)
  assert.equal(instance.page.commentBody.value, '尚待查證的留言')
  assert.equal(instance.requests.filter(
    // 下架後的回讀只能是 GET，不得重送先前結果不明的留言。
    (request) => request.method === 'POST' && request.url.endsWith('/comments'),
  ).length, 1)
})

// 文章下架結果不明後，留言成功或失敗都不能清除原文章的同 ID 查證入口。
test('visibility PUT unknown remains independently actionable after comment POST', async (t) => {
  for (const commentResult of ['success', 'unknown']) {
    const instance = await writableArticle(t)
    instance.approve(true)
    const hide = instance.page.submitVisibility('hidden')
    instance.requests[3].reject(new Error('connection interrupted'))
    await hide
    assert.equal(instance.page.lifecycleUnknown.value, true)
    instance.page.commentBody.value = '另一筆留言'
    const post = instance.page.submitComment()
    if (commentResult === 'success') await respond(instance.requests[4], 201, success({}))
    else instance.requests[4].reject(new Error('connection interrupted'))
    await post
    if (commentResult === 'success') await respond(instance.requests[5], 200, list([]))
    const html = await renderedPage(instance.page)
    assert.equal(instance.page.lifecycleUnknown.value, true)
    assert.match(html, /更新結果尚未確認/)
    assert.match(html, /href="\/vue\/articles\/7"[^>]*>依原 ID 開啟詳情查證<\/a>/)
    if (commentResult === 'unknown') {
      assert.equal(instance.page.writeUnknown.value, true)
      assert.match(html, /留言結果尚未確認/)
      assert.match(html, /href="\/vue\/me\/comments"[^>]*>到我的留言查證<\/a>/)
    }
    assert.equal(instance.requests.filter(
      // 後續留言僅由使用者送一次，不自動重送先前狀態 PUT。
      (request) => request.method === 'PUT',
    ).length, 1)
    t.after(instance.close)
  }
})
