<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, emptySuccess, loginHref, positiveUrlInteger } from '../../../utils/contracts.js'
import { readMember } from '../../../utils/admin.js'

useHead({ title: '會員詳情｜文章集' })
const route = useRoute()
const site = useSite()
const member = ref(null)
const loading = ref(false)
const pending = ref(false)
const error = ref('')
const logId = ref('')
const statusError = ref('')
const errorSummary = ref(null)
const statusButton = ref(null)
const notice = ref('')
const unknown = ref(false)
const needsReadback = ref(false)
let readController = null
let writeController = null
let statusOwner = null

// 資源 ID 必須是 API 接受的安全正整數。
function memberId() { return positiveUrlInteger(route.params.id) }

// 同一元件切換會員 ID 時仍須重新讀取新的目標。
function routeId() { return route.params.id }

// 手動重讀使用同一程序，無 watch 清理時不附加動作。
function noCleanup() {}

// 重讀同一會員才顯示目前狀態；較晚的舊權限或舊後端資料不能覆蓋畫面。
async function load(_value, _old, onCleanup = noCleanup) {
  readController?.abort()
  member.value = null
  loading.value = false
  error.value = ''
  logId.value = ''
  statusError.value = ''
  if (!site.ready.value || site.leaving.value || site.account.value?.role !== 'admin' || !site.token.value) return
  const id = memberId()
  if (id === null) { error.value = '找不到指定資料或 API'; return }
  const request = new AbortController()
  readController = request
  onCleanup(
    // 路由與身分改變時取消舊會員讀取。
    () => request.abort(),
  )
  // GET 固定目前 token 與後端，較慢的舊管理回應不可覆蓋新帳號或來源。
  const token = site.token.value
  const backend = site.backend.value
  loading.value = true
  try {
    const response = await apiRequest(`/api/members/${id}`, { token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || token !== site.token.value || backend !== site.backend.value || id !== memberId()) return
    const result = readMember(response.status, response.body, id)
    if (result) { member.value = result; needsReadback.value = false }
    else {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
      logId.value = failure.logId
    }
    // 連線失敗只影響目前讀取；原 PUT 未知事實與尚未送出的草稿不因此被確認。
  } catch {
    if (!request.signal.aborted) error.value = '無法讀取會員；請檢查連線後手動重試。'
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 停用／恢復只送核准欄位；成功也要讀回，結果不明則先禁止再次寫入。
async function changeStatus() {
  if (!member.value || pending.value || loading.value || needsReadback.value || site.account.value?.role !== 'admin' || !site.token.value || site.leaving.value) return
  const id = memberId()
  // 依最後確認值決定下一狀態，每次仍要求使用者確認以免誤停用會員。
  const desired = member.value.status === 'active' ? 'disabled' : 'active'
  if (!window.confirm(`確定${desired === 'disabled' ? '停用' : '恢復'}這位會員嗎？`)) return
  // 固定送出的身分與來源；管理更新沒有版本欄位，成功仍須 GET 讀回目前值。
  const request = new AbortController()
  writeController = request
  const token = site.token.value
  const backend = site.backend.value
  const owner = site.account.value.id
  statusOwner = owner
  pending.value = true
  site.adminPending.value = true
  notice.value = ''
  error.value = ''
  logId.value = ''
  statusError.value = ''
  try {
    const response = await apiRequest(`/api/members/${id}/status`, { method: 'PUT', body: { status: desired }, token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || backend !== site.backend.value || id !== memberId()) return
    // 舊 token 結果不能撤銷新登入；只讓同帳號看到未確認結果或明確拒絕。
    if (token !== site.token.value) {
      // 舊憑證晚到時不能更新新帳號畫面；非明確拒絕仍須人工查證。
      if (!site.account.value || site.account.value.id === owner) {
        const failure = apiFailure(response.status, response.body)
        const rejected = response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102'
        unknown.value = !rejected
        needsReadback.value = !rejected
        error.value = rejected ? failure.message : '更新結果尚未確認；請重新登入並重讀同一會員。'
      }
      return
    }
    if (emptySuccess(response.status, response.body, 200)) {
      notice.value = '更新已送達；正在重新讀取會員狀態。'
      await load()
      // 讀回等待期間若切換身分、後端、會員或前端，舊 PUT 不可寫入新畫面的提示。
      if (request.signal.aborted || site.leaving.value || token !== site.token.value || backend !== site.backend.value || id !== memberId() || site.account.value?.id !== owner) return
      notice.value = member.value ? `已重讀會員，目前狀態為${member.value.status === 'active' ? '啟用' : '停用'}。` : '更新已送達，但尚未讀到最新會員狀態；請手動重新讀取。'
      return
    }
    // 一般拒絕可人工修正；5xx／不完整回應可能已提交，必須先查證再決定。
    const failure = apiFailure(response.status, response.body, ['status'])
    if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
    if (response.status >= 500 || !failure.code) { unknown.value = true; needsReadback.value = true }
    if (response.status === 403 && failure.code === 'E0003' || response.status === 404 && failure.code === 'E0004') member.value = null
    statusError.value = failure.errors.status?.join('；') || ''
    error.value = needsReadback.value ? `更新結果尚未確認；請先重讀同一會員查證。${failure.message}` : failure.message
    logId.value = failure.logId
    if (response.status === 422) { await nextTick(); errorSummary.value?.focus() }
    // 連線例外保留原管理員的未知結果，不因前端取消就宣稱伺服器回滾。
  } catch {
    if (!request.signal.aborted && !site.leaving.value && backend === site.backend.value && id === memberId() && (!site.account.value || site.account.value.id === owner)) { unknown.value = true; needsReadback.value = true; error.value = '更新結果尚未確認；請先重讀同一會員查證，再決定是否手動重試。' }
  // 本次 PUT 與其讀回一起結束才解除管理等待；舊控制器不能收尾新操作。
  } finally { if (writeController === request) { writeController = null; pending.value = false; site.adminPending.value = false } }
}

// 複製追蹤編號方便回報，同時保留可選取的原文。
async function copyLogId() {
  try { await navigator.clipboard.writeText(logId.value); notice.value = '已複製追蹤編號。' }
  catch { notice.value = '無法自動複製；請手動選取追蹤編號。' }
}

// 權限、來源與 ID 改變都重新授權讀取，避免沿用別人的資料。
watch([routeId, site.ready, site.account, site.token, site.backend], load, { immediate: true })
watch(site.account,
  // 同帳號重登保留查證提示；不同管理員不可繼承舊 PUT 或其未知結果。
  (account) => {
    if (!account || statusOwner === null || account.id === statusOwner) return
    // 先隔離在途請求及私有畫面，再讓新帳號的 watcher 重新取得資料。
    readController?.abort()
    writeController?.abort()
    writeController = null
    member.value = null
    loading.value = false
    pending.value = false
    site.adminPending.value = false
    site.dirty.value = false
    unknown.value = false
    needsReadback.value = false
    notice.value = ''
    error.value = ''
    logId.value = ''
    statusError.value = ''
    statusOwner = null
  },
  { flush: 'sync' },
)
watch([pending, unknown],
  // 等待寫入或尚待人工確認的結果都需在離頁前提醒。
  () => { site.dirty.value = !site.leaving.value && (pending.value || unknown.value) },
)
watch(site.leaving,
  // 前端切換立即撤銷請求，舊畫面不可恢復。
  () => { readController?.abort(); writeController?.abort() },
  { flush: 'sync' },
)
watch(site.backend,
  // 換後端後舊 PUT 結果與未知狀態不屬於新資料來源。
  () => { writeController?.abort(); statusOwner = null; unknown.value = false; needsReadback.value = false; notice.value = '' },
  { flush: 'sync' },
)
onBeforeUnmount(
  // 清理頁面持有的請求。
  () => { readController?.abort(); writeController?.abort(); site.adminPending.value = false; site.dirty.value = false },
)
</script>

<template>
  <main class="reading-page admin-page">
    <div class="author-heading">
      <div>
        <h1>會員詳情</h1>
      </div>
      <a class="secondary-button"
        href="/nuxt/admin/members">返回會員管理</a>
    </div>
    <p v-if="!site.ready.value"
      class="state-block"
      role="status">正在確認登入狀態…</p>
    <div v-else-if="!site.account.value"
      class="state-block">
      <p>請先登入以使用管理頁。</p>
      <a :href="loginHref(route.path)">前往登入</a>
    </div>
    <div v-else-if="site.account.value.role !== 'admin'"
      class="state-block"
      role="alert">沒有權限使用此功能。</div>
    <template v-else>
      <p v-if="notice"
        class="form-success"
        role="status">{{ notice }}</p>
      <p v-if="unknown"
        class="state-block"
        role="status">先前送出的操作是否完成仍無法判定。{{ needsReadback ? '請重新讀取同一會員，查證目前狀態後再決定是否操作。' : '目前狀態已重讀；再次操作仍需手動確認。' }}</p>
      <p v-if="loading"
        class="state-block"
        role="status">正在讀取會員…</p>
      <div v-else-if="error"
        ref="errorSummary"
        class="state-block"
        role="alert"
        tabindex="-1">
        <p>{{ error }}</p>
        <button v-if="statusError"
          type="button"
          class="inline-button"
          @click="statusButton?.focus()">會員狀態：{{ statusError }}</button>
        <small v-if="logId">log_id：{{ logId }}</small>
        <button v-if="logId"
          type="button"
          class="inline-button"
          @click="copyLogId">複製編號</button>
        <button type="button"
          class="inline-button"
          :disabled="pending"
          @click="load">重新讀取</button>
      </div>
      <article v-if="member && !loading"
        class="admin-detail">
        <dl>
          <div>
            <dt>會員 ID</dt>
            <dd>{{ member.id }}</dd>
          </div>
          <div>
            <dt>暱稱</dt>
            <dd>{{ member.nickname }}</dd>
          </div>
          <div>
            <dt>Email</dt>
            <dd>{{ member.email }}</dd>
          </div>
          <div>
            <dt>狀態</dt>
            <dd>{{ member.status === 'active' ? '啟用' : '停用' }}</dd>
          </div>
          <div>
            <dt>建立時間</dt>
            <dd>{{ member.created_at }}</dd>
          </div>
        </dl>
        <button ref="statusButton"
          class="secondary-button"
          type="button"
          :disabled="pending || needsReadback || site.leaving.value"
          :aria-describedby="statusError ? 'member-status-error' : undefined"
          @click="changeStatus">{{ pending ? '更新處理中…' : member.status === 'active' ? '停用會員' : '恢復會員' }}</button>
        <p v-if="statusError"
          id="member-status-error"
          class="form-error">{{ statusError }}</p>
      </article>
    </template>
  </main>
</template>
