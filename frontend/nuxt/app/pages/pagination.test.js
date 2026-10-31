import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import { parse } from '@vue/compiler-sfc'
import { renderToString } from '@vue/server-renderer'
import * as vue from 'vue'

// 編譯正式 Nuxt 頁面模板，直接檢查清單狀態與分頁導覽的可見文字。
async function renderList(file, state) {
  const source = readFileSync(new URL(file, import.meta.url), 'utf8')
  const render = vue.compile(parse(source).descriptor.template.content)
  const app = vue.createSSRApp({
    // 渲染使用案例提供的分頁狀態，避免夾具自行重作正式分支。
    setup() { return state }, render,
  })
  app.component('ReauthForm', { template: '<span />' })
  return renderToString(app)
}

// 零筆超頁仍是原第七頁；首頁零筆與有資料總數的超頁各保留原本語意。
test('public, article comments, author and admin lists render zero-total overpage', async () => {
  for (const [requested, total] of [[1, 0], [7, 0], [7, 41]]) {
    const pagination = { page: requested, per_page: 20, total, total_pages: Math.ceil(total / 20) }
    const data = { list: [], pagination }
    const publicHtml = await renderList('./index.vue', { data, page: requested, loading: false, error: '',
      // 導覽網址由案例固定，確認模板有提供可回第一頁的入口。
      pageHref: (number) => `/nuxt/?page=${number}` })
    assert.match(publicHtml, new RegExp(`共 ${total} 篇`))
    const authorHtml = await renderList('../components/AuthorList.vue', {
      kind: 'articles', site: { ready: { value: true }, account: { value: { id: 2 } } }, route: { path: '/nuxt/me/articles', query: {} },
      data, page: requested, loading: false, error: '',
      // 作者清單的分頁不應跳到公開清單。
      pageHref: (number) => `/nuxt/me/articles?page=${number}`,
    })
    const adminHtml = await renderList('../components/AdminList.vue', {
      kind: 'members', site: { ready: { value: true }, account: { value: { role: 'admin' } } }, route: { path: '/nuxt/admin/members', query: { keyword: 'missing' } },
      data, loading: false, error: '', keyword: 'missing', fieldError: '',
      // 會員搜尋沿用原頁碼與關鍵字，測試第一頁無符合結果的文案。
      requestedPage: () => requested,
      // 首頁的空搜尋結果仍應說明沒有符合條件的會員。
      searchedKeyword: () => 'missing',
      // 管理分頁返回入口仍指向相同列表。
      pageHref: (number) => `/nuxt/admin/members?page=${number}`,
      // 渲染表單時保留提交 callback；本案例不送出搜尋。
      submitSearch: () => {},
    })
    const commentHtml = await renderList('./articles/[id]/index.vue', {
      site: { account: { value: null } }, route: { path: '/nuxt/articles/1', query: {} }, article: { id: 1, title: '文章', body: '內文', author: { nickname: '作者' }, visibility: 'hidden', created_at: '今天', created_backend: 'laravel' },
      comments: data, page: requested, loading: false, error: '', commentError: '', visibilityError: '', commentOwner: null, writeError: '', uncertain: false,
      isAuthor: false, canChangeVisibility: false,
      // 來源標籤與分頁網址保持正式模板所需的函式形狀。
      backendName: () => 'Laravel',
      // 留言分頁返回同一文章的 comments 錨點。
      pageHref: (number) => `/nuxt/articles/1?page=${number}#comments`,
    })
    for (const html of [publicHtml, authorHtml, adminHtml, commentHtml]) {
      if (requested === 1) assert.doesNotMatch(html, /回第一頁/)
      else {
        assert.match(html, new RegExp(`第 ${requested} 頁／共 ${pagination.total_pages} 頁`), 'must keep original page and API total_pages')
        assert.match(html, /回第一頁/)
      }
    }
    if (requested === 1) {
      assert.match(publicHtml, /目前還沒有公開文章/)
      assert.match(authorHtml, /目前還沒有自己的內容/)
      assert.match(adminHtml, /沒有符合條件的會員/)
      assert.match(commentHtml, /目前還沒有留言/)
    } else if (total === 0) {
      assert.match(publicHtml, /此頁沒有文章。/)
      assert.match(authorHtml, /此頁沒有內容。/)
      assert.match(adminHtml, /此頁沒有資料。/)
      assert.match(commentHtml, /此頁沒有留言。/)
      for (const html of [publicHtml, authorHtml, adminHtml, commentHtml]) assert.doesNotMatch(html, /前面的頁面仍有/)
    } else {
      for (const html of [publicHtml, authorHtml, adminHtml, commentHtml]) assert.match(html, /前面的頁面仍有/)
    }
  }
})

// 操作回饋不依附成功讀取的資源；失權、讀取中與失敗時仍保留查證入口及原 PUT 編號。
test('write recovery remains rendered outside article comment and editor read branches', async () => {
  for (const loading of [true, false]) {
    const site = { ready: { value: true }, account: { value: null } }
    const route = { path: '/articles/1', query: {} }
    const shared = { site, route, loading, error: loading ? '' : '讀取失敗', logId: 'read-trace',
      // 僅驗模板的查證控制；實際 callback 與請求順序由 author 及瀏覽器案例覆蓋。
      load() {}, loginHref() { return '/nuxt/login' },
    }
    const articleHtml = await renderList('./articles/[id]/index.vue', {
      ...shared, article: null, commentOwner: null, fieldErrors: {}, writeError: '留言結果尚未確認', writeLogId: 'comment-write-trace', uncertain: true,
      visibilityError: '下架／恢復結果尚未確認', visibilityLogId: 'visibility-write-trace', visibilityPending: false,
      // 模板回饋只應連接各自操作，不能借另一項操作清除未知結果。
      refreshVisibility() {}, checkedComment() {},
    })
    assert.match(articleHtml, /留言結果尚未確認/)
    assert.match(articleHtml, /重讀留言列表/)
    assert.match(articleHtml, /comment-write-trace/)
    assert.match(articleHtml, /visibility-write-trace/)
    const commentHtml = await renderList('./comments/[id]/index.vue', {
      ...shared, comment: null, writeError: '下架／恢復結果尚未確認', writeLogId: 'comment-visibility-trace', pending: false,
      // 人工查證入口在沒有可讀留言時也必須存在。
      refresh() {},
    })
    assert.match(commentHtml, /下架／恢復結果尚未確認/)
    assert.match(commentHtml, /重讀留言確認/)
    assert.match(commentHtml, /comment-visibility-trace/)
    const editorHtml = await renderList('../components/ContentEditor.vue', {
      ...shared, kind: 'article', id: 1, resource: null, draftReady: true, draftOwner: 2, conflict: true,
      uncertain: true, writeError: '修改結果尚未確認', writeLogId: 'editor-write-trace',
      // 保留重登事件形狀，模板不應將草稿或比較內容帶出授權分支。
      restored() {}, resetForIdentity() {}, checkedResult() {},
    })
    assert.match(editorHtml, /修改結果尚未確認/)
    assert.match(editorHtml, /重讀詳情查證/)
    assert.match(editorHtml, /editor-write-trace/)
    assert.match(editorHtml, /重新讀取最新內容/)
    assert.doesNotMatch(editorHtml, /最新伺服器內容（版本/)
  }
})
