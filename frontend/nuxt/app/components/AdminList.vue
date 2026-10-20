<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, backendName, loginHref, positiveUrlInteger, readList } from '../utils/contracts.js'
import { readMemberList } from '../utils/admin.js'

const props = defineProps({ kind: { type: String, required: true } })
const route = useRoute()
const site = useSite()
const data = ref(null)
const loading = ref(false)
const error = ref('')
const logId = ref('')
const keyword = ref('')
const fieldError = ref('')
const copyNotice = ref('')
const errorSummary = ref(null)
const keywordInput = ref(null)
let controller = null

// 只把合法頁碼交給 API；非法查詢回到可預期的第一頁。
function requestedPage() { return positiveUrlInteger(route.query.page) ?? 1 }

// 搜尋輸入先限縮已核准的 Unicode 長度；空值代表不篩選。
function searchedKeyword() { return typeof route.query.keyword === 'string' ? route.query.keyword.replace(/^ *| *$/g, '') : '' }

// 分頁僅保留會員搜尋條件，不把其他管理頁的查詢誤送 API。
function pageHref(number) {
  const query = new URLSearchParams({ page: String(number) })
  if (props.kind === 'members' && searchedKeyword()) query.set('keyword', searchedKeyword())
  return `/nuxt/admin/${props.kind}?${query}`
}

// 點搜尋時更新語意網址；伺服器仍是搜尋結果的唯一來源。
async function submitSearch() {
  const value = keyword.value.replace(/^ *| *$/g, '')
  if ([...value].length > 254) { fieldError.value = '搜尋關鍵字最多 254 個字。'; error.value = '欄位資料不正確，請確認後再送出'; await nextTick(); errorSummary.value?.focus(); return }
  fieldError.value = ''
  window.location.assign(`/nuxt/admin/members${value ? `?keyword=${encodeURIComponent(value)}` : ''}`)
}

// 追蹤編號可直接複製，剪貼簿權限失敗仍保留可選取文字。
async function copyLogId() {
  try { await navigator.clipboard.writeText(logId.value); copyNotice.value = '已複製追蹤編號。' }
  catch { copyNotice.value = '無法自動複製；請手動選取追蹤編號。' }
}

// 手動重讀沿用同一入口，不需額外的 watch 清理程序。
function noCleanup() {}

// 從網址讀取頁碼，讓 Vue watch 在同一路由查詢變更時重查。
function routePage() { return route.query.page }

// 會員搜尋字串變更時重查，不沿用上一個關鍵字的私有清單。
function routeKeyword() { return route.query.keyword }

// 每次身分、來源或查詢變更都取消舊讀取；沒有管理權限不發出管理 API。
async function load(_value, _old, onCleanup = noCleanup) {
  controller?.abort()
  // 舊查詢的清單與驗證結果一起失效；本次關鍵字若無效會重新標示。
  data.value = null
  error.value = ''
  fieldError.value = ''
  logId.value = ''
  loading.value = false
  if (!site.ready.value || site.leaving.value || site.account.value?.role !== 'admin' || !site.token.value) return
  const request = new AbortController()
  controller = request
  onCleanup(
    // 新狀態出現即撤銷舊請求，避免私有清單閃回。
    () => request.abort(),
  )
  const page = requestedPage()
  const search = searchedKeyword()
  keyword.value = search
  if (props.kind === 'members' && [...search].length > 254) { fieldError.value = '搜尋關鍵字最多 254 個字。'; error.value = '欄位資料不正確，請確認後再送出'; await nextTick(); errorSummary.value?.focus(); return }
  const path = props.kind === 'members' ? '/api/members' : props.kind === 'articles' ? '/api/articles' : '/api/comments'
  const suffix = props.kind === 'members' && search ? `&filters%5Bkeyword%5D=${encodeURIComponent(search)}` : props.kind === 'articles' ? '&filters%5Bscope%5D=management' : ''
  const token = site.token.value
  const backend = site.backend.value
  loading.value = true
  try {
    const response = await apiRequest(`${path}?page=${page}&per_page=20${suffix}`, { token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || token !== site.token.value || backend !== site.backend.value) return
    const result = props.kind === 'members' ? readMemberList(response.status, response.body, page) : readList(response.status, response.body, page, props.kind === 'articles' ? 'article' : 'comment')
    if (result) data.value = result
    else {
      const failure = apiFailure(response.status, response.body, ['filters.keyword'])
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      fieldError.value = failure.errors['filters.keyword']?.join('；') || ''
      error.value = failure.message
      logId.value = failure.logId
      if (response.status === 422) { await nextTick(); errorSummary.value?.focus() }
    }
  } catch {
    if (!request.signal.aborted) error.value = '無法讀取管理資料；請檢查連線後手動重試。'
  } finally { if (!request.signal.aborted) loading.value = false }
}

// URL 查詢與有效權限共同決定清單，切換後端不得保留舊資料。
watch([routePage, routeKeyword, site.ready, site.account, site.token, site.backend], load, { immediate: true })
watch(site.leaving,
  // 切換前端時同步停止目前管理清單請求。
  () => controller?.abort(),
  { flush: 'sync' },
)
onBeforeUnmount(
  // 離開頁面時不讓晚到結果更新已卸載畫面。
  () => controller?.abort(),
)
</script>

<template>
  <main class="reading-page admin-page">
    <div class="author-heading"><div><p class="section-label">文章集 / 管理</p><h1>{{ kind === 'members' ? '會員管理' : kind === 'articles' ? '文章管理' : '留言管理' }}</h1></div></div>
    <p v-if="!site.ready.value" class="state-block" role="status">正在確認登入狀態…</p>
    <div v-else-if="!site.account.value" class="state-block"><p>請先登入以使用管理頁。</p><a :href="loginHref(route.path)">前往登入</a></div>
    <div v-else-if="site.account.value.role !== 'admin'" class="state-block" role="alert"><p>沒有權限使用此功能。</p><a href="/nuxt/">返回文章列表</a></div>
    <template v-else>
      <form v-if="kind === 'members'" class="admin-search" @submit.prevent="submitSearch"><label for="member-keyword">搜尋會員暱稱或 Email</label><div class="admin-search-row"><input id="member-keyword" ref="keywordInput" v-model="keyword" type="search" :aria-invalid="Boolean(fieldError)" :aria-describedby="fieldError ? 'member-keyword-error member-keyword-help' : 'member-keyword-help'"><button class="secondary-button" type="submit">搜尋</button></div><p id="member-keyword-help">以字面包含搜尋；不使用萬用字元。</p><p v-if="fieldError" id="member-keyword-error" class="form-error" role="alert">{{ fieldError }}</p></form>
      <p v-if="loading" class="state-block" role="status">正在讀取管理資料…</p>
      <div v-else-if="error" ref="errorSummary" class="state-block" role="alert" tabindex="-1"><p>{{ error }}</p><button v-if="fieldError" type="button" class="inline-button" @click="keywordInput?.focus()">前往搜尋欄位：{{ fieldError }}</button><small v-if="logId">log_id：{{ logId }}</small><button v-if="logId" type="button" class="inline-button" @click="copyLogId">複製編號</button><span v-if="copyNotice" role="status">{{ copyNotice }}</span><button type="button" class="inline-button" @click="load">重新讀取</button></div>
      <template v-else-if="data">
        <div class="section-rule"><span>{{ kind === 'members' ? '會員索引' : kind === 'articles' ? '文章索引' : '留言索引' }}</span><span>共 {{ data.pagination.total }} {{ kind === 'members' ? '位' : '則' }}</span></div>
        <ol v-if="data.list.length" class="admin-list">
          <li v-for="item in data.list" :key="item.id" class="admin-row">
            <p class="teaser-meta"><span class="admin-row-field"><span class="admin-mobile-label">ID：</span>#{{ item.id }}</span><span class="admin-desktop-separator">　</span><span class="admin-row-field"><span class="admin-mobile-label">建立日期：</span>{{ item.created_at }}</span><template v-if="kind !== 'members'"><span class="admin-desktop-separator">　/　</span><span class="admin-row-field"><span class="admin-mobile-label">建立後端：</span>{{ backendName(item.created_backend) }}</span></template></p>
            <h2 v-if="kind === 'members'"><a :href="`/nuxt/admin/members/${item.id}`">{{ item.nickname }}</a></h2>
            <h2 v-else-if="kind === 'articles'"><a :href="`/nuxt/articles/${item.id}`">{{ item.title }}</a></h2>
            <p v-else class="author-excerpt">{{ item.body }}</p>
            <p class="admin-row-meta"><template v-if="kind === 'members'"><span class="admin-row-field"><span class="admin-mobile-label">Email：</span>{{ item.email }}</span><span class="admin-desktop-separator">　/　</span><span class="admin-row-field"><span class="admin-mobile-label">狀態：</span>{{ item.status === 'active' ? '啟用' : '停用' }}</span></template><template v-else><span class="admin-row-field"><span class="admin-mobile-label">作者：</span>{{ item.author.nickname }}</span><span class="admin-desktop-separator">　/　</span><span class="admin-row-field"><span class="admin-mobile-label">狀態：</span>{{ item.visibility === 'visible' ? '公開' : '已下架' }}</span></template></p>
            <a class="secondary-button" :href="kind === 'members' ? `/nuxt/admin/members/${item.id}` : kind === 'articles' ? `/nuxt/articles/${item.id}` : `/nuxt/comments/${item.id}`">查看{{ kind === 'members' ? '會員' : kind === 'articles' ? '文章' : '留言' }}</a>
          </li>
        </ol>
        <!-- 零筆搜尋首頁顯示無符合結果；零筆超頁仍顯示原頁碼與返回入口。 -->
        <div v-else class="state-block"><p>{{ requestedPage() > 1 && requestedPage() > data.pagination.total_pages ? data.pagination.total === 0 ? '此頁沒有資料。' : '此頁沒有資料，前面的頁面仍有內容。' : kind === 'members' && searchedKeyword() ? '沒有符合條件的會員。' : '目前沒有資料。' }}</p><a v-if="requestedPage() > 1" :href="pageHref(1)">回第一頁</a></div>
        <nav v-if="data.pagination.total_pages > 1 || requestedPage() > 1" class="pagination" aria-label="管理清單分頁"><a v-if="requestedPage() > 1" :href="pageHref(requestedPage() - 1)">上一頁</a><span>第 {{ requestedPage() }} 頁／共 {{ data.pagination.total_pages }} 頁</span><a v-if="requestedPage() < data.pagination.total_pages" :href="pageHref(requestedPage() + 1)">下一頁</a></nav>
      </template>
    </template>
  </main>
</template>
