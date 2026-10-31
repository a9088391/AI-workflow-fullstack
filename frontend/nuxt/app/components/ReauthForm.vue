<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, formatCountdown, loginData, remainingSeconds, retryAfterSeconds, validateCredentials } from '../utils/contracts.js'

const props = defineProps({ ownerId: { type: Number, default: null } })
const emit = defineEmits(['restored', 'different'])
const site = useSite()
const email = ref('')
const password = ref('')
const error = ref('')
const errors = ref({})
const summaryElement = ref(null)
const emailElement = ref(null)
const passwordElement = ref(null)
const logId = ref('')
const pending = ref(false)
const secondsLeft = ref(0)
let controller = null
let timer = null
let deadline = 0

// 倒數依實際時鐘計算，背景分頁節流不延長登入鎖定提示。
function tick() {
  secondsLeft.value = remainingSeconds(deadline)
  // 歸零只清計時器並恢復人工操作，不因倒數結束自動送帳密。
  if (secondsLeft.value === 0) { clearInterval(timer); timer = null; if (error.value === '登入暫時鎖定') error.value = '' }
}

// 只接受伺服器有效 Retry-After，不自造鎖定期限。
function startCountdown(value) {
  const seconds = retryAfterSeconds(value)
  if (seconds === null) return false
  clearInterval(timer)
  deadline = Date.now() + seconds * 1000
  tick()
  if (secondsLeft.value > 0) timer = setInterval(tick, 250)
  return true
}

// 欄位錯誤從摘要導向 Email 或密碼，鍵盤操作不需自行找欄位。
function focusFirstError() { if (errors.value.email?.length) emailElement.value?.focus(); else if (errors.value.password?.length) passwordElement.value?.focus() }

// 原頁重新登入只恢復同帳號操作；不同帳號交由父表單丟棄舊文字。
async function submit() {
  if (pending.value || secondsLeft.value > 0 || site.leaving.value) return
  // 先回饋共同欄位格式，避免無效帳密送到後端並影響登入判定。
  const checked = validateCredentials({ email: email.value, password: password.value })
  if (Object.keys(checked.errors).length) { errors.value = checked.errors; error.value = '請確認 Email 與密碼格式。'; await nextTick(); summaryElement.value?.focus(); return }
  // 保留本次後端與控制器，原頁重登的結果不能帶入切換後的新前端。
  const request = new AbortController()
  controller = request
  const backend = site.backend.value
  pending.value = true
  error.value = ''
  errors.value = {}
  logId.value = ''
  // 送出的來源與控制器固定到本次請求，回應先確認歸屬才可改動表單。
  try {
    const response = await apiRequest('/api/auth/login', { method: 'POST', body: { email: checked.email, password: password.value }, signal: request.signal })
    if (request.signal.aborted || backend !== site.backend.value || site.leaving.value) return
    // 登入可換成另一帳號；父表單依 ownerId 決定保留或丟棄原作者草稿。
    const data = loginData(response.status, response.body)
    if (data) {
      if (!site.acceptLogin(data)) return
      password.value = ''
      email.value = ''
      if (props.ownerId !== null && data.user.id !== props.ownerId) emit('different')
      else emit('restored')
      return
    }
    // 錯誤依 HTTP／error_code 分流，欄位明細與追蹤碼保留供修正與回報。
    const failure = apiFailure(response.status, response.body, ['email', 'password'])
    errors.value = failure.errors
    // 倒數與停用分開處理；沒有有效秒數時不虛構鎖定時間。
    if (response.status === 429 && failure.code === 'E0105') {
      if (!startCountdown(response.retryAfter)) error.value = `${failure.message}。無法取得有效倒數秒數，請稍後再試。`
      else error.value = '登入暫時鎖定'
    } else {
      if (response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
      error.value = failure.message
    }
    logId.value = failure.logId
    if (response.status === 422) { await nextTick(); summaryElement.value?.focus() }
    // 連線例外不能證明登入失敗或成功，只提供人工確認，不自動重送。
  } catch {
    if (!request.signal.aborted) error.value = '無法確認登入結果；請手動重新驗證後再試。'
  // 最後只收尾仍由本表單持有的請求；取消不代表伺服器已撤銷登入操作。
  } finally { if (controller === request) { controller = null; pending.value = false } }
}

watch([site.backend, site.leaving],
  // 切換後端或前端時立刻取消原登入請求與倒數。
  () => { controller?.abort(); clearInterval(timer); timer = null; secondsLeft.value = 0 },
  { flush: 'sync' },
)
onBeforeUnmount(
  // 離開恢復表單時清除計時器與在途請求。
  () => { controller?.abort(); clearInterval(timer) },
)
</script>

<template>
  <form class="reauth-form"
    novalidate
    @submit.prevent="submit">
    <h2>在原頁重新登入</h2>
    <p>同一帳號登入後，請自行檢查並手動送出。切換帳號會丟棄舊文字。</p>
    <p v-if="error"
      ref="summaryElement"
      class="form-error"
      tabindex="-1"
      role="alert">{{ error }}<small v-if="logId">log_id：{{ logId }}</small><button v-if="errors.email?.length || errors.password?.length"
        type="button"
        class="inline-button"
        @click="focusFirstError">前往第一個錯誤欄位</button></p>
    <p v-if="secondsLeft > 0"
      class="lock-message">登入暫時鎖定，約剩 {{ formatCountdown(secondsLeft) }}</p>
    <label for="reauth-email">Email</label>
    <input id="reauth-email"
      ref="emailElement"
      v-model="email"
      type="email"
      autocomplete="email"
      :aria-invalid="Boolean(errors.email)"
      :aria-describedby="errors.email ? 'reauth-email-error' : undefined"
      :disabled="pending || secondsLeft > 0">
    <small v-if="errors.email"
      id="reauth-email-error"
      class="field-error">{{ errors.email.join('；') }}</small>
    <label for="reauth-password">密碼</label>
    <input id="reauth-password"
      ref="passwordElement"
      v-model="password"
      type="password"
      autocomplete="current-password"
      :aria-invalid="Boolean(errors.password)"
      :aria-describedby="errors.password ? 'reauth-password-error' : undefined"
      :disabled="pending || secondsLeft > 0">
    <small v-if="errors.password"
      id="reauth-password-error"
      class="field-error">{{ errors.password.join('；') }}</small>
    <button class="primary-button"
      type="submit"
      :disabled="pending || secondsLeft > 0">{{ pending ? '登入中…' : '重新登入' }}</button>
  </form>
</template>
