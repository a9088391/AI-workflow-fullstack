<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, emptySuccess, validateCredentials } from '../utils/contracts.js'

useHead({ title: '註冊會員｜文章集' })
const email = ref('')
const password = ref('')
const nickname = ref('')
const errors = ref({})
const summary = ref('')
const logId = ref('')
const pending = ref(false)
const created = ref(false)
const summaryElement = ref(null)
const successElement = ref(null)
const emailElement = ref(null)
const passwordElement = ref(null)
const nicknameElement = ref(null)
let controller = null
const site = useSite()

// 離開表單時取消尚未完成的請求，避免舊回應更新已離開的畫面。
onBeforeUnmount(
  // 取消本頁唯一在途註冊請求。
  () => { controller?.abort(); site.dirty.value = false },
)

// 註冊草稿留在本頁記憶體，讓使用者先決定是否捨棄。
watch([email, password, nickname],
  // 確認建立前任一欄有文字即保護離頁。
  () => { site.dirty.value = !created.value && Boolean(email.value || password.value || nickname.value) },
)
// 換前端或後端同步取消舊 POST，可能已送達的結果不由新頁重送。
watch([site.backend, site.leaving],
  // 回應即使忽略 abort 仍由提交函式的離頁條件擋住。
  () => controller?.abort(),
  { flush: 'sync' },
)

// 錯誤摘要移到鍵盤焦點，欄位明細同時靠近欄位顯示。
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
  else if (errors.value.nickname?.length) nicknameElement.value?.focus()
}

// 註冊只送三個核准欄位；成功後轉登入，寫入結果不明時不自動重送。
async function submit() {
  if (pending.value || created.value || site.leaving.value) return
  // 先檢查共同帳密與暱稱格式，無效輸入留在原表單修正，不送出建立帳號請求。
  const checked = validateCredentials({ email: email.value, password: password.value, nickname: nickname.value }, true)
  if (Object.keys(checked.errors).length) { await showError('欄位資料不正確，請確認後再送出', checked.errors); return }
  // 重新提交只清本表單的舊錯誤，先鎖住表單避免建立重複帳號。
  pending.value = true
  summary.value = ''
  errors.value = {}
  logId.value = ''
  controller = new AbortController()
  const request = controller
  const sentBackend = site.backend.value
  // 送出的來源與控制器固定到本次請求，回應先確認歸屬才可改動表單。
  try {
    const response = await apiRequest('/api/auth/register', { method: 'POST', body: { email: checked.email, password: password.value, nickname: nickname.value }, signal: request.signal })
    if (request.signal.aborted || sentBackend !== site.backend.value || site.leaving.value) return
    // 只有完整 201 才進入成功回退畫面；一般寫入不從空 data 猜帳號資料。
    if (emptySuccess(response.status, response.body, 201)) {
      // 確認建立後先鎖住表單並清除密碼；導頁未完成或返回時仍可手動前往登入。
      created.value = true
      site.dirty.value = false
      password.value = ''
      await nextTick()
      successElement.value?.focus()
      // 以 replace 避免已成功的註冊頁留在上一頁歷史紀錄。
      window.location.replace('/nuxt/login?registered=1')
      return
    }
    // 錯誤依 HTTP／error_code 分流，欄位明細與追蹤碼保留供修正與回報。
    const failure = apiFailure(response.status, response.body, ['email', 'password', 'nickname'])
    await showError(response.status >= 500 || response.status === 201 ? `無法確認註冊結果；請先確認帳號狀態，再決定是否重試。${failure.message}` : failure.message, failure.errors, failure.logId)
  } catch {
    // 已確認 201 後若導頁失敗，保留成功回退畫面，不誤報為寫入結果不明。
    if (!created.value && !request.signal.aborted && sentBackend === site.backend.value && !site.leaving.value) await showError('無法確認註冊結果；請先確認帳號狀態，再決定是否重試。')
    // 註冊頁僅持有這一個提交，finally 解除等待；不代表伺服器寫入已取消。
  } finally { pending.value = false; controller = null }
}
</script>

<template>
  <main class="form-page">
    <div class="form-intro">
      <h1>註冊會員</h1>
      <p>建立帳號後，請再登入以使用會員功能。</p>
    </div>
    <form class="auth-form"
      novalidate
      @submit.prevent="submit">
      <h2>註冊會員</h2>
      <template v-if="created">
        <p ref="successElement"
          class="form-success"
          tabindex="-1"
          role="status">註冊成功，請登入。若未自動轉頁，請使用下方連結。</p>
        <p class="form-foot"><a href="/nuxt/login?registered=1">前往登入</a></p>
      </template>
      <template v-else>
        <p v-if="summary"
          ref="summaryElement"
          class="form-error"
          tabindex="-1"
          role="alert">{{ summary }} <small v-if="logId">log_id：{{ logId }}</small><button v-if="errors.email?.length || errors.password?.length || errors.nickname?.length"
            type="button"
            class="inline-button"
            @click="focusFirstError">前往第一個錯誤欄位</button></p>
        <label for="register-email">Email</label>
        <input id="register-email"
          ref="emailElement"
          v-model="email"
          autocomplete="email"
          inputmode="email"
          :aria-invalid="Boolean(errors.email)"
          :aria-describedby="errors.email ? 'register-email-error' : undefined"
          :disabled="pending">
        <small v-if="errors.email"
          id="register-email-error"
          class="field-error">{{ errors.email.join('；') }}</small>
        <label for="register-password">密碼</label>
        <input id="register-password"
          ref="passwordElement"
          v-model="password"
          type="password"
          autocomplete="new-password"
          :aria-invalid="Boolean(errors.password)"
          :aria-describedby="errors.password ? 'register-password-error' : undefined"
          :disabled="pending">
        <small v-if="errors.password"
          id="register-password-error"
          class="field-error">{{ errors.password.join('；') }}</small>
        <p class="field-hint">4–16 碼英文字母或數字</p>
        <label for="register-nickname">暱稱</label>
        <input id="register-nickname"
          ref="nicknameElement"
          v-model="nickname"
          autocomplete="nickname"
          :aria-invalid="Boolean(errors.nickname)"
          :aria-describedby="errors.nickname ? 'register-nickname-error' : undefined"
          :disabled="pending">
        <small v-if="errors.nickname"
          id="register-nickname-error"
          class="field-error">{{ errors.nickname.join('；') }}</small>
        <button class="primary-button"
          :disabled="pending"
          type="submit">{{ pending ? '建立中…' : '建立帳號' }}</button>
        <p class="form-foot">已經有帳號？ <a href="/nuxt/login">前往登入</a></p>
      </template>
    </form>
  </main>
</template>
