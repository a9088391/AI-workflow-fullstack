<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, backendName, emptySuccess, loginHref, positiveUrlInteger, readArticle, readComment } from '../../../utils/contracts.js'

useHead({ title: '留言詳情｜文章集' })
const route = useRoute()
const site = useSite()
const comment = ref(null)
const parent = ref(null)
const loading = ref(false)
const parentLoading = ref(false)
const pending = ref(false)
const unknown = ref(false)
const conflict = ref(false)
const error = ref('')
const parentError = ref('')
const writeError = ref('')
const logId = ref('')
let controller = null
let parentController = null
let writeController = null
let writeOwner = null

const id = computed(
  // 留言 ID 必須是安全正整數才進入共同 API。
  () => positiveUrlInteger(route.params.id),
)
const isAuthor = computed(
  // 管理員只在自己的留言上具有作者編修權。
  () => Boolean(site.account.value && comment.value?.author.id === site.account.value.id),
)
const canChangeVisibility = computed(
  // 狀態操作獨立於父文章讀取；恢復權限仍由目前回應決定。
  () => Boolean(site.account.value && comment.value && (isAuthor.value || site.account.value.role === 'admin') && (comment.value.visibility === 'visible' || comment.value.can_restore)),
)

// 手動讀取沒有 watch 清理參數，控制器仍負責取消舊請求。
function noCleanup() {}

// 先驗證可讀留言，父文章另行 GET，避免父文 404 隱藏自己的留言。
async function load(_value, _old, onCleanup = noCleanup) {
  if (!site.ready.value || site.leaving.value) return
  controller?.abort()
  parentController?.abort()
  // 舊 GET 取消後不會在 finally 收尾，先結束載入以顯示登入入口。
  loading.value = false
  parentLoading.value = false
  const request = new AbortController()
  controller = request
  onCleanup(
    // 路由或登入身分變更後不採用舊留言回應。
    () => request.abort(),
  )
  comment.value = null
  parent.value = null
  error.value = ''
  parentError.value = ''
  logId.value = ''
  if (!id.value) { error.value = '找不到指定資料或 API'; return }
  if (!site.account.value || !site.token.value) return
  loading.value = true
  try {
    const response = await apiRequest(`/api/comments/${id.value}`, { token: site.token.value, signal: request.signal })
    if (request.signal.aborted || site.leaving.value) return
    const data = readComment(response.status, response.body, id.value)
    if (data) { comment.value = data; loadParent(data.article_id) }
    else {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
      logId.value = failure.logId
    }
  } catch {
    if (!request.signal.aborted) error.value = '無法讀取留言；請檢查連線後手動重試。'
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 父文章錯誤只影響父文資訊；404 不推斷下架原因，也不清除留言。
async function loadParent(articleId = comment.value?.article_id) {
  if (!articleId || !site.token.value || site.leaving.value) return
  parentController?.abort()
  const request = new AbortController()
  parentController = request
  parentLoading.value = true
  parent.value = null
  parentError.value = ''
  try {
    const response = await apiRequest(`/api/articles/${articleId}`, { token: site.token.value, signal: request.signal })
    if (request.signal.aborted || site.leaving.value) return
    const data = readArticle(response.status, response.body, articleId)
    if (data) parent.value = data
    else {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      parentError.value = response.status === 404 ? '所屬文章目前無法讀取' : failure.message
    }
  } catch {
    if (!request.signal.aborted) parentError.value = '無法確認所屬文章狀態；請手動重試。'
  } finally { if (!request.signal.aborted) parentLoading.value = false }
}

// 留言下架／恢復先確認，使用目前版本；成功後重讀既有留言 ID。
async function changeVisibility() {
  if (!canChangeVisibility.value || pending.value || unknown.value || conflict.value || site.leaving.value) return
  const desired = comment.value.visibility === 'visible' ? 'hidden' : 'visible'
  if (!window.confirm(`確定${desired === 'hidden' ? '下架' : '恢復'}這則留言嗎？`)) return
  const request = new AbortController()
  writeController = request
  const token = site.token.value
  const backend = site.backend.value
  const sentId = id.value
  const sentAccount = site.account.value.id
  writeOwner = sentAccount
  pending.value = true
  writeError.value = ''
  try {
    const response = await apiRequest(`/api/comments/${id.value}/visibility`, { method: 'PUT', body: { visibility: desired, version: comment.value.version }, token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || backend !== site.backend.value || sentId !== id.value) return
    // 舊憑證的明確驗證拒絕表示狀態未變；其餘晚到結果需查證，異帳號不繼承。
    if (token !== site.token.value) {
      if (!site.account.value || site.account.value.id === sentAccount) {
        const failure = apiFailure(response.status, response.body)
        const rejected = response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102'
        unknown.value = !rejected
        writeError.value = rejected ? failure.message : '下架／恢復結果尚未確認，請先重讀留言查證。'
        logId.value = rejected ? failure.logId : ''
      }
      return
    }
    if (emptySuccess(response.status, response.body, 200)) { await load(); return }
    const failure = apiFailure(response.status, response.body)
    if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
    if (response.status === 409 && failure.code === 'E0201') conflict.value = true
    if (response.status >= 500 || !failure.code) unknown.value = true
    writeError.value = unknown.value ? '下架／恢復結果尚未確認，請先重讀留言查證。' : failure.message
    logId.value = failure.logId
  } catch {
    if (!request.signal.aborted && !site.leaving.value && backend === site.backend.value && sentId === id.value && (!site.account.value || site.account.value.id === sentAccount)) { unknown.value = true; writeError.value = '下架／恢復結果尚未確認，請先重讀留言查證。' }
  } finally { if (writeController === request) { writeController = null; pending.value = false } }
}

// 重讀後再開放使用者重新確認狀態操作，絕不自動重送。
async function refresh() { unknown.value = false; conflict.value = false; writeError.value = ''; await load() }

watch([id, site.ready, site.account, site.token, site.backend], load, { immediate: true })
watch(site.account,
  // 新帳號不能看到前一帳號的可見性未知結果，仍由自己重新授權讀取。
  (account) => { if (account && writeOwner !== null && account.id !== writeOwner) { unknown.value = false; conflict.value = false; writeError.value = '' } },
)
watch(id,
  // 新留言 ID 應重新確認狀態，舊 ID 的未知寫入警示不沿用。
  () => { writeController?.abort(); unknown.value = false; conflict.value = false; writeError.value = '' },
  { flush: 'sync' },
)
watch(site.backend,
  // 新後端必須重新取得狀態；舊後端的衝突或未知結果不沿用。
  () => { writeController?.abort(); unknown.value = false; conflict.value = false; writeError.value = '' },
  { flush: 'sync' },
)
watch(site.leaving,
  // 切換前端當下隔離留言、父文及狀態寫入回應。
  () => { controller?.abort(); parentController?.abort(); writeController?.abort() },
  { flush: 'sync' },
)
onBeforeUnmount(
  // 卸載後不讓本頁三種請求繼續更新畫面。
  () => { controller?.abort(); parentController?.abort(); writeController?.abort() },
)
</script>

<template>
  <main class="reading-page author-page">
    <a class="back-link" href="/nuxt/me/comments">← 我的留言</a><div class="author-heading"><div><p class="section-label">會員留言</p><h1>留言詳情</h1></div></div>
    <p v-if="!site.ready.value || loading" class="state-block" role="status">正在讀取留言…</p>
    <div v-else-if="!site.account.value" class="state-block"><p>請先登入，查看可讀的留言。</p><a :href="loginHref(route.path)">前往登入</a></div>
    <div v-else-if="error" class="state-block" role="alert"><p>{{ error }}</p><small v-if="logId">log_id：{{ logId }}</small><button type="button" class="inline-button" @click="load">重新讀取</button></div>
    <template v-else-if="comment">
      <p class="teaser-meta">建立來源 {{ backendName(comment.created_backend) }}　/　{{ comment.created_at }}　/　{{ comment.visibility === 'hidden' ? '已下架' : '公開' }}</p><div class="article-body">{{ comment.body }}</div>
      <div class="state-block"><p v-if="parentLoading">正在確認所屬文章…</p><p v-else-if="parentError">{{ parentError }}</p><p v-else-if="parent">所屬文章：<a :href="`/nuxt/articles/${parent.id}`">{{ parent.title }}</a>（{{ parent.visibility === 'visible' ? '公開' : '已下架' }}）</p><button v-if="parentError" type="button" class="inline-button" @click="loadParent()">單獨重試文章讀取</button></div>
      <div class="author-actions"><a v-if="isAuthor && parent?.visibility === 'visible'" :href="`/nuxt/comments/${comment.id}/edit`">編輯留言</a><button v-if="canChangeVisibility" type="button" class="inline-button" :disabled="pending || unknown || conflict" @click="changeVisibility">{{ pending ? '處理中…' : comment.visibility === 'visible' ? '下架留言' : '恢復留言' }}</button></div>
      <div v-if="writeError" class="state-block" role="alert"><p>{{ writeError }}</p><small v-if="logId">log_id：{{ logId }}</small><button type="button" class="inline-button" @click="refresh">重讀留言確認</button></div>
    </template>
  </main>
</template>
