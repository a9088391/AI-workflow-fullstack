<script setup>
import { onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, backendName, loginHref, positiveUrlInteger, readList } from '../utils/contracts.js'

const props = defineProps({ kind: { type: String, required: true } })
const route = useRoute()
const site = useSite()
const data = ref(null)
const loading = ref(false)
const error = ref('')
const logId = ref('')
const page = ref(1)
let controller = null

// 只有 URL 中的合法頁碼會送到共同列表 API，其餘回到第一頁。
function requestedPage() { return positiveUrlInteger(route.query.page) ?? 1 }

// 列表分頁保留目前作者頁種類，但不攜帶別頁搜尋或草稿。
function pageHref(number) { return `/nuxt/me/${props.kind}?page=${number}` }

// 手動重讀沿用同一函式，缺省清理回呼不執行額外工作。
function noCleanup() {}

// 身分、後端或頁碼變更時重查自己的資料，取消並隔離較晚的舊回應。
async function load(_value, _old, onCleanup = noCleanup) {
  if (!site.ready.value || site.leaving.value) return
  controller?.abort()
  const request = new AbortController()
  controller = request
  onCleanup(
    // 下一輪讀取前先取消本輪請求。
    () => request.abort(),
  )
  data.value = null
  error.value = ''
  logId.value = ''
  page.value = requestedPage()
  if (!site.account.value || !site.token.value) return
  loading.value = true
  try {
    const response = await apiRequest(`/api/me/${props.kind}?page=${page.value}&per_page=20`, { token: site.token.value, signal: request.signal })
    if (request.signal.aborted || site.leaving.value) return
    const result = readList(response.status, response.body, page.value, props.kind === 'articles' ? 'article' : 'comment')
    if (result) data.value = result
    else {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
      logId.value = failure.logId
    }
  } catch {
    if (!request.signal.aborted) error.value = '無法讀取內容；請檢查連線後手動重試。'
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 路由查詢變更須重取對應頁碼，不能沿用前頁內容。
function routePage() { return route.query.page }

watch([routePage, site.ready, site.account, site.token, site.backend], load, { immediate: true })
watch(site.leaving,
  // 前端切換當下就作廢作者列表請求。
  () => controller?.abort(),
  { flush: 'sync' },
)
onBeforeUnmount(
  // 離開本頁時不讓晚到清單更新舊頁。
  () => controller?.abort(),
)
</script>

<template>
  <main class="reading-page author-page">
    <div class="author-heading"><div><p class="section-label">個人內容</p><h1>{{ kind === 'articles' ? '我的文章' : '我的留言' }}</h1></div><a v-if="kind === 'articles' && site.account.value" class="secondary-button" href="/nuxt/articles/new">發表文章</a></div>
    <p v-if="kind === 'articles' && route.query.published === '1' && site.account.value" class="form-success" role="status">文章已發表，以下依建立時間顯示我的文章。</p>
    <p v-if="!site.ready.value" class="state-block" role="status">正在確認登入狀態…</p>
    <div v-else-if="!site.account.value" class="state-block"><p>請先登入，查看自己的{{ kind === 'articles' ? '文章' : '留言' }}。</p><a :href="loginHref(route.path)">前往登入</a></div>
    <p v-else-if="loading" class="state-block" role="status">正在讀取內容…</p>
    <div v-else-if="error" class="state-block" role="alert"><p>{{ error }}</p><small v-if="logId">log_id：{{ logId }}</small><button type="button" class="inline-button" @click="load">重新讀取</button></div>
    <template v-else-if="data">
      <div class="section-rule"><span>{{ kind === 'articles' ? '文章索引' : '留言索引' }}</span><span>共 {{ data.pagination.total }} 則</span></div>
      <ol v-if="data.list.length" class="author-list"><li v-for="item in data.list" :key="item.id"><p class="teaser-meta">{{ item.created_at }}　/　{{ backendName(item.created_backend) }}　/　{{ item.visibility === 'hidden' ? '已下架' : '公開' }}</p><h2 v-if="kind === 'articles'"><a :href="`/nuxt/articles/${item.id}`">{{ item.title }}</a></h2><p v-else class="author-excerpt">{{ item.body }}</p><div class="author-actions"><a v-if="kind === 'articles'" :href="`/nuxt/articles/${item.id}/edit`">編輯文章</a><a v-else :href="`/nuxt/comments/${item.id}`">查看留言詳情</a></div></li></ol>
      <!-- total_pages=0 時仍用原頁碼分辨尚無內容與合法超頁。 -->
      <div v-else class="state-block"><p>{{ page > 1 && page > data.pagination.total_pages ? data.pagination.total === 0 ? '此頁沒有內容。' : '此頁沒有內容，前面的頁面仍有資料。' : '目前還沒有自己的內容。' }}</p><a v-if="page > 1" :href="pageHref(1)">回第一頁</a></div>
      <nav v-if="data.pagination.total_pages > 1 || page > 1" class="pagination" aria-label="我的內容分頁"><a v-if="page > 1" :href="pageHref(page - 1)">上一頁</a><span>第 {{ page }} 頁／共 {{ data.pagination.total_pages }} 頁</span><a v-if="page < data.pagination.total_pages" :href="pageHref(page + 1)">下一頁</a></nav>
    </template>
  </main>
</template>
