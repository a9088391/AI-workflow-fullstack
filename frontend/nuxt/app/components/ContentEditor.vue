<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, backendName, emptySuccess, loginHref, positiveUrlInteger, readArticle, readComment, validateContent } from '../utils/contracts.js'

const props = defineProps({ kind: { type: String, required: true } })
const route = useRoute()
const site = useSite()
const resource = ref(null)
const parent = ref(null)
const latest = ref(null)
const title = ref('')
const body = ref('')
const baseTitle = ref('')
const baseBody = ref('')
const version = ref(null)
const draftOwner = ref(null)
const draftReady = ref(false)
const loading = ref(false)
const parentLoading = ref(false)
const pending = ref(false)
const conflict = ref(false)
const uncertain = ref(false)
const error = ref('')
const parentError = ref('')
const writeError = ref('')
const logId = ref('')
const errors = ref({})
const summaryElement = ref(null)
const titleElement = ref(null)
const bodyElement = ref(null)
let readController = null
let parentController = null
let writeController = null

const id = computed(
  // URL ID 嚴格解析後才用於讀寫路徑。
  () => positiveUrlInteger(route.params.id),
)
const isArticle = computed(
  // 兩種編修共用流程，端點與欄位仍依原資源種類分開。
  () => props.kind === 'article',
)
const endpoint = computed(
  // 讀取與更新同一既有 ID，不從成功空物件猜新資源。
  () => `/api/${isArticle.value ? 'articles' : 'comments'}/${id.value}`,
)
const dirty = computed(
  // 只有已載入的編修文字與原值不同時才需要離頁確認。
  () => draftReady.value && (body.value !== baseBody.value || isArticle.value && title.value !== baseTitle.value),
)
const canEdit = computed(
  // 管理員也僅能改自己的內文；留言還須確認父文章公開。
  () => Boolean(site.account.value && resource.value?.author.id === site.account.value.id && (isArticle.value || parent.value?.visibility === 'visible')),
)

// 手動重讀不帶 watch 清理回呼，仍由本元件控制器取消舊請求。
function noCleanup() {}

// 詳情讀取不覆蓋已修改文字，較新的版本另外保留供人工比較。
async function load(_value, _old, onCleanup = noCleanup) {
  if (!site.ready.value || site.leaving.value) return
  readController?.abort()
  parentController?.abort()
  // 舊 GET 的 finally 會因取消而略過；先結束舊載入，登出時才能看到原頁重登入口。
  loading.value = false
  parentLoading.value = false
  const request = new AbortController()
  readController = request
  onCleanup(
    // 路由或身分改變後作廢舊詳情回應。
    () => request.abort(),
  )
  error.value = ''
  logId.value = ''
  parent.value = null
  if (!id.value) { resource.value = null; error.value = '找不到指定資料或 API'; return }
  if (!site.account.value || !site.token.value) { resource.value = null; return }
  loading.value = true
  try {
    const response = await apiRequest(endpoint.value, { token: site.token.value, signal: request.signal })
    if (request.signal.aborted || site.leaving.value) return
    const data = isArticle.value ? readArticle(response.status, response.body, id.value) : readComment(response.status, response.body, id.value)
    if (!data) {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      resource.value = null
      error.value = failure.message
      logId.value = failure.logId
      return
    }
    resource.value = data
    latest.value = data
    if (!draftReady.value) {
      draftOwner.value = data.author.id
      title.value = isArticle.value ? data.title : ''
      body.value = data.body
      baseTitle.value = title.value
      baseBody.value = data.body
      version.value = data.version
      draftReady.value = true
    } else if (data.version !== version.value) conflict.value = true
    // 父文慢或不可讀時仍先呈現已授權的留言與草稿，編修按鈕等父文結果。
    if (!isArticle.value) loadParent(data.article_id)
  } catch {
    if (!request.signal.aborted) error.value = '無法讀取內容；請檢查連線後手動重試。'
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 留言的父文章獨立 GET；404 只代表目前不可讀，不透露標題或原因。
async function loadParent(articleId = resource.value?.article_id) {
  if (!articleId || site.leaving.value || !site.token.value) return
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

// 409 後先展示最新內容，明確採用其版本才允許人工再次送出。
function adoptLatest() {
  if (!latest.value || !resource.value || latest.value.id !== resource.value.id) return
  version.value = latest.value.version
  conflict.value = false
  writeError.value = '已採用最新版本。請比較文字並手動送出，或捨棄自己的修改。'
}

// 使用者選擇放棄草稿時才把伺服器最新內容填回編修欄位。
function discardDraft() {
  if (!latest.value) return
  title.value = isArticle.value ? latest.value.title : ''
  body.value = latest.value.body
  baseTitle.value = title.value
  baseBody.value = body.value
  version.value = latest.value.version
  conflict.value = false
  uncertain.value = false
  writeError.value = ''
}

// 驗證摘要可供鍵盤聚焦，點摘要動作則跳至第一個錯誤欄位。
function focusFirstError() { if (errors.value.title?.length) titleElement.value?.focus(); else if (errors.value.body?.length) bodyElement.value?.focus() }

// 已知 ID 更新只送目前編修值與版本，成功後返回該 ID 的詳情重新讀取。
async function submit() {
  if (!canEdit.value || pending.value || conflict.value || uncertain.value || site.leaving.value || !version.value) return
  const checked = validateContent({ title: title.value, body: body.value }, props.kind)
  if (Object.keys(checked).length) { errors.value = checked; writeError.value = '欄位資料不正確，請確認後再送出'; await nextTick(); summaryElement.value?.focus(); return }
  const request = new AbortController()
  writeController = request
  const token = site.token.value
  const backend = site.backend.value
  const sentOwner = draftOwner.value
  const sentId = id.value
  pending.value = true
  errors.value = {}
  writeError.value = ''
  const payload = isArticle.value ? { title: title.value, body: body.value, version: version.value } : { body: body.value, version: version.value }
  try {
    const response = await apiRequest(endpoint.value, { method: 'PUT', body: payload, token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || backend !== site.backend.value || sentId !== id.value) return
    // 舊憑證的明確驗證拒絕表示未修改；其餘晚到結果需查證，且不得撤銷新憑證。
    if (token !== site.token.value) {
      if (draftReady.value && draftOwner.value === sentOwner && (!site.account.value || site.account.value.id === sentOwner)) {
        const failure = apiFailure(response.status, response.body)
        const rejected = response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102'
        uncertain.value = !rejected
        writeError.value = rejected ? failure.message : '修改結果尚未確認。請先重讀詳情查證，再決定是否重新送出。'
        logId.value = rejected ? failure.logId : ''
      }
      return
    }
    if (emptySuccess(response.status, response.body, 200)) { site.dirty.value = false; window.location.assign(`/nuxt/${isArticle.value ? 'articles' : 'comments'}/${id.value}`); return }
    const failure = apiFailure(response.status, response.body, isArticle.value ? ['title', 'body'] : ['body'])
    if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
    if (response.status === 409 && failure.code === 'E0201') { conflict.value = true; latest.value = null; await load() }
    if (response.status >= 500 || !failure.code) uncertain.value = true
    errors.value = failure.errors
    writeError.value = uncertain.value ? '修改結果尚未確認。請先重讀詳情查證，再決定是否重新送出。' : failure.message
    logId.value = failure.logId
    if (response.status === 422) { await nextTick(); summaryElement.value?.focus() }
  } catch {
    if (!request.signal.aborted && !site.leaving.value && backend === site.backend.value && sentId === id.value && draftReady.value && draftOwner.value === sentOwner && (!site.account.value || site.account.value.id === sentOwner)) { uncertain.value = true; writeError.value = '修改結果尚未確認。請先重讀詳情查證，再決定是否重新送出。' }
  } finally { if (writeController === request) { writeController = null; pending.value = false } }
}

// 查證結果後解除送出鎖定，但不代替使用者判斷或自動重送。
function checkedResult() { uncertain.value = false; writeError.value = '請確認修改尚未完成，再手動送出。' }

// 帳號變更後清除前人文字，父文章與內容都必須重新授權讀取。
function resetForIdentity() {
  title.value = ''
  body.value = ''
  baseTitle.value = ''
  baseBody.value = ''
  draftReady.value = false
  draftOwner.value = null
  resource.value = null
  parent.value = null
  conflict.value = false
  uncertain.value = false
  site.dirty.value = false
}

// 不同帳號登入時先清空草稿；同帳號恢復則保留文字供人工送出。
function restored() { if (draftOwner.value !== null && site.account.value?.id !== draftOwner.value) resetForIdentity(); load() }

watch(dirty,
  // 共用切換控制只需要目前頁面是否有未儲存修改。
  (value) => { site.dirty.value = value },
)
watch(site.account,
  // 已驗證新帳號若非舊作者，不能看到或送出舊作者文字。
  (account) => { if (account && draftOwner.value !== null && account.id !== draftOwner.value) resetForIdentity() },
)
watch(id,
  // 同一元件導航到另一資源時，舊 ID 的草稿與版本不得帶入新 PUT。
  () => { readController?.abort(); parentController?.abort(); writeController?.abort(); resetForIdentity() },
  { flush: 'sync' },
)
watch([id, site.ready, site.account, site.token, site.backend], load, { immediate: true })
watch([site.backend, site.leaving],
  // 切換立即取消所有在途請求；後端切換則一併丟棄舊來源草稿。
  () => { readController?.abort(); parentController?.abort(); writeController?.abort(); if (!site.leaving.value) resetForIdentity() },
  { flush: 'sync' },
)
onBeforeUnmount(
  // 頁面卸載時清理三種請求與共用未儲存旗標。
  () => { readController?.abort(); parentController?.abort(); writeController?.abort(); site.dirty.value = false },
)
</script>

<template>
  <main class="reading-page author-page">
    <a class="back-link" :href="`/nuxt/${kind === 'article' ? 'articles' : 'comments'}/${id || ''}`">← 返回詳情</a>
    <div class="author-heading"><div><p class="section-label">作者編修</p><h1>編輯{{ kind === 'article' ? '文章' : '留言' }}</h1></div></div>
    <p v-if="!site.ready.value || loading" class="state-block" role="status">正在讀取內容…</p>
    <div v-else-if="!site.account.value && !draftReady" class="state-block"><p>請先登入後編輯自己的內容。</p><a :href="loginHref(route.path)">前往登入</a></div>
    <template v-else>
      <ReauthForm v-if="!site.account.value && draftReady" :owner-id="draftOwner" @restored="restored" @different="resetForIdentity" />
      <div v-if="error" class="state-block" role="alert"><p>{{ error }}</p><small v-if="logId">log_id：{{ logId }}</small><button type="button" class="inline-button" @click="load">重新讀取</button></div>
      <template v-if="resource && draftReady">
        <p class="teaser-meta">建立來源 {{ backendName(resource.created_backend) }}　/　{{ resource.visibility === 'hidden' ? '已下架' : '公開' }}　/　版本 {{ resource.version }}</p>
        <div v-if="!isArticle" class="state-block"><p v-if="parentLoading">正在確認所屬文章…</p><p v-else-if="parentError">{{ parentError }}</p><p v-else-if="parent">所屬文章：{{ parent.title }}（{{ parent.visibility === 'visible' ? '公開' : '已下架' }}）</p><button v-if="parentError" type="button" class="inline-button" @click="loadParent()">單獨重試文章讀取</button></div>
        <p v-if="!canEdit" class="state-block" role="alert">{{ isArticle ? '只有作者能修改本文。' : '須是留言作者，且已確認所屬文章公開，才能修改內文。' }}</p>
        <div v-if="conflict" class="state-block" role="alert"><p>內容已更新，請確認最新內容後再送出。你的文字仍保留。</p><div v-if="latest" class="comparison"><h2>最新伺服器內容（版本 {{ latest.version }}）</h2><p v-if="isArticle">{{ latest.title }}</p><div class="article-body">{{ latest.body }}</div></div><div class="state-actions"><button type="button" class="inline-button" @click="load">重新讀取最新內容</button><button type="button" class="inline-button" @click="adoptLatest">採用最新版本，保留我的文字</button><button type="button" class="inline-button" @click="discardDraft">捨棄我的修改</button></div></div>
        <form class="editor-form" novalidate @submit.prevent="submit"><p v-if="writeError" ref="summaryElement" class="form-error" tabindex="-1" role="alert">{{ writeError }}<small v-if="logId">log_id：{{ logId }}</small><button v-if="errors.title?.length || errors.body?.length" type="button" class="inline-button" @click="focusFirstError">前往第一個錯誤欄位</button></p><div v-if="uncertain" class="state-actions"><button type="button" class="inline-button" @click="load">重讀詳情查證</button><button type="button" class="inline-button" @click="checkedResult">我已查證結果</button></div><p class="field-hint">以純文字儲存，原有換行會保留。</p><template v-if="isArticle"><label for="edit-title">標題</label><input id="edit-title" ref="titleElement" v-model="title" :aria-invalid="Boolean(errors.title)" :aria-describedby="errors.title ? 'edit-title-error' : undefined" :disabled="pending || !canEdit"><small v-if="errors.title" id="edit-title-error" class="field-error">{{ errors.title.join('；') }}</small></template><label for="edit-body">內文</label><textarea id="edit-body" ref="bodyElement" v-model="body" rows="14" :aria-invalid="Boolean(errors.body)" :aria-describedby="errors.body ? 'edit-body-error' : undefined" :disabled="pending || !canEdit" /><small v-if="errors.body" id="edit-body-error" class="field-error">{{ errors.body.join('；') }}</small><button class="primary-button" type="submit" :disabled="pending || conflict || uncertain || !canEdit">{{ pending ? '儲存中…' : '儲存修改' }}</button></form>
      </template>
    </template>
  </main>
</template>
