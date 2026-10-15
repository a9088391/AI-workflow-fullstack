import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { runInNewContext } from 'node:vm'
import { parse } from '@vue/compiler-sfc'
import * as vue from 'vue'
import * as contracts from '../utils/contracts.js'

const account = { id: 2, nickname: '作者', role: 'member' }
const stamp = '2026-09-24 10:00:00'
const article = { id: 1, title: '文章', body: '原文', author: { id: 2, nickname: '作者' }, version: 1, created_backend: 'laravel', created_at: stamp, updated_at: stamp, visibility: 'visible', can_restore: false }
const comment = { id: 5, article_id: 1, body: '舊留言', author: article.author, version: 1, created_backend: 'laravel', created_at: stamp, updated_at: stamp, visibility: 'visible', can_restore: false }

// 元件事件在頁面狀態測試中沒有父層接收者，故只保留可呼叫介面。
function ignoreEmit() {}

// 用真實 Vue ref/watch 執行頁面 script，HTTP 回應由測試控制以辨別操作先後。
function page(file, kind = 'article') {
  const requests = []
  const navigations = []
  const lifecycle = {}
  const site = { ready: vue.ref(true), account: vue.ref(account), token: vue.ref('jwt'), backend: vue.ref(contracts.API_BASE), leaving: vue.ref(false), dirty: vue.ref(false),
    // 伺服器確認憑證失效時撤銷身分，但原頁草稿由頁面自己決定是否保留。
    forget() { this.account.value = null; this.token.value = null },
  }
  const route = { path: '/nuxt/articles/1', params: { id: kind === 'article' ? '1' : '5' }, query: {} }
  const context = { ...vue, ...contracts, AbortController,
    // 本測試不建立瀏覽器 head，只檢查頁面執行流程。
    useHead() {},
    // 固定 URL 以驗證已知資源 ID 的請求與導覽。
    useRoute() { return route },
    // 讓頁面與 UI 共享目前驗證狀態。
    useSite() { return site },
    // 捕捉元件清理，避免跨測試保留 watcher。
    onBeforeUnmount(callback) { lifecycle.unmount = callback },
    // 每個請求保留獨立可完成 promise，包含已取消仍晚到的回應。
    apiRequest(path, options) { const response = Promise.withResolvers(); requests.push({ path, ...options, ...response }); return response.promise },
    window: { location: {
      // 捕捉成功後導覽，不讓測試離開受控頁面。
      assign(target) { navigations.push(target) },
    },
    // 本測試的狀態操作均允許確認，以驗證送往 API 的內容。
    confirm() { return true } },
    // 測試真實共用元件的 article/comment 類型分支。
    defineProps() { return { kind } },
    // 本測試無父元件事件接收者，保留元件宣告的 emit 邊界。
    defineEmits() { return ignoreEmit },
  }
  const source = readFileSync(new URL(file, import.meta.url), 'utf8')
  const script = parse(source).descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const names = ['title', 'body', 'article', 'comments', 'data', 'comment', 'commentBody', 'ownerId', 'commentOwner', 'message', 'loading', 'draftReady', 'pending', 'writePending', 'visibilityUnknown', 'visibilityError', 'unknown', 'uncertain', 'conflict', 'version', 'parent', 'parentError', 'canEdit', 'canChangeVisibility', 'resource', 'writeError', 'submit', 'submitComment', 'changeVisibility', 'load', 'loadParent', 'adoptLatest', 'checkedResult', 'checkedComment', 'refresh', 'refreshVisibility', 'restored', 'restoredComment']
  const scope = vue.effectScope()
  // 各頁只暴露自己宣告的狀態，缺少的名稱安全地讀作 undefined。
  function optionalBinding(name) { return `${name}: typeof ${name} === 'undefined' ? undefined : ${name}` }
  // 在 Vue effectScope 中執行 setup，讓 watcher 可於測試結束時完整清理。
  function evaluatePage() { return runInNewContext(`${script}\n;({ ${names.map(optionalBinding).join(', ')} })`, context) }
  const state = scope.run(evaluatePage)
  return { requests, navigations, site, state,
    // 每個測試結束時執行元件原清理並停止 Vue effect。
    close() { lifecycle.unmount?.(); scope.stop() },
  }
}

// 讓 Vue watch 與 async continuation 都走完一輪，觀察穩定的下一步請求。
async function settle() { await vue.nextTick(); await setImmediate(); await vue.nextTick() }

// 契約成功 envelope 不帶新增 ID，頁面只能使用已知導覽目的地。
function ok(data = {}, status = 200) { return { status, body: { success: true, message: 'OK', data } } }

// API 錯誤由狀態與錯誤碼決定恢復流程，不能只靠訊息字串。
function fail(status, code) { return { status, body: { success: false, error_code: code, message: '錯誤', log_id: 'test-log' } } }

// 只計算真正發出的指定寫入，確認未知結果沒有自動重送。
function countMethod(requests, method) {
  let count = 0
  for (const request of requests) if (request.method === method) count++
  return count
}

// 編修 GET 在登出時取消後，原頁草稿與重登入口必須可見；同帳號恢復須完成新 GET。
test('editor pending read logout clears loading and same-account read recovers draft', async (t) => {
  const app = page('../components/ContentEditor.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.state.body.value = '尚未儲存的內文'
  await settle()
  app.state.load()
  assert.equal(app.state.loading.value, true)
  app.site.forget()
  await settle()
  assert.equal(app.requests[1].signal.aborted, true)
  assert.equal(app.state.loading.value, false)
  assert.equal(app.state.draftReady.value, true)
  assert.equal(app.state.body.value, '尚未儲存的內文')
  app.requests[1].resolve(ok({ ...article, body: '晚到舊內容' }))
  await settle()
  assert.equal(app.state.body.value, '尚未儲存的內文')
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  assert.equal(app.requests[2].path, '/api/articles/1')
  app.requests[2].resolve(ok(article))
  await settle()
  assert.equal(app.state.loading.value, false)
  assert.equal(app.state.body.value, '尚未儲存的內文')
})

// 留言詳情登出時需終止舊載入畫面，重登後重新授權留言與父文章讀取。
test('comment detail pending read logout exposes login and reauth loads comment', async (t) => {
  const app = page('./comments/[id]/index.vue', 'comment')
  t.after(app.close)
  assert.equal(app.state.loading.value, true)
  app.site.forget()
  await settle()
  assert.equal(app.requests[0].signal.aborted, true)
  assert.equal(app.state.loading.value, false)
  assert.equal(app.site.account.value, null)
  app.requests[0].resolve(ok(comment))
  await settle()
  assert.equal(app.state.comment.value, null)
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  app.requests[1].resolve(ok(comment))
  await settle()
  app.requests[2].resolve(ok(article))
  await settle()
  assert.equal(app.state.loading.value, false)
  assert.equal(app.state.comment.value?.id, 5)
})

// 已送出的 POST 在登出後才回 201，仍須先查證；同帳號重登不可直接重複建立。
test('new article late publish after logout requires same-account readback before resend', async (t) => {
  const app = page('./articles/new.vue')
  t.after(app.close)
  app.state.title.value = '待查證標題'
  app.state.body.value = '待查證內文'
  await settle()
  app.state.submit()
  app.site.forget()
  app.requests[0].resolve(ok({}, 201))
  await settle()
  assert.equal(app.state.uncertain.value, true)
  assert.match(app.state.message.value, /發表結果尚未確認/)
  assert.equal(app.navigations.length, 0)
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  app.state.restored()
  assert.match(app.state.message.value, /發表結果尚未確認/)
  app.state.submit()
  assert.equal(app.requests.length, 1)
  app.state.checkedResult()
  app.state.submit()
  assert.equal(app.requests.length, 2)
})

// 舊帳號的在途 POST 不得讓新帳號背負舊草稿或未知結果。
test('different account rejects late old article publish state', async (t) => {
  const app = page('./articles/new.vue')
  t.after(app.close)
  app.state.title.value = '舊帳號標題'
  app.state.body.value = '舊帳號內文'
  await settle()
  app.state.submit()
  app.site.account.value = { ...account, id: 3 }
  app.site.token.value = 'other-jwt'
  await settle()
  app.requests[0].resolve(ok({}, 201))
  await settle()
  assert.equal(app.state.body.value, '')
  assert.equal(app.state.uncertain.value, false)
  assert.equal(app.navigations.length, 0)
})

// 換後端會捨棄來源草稿；晚到舊後端回應不可產生新來源的未知寫入提示。
test('backend switch discards pending old article publish without carrying uncertainty', async (t) => {
  const app = page('./articles/new.vue')
  t.after(app.close)
  app.state.title.value = '舊後端標題'
  app.state.body.value = '舊後端內文'
  await settle()
  app.state.submit()
  app.site.backend.value = contracts.BACKENDS.symfony
  await settle()
  assert.equal(app.requests[0].signal.aborted, true)
  app.requests[0].resolve(ok({}, 201))
  await settle()
  assert.equal(app.state.body.value, '')
  assert.equal(app.state.uncertain.value, false)
  assert.deepEqual(app.navigations, [])
})

// 留言 POST 已送出後登出，晚到 201 不能在同帳號恢復時成為第二次自動送出。
test('article comment late publish after logout stays uncertain until manual check', async (t) => {
  const app = page('./articles/[id]/index.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  app.state.commentBody.value = '待查證留言'
  await settle()
  app.state.submitComment()
  assert.equal(app.requests[2].path, '/api/articles/1/comments')
  app.site.forget()
  await settle()
  app.requests[2].resolve(ok({}, 201))
  await settle()
  assert.equal(app.state.uncertain.value, true)
  assert.match(app.state.writeError.value, /留言結果尚未確認/)
  assert.equal(app.state.commentBody.value, '待查證留言')
  assert.equal(app.navigations.length, 0)
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  app.state.restoredComment()
  assert.match(app.state.writeError.value, /留言結果尚未確認/)
  const articleRead = app.requests.at(-1)
  assert.equal(articleRead.path, '/api/articles/1')
  articleRead.resolve(ok(article))
  await settle()
  app.requests.at(-1).resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  app.state.submitComment()
  assert.equal(countMethod(app.requests, 'POST'), 1)
  app.state.checkedComment()
  app.state.submitComment()
  assert.equal(countMethod(app.requests, 'POST'), 2)
})

// 已知 401 是後端明確拒絕，保留草稿重登但不誤報為可能已建立。
test('known comment POST 401 keeps draft without unknown-result guard', async (t) => {
  const app = page('./articles/[id]/index.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  app.state.commentBody.value = '401 留言草稿'
  await settle()
  app.state.submitComment()
  app.requests[2].resolve(fail(401, 'E0002'))
  await settle()
  assert.equal(app.state.uncertain.value, false)
  assert.equal(app.state.commentBody.value, '401 留言草稿')
  assert.equal(app.site.account.value, null)
  assert.equal(countMethod(app.requests, 'POST'), 1)
})

// 舊 POST 的明確驗證拒絕代表未建立；同帳號新 token 不應被舊回應撤銷或卡在未知結果。
test('late article and comment POST auth rejection preserves drafts and fresh login', async (t) => {
  const newPage = page('./articles/new.vue')
  const articlePage = page('./articles/[id]/index.vue')
  t.after(newPage.close)
  t.after(articlePage.close)
  articlePage.requests[0].resolve(ok(article))
  await settle()
  articlePage.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  newPage.state.title.value = '舊請求標題'
  newPage.state.body.value = '舊請求內文'
  articlePage.state.commentBody.value = '舊請求留言'
  await settle()
  newPage.state.submit()
  articlePage.state.submitComment()
  newPage.site.forget()
  articlePage.site.forget()
  await settle()
  newPage.site.account.value = account
  newPage.site.token.value = 'fresh-jwt'
  articlePage.site.account.value = account
  articlePage.site.token.value = 'fresh-jwt'
  await settle()
  newPage.requests[0].resolve(fail(401, 'E0002'))
  articlePage.requests[2].resolve(fail(403, 'E0102'))
  await settle()
  assert.equal(newPage.state.uncertain.value, false)
  assert.equal(articlePage.state.uncertain.value, false)
  assert.equal(newPage.state.body.value, '舊請求內文')
  assert.equal(articlePage.state.commentBody.value, '舊請求留言')
  assert.equal(newPage.site.token.value, 'fresh-jwt')
  assert.equal(articlePage.site.token.value, 'fresh-jwt')
  assert.doesNotMatch(newPage.state.message.value, /結果尚未確認/)
  assert.doesNotMatch(articlePage.state.writeError.value, /結果尚未確認/)
  assert.deepEqual(newPage.navigations, [])
  assert.deepEqual(articlePage.navigations, [])
  // 舊 POST 已確定未成功，新文章由目前登入者自行按下送出才有第二次請求。
  newPage.state.submit()
  assert.equal(countMethod(newPage.requests, 'POST'), 2)
  assert.equal(newPage.requests[1].token, 'fresh-jwt')
})

// 編修 PUT 晚到時不可視為成功導覽；重登後需先重讀比較並自行解除未知結果。
test('editor late update after logout preserves draft and blocks resubmit until check', async (t) => {
  const app = page('../components/ContentEditor.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.state.body.value = '新的內文'
  await settle()
  app.state.submit()
  assert.equal(app.requests[1].method, 'PUT')
  app.site.forget()
  await settle()
  app.requests[1].resolve(ok())
  await settle()
  assert.equal(app.state.uncertain.value, true)
  assert.match(app.state.writeError.value, /修改結果尚未確認/)
  assert.equal(app.state.body.value, '新的內文')
  assert.deepEqual(app.navigations, [])
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  app.requests[2].resolve(ok(article))
  await settle()
  app.state.submit()
  assert.equal(countMethod(app.requests, 'PUT'), 1)
  app.state.checkedResult()
  app.state.submit()
  assert.equal(countMethod(app.requests, 'PUT'), 2)
})

// 已知編修 401 表示未獲授權，草稿留下供重登，不需猜測 PUT 是否成功。
test('known editor PUT 401 keeps draft without unknown-result guard', async (t) => {
  const app = page('../components/ContentEditor.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.state.body.value = '401 編修草稿'
  await settle()
  app.state.submit()
  app.requests[1].resolve(fail(401, 'E0002'))
  await settle()
  assert.equal(app.state.uncertain.value, false)
  assert.equal(app.state.body.value, '401 編修草稿')
  assert.equal(app.site.account.value, null)
  assert.equal(countMethod(app.requests, 'PUT'), 1)
})

// 舊編修 PUT 的明確 401 不需查證寫入；重登同帳號後保留草稿供人工再送。
test('late editor PUT 401 keeps draft and does not revoke fresh token', async (t) => {
  const app = page('../components/ContentEditor.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.state.body.value = '待重新送出的內文'
  await settle()
  app.state.submit()
  app.site.forget()
  await settle()
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  app.requests[1].resolve(fail(401, 'E0002'))
  await settle()
  assert.equal(app.state.uncertain.value, false)
  assert.equal(app.state.body.value, '待重新送出的內文')
  assert.equal(app.site.token.value, 'fresh-jwt')
  assert.doesNotMatch(app.state.writeError.value, /結果尚未確認/)
  assert.deepEqual(app.navigations, [])
  // 重登入後的 GET 確認目前版本，再由使用者手動提交保存的編修草稿。
  app.requests[2].resolve(ok(article))
  await settle()
  app.state.submit()
  assert.equal(countMethod(app.requests, 'PUT'), 2)
  assert.equal(app.requests[3].token, 'fresh-jwt')
})

// 文章狀態 PUT 的舊結果在登出後不可被忽略，重登重讀仍保留查證門禁。
test('article visibility late update after logout needs readback before another PUT', async (t) => {
  const app = page('./articles/[id]/index.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  app.state.changeVisibility()
  assert.equal(app.requests[2].path, '/api/articles/1/visibility')
  app.site.forget()
  await settle()
  app.requests[2].resolve(ok())
  await settle()
  assert.equal(app.state.visibilityUnknown.value, true)
  assert.match(app.state.visibilityError.value, /結果尚未確認/)
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  app.requests.at(-1).resolve(ok(article))
  await settle()
  app.requests.at(-1).resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  app.state.changeVisibility()
  assert.equal(countMethod(app.requests, 'PUT'), 1)
})

// 留言可見性晚到的 PUT 也須保留未知結果，不讓重登直接再送相反狀態。
test('comment visibility late update after logout needs readback before another PUT', async (t) => {
  const app = page('./comments/[id]/index.vue', 'comment')
  t.after(app.close)
  app.requests[0].resolve(ok(comment))
  await settle()
  app.requests[1].resolve(ok(article))
  await settle()
  app.state.changeVisibility()
  assert.equal(app.requests[2].path, '/api/comments/5/visibility')
  app.site.forget()
  await settle()
  app.requests[2].resolve(ok())
  await settle()
  assert.equal(app.state.unknown.value, true)
  assert.match(app.state.writeError.value, /結果尚未確認/)
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  app.requests.at(-1).resolve(ok(comment))
  await settle()
  app.requests.at(-1).resolve(ok(article))
  await settle()
  app.state.changeVisibility()
  assert.equal(countMethod(app.requests, 'PUT'), 1)
})

// 他人登入時不得接收前一帳號的狀態查證提示，兩種詳情頁都要清除舊門禁。
test('different account does not inherit old visibility uncertainty', async (t) => {
  const articlePage = page('./articles/[id]/index.vue')
  const commentPage = page('./comments/[id]/index.vue', 'comment')
  t.after(articlePage.close)
  t.after(commentPage.close)
  articlePage.requests[0].resolve(ok(article))
  commentPage.requests[0].resolve(ok(comment))
  await settle()
  articlePage.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  commentPage.requests[1].resolve(ok(article))
  await settle()
  articlePage.state.changeVisibility()
  commentPage.state.changeVisibility()
  articlePage.site.forget()
  commentPage.site.forget()
  await settle()
  articlePage.requests[2].resolve(ok())
  commentPage.requests[2].resolve(ok())
  await settle()
  assert.equal(articlePage.state.visibilityUnknown.value, true)
  assert.equal(commentPage.state.unknown.value, true)
  articlePage.site.account.value = { ...account, id: 3 }
  articlePage.site.token.value = 'other-jwt'
  commentPage.site.account.value = { ...account, id: 3 }
  commentPage.site.token.value = 'other-jwt'
  await settle()
  assert.equal(articlePage.state.visibilityUnknown.value, false)
  assert.equal(articlePage.state.visibilityError.value, '')
  assert.equal(commentPage.state.unknown.value, false)
  assert.equal(commentPage.state.writeError.value, '')
})

// 文章與留言可見性 PUT 的明確 401 保留登入入口，但不能誤列未知結果。
test('known visibility PUT 401 does not claim an unknown update', async (t) => {
  const articlePage = page('./articles/[id]/index.vue')
  const commentPage = page('./comments/[id]/index.vue', 'comment')
  t.after(articlePage.close)
  t.after(commentPage.close)
  articlePage.requests[0].resolve(ok(article))
  commentPage.requests[0].resolve(ok(comment))
  await settle()
  articlePage.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  commentPage.requests[1].resolve(ok(article))
  await settle()
  articlePage.state.changeVisibility()
  commentPage.state.changeVisibility()
  articlePage.requests[2].resolve(fail(401, 'E0002'))
  commentPage.requests[2].resolve(fail(401, 'E0002'))
  await settle()
  assert.equal(articlePage.state.visibilityUnknown.value, false)
  assert.equal(commentPage.state.unknown.value, false)
  assert.equal(articlePage.site.account.value, null)
  assert.equal(commentPage.site.account.value, null)
})

// 兩種可見性 PUT 的晚到驗證拒絕均未改動資料，且不得撤銷重登後的新憑證。
test('late visibility PUT auth rejection does not require unknown-result readback', async (t) => {
  const articlePage = page('./articles/[id]/index.vue')
  const commentPage = page('./comments/[id]/index.vue', 'comment')
  t.after(articlePage.close)
  t.after(commentPage.close)
  articlePage.requests[0].resolve(ok(article))
  commentPage.requests[0].resolve(ok(comment))
  await settle()
  articlePage.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  commentPage.requests[1].resolve(ok(article))
  await settle()
  articlePage.state.changeVisibility()
  commentPage.state.changeVisibility()
  articlePage.site.forget()
  commentPage.site.forget()
  await settle()
  articlePage.site.account.value = account
  articlePage.site.token.value = 'fresh-jwt'
  commentPage.site.account.value = account
  commentPage.site.token.value = 'fresh-jwt'
  await settle()
  articlePage.requests[2].resolve(fail(401, 'E0002'))
  commentPage.requests[2].resolve(fail(403, 'E0102'))
  await settle()
  assert.equal(articlePage.state.visibilityUnknown.value, false)
  assert.equal(commentPage.state.unknown.value, false)
  assert.equal(articlePage.site.token.value, 'fresh-jwt')
  assert.equal(commentPage.site.token.value, 'fresh-jwt')
  assert.doesNotMatch(articlePage.state.visibilityError.value, /結果尚未確認/)
  assert.doesNotMatch(commentPage.state.writeError.value, /結果尚未確認/)
  // 新憑證重讀後，舊 401 不再阻擋作者自行決定是否再次下架。
  const freshArticleRead = articlePage.requests.at(-1)
  assert.equal(freshArticleRead.path, '/api/articles/1')
  freshArticleRead.resolve(ok(article))
  await settle()
  const freshCommentsRead = articlePage.requests.at(-1)
  assert.equal(freshCommentsRead.path, '/api/articles/1/comments?page=1&per_page=20')
  freshCommentsRead.resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  assert.equal(articlePage.state.article.value?.id, 1)
  assert.equal(articlePage.state.canChangeVisibility.value, true)
  articlePage.state.changeVisibility()
  assert.equal(countMethod(articlePage.requests, 'PUT'), 2)
  assert.equal(articlePage.requests.at(-1).token, 'fresh-jwt')
})

// 新文章只有實際輸入文字才算未儲存；手動清空兩欄後不得再要求捨棄。
test('new article dirty state follows entering and clearing its draft', async (t) => {
  const app = page('./articles/new.vue')
  t.after(app.close)
  app.state.title.value = '暫存標題'
  await settle()
  assert.equal(app.site.dirty.value, true)
  app.state.title.value = ''
  await settle()
  assert.equal(app.site.dirty.value, false)
  app.state.body.value = '暫存內文'
  await settle()
  assert.equal(app.site.dirty.value, true)
  app.state.body.value = ''
  await settle()
  assert.equal(app.site.dirty.value, false)
})

// 發文的 401 留原頁與文字；同帳號恢復後仍需要使用者再次按送出。
test('new article preserves same-account draft on 401 and never auto-resubmits', async (t) => {
  const app = page('./articles/new.vue')
  t.after(app.close)
  app.state.title.value = '草稿標題'
  app.state.body.value = '草稿內文'
  await settle()
  assert.equal(app.site.dirty.value, true)
  app.state.submit()
  assert.equal(app.requests[0].path, '/api/articles')
  app.requests[0].resolve(fail(401, 'E0002'))
  await settle()
  assert.equal(app.state.body.value, '草稿內文')
  assert.equal(app.state.uncertain.value, false)
  assert.equal(app.site.account.value, null)
  assert.equal(app.requests.length, 1)
  app.site.account.value = account
  app.site.token.value = 'fresh-jwt'
  await settle()
  assert.equal(app.requests.length, 1)
  app.state.submit()
  assert.equal(app.requests[1].token, 'fresh-jwt')
  app.requests[1].resolve(ok({}, 201))
  await settle()
  assert.equal(app.site.dirty.value, false)
  assert.equal(app.navigations[0], '/nuxt/me/articles?published=1')
})

// 結果不明阻止連點重送；切換成另一帳號即清空前一帳號草稿。
test('new article unknown write needs check and different account clears draft', async (t) => {
  const app = page('./articles/new.vue')
  t.after(app.close)
  app.state.title.value = '草稿'
  app.state.body.value = '內文'
  await settle()
  assert.equal(app.site.dirty.value, true)
  app.state.submit()
  app.requests[0].reject(new Error('connection lost'))
  await settle()
  assert.equal(app.state.uncertain.value, true)
  app.state.submit()
  assert.equal(app.requests.length, 1)
  app.site.account.value = { ...account, id: 3 }
  await settle()
  assert.equal(app.state.title.value, '')
  assert.equal(app.state.body.value, '')
  assert.equal(app.site.dirty.value, false)
})

// 初始後端還原沒有文字時不宣稱丟失草稿；真正跨後端草稿仍須清除並提示。
test('backend restore only reports discarded article and comment text when text existed', async (t) => {
  const articlePage = page('./articles/new.vue')
  const detailPage = page('./articles/[id]/index.vue')
  t.after(articlePage.close)
  t.after(detailPage.close)
  articlePage.site.backend.value = contracts.BACKENDS.symfony
  detailPage.site.backend.value = contracts.BACKENDS.symfony
  await settle()
  assert.equal(articlePage.state.message.value, '')
  assert.equal(detailPage.state.writeError.value, '')
  articlePage.state.title.value = '待發表標題'
  detailPage.state.commentBody.value = '待發表留言'
  await settle()
  articlePage.site.backend.value = contracts.BACKENDS.django
  detailPage.site.backend.value = contracts.BACKENDS.django
  await settle()
  assert.equal(articlePage.state.title.value, '')
  assert.equal(detailPage.state.commentBody.value, '')
  assert.match(articlePage.state.message.value, /已清除上一個帳號的文字/)
  assert.match(detailPage.state.writeError.value, /已清除上一個帳號的文字/)
})

// 改由另一帳號登入時草稿仍須隔離；空白表單不顯示虛假的清除通知。
test('different account clears existing author text without warning for empty forms', async (t) => {
  const filled = page('./articles/new.vue')
  const filledComment = page('./articles/[id]/index.vue')
  const empty = page('./articles/[id]/index.vue')
  t.after(filled.close)
  t.after(filledComment.close)
  t.after(empty.close)
  filled.state.body.value = '舊帳號內文'
  filledComment.state.commentBody.value = '舊帳號留言'
  await settle()
  filled.site.account.value = { ...account, id: 3 }
  filledComment.site.account.value = { ...account, id: 3 }
  empty.site.account.value = { ...account, id: 3 }
  await settle()
  assert.equal(filled.state.body.value, '')
  assert.match(filled.state.message.value, /已清除上一個帳號的文字/)
  assert.equal(filledComment.state.commentBody.value, '')
  assert.match(filledComment.state.writeError.value, /已清除上一個帳號的文字/)
  assert.equal(empty.state.writeError.value, '')
})

// 父文章 404 只封鎖留言內文編修；獨立重試確認公開後才可送出。
test('comment editor retries parent independently and compares 409 without overwriting draft', async (t) => {
  const app = page('../components/ContentEditor.vue', 'comment')
  t.after(app.close)
  assert.equal(app.requests[0].path, '/api/comments/5')
  app.requests[0].resolve(ok(comment))
  await settle()
  assert.equal(app.requests[1].path, '/api/articles/1')
  app.requests[1].resolve(fail(404, 'E0004'))
  await settle()
  assert.equal(app.state.parent.value, null)
  assert.equal(app.state.canEdit.value, false)
  app.state.body.value = '保留我的文字'
  app.state.submit()
  assert.equal(app.requests.length, 2)
  app.state.loadParent()
  app.requests[2].resolve(ok(article))
  await settle()
  assert.equal(app.state.canEdit.value, true)
  app.state.submit()
  assert.equal(app.requests[3].path, '/api/comments/5')
  assert.equal(app.requests[3].body.version, 1)
  app.requests[3].resolve(fail(409, 'E0201'))
  await settle()
  assert.equal(app.requests[4].path, '/api/comments/5')
  app.requests[4].resolve(ok({ ...comment, body: '別人的更新', version: 2 }))
  await settle()
  assert.equal(app.requests[5].path, '/api/articles/1')
  app.requests[5].resolve(ok(article))
  await settle()
  assert.equal(app.state.body.value, '保留我的文字')
  assert.equal(app.state.conflict.value, true)
  assert.equal(app.state.version.value, 1)
  app.state.adoptLatest()
  assert.equal(app.state.version.value, 2)
  assert.equal(app.requests.length, 6)
})

// 留言詳情即使父文不可讀仍保留自己的留言，且可獨立下架。
test('comment detail keeps readable comment and visibility when parent is hidden', async (t) => {
  const app = page('./comments/[id]/index.vue', 'comment')
  t.after(app.close)
  app.requests[0].resolve(ok(comment))
  await settle()
  app.requests[1].resolve(fail(404, 'E0004'))
  await settle()
  assert.equal(app.state.comment?.value?.body, '舊留言')
  assert.equal(app.state.parent.value, null)
  assert.equal(app.state.canChangeVisibility.value, true)
  app.state.changeVisibility()
  assert.equal(app.requests[2].path, '/api/comments/5/visibility')
  assert.equal(app.requests[2].body.visibility, 'hidden')
  app.requests[2].resolve(fail(409, 'E0201'))
  await settle()
  assert.equal(app.state.conflict.value, true)
  app.state.changeVisibility()
  assert.equal(app.requests.length, 3)
})

// 管理員可操作他人的可見性，但作者內文編修仍以實際 author.id 判定。
test('admin cannot edit another author comment body', async (t) => {
  const app = page('../components/ContentEditor.vue', 'comment')
  t.after(app.close)
  app.site.account.value = { ...account, id: 9, role: 'admin' }
  await settle()
  app.requests.at(-1).resolve(ok(comment))
  await settle()
  app.requests.at(-1).resolve(ok(article))
  await settle()
  assert.equal(app.state.canEdit.value, false)
  const count = app.requests.length
  app.state.submit()
  assert.equal(app.requests.length, count)
})

// 作者列表向目前後端讀取自己的資源，隱藏內容仍由 API 回應決定。
test('my articles request includes pagination and preserves hidden entries', async (t) => {
  const app = page('../components/AuthorList.vue', 'articles')
  t.after(app.close)
  assert.equal(app.requests[0].path, '/api/me/articles?page=1&per_page=20')
  app.requests[0].resolve(ok({ list: [{ ...article, visibility: 'hidden' }], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }))
  await settle()
  assert.equal(app.state.data.value.list[0].visibility, 'hidden')
})

// 新增留言成功只回文章留言第一頁；前端離開時晚到的 201 不得導覽。
test('article comment creation uses known article path and ignores late frontend response', async (t) => {
  const app = page('./articles/[id]/index.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  app.state.commentBody.value = '新留言'
  await settle()
  app.state.submitComment()
  assert.equal(app.requests[2].path, '/api/articles/1/comments')
  assert.equal(app.requests[2].body.body, '新留言')
  app.site.leaving.value = true
  assert.equal(app.requests[2].signal.aborted, true)
  app.requests[2].resolve(ok({}, 201))
  await settle()
  assert.equal(app.navigations.length, 0)
})

// 另一個狀態寫入成功不能證明先前留言已建立，未知結果仍要求人工查證。
test('independent visibility success does not clear unknown comment result', async (t) => {
  const app = page('./articles/[id]/index.vue')
  t.after(app.close)
  app.requests[0].resolve(ok(article))
  await settle()
  app.requests[1].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  app.state.commentBody.value = '待查證留言'
  await settle()
  app.state.submitComment()
  app.requests[2].reject(new Error('connection lost'))
  await settle()
  assert.equal(app.state.uncertain.value, true)
  app.state.changeVisibility()
  assert.equal(app.requests[3].path, '/api/articles/1/visibility')
  app.requests[3].resolve(ok())
  await settle()
  app.requests[4].resolve(ok({ ...article, visibility: 'hidden', version: 2 }))
  await settle()
  app.requests[5].resolve(ok({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }))
  await settle()
  assert.equal(app.state.uncertain.value, true)
  assert.equal(app.state.commentBody.value, '待查證留言')
})
