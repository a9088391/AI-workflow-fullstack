<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, backendName, emptySuccess, loginHref, positiveUrlInteger, readArticle, readList, validateContent } from '../../../utils/contracts.js'

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
const commentBody = ref('')
const writeError = ref('')
const writeLogId = ref('')
const fieldErrors = ref({})
const commentSummary = ref(null)
const commentInput = ref(null)
const writePending = ref(false)
const uncertain = ref(false)
const commentOwner = ref(null)
const visibilityPending = ref(false)
const visibilityError = ref('')
const visibilityLogId = ref('')
const visibilityNeedsReadback = ref(false)
const visibilityUnknown = ref(false)
const visibilityConflict = ref(false)
let controller = null
let writeController = null
let visibilityController = null
let visibilityOwner = null

const isAuthor = computed(
  // 管理員自己的文章仍以作者身分編輯，別人的內文不可修改。
  () => Boolean(site.account.value && article.value?.author.id === site.account.value.id),
)
const canChangeVisibility = computed(
  // 管理員可下架他人文章；恢復仍依後端提供的目前授權旗標。
  () => Boolean(site.account.value && article.value && (isAuthor.value || site.account.value.role === 'admin') && (article.value.visibility === 'visible' || article.value.can_restore)),
)

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
  if (!site.ready.value || site.leaving.value) return
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
  // 文章 ID 與留言頁碼來自目前 URL；先清前頁資料，避免換頁時閃出舊資源。
  page.value = commentPage()
  const id = articleId()
  if (id === null) { error.value = '找不到指定資料或 API'; return }
  loading.value = true
  const token = site.token.value
  try {
    const response = await apiRequest(`/api/articles/${id}`, { token, signal: request.signal })
    if (request.signal.aborted) return
    // 文章必須先通過目前身分的可讀性，才有資格查詢其留言。
    const data = readArticle(response.status, response.body, id)
    if (!data) {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
      logId.value = failure.logId
      return
    }
    article.value = data
    // 留言暫時失敗仍保留可讀文章；手動重試會重新確認文章及留言。
    const responseComments = await apiRequest(`/api/articles/${id}/comments?page=${page.value}&per_page=20`, { token, signal: request.signal })
    if (request.signal.aborted) return
    const list = readList(responseComments.status, responseComments.body, page.value, 'comment', id)
    if (list) comments.value = list
    else {
      // 留言授權若顯示文章已不可讀，也撤下文章；其他讀取故障保留可讀文章。
      const failure = apiFailure(responseComments.status, responseComments.body)
      if (responseComments.status === 401 && failure.code === 'E0002' || responseComments.status === 403 && failure.code === 'E0102') { article.value = null; site.forget(failure.message) }
      else if (responseComments.status === 404 || responseComments.status === 403) { article.value = null; error.value = failure.message; logId.value = failure.logId }
      commentError.value = failure.message
      commentLogId.value = failure.logId
    }
    // 回傳目前仍有效的文章，供人工查證判斷是否真的完成讀回。
    return article.value
  } catch {
    if (!request.signal.aborted) {
      if (article.value) commentError.value = '無法讀取留言；請檢查連線後手動重試。'
      else error.value = '無法讀取文章；請檢查連線後手動重試。'
      return article.value
    }
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 留言草稿只綁目前帳號與頁面記憶體，不跨前端或不同帳號傳遞。
watch(commentBody,
  // 第一次輸入時固定作者 ID，供 401 重登後檢查。
  (value) => { if (value && commentOwner.value === null && site.account.value) commentOwner.value = site.account.value.id; site.dirty.value = Boolean(value) },
)
watch(site.account,
  // 另一個帳號登入時丟棄前一作者的留言與狀態查證門禁。
  (account) => {
    if (!account) return
    if (commentOwner.value !== null && account.id !== commentOwner.value) clearComment()
    if (visibilityOwner !== null && account.id !== visibilityOwner) { visibilityUnknown.value = false; visibilityNeedsReadback.value = false; visibilityConflict.value = false; visibilityError.value = ''; visibilityLogId.value = ''; visibilityOwner = null }
  },
)
watch(site.backend,
  // 更換 API 來源後舊寫入及草稿不可留給新後端。
  () => { writeController?.abort(); visibilityController?.abort(); visibilityUnknown.value = false; visibilityNeedsReadback.value = false; visibilityConflict.value = false; visibilityError.value = ''; visibilityLogId.value = ''; visibilityOwner = null; clearComment() },
  { flush: 'sync' },
)

// 不同帳號或後端不能沿用舊留言；沒有文字時不可誤稱清除了草稿。
function clearComment() {
  const hadDraft = Boolean(commentBody.value)
  commentBody.value = ''
  commentOwner.value = site.account.value?.id ?? null
  uncertain.value = false
  writeLogId.value = ''
  fieldErrors.value = {}
  site.dirty.value = false
  writeError.value = hadDraft ? '已清除上一個帳號的文字。' : ''
}

// 同帳號重登後保留在途留言的查證提示，防止誤以為可以直接重送。
function restoredComment() { if (!uncertain.value) writeError.value = '已恢復同帳號登入，請確認留言後手動送出。' }

// 錯誤摘要移到焦點，使用者可直接前往留言欄修正。
function focusCommentError() { commentInput.value?.focus() }

// 留言新增成功後固定重讀第一頁；201 空 data 不提供新留言 ID。
async function submitComment() {
  const id = articleId()
  if (writePending.value || uncertain.value || !id || !article.value || article.value.visibility !== 'visible' || !site.account.value || !site.token.value || site.leaving.value || commentOwner.value !== null && commentOwner.value !== site.account.value.id) return
  // 先驗證留言文字並保留原稿；目前文章公開且身分有效才可送出。
  const checked = validateContent({ body: commentBody.value }, 'comment')
  if (Object.keys(checked).length) { fieldErrors.value = checked; writeError.value = '欄位資料不正確，請確認後再送出'; await nextTick(); commentSummary.value?.focus(); return }
  // 留言新增與可見性是獨立操作，固定本次作者／token／後端避免回饋互相覆蓋。
  const request = new AbortController()
  writeController = request
  const token = site.token.value
  const backend = site.backend.value
  const sentOwner = site.account.value.id
  writePending.value = true
  writeError.value = ''
  writeLogId.value = ''
  fieldErrors.value = {}
  try {
    const response = await apiRequest(`/api/articles/${id}/comments`, { method: 'POST', body: { body: commentBody.value }, token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || backend !== site.backend.value) return
    // 舊憑證的明確驗證拒絕表示留言未建立；其餘晚到結果需查證，且不可撤銷新憑證。
    if (token !== site.token.value) {
      if (commentOwner.value === sentOwner && (!site.account.value || site.account.value.id === sentOwner)) {
        const failure = apiFailure(response.status, response.body)
        const rejected = response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102'
        uncertain.value = !rejected
        writeError.value = rejected ? failure.message : '留言結果尚未確認。請先重讀留言列表查證，再決定是否重新送出。'
        writeLogId.value = rejected ? failure.logId : ''
      }
      return
    }
    // 只有完整 201 才清文字並回原文章留言第一頁，不猜新留言 ID。
    if (emptySuccess(response.status, response.body, 201)) { commentBody.value = ''; site.dirty.value = false; window.location.assign(`/nuxt/articles/${id}?commented=1#comments`); return }
    // 已知拒絕可以修正或重登；可能已送達的回應須保留未知結果供人工查證。
    const failure = apiFailure(response.status, response.body, ['body'])
    if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
    if (response.status >= 500 || !failure.code) uncertain.value = true
    fieldErrors.value = failure.errors
    writeError.value = uncertain.value ? '留言結果尚未確認。請先重讀留言列表查證，再決定是否重新送出。' : failure.message
    writeLogId.value = failure.logId
    if (response.status === 422) { await nextTick(); commentSummary.value?.focus() }
    // 連線中斷不代表後端沒有建立留言；來源與作者未變才保留這次未知提示。
  } catch {
    if (!request.signal.aborted && !site.leaving.value && backend === site.backend.value && commentOwner.value === sentOwner && (!site.account.value || site.account.value.id === sentOwner)) { uncertain.value = true; writeError.value = '留言結果尚未確認。請先重讀留言列表查證，再決定是否重新送出。' }
  // 留言 POST 只收尾自己的等待；獨立可見性操作的狀態不受影響。
  } finally { if (writeController === request) { writeController = null; writePending.value = false } }
}

// 使用者確認已查證後才允許人工再送；此按鈕不判定成功，也不重送。
function checkedComment() { uncertain.value = false; writeError.value = '請確認留言尚未建立，再手動送出。' }

// 狀態操作使用詳情版本，成功後依同一 ID 重讀以取得伺服器狀態。
async function changeVisibility() {
  // 前次未知或衝突必須先完成有效讀回，且每次新操作仍須人工確認。
  if (!canChangeVisibility.value || visibilityPending.value || visibilityNeedsReadback.value || visibilityConflict.value || site.leaving.value) return
  const id = articleId()
  const desired = article.value.visibility === 'visible' ? 'hidden' : 'visible'
  if (!window.confirm(`確定${desired === 'hidden' ? '下架' : '恢復'}這篇文章嗎？`)) return
  const request = new AbortController()
  visibilityController = request
  const token = site.token.value
  const backend = site.backend.value
  const sentId = id
  const sentAccount = site.account.value.id
  visibilityOwner = sentAccount
  // 新的人工操作只重設自己的提示；不能清掉另一項留言的未知結果。
  visibilityPending.value = true
  visibilityUnknown.value = false
  visibilityNeedsReadback.value = false
  visibilityConflict.value = false
  visibilityError.value = ''
  visibilityLogId.value = ''
  try {
    const response = await apiRequest(`/api/articles/${id}/visibility`, { method: 'PUT', body: { visibility: desired, version: article.value.version }, token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || backend !== site.backend.value || sentId !== articleId()) return
    // 舊憑證的明確驗證拒絕表示狀態未變；其餘晚到結果需查證，不帶到別的帳號。
    if (token !== site.token.value) {
      if (!site.account.value || site.account.value.id === sentAccount) {
        const failure = apiFailure(response.status, response.body)
        const rejected = response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102'
        visibilityUnknown.value = !rejected
        visibilityNeedsReadback.value = !rejected
        visibilityLogId.value = failure.logId
        visibilityError.value = rejected ? failure.message : '下架／恢復結果尚未確認，請先重讀文章查證。'
      }
      return
    }
    // 成功只觸發同 ID 讀回；錯誤與未知結果留在本操作，不影響其他寫入。
    if (emptySuccess(response.status, response.body, 200)) { await load(); return }
    const failure = apiFailure(response.status, response.body)
    if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
    if (response.status === 409 && failure.code === 'E0201') { visibilityConflict.value = true; visibilityNeedsReadback.value = true }
    if (response.status >= 500 || !failure.code) { visibilityUnknown.value = true; visibilityNeedsReadback.value = true }
    visibilityLogId.value = failure.logId
    visibilityError.value = visibilityUnknown.value ? '下架／恢復結果尚未確認，請先重讀文章查證。' : failure.message
  } catch {
    if (!request.signal.aborted && !site.leaving.value && backend === site.backend.value && sentId === articleId() && (!site.account.value || site.account.value.id === sentAccount)) { visibilityUnknown.value = true; visibilityNeedsReadback.value = true; visibilityError.value = '下架／恢復結果尚未確認，請先重讀文章查證。' }
  // 可見性 PUT 只結束自己仍持有的等待，不能順便確認留言 POST 的結果。
  } finally { if (visibilityController === request) { visibilityController = null; visibilityPending.value = false } }
}

// 有效讀回只確認目前狀態；原 PUT 是否成功仍未知，失敗不能提前解鎖。
async function refreshVisibility() {
  const token = site.token.value
  const backend = site.backend.value
  const owner = site.account.value?.id
  const id = articleId()
  const result = await load()
  // 讀回等待期間換人、換來源或換文章時，不得替新上下文解除門禁。
  if (!result || !owner || token !== site.token.value || owner !== site.account.value?.id || backend !== site.backend.value || id !== articleId() || site.leaving.value) return
  visibilityNeedsReadback.value = false
  visibilityConflict.value = false
  visibilityError.value = visibilityUnknown.value ? '下架／恢復結果尚未確認；目前文章狀態已重讀，再次操作仍須手動確認。' : '目前文章狀態已重讀，請重新確認操作。'
}

// 路由 ID 變更時需讀取另一篇文章。
function routeId() { return route.params.id }

watch(routeId,
  // 同一元件若改看別篇文章，舊文章的留言草稿不可沿用。
  () => { writeController?.abort(); visibilityController?.abort(); visibilityUnknown.value = false; visibilityNeedsReadback.value = false; visibilityConflict.value = false; visibilityError.value = ''; visibilityLogId.value = ''; visibilityOwner = null; clearComment() },
  { flush: 'sync' },
)

// 留言頁碼變更時需讀取另一頁清單。
function routePage() { return route.query.page }

watch(
  [routeId, routePage, site.ready, site.token, site.backend],
  load,
  { immediate: true },
)

// 離頁時同步取消詳情與留言 GET，原 token 不變也不能覆蓋新前端畫面。
watch(site.leaving,
  // 取消目前頁面持有的同一個讀取控制器。
  () => { controller?.abort(); writeController?.abort(); visibilityController?.abort() },
  { flush: 'sync' },
)

// 深層頁離開時不讓在途回應回寫已銷毀畫面。
onBeforeUnmount(
  // 終止本頁目前讀取。
  () => { controller?.abort(); writeController?.abort(); visibilityController?.abort(); site.dirty.value = false },
)
</script>

<template>
  <main class="reading-page">
    <a class="back-link"
      href="/nuxt/">← 所有文章</a>
    <a v-if="site.account.value?.role === 'admin'"
      class="back-link admin-return"
      href="/nuxt/admin/articles">返回文章管理</a>
    <ReauthForm v-if="!site.account.value && commentOwner !== null"
      :owner-id="commentOwner"
      @restored="restoredComment"
      @different="clearComment" />
    <div v-if="visibilityError"
      class="state-block"
      role="alert">
      <p>{{ visibilityError }}</p>
      <small v-if="visibilityLogId">log_id：{{ visibilityLogId }}</small>
      <button type="button"
        class="inline-button"
        :disabled="loading || visibilityPending"
        @click="refreshVisibility">重讀文章確認</button>
    </div>
    <p v-if="writeError"
      ref="commentSummary"
      class="form-error"
      tabindex="-1"
      role="alert">{{ writeError }}<small v-if="writeLogId">log_id：{{ writeLogId }}</small><button v-if="fieldErrors.body?.length && article?.visibility === 'visible'"
        type="button"
        class="inline-button"
        @click="focusCommentError">前往留言欄位</button></p>
    <div v-if="uncertain"
      class="state-actions">
      <button type="button"
        class="inline-button"
        :disabled="loading"
        @click="load">重讀留言列表</button>
      <button type="button"
        class="inline-button"
        @click="checkedComment">我已查證結果</button>
    </div>
    <p v-if="loading && !article"
      class="state-block"
      role="status">正在載入文章…</p>
    <div v-else-if="error"
      class="state-block"
      role="alert">
      <p>{{ error }}</p>
      <small v-if="logId">log_id：{{ logId }}</small>
      <div class="state-actions">
        <button type="button"
          class="inline-button"
          @click="load">重新讀取</button>
        <a href="/nuxt/">返回文章列表</a>
        <a v-if="error.includes('登入')"
          :href="loginHref(route.path)">重新登入</a>
      </div>
    </div>
    <template v-else-if="article">
      <article class="reading-article">
        <p class="teaser-meta">建立來源 {{ backendName(article.created_backend) }} / {{ article.created_at }}</p>
        <h1>{{ article.title }}</h1>
        <p class="reading-byline">文字／{{ article.author.nickname }}　·　{{ article.visibility === 'hidden' ? '已下架' : '公開' }}</p>
        <div class="reading-rule" />
        <div class="article-body">{{ article.body }}</div>
      </article>
      <div v-if="isAuthor || canChangeVisibility"
        class="author-actions">
        <a v-if="isAuthor"
          :href="`/nuxt/articles/${article.id}/edit`">編輯文章</a>
        <button v-if="canChangeVisibility"
          type="button"
          class="inline-button"
          :disabled="visibilityPending || visibilityNeedsReadback || visibilityConflict"
          @click="changeVisibility">{{ visibilityPending ? '處理中…' : article.visibility === 'visible' ? '下架文章' : '恢復文章' }}</button>
      </div>
      <section id="comments"
        class="comments-section"
        aria-labelledby="comments-title">
        <div class="comments-heading">
          <h2 id="comments-title">留言</h2>
          <span v-if="comments">{{ comments.pagination.total }} 則</span>
        </div>
        <p v-if="route.query.commented === '1' && comments"
          class="form-success"
          role="status">留言已發表，已重新讀取第一頁。</p>
        <p v-if="loading && !comments"
          class="state-block"
          role="status">正在載入留言…</p>
        <div v-else-if="commentError"
          class="state-block"
          role="alert">
          <p>{{ commentError }}</p>
          <small v-if="commentLogId">log_id：{{ commentLogId }}</small>
          <button type="button"
            class="inline-button"
            @click="load">重新讀取</button>
        </div>
        <ol v-else-if="comments && comments.list.length"
          class="comment-list">
          <li v-for="comment in comments.list"
            :key="comment.id">
            <div class="comment-meta"><strong>{{ comment.author.nickname }}</strong><span>{{ comment.created_at }} / 建立來源 {{ backendName(comment.created_backend) }}</span></div>
            <p>{{ comment.body }}</p>
            <a v-if="site.account.value && (site.account.value.id === comment.author.id || site.account.value.role === 'admin')"
              :href="`/nuxt/comments/${comment.id}`">留言詳情</a>
          </li>
        </ol>
        <!-- 留言零筆時保留原頁碼，第 1 頁與超頁使用不同提示。 -->
        <div v-else-if="comments"
          class="state-block">
          <p>{{ page > 1 && page > comments.pagination.total_pages ? comments.pagination.total === 0 ? '此頁沒有留言。' : '此頁沒有留言，前面的頁面仍有內容。' : '目前還沒有留言。' }}</p>
          <a v-if="page > 1"
            :href="pageHref(1)">回第一頁</a>
        </div>
        <nav v-if="comments && (comments.pagination.total_pages > 1 || page > 1)"
          class="pagination"
          aria-label="留言分頁">
          <a v-if="page > 1"
            :href="pageHref(page - 1)">上一頁</a>
          <span>第 {{ page }} 頁／共 {{ comments.pagination.total_pages }} 頁</span>
          <a v-if="page < comments.pagination.total_pages"
            :href="pageHref(page + 1)">下一頁</a>
        </nav>
        <div v-if="article.visibility === 'visible'"
          class="comment-compose">
          <div v-if="!site.account.value && commentOwner === null"
            class="state-block">
            <p>登入後可以留言。</p>
            <a :href="loginHref(route.path)">前往登入</a>
          </div>
          <template v-else>
            <form class="editor-form"
              novalidate
              @submit.prevent="submitComment">
              <h3>留下留言</h3>
              <p class="field-hint">留言以純文字顯示，換行會保留。</p>
              <label for="article-comment-body">留言</label>
              <textarea id="article-comment-body"
                ref="commentInput"
                v-model="commentBody"
                rows="5"
                :aria-invalid="Boolean(fieldErrors.body)"
                :aria-describedby="fieldErrors.body ? 'article-comment-error' : undefined"
                :disabled="writePending || !site.account.value" />
              <small v-if="fieldErrors.body"
                id="article-comment-error"
                class="field-error">{{ fieldErrors.body.join('；') }}</small>
              <button class="primary-button"
                type="submit"
                :disabled="writePending || uncertain || !site.account.value">{{ writePending ? '送出中…' : '發表留言' }}</button>
            </form>
          </template>
        </div>
      </section>
    </template>
  </main>
</template>
