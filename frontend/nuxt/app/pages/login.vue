<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, formatCountdown, loginData, remainingSeconds, retryAfterSeconds, validReturn, validateCredentials } from '../utils/contracts.js'

useHead({ title: '會員登入｜文章集' })
const route = useRoute()
const site = useSite()
const email = ref('')
const password = ref('')
const errors = ref({})
const summary = ref('')
const logId = ref('')
const pending = ref(false)
const secondsLeft = ref(0)
const retryReady = ref(false)
const summaryElement = ref(null)
const emailElement = ref(null)
const passwordElement = ref(null)
const registered = route.query.registered === '1'
const returnTo = validReturn(route.query.return_to) ? route.query.return_to : '/nuxt/'
const countdown = computed(
  // 倒數文字由剩餘秒數推導，並不修改後端鎖定期限。
  () => formatCountdown(secondsLeft.value),
)
let deadline = 0
let timer = null
let controller = null

// 離開登入頁時取消請求與計時器，舊帳號的回應不進入下一個表單。
onBeforeUnmount(
  // 清理本頁持有的兩種非同步工作。
  () => { controller?.abort(); if (timer) clearInterval(timer); site.dirty.value = false },
)

// 登入文字只屬目前頁面，供切換選單確認是否捨棄。
watch([email, password],
  // 帳密任一欄有文字時保護離頁。
  () => { site.dirty.value = Boolean(email.value || password.value) },
)
// 後端或前端一旦切換，同步取消舊 POST；不等待元件銷毀才作廢。
watch([site.backend, site.leaving],
  // 同步 abort 讓同一事件循環中完成的舊回應也無法登入。
  () => controller?.abort(),
  { flush: 'sync' },
)

// 鎖定顯示使用實際時鐘，歸零只恢復編輯而不自動送出帳密。
function tick() {
  secondsLeft.value = remainingSeconds(deadline)
  // 歸零只清計時器並恢復人工操作，不因倒數結束自動送帳密。
  if (secondsLeft.value === 0) {
    clearInterval(timer)
    timer = null
    retryReady.value = true
    summary.value = ''
  }
}

// 新的有效 Retry-After 取代舊倒數；不接受日期或缺少秒數時自行猜測。
function startCountdown(seconds) {
  if (timer) clearInterval(timer)
  deadline = Date.now() + seconds * 1000
  retryReady.value = false
  tick()
  if (secondsLeft.value > 0) timer = setInterval(tick, 250)
}

// 表單錯誤移到焦點供鍵盤使用者閱讀，欄位錯誤留在各欄附近。
async function showError(message, details = {}, id = '') {
  summary.value = message
  errors.value = details
  logId.value = id
  // 等摘要真正渲染後再移動焦點，讓鍵盤使用者能讀到本次錯誤。
  await nextTick()
  summaryElement.value?.focus()
}

// 摘要先取得焦點；使用者點擊導引時再移到表單順序中的第一個錯誤欄位。
function focusFirstError() {
  if (errors.value.email?.length) emailElement.value?.focus()
  else if (errors.value.password?.length) passwordElement.value?.focus()
}

// 倒數結束後改用另一個 Email 時，舊帳號的「可再次嘗試」提示不再適用。
function clearRetryReady() { retryReady.value = false }

// 登入前先確認格式；鎖定中含 Enter 都不可送出，成功僅保存原到期時間。
async function submit() {
  if (pending.value || secondsLeft.value > 0 || site.checking.value || site.leaving.value) return
  // 先回饋共同欄位格式，避免無效帳密送到後端並影響登入判定。
  const checked = validateCredentials({ email: email.value, password: password.value })
  if (Object.keys(checked.errors).length) { await showError('欄位資料不正確，請確認後再送出', checked.errors); return }
  // 新的一次人工提交清除前次摘要，pending 同時擋住按鈕與 Enter 重複提交。
  pending.value = true
  retryReady.value = false
  summary.value = ''
  errors.value = {}
  logId.value = ''
  controller = new AbortController()
  const request = controller
  const sentBackend = site.backend.value
  // 送出的來源與控制器固定到本次請求，回應先確認歸屬才可改動表單。
  try {
    const response = await apiRequest('/api/auth/login', { method: 'POST', body: { email: checked.email, password: password.value }, signal: request.signal })
    if (request.signal.aborted || sentBackend !== site.backend.value || site.leaving.value) return
    // 完整登入成功才保存憑證並回安全目標，不能因不完整 200 提早離頁。
    const data = loginData(response.status, response.body)
    if (data) {
      if (!site.acceptLogin(data)) return
      site.dirty.value = false
      window.location.assign(returnTo)
      return
    }
    // 錯誤依 HTTP／error_code 分流，欄位明細與追蹤碼保留供修正與回報。
    const failure = apiFailure(response.status, response.body, ['email', 'password'])
    // 鎖定秒數只接受有效標頭；沒有秒數時不虛構倒數，也不自動再登入。
    if (response.status === 429 && failure.code === 'E0105') {
      const seconds = retryAfterSeconds(response.retryAfter)
      if (seconds !== null) startCountdown(seconds)
      else await showError(`${failure.message}。無法取得有效倒數秒數，請稍後再試。`, {}, failure.logId)
    // 停用是明確失權，不能繼續沿用目前頁面的已驗證身分。
    } else if (response.status === 403 && failure.code === 'E0102') {
      site.forget(failure.message)
    }
    if (secondsLeft.value > 0) await showError('登入暫時鎖定', {}, failure.logId)
    else if (!(response.status === 429 && failure.code === 'E0105' && retryAfterSeconds(response.retryAfter) === null)) await showError(failure.message, failure.errors, failure.logId)
  } catch {
    if (!request.signal.aborted && !site.leaving.value) await showError('無法確認登入結果；請手動重新驗證後再試。')
    // 無法連線只提示手動確認；取消來源不再回填舊表單。
  } finally {
    // 只有這次請求仍持有控制器才解除 pending，避免舊 finally 干擾後來提交。
    if (controller === request) { pending.value = false; controller = null }
  }
}
</script>

<template>
  <main class="form-page">
    <div class="form-intro">
      <h1>會員登入</h1>
      <p>登入後仍使用同一次登入的原到期時間。</p>
    </div>
    <form class="auth-form"
      novalidate
      @submit.prevent="submit">
      <h2>會員登入</h2>
      <p v-if="registered"
        class="form-success"
        role="status">註冊成功，請登入。</p>
      <p v-if="summary"
        ref="summaryElement"
        class="form-error"
        tabindex="-1"
        role="alert">{{ summary }} <small v-if="logId">log_id：{{ logId }}</small><button v-if="errors.email?.length || errors.password?.length"
          type="button"
          class="inline-button"
          @click="focusFirstError">前往第一個錯誤欄位</button></p>
      <p v-if="secondsLeft > 0"
        class="lock-message">登入暫時鎖定，約剩 <strong>{{ countdown }}</strong></p>
      <p v-if="retryReady"
        class="form-success"
        role="status">可再次嘗試登入。</p>
      <label for="login-email">Email</label>
      <input id="login-email"
        ref="emailElement"
        v-model="email"
        autocomplete="email"
        inputmode="email"
        :aria-invalid="Boolean(errors.email)"
        :aria-describedby="errors.email ? 'login-email-error' : undefined"
        :disabled="pending || secondsLeft > 0 || site.checking.value"
        @input="clearRetryReady">
      <small v-if="errors.email"
        id="login-email-error"
        class="field-error">{{ errors.email.join('；') }}</small>
      <label for="login-password">密碼</label>
      <input id="login-password"
        ref="passwordElement"
        v-model="password"
        type="password"
        autocomplete="current-password"
        :aria-invalid="Boolean(errors.password)"
        :aria-describedby="errors.password ? 'login-password-error' : undefined"
        :disabled="pending || secondsLeft > 0 || site.checking.value">
      <small v-if="errors.password"
        id="login-password-error"
        class="field-error">{{ errors.password.join('；') }}</small>
      <button class="primary-button"
        :disabled="pending || secondsLeft > 0 || site.checking.value"
        type="submit">{{ pending ? '登入中…' : '登入' }}</button>
      <p class="form-foot">尚未加入？ <a href="/nuxt/register">註冊會員</a></p>
    </form>
  </main>
</template>
