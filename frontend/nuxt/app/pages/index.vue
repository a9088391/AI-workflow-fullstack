<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, backendName, positiveUrlInteger, readList } from '../utils/contracts.js'

useHead({ title: '所有文章｜文章集' })
const route = useRoute()
const site = useSite()
const data = ref(null)
const loading = ref(false)
const error = ref('')
const logId = ref('')
const page = ref(1)
let controller = null

// URL 只接受安全正整數頁碼，避免把錯誤參數帶到共同 API。
function requestedPage(value) {
  return positiveUrlInteger(value) ?? 1
}

// 每個列表頁都保留自己的可直接刷新 URL。
function pageHref(number) { return `/nuxt/?page=${number}` }

// 列表只節錄純文字，完整內容留給文章詳情。
function preview(body) { return [...body].slice(0, 135).join('') + ([...body].length > 135 ? '…' : '') }

// 手動重新讀取沒有 watch 清理函式，仍由本頁控制器取消舊請求。
function noCleanup() {}

// 驗證完成或頁碼改變後讀取公開文章；取消舊請求防止舊頁覆蓋新頁。
async function load(_values, _previous, onCleanup = noCleanup) {
  if (!site.ready.value || site.leaving.value) return
  controller?.abort()
  const request = new AbortController()
  controller = request
  onCleanup(
    // 路由或身分改變時先作廢舊回應。
    () => request.abort(),
  )
  const currentPage = requestedPage(route.query.page)
  page.value = currentPage
  data.value = null
  loading.value = true
  error.value = ''
  logId.value = ''
  try {
    const response = await apiRequest(`/api/articles?page=${currentPage}&per_page=20`, { token: site.token.value, signal: request.signal })
    if (request.signal.aborted) return
    const list = readList(response.status, response.body, currentPage)
    if (list) data.value = list
    else {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
      logId.value = failure.logId
    }
  } catch {
    if (!request.signal.aborted) error.value = '無法讀取文章；請檢查連線後手動重試。'
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 路由的頁碼變動須重新取得該頁資料。
function routePage() { return route.query.page }

watch(
  [routePage, site.ready, site.token, site.backend],
  load,
  { immediate: true },
)

// 同一分頁離開時立即作廢公開 GET，token 未變也不能讓舊文章列表回寫。
watch(site.leaving,
  // 離頁與完成回應若同時發生，先取消本頁控制器。
  () => controller?.abort(),
  { flush: 'sync' },
)

// 離開頁面時取消未完成的讀取。
onBeforeUnmount(
  // 避免取消後的結果更新別的文章頁。
  () => controller?.abort(),
)
</script>

<template>
  <main class="index-page">
    <div class="index-head"><div><p class="section-label">文字與日常</p><h1>所有文章</h1></div><p>每篇文字都有它自己的節奏。<br>選一篇，慢慢讀。</p></div>
    <div class="section-rule"><span>文章索引</span><span v-if="data">共 {{ data.pagination.total }} 篇</span></div>
    <p v-if="loading" class="state-block" role="status">正在載入文章…</p>
    <div v-else-if="error" class="state-block" role="alert"><p>{{ error }}</p><small v-if="logId">log_id：{{ logId }}</small><button type="button" class="inline-button" @click="load">重新讀取</button></div>
    <div v-else-if="data && data.list.length" class="article-grid">
      <article v-for="(article, index) in data.list" :key="article.id" class="article-teaser">
        <div class="teaser-number">{{ String((page - 1) * 20 + index + 1).padStart(2, '0') }}</div>
        <div class="teaser-copy"><p class="teaser-meta">{{ article.created_at }}　/　{{ backendName(article.created_backend) }}</p><h2><a :href="`/nuxt/articles/${article.id}`">{{ article.title }}</a></h2><p class="teaser-excerpt">{{ preview(article.body) }}</p><p class="teaser-author">文字／{{ article.author.nickname }}</p></div>
      </article>
    </div>
    <div v-else-if="data" class="state-block">
      <p>{{ data.pagination.total === 0 ? '目前還沒有公開文章。' : '此頁沒有文章，前面的頁面仍有內容。' }}</p>
      <a v-if="page > 1" href="/nuxt/">回第一頁</a>
    </div>
    <nav v-if="data && data.pagination.total_pages > 1" class="pagination" aria-label="文章分頁">
      <a v-if="page > 1" :href="pageHref(page - 1)">上一頁</a>
      <span>第 {{ page }} 頁／共 {{ data.pagination.total_pages }} 頁</span>
      <a v-if="page < data.pagination.total_pages" :href="pageHref(page + 1)">下一頁</a>
    </nav>
  </main>
</template>
