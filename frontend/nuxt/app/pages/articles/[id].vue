<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, backendName, loginHref, positiveUrlInteger, readArticle, readList } from '../../utils/contracts.js'

const route = useRoute()
const site = useSite()
const article = ref(null)
const comments = ref(null)
const loading = ref(false)
const error = ref('')
const logId = ref('')
const commentError = ref('')
const commentLogId = ref('')
const page = ref(1)
let controller = null

useHead({ title: '閱讀文章｜文章集' })

// 只接受安全正整數資源 ID，避免錯誤路徑被送到後端。
function articleId() {
  return positiveUrlInteger(route.params.id)
}

// 留言分頁採同一組嚴格頁碼規則，非法查詢回第一頁。
function commentPage() {
  return positiveUrlInteger(route.query.page) ?? 1
}

// 換頁仍保留目前文章 ID，能直接開啟或刷新。
function pageHref(number) { return `/nuxt/articles/${articleId()}?page=${number}#comments` }

// 手動重新讀取沒有 watch 清理函式，仍由本頁控制器取消舊請求。
function noCleanup() {}

// 文章先確認可讀才查留言；舊身分、ID 或頁碼的回應不得覆蓋新畫面。
async function load(_values, _previous, onCleanup = noCleanup) {
  if (!site.ready.value) return
  controller?.abort()
  const request = new AbortController()
  controller = request
  onCleanup(
    // 路由／身分改變時同時取消文章與後續留言請求。
    () => request.abort(),
  )
  // 清掉上一個 ID 的畫面與讀取狀態，無效 ID 可立即呈現錯誤。
  article.value = null
  comments.value = null
  error.value = ''
  commentError.value = ''
  logId.value = ''
  commentLogId.value = ''
  loading.value = false
  page.value = commentPage()
  const id = articleId()
  if (id === null) { error.value = '找不到指定資料或 API'; return }
  loading.value = true
  const token = site.token.value
  try {
    const response = await apiRequest(`/api/articles/${id}`, { token, signal: request.signal })
    if (request.signal.aborted) return
    const data = readArticle(response.status, response.body, id)
    if (!data) {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
      logId.value = failure.logId
      return
    }
    article.value = data
    // 留言失敗保留已驗證可讀的文章，讓使用者只重讀失敗部分。
    const responseComments = await apiRequest(`/api/articles/${id}/comments?page=${page.value}&per_page=20`, { token, signal: request.signal })
    if (request.signal.aborted) return
    const list = readList(responseComments.status, responseComments.body, page.value, 'comment', id)
    if (list) comments.value = list
    else {
      const failure = apiFailure(responseComments.status, responseComments.body)
      if (responseComments.status === 401 && failure.code === 'E0002' || responseComments.status === 403 && failure.code === 'E0102') { article.value = null; site.forget(failure.message) }
      else if (responseComments.status === 404 || responseComments.status === 403) { article.value = null; error.value = failure.message; logId.value = failure.logId }
      commentError.value = failure.message
      commentLogId.value = failure.logId
    }
  } catch {
    if (!request.signal.aborted) {
      if (article.value) commentError.value = '無法讀取留言；請檢查連線後手動重試。'
      else error.value = '無法讀取文章；請檢查連線後手動重試。'
    }
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 路由 ID 變更時需讀取另一篇文章。
function routeId() { return route.params.id }

// 留言頁碼變更時需讀取另一頁清單。
function routePage() { return route.query.page }

watch(
  [routeId, routePage, site.ready, site.token],
  load,
  { immediate: true },
)

// 深層頁離開時不讓在途回應回寫已銷毀畫面。
onBeforeUnmount(
  // 終止本頁目前讀取。
  () => controller?.abort(),
)
</script>

<template>
  <main class="reading-page">
    <a class="back-link" href="/nuxt/">← 所有文章</a>
    <p v-if="loading && !article" class="state-block" role="status">正在載入文章…</p>
    <div v-else-if="error" class="state-block" role="alert"><p>{{ error }}</p><small v-if="logId">log_id：{{ logId }}</small><div class="state-actions"><button type="button" class="inline-button" @click="load">重新讀取</button><a href="/nuxt/">返回文章列表</a><a v-if="error.includes('登入')" :href="loginHref(route.path)">重新登入</a></div></div>
    <template v-else-if="article">
      <article class="reading-article"><p class="section-label">{{ backendName(article.created_backend) }} / {{ article.created_at }}</p><h1>{{ article.title }}</h1><p class="reading-byline">文字／{{ article.author.nickname }}</p><div class="reading-rule" /><div class="article-body">{{ article.body }}</div></article>
      <section id="comments" class="comments-section" aria-labelledby="comments-title"><div class="comments-heading"><h2 id="comments-title">留言</h2><span v-if="comments">{{ comments.pagination.total }} 則</span></div>
        <p v-if="loading && !comments" class="state-block" role="status">正在載入留言…</p>
        <div v-else-if="commentError" class="state-block" role="alert"><p>{{ commentError }}</p><small v-if="commentLogId">log_id：{{ commentLogId }}</small><button type="button" class="inline-button" @click="load">重新讀取</button></div>
        <ol v-else-if="comments && comments.list.length" class="comment-list"><li v-for="comment in comments.list" :key="comment.id"><div class="comment-meta"><strong>{{ comment.author.nickname }}</strong><span>{{ comment.created_at }} / {{ backendName(comment.created_backend) }}</span></div><p>{{ comment.body }}</p></li></ol>
        <div v-else-if="comments" class="state-block"><p>{{ comments.pagination.total === 0 ? '目前還沒有留言。' : '此頁沒有留言，前面的頁面仍有內容。' }}</p><a v-if="page > 1" :href="pageHref(1)">回第一頁</a></div>
        <nav v-if="comments && comments.pagination.total_pages > 1" class="pagination" aria-label="留言分頁"><a v-if="page > 1" :href="pageHref(page - 1)">上一頁</a><span>第 {{ page }} 頁／共 {{ comments.pagination.total_pages }} 頁</span><a v-if="page < comments.pagination.total_pages" :href="pageHref(page + 1)">下一頁</a></nav>
      </section>
    </template>
  </main>
</template>
