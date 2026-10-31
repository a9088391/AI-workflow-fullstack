<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, emptySuccess, loginHref } from '../../../utils/contracts.js'
import { readLockoutSettings, validateLockoutSettings } from '../../../utils/admin.js'

useHead({ title: '登入鎖定設定｜文章集' })
const route = useRoute()
const site = useSite()
const current = ref(null)
const confirmed = ref(null)
const draft = ref(null)
const loading = ref(false)
const pending = ref(false)
const error = ref('')
const errors = ref({})
const logId = ref('')
const notice = ref('')
const unknown = ref(false)
const needsReadback = ref(false)
const summary = ref(null)
const fieldLabels = { enabled: '鎖定功能', window_minutes: '累計期間', failure_threshold: '失敗門檻', lock_seconds: '鎖定時間' }
let readController = null
let writeController = null
let draftOwner = null

// 手動重讀不需要額外清理回呼。
function noCleanup() {}

// 設定來自伺服器完整回應；重讀時保留已輸入草稿供使用者比較。
async function load(_value, _old, onCleanup = noCleanup) {
  readController?.abort()
  // 可見值載入期間會清空；最後確認值仍作為草稿與離頁提示的比較基準。
  const previous = confirmed.value
  // 重讀清除前次驗證結果；草稿由下方讀回規則獨立保留。
  current.value = null
  loading.value = false
  error.value = ''
  errors.value = {}
  logId.value = ''
  if (!site.ready.value || site.leaving.value || site.account.value?.role !== 'admin' || !site.token.value) return
  const request = new AbortController()
  readController = request
  onCleanup(
    // 身分或來源改變時停止舊設定讀取。
    () => request.abort(),
  )
  // GET 固定目前 token 與後端，較慢的舊管理回應不可覆蓋新帳號或來源。
  const token = site.token.value
  const backend = site.backend.value
  loading.value = true
  try {
    const response = await apiRequest('/api/settings/login-lockout', { token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || token !== site.token.value || backend !== site.backend.value) return
    const result = readLockoutSettings(response.status, response.body)
    if (result) {
      current.value = result
      confirmed.value = result
      needsReadback.value = false
      // 使用者已改過的草稿不因手動查證被覆蓋；未編輯時同步伺服器值。
      if (!draft.value || draftOwner !== site.account.value.id || previous && ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].every(
        // 四欄都仍等於舊伺服器值才代表沒有草稿變更。
        (name) => draft.value[name] === previous[name],
      )) { draft.value = { ...result }; draftOwner = site.account.value.id }
    } else {
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
      logId.value = failure.logId
    }
    // 連線失敗只影響目前讀取；原 PUT 未知事實與尚未送出的草稿不因此被確認。
  } catch {
    if (!request.signal.aborted) error.value = '無法讀取設定；請檢查連線後手動重試。'
  } finally { if (!request.signal.aborted) loading.value = false }
}

// 錯誤摘要聚焦表單第一個錯誤，並保留各欄原輸入。
async function showValidation(issues) {
  errors.value = issues
  error.value = '欄位資料不正確，請確認後再送出'
  await nextTick()
  summary.value?.focus()
}

// 完整設定原子送出；空成功回應不代表可直接顯示新值。
async function save() {
  if (!draft.value || !current.value || loading.value || pending.value || needsReadback.value || site.account.value?.role !== 'admin' || !site.token.value || site.leaving.value) return
  // 四欄依共同範圍一次驗證，關閉功能也不能跳過數值；失敗不送 PUT。
  const checked = validateLockoutSettings(draft.value)
  if (Object.keys(checked).length) { await showValidation(checked); return }
  const payload = { enabled: draft.value.enabled, window_minutes: draft.value.window_minutes, failure_threshold: draft.value.failure_threshold, lock_seconds: draft.value.lock_seconds }
  // 固定送出的身分與來源；管理更新沒有版本欄位，成功仍須 GET 讀回目前值。
  const request = new AbortController()
  writeController = request
  const token = site.token.value
  const backend = site.backend.value
  const owner = site.account.value.id
  pending.value = true
  site.adminPending.value = true
  error.value = ''
  errors.value = {}
  logId.value = ''
  notice.value = ''
  try {
    const response = await apiRequest('/api/settings/login-lockout', { method: 'PUT', body: payload, token, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || backend !== site.backend.value) return
    // 舊 token 結果不能撤銷新登入；只讓同帳號看到未確認結果或明確拒絕。
    if (token !== site.token.value) {
      // 新登入不可接收舊憑證寫入結果；只在同帳號顯示查證提示。
      if (!site.account.value || site.account.value.id === owner) {
        const failure = apiFailure(response.status, response.body)
        const rejected = response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102'
        unknown.value = !rejected
        needsReadback.value = !rejected
        error.value = rejected ? failure.message : '更新結果尚未確認；請重新登入並重讀設定。'
      }
      return
    }
    if (emptySuccess(response.status, response.body, 200)) {
      notice.value = '更新已送達；正在重新讀取伺服器設定。'
      await load()
      // 讀回等待期間若已切換身分或來源，舊寫入不可再更新新頁提示與草稿。
      if (request.signal.aborted || site.leaving.value || token !== site.token.value || backend !== site.backend.value) return
      if (current.value) draft.value = { ...current.value }
      notice.value = current.value ? '已重新讀取並顯示目前伺服器設定。' : '更新已送達，但尚未讀到伺服器設定；請手動重新讀取。'
      return
    }
    // 一般拒絕可人工修正；5xx／不完整回應可能已提交，必須先查證再決定。
    const failure = apiFailure(response.status, response.body, ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'])
    if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
    if (response.status >= 500 || !failure.code) { unknown.value = true; needsReadback.value = true }
    if (response.status === 403 && failure.code === 'E0003') current.value = null
    errors.value = failure.errors
    error.value = needsReadback.value ? `更新結果尚未確認；請先重讀設定查證。${failure.message}` : failure.message
    logId.value = failure.logId
    if (response.status === 422) { await nextTick(); summary.value?.focus() }
    // 連線例外保留原管理員的未知結果，不因前端取消就宣稱伺服器回滾。
  } catch {
    if (!request.signal.aborted && !site.leaving.value && backend === site.backend.value && (!site.account.value || site.account.value.id === owner)) { unknown.value = true; needsReadback.value = true; error.value = '更新結果尚未確認；請先重讀設定查證，再決定是否手動重試。' }
  // 完整寫入／讀回鏈由同一控制器收尾，避免舊 finally 清掉後來管理操作的等待。
  } finally { if (writeController === request) { writeController = null; pending.value = false; site.adminPending.value = false } }
}

// 手動複製 API 追蹤碼，剪貼簿不可用時仍能選取文字。
async function copyLogId() {
  try { await navigator.clipboard.writeText(logId.value); notice.value = '已複製追蹤編號。' }
  catch { notice.value = '無法自動複製；請手動選取追蹤編號。' }
}

// 欄位摘要直接帶鍵盤焦點到有問題的控制項。
function focusField(name) { document.getElementById(name)?.focus() }

// 後端、登入身分變動時，不把上一個管理員的草稿帶到新帳號。
watch([site.ready, site.account, site.token, site.backend], load, { immediate: true })
watch([draft, confirmed, pending, unknown],
  // 重讀期間不以暫空的可見值判斷草稿；在途 PUT 與未知結果也須提示捨棄。
  () => { site.dirty.value = !site.leaving.value && Boolean(pending.value || unknown.value || draft.value && confirmed.value && ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].some(
    // 僅比較四個已核准欄位，避免非表單狀態觸發提示。
    (name) => draft.value[name] !== confirmed.value[name],
  )) },
  { deep: true },
)
watch(site.account,
  // 只在確認新帳號後丟棄前一管理員的表單與未知寫入提示。
  (account) => { if (account && draftOwner !== null && account.id !== draftOwner) { draft.value = null; current.value = null; confirmed.value = null; unknown.value = false; needsReadback.value = false; writeController?.abort() } },
)
watch(site.backend,
  // 切換資料來源立即撤銷舊讀寫；舊草稿與比較基準也失效。
  () => { readController?.abort(); writeController?.abort(); draft.value = null; current.value = null; confirmed.value = null; unknown.value = false; needsReadback.value = false; notice.value = ''; draftOwner = null },
  { flush: 'sync' },
)
watch(site.leaving,
  // 前端切換立即撤銷目前讀寫請求。
  () => { readController?.abort(); writeController?.abort() },
  { flush: 'sync' },
)
onBeforeUnmount(
  // 卸載時清理設定頁持有的請求。
  () => { readController?.abort(); writeController?.abort(); site.dirty.value = false; site.adminPending.value = false },
)
</script>

<template>
  <main class="reading-page admin-page">
    <div class="author-heading">
      <div>
        <p class="section-label">文章集 / 管理</p>
        <h1>系統設定</h1>
      </div>
    </div>
    <h2 class="admin-setting-title">登入錯誤鎖定</h2>
    <p class="admin-intro">設定會員登入連續失敗時的鎖定規則。關閉鎖定會清除會員既有的鎖定與失敗計數；再次啟用時從零開始計算。</p>
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
        role="status">先前 PUT 結果尚未確認。{{ needsReadback ? '請先手動重新讀取目前伺服器設定；不會自動重送。' : '目前值已重讀，但可能已有其他管理員更新，無法藉此判定先前 PUT 是否成功。' }}</p>
      <section v-if="error"
        ref="summary"
        class="form-error"
        role="alert"
        tabindex="-1">
        <h2>請確認設定</h2>
        <p>{{ error }}</p>
        <ul v-if="Object.keys(errors).length">
          <li v-for="(messages, name) in errors"
            :key="name"><button type="button"
              class="inline-button"
              @click="focusField(name)">{{ fieldLabels[name] || name }}：{{ messages.join('；') }}</button></li>
        </ul>
        <small v-if="logId">log_id：{{ logId }}</small>
        <button v-if="logId"
          type="button"
          class="inline-button"
          @click="copyLogId">複製編號</button>
      </section>
      <p v-if="loading"
        class="state-block"
        role="status">正在讀取設定…</p>
      <button v-else
        type="button"
        class="secondary-button"
        :disabled="pending"
        @click="load">重新讀取目前設定{{ needsReadback ? '並查證' : '' }}</button>
      <p v-if="current"
        class="admin-current">目前伺服器值：{{ current.enabled ? '啟用' : '關閉' }}；{{ current.window_minutes }} 分鐘內失敗 {{ current.failure_threshold }} 次，鎖定 {{ current.lock_seconds }} 秒。</p>
      <form v-if="draft && current"
        class="admin-settings"
        novalidate
        @submit.prevent="save">
        <div>
          <label for="enabled">鎖定功能</label>
          <select id="enabled"
            v-model="draft.enabled"
            :disabled="pending"
            :aria-invalid="Boolean(errors.enabled)"
            :aria-describedby="errors.enabled ? 'enabled-error' : undefined"><option :value="true">啟用</option><option :value="false">關閉</option></select>
          <p>關閉後仍保存並驗證以下三個數值；重新啟用時沿用。</p>
          <p v-if="errors.enabled"
            id="enabled-error"
            class="form-error">{{ errors.enabled.join('；') }}</p>
        </div>
        <div v-for="field in [{ name: 'window_minutes', label: '累計期間（分鐘）', max: 60 }, { name: 'failure_threshold', label: '失敗門檻（次）', max: 100 }, { name: 'lock_seconds', label: '鎖定時間（秒）', max: 86400 }]"
          :key="field.name">
          <label :for="field.name">{{ field.label }}</label>
          <input :id="field.name"
            v-model.number="draft[field.name]"
            type="number"
            min="1"
            :max="field.max"
            step="1"
            inputmode="numeric"
            :disabled="pending"
            :aria-invalid="Boolean(errors[field.name])"
            :aria-describedby="errors[field.name] ? `${field.name}-error` : undefined">
          <p>可設定 1–{{ field.max }} 的整數。</p>
          <p v-if="errors[field.name]"
            :id="`${field.name}-error`"
            class="form-error">{{ errors[field.name].join('；') }}</p>
        </div>
        <p class="admin-preview">{{ draft.enabled ? `啟用：${draft.window_minutes} 分鐘內失敗 ${draft.failure_threshold} 次，鎖定 ${draft.lock_seconds} 秒。` : '關閉：會員登入失敗不會觸發暫時鎖定。' }}</p>
        <div class="admin-notes">
          <strong>注意事項</strong>
          <ol>
            <li>僅適用會員，不適用管理員。</li>
            <li>調整數值不改變現有鎖定的到期時間。</li>
            <li>關閉此功能會解除暫時鎖定，不影響停用帳號。</li>
          </ol>
        </div>
        <button class="primary-button"
          type="submit"
          :disabled="pending || needsReadback || loading">{{ pending ? '儲存中…' : '儲存完整設定' }}</button>
      </form>
    </template>
  </main>
</template>
