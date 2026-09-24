<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { API_BASE, clearSession, formatCountdown, interpretLogin, interpretLogout, interpretMe, readSession, remainingSeconds, saveSession, validateLogin } from './auth.js'
import { interpretRegistrationResponse, validateRegistration } from './registration.js'

const path = window.location.pathname
const page = path === '/vue/' ? 'articles' : path === '/vue/register' ? 'register' : path === '/vue/login' ? 'login' : 'missing'
// 各路徑使用自己的分頁名稱，避免重載後把文章或找不到頁面誤認為註冊。
document.title = { articles: '文章集｜文章入口', register: '文章集｜註冊會員', login: '文章集｜會員登入', missing: '文章集｜找不到頁面' }[page]
const registered = page === 'login' && new URLSearchParams(window.location.search).get('registered') === '1'
const email = ref('')
const password = ref('')
const nickname = ref('')
const errors = ref({})
const summary = ref('')
const summaryCode = ref('')
const logId = ref('')
const copyState = ref('')
const pending = ref(false)
const credential = ref(null)
const account = ref(null)
const authChecking = ref(false)
const logoutPending = ref(false)
const lockDeadline = ref(0)
const lockNow = ref(0)
const lockNotice = ref('')
const navOpen = ref(false)
const versionsOpen = ref(false)
const navToggle = ref(null)
const versionsToggle = ref(null)
const errorSummary = ref(null)
let requestController = null
let timeout = null
let submitted = false
let authController = null
let authTimeout = null
let authEpoch = 0
let lockTimer = null
let alive = false

// 倒數是記憶體狀態；用單調時鐘校正分頁背景節流後的實際剩餘時間。
const lockedSeconds = computed(() => lockDeadline.value ? remainingSeconds(lockDeadline.value, lockNow.value) : 0)
// 表單停用狀態只依尚未歸零的有效期限決定。
const locked = computed(() => lockedSeconds.value > 0)
// 倒數期間顯示剩餘時間；結束後顯示一次可重試提示。
const lockText = computed(() => locked.value ? `登入暫時鎖定，約剩 ${formatCountdown(lockedSeconds.value)}` : lockNotice.value)

// 草稿僅存在目前頁面記憶體；任何未送出內容都應觸發離頁確認。
const hasDraft = computed(() => page === 'register' && !submitted && Boolean(email.value || password.value || nickname.value))
const fieldOrder = ['email', 'password', 'nickname']
const fieldNames = { email: 'Email', password: '密碼', nickname: '暱稱' }

// 摘要只列出有訊息的欄位，避免空的後端陣列產生無作用連結。
function hasFieldError(field) {
  return Boolean(errors.value[field]?.length)
}

// 隨驗證狀態更新摘要欄位，模板不需要定義未註解的 callback。
const errorFields = computed(() => fieldOrder.filter(hasFieldError))

// 保留瀏覽器原生捨棄確認；未完成送出時離開仍可能由後端完成建立。
function warnOnLeave(event) {
  if (!hasDraft.value) return
  event.preventDefault()
  event.returnValue = ''
}

// 切頁時撤銷等待中的前端請求，避免舊回應覆蓋目前畫面。
function stopRequest() {
  if (timeout) clearTimeout(timeout)
  timeout = null
  requestController?.abort()
  requestController = null
}

// 離頁或下一次認證請求會撤銷舊回應；epoch 再擋住未遵守 abort 的完成事件。
function stopAuthRequest() {
  authEpoch += 1
  if (authTimeout) clearTimeout(authTimeout)
  authTimeout = null
  authController?.abort()
  authController = null
}

// 每次認證操作只保留最新請求，逾時仍視為結果未知並交由使用者手動重試。
function startAuthRequest() {
  stopAuthRequest()
  authController = new AbortController()
  const controller = authController
  const epoch = authEpoch
  // 逾時撤銷等待；後端是否完成仍由回應確認。
  authTimeout = setTimeout(() => controller.abort(), 20000)
  return { controller, epoch }
}

// 非同步結果只可寫回當前頁面與同一次請求。
function currentAuthRequest(request) {
  return alive && authController === request.controller && authEpoch === request.epoch
}

// 結束仍屬最新的一次請求；舊請求不得清掉新請求的逾時計時。
function finishAuthRequest(request) {
  if (!currentAuthRequest(request)) return
  if (authTimeout) clearTimeout(authTimeout)
  authTimeout = null
  authController = null
}

// 倒數到零立即還原表單，不輪詢伺服器也不自動再送登入。
function tickLock() {
  lockNow.value = performance.now()
  if (lockDeadline.value && remainingSeconds(lockDeadline.value, lockNow.value) === 0) {
    clearInterval(lockTimer)
    lockTimer = null
    lockDeadline.value = 0
    lockNotice.value = '可再次嘗試登入'
  }
}

// 新鎖定覆蓋舊期限；遺失標頭只能顯示訊息，不能推測秒數。
function setLock(seconds) {
  clearLock()
  if (!seconds) {
    lockNotice.value = '登入暫時鎖定，請稍後手動重試。'
    console.warn('Login lockout response has invalid Retry-After header')
    return
  }
  lockNow.value = performance.now()
  lockDeadline.value = lockNow.value + seconds * 1000
  lockTimer = setInterval(tickLock, 250)
}

// 登入成功或帳號停用時完整移除舊計時器與提示。
function clearLock() {
  if (lockTimer) clearInterval(lockTimer)
  lockTimer = null
  lockDeadline.value = 0
  lockNotice.value = ''
}

// 改變帳密使前次請求失效；解鎖後只在更換 Email 時清除該帳號的舊鎖定訊息。
watch([email, password], ([nextEmail], [previousEmail]) => {
  if (page !== 'login') return
  if (pending.value) {
    stopAuthRequest()
    pending.value = false
  }
  if (nextEmail !== previousEmail && !locked.value && lockNotice.value) {
    lockNotice.value = ''
    if (summaryCode.value === 'E0105') {
      summary.value = ''
      summaryCode.value = ''
      errors.value = {}
      logId.value = ''
      copyState.value = ''
    }
  }
})

// 收合導覽完成後再聚焦按鈕，避免焦點落在已隱藏項目。
function focusNavToggle() {
  navToggle.value?.focus()
}

// 收合版本面板完成後再聚焦按鈕，維持鍵盤操作位置。
function focusVersionsToggle() {
  versionsToggle.value?.focus()
}

// 關閉收合區後把鍵盤焦點還給開啟控制，避免焦點停在隱藏內容。
function closePanel(panel) {
  if (panel === 'nav') {
    navOpen.value = false
    nextTick(focusNavToggle)
  } else {
    versionsOpen.value = false
    nextTick(focusVersionsToggle)
  }
}

// 開關主導覽時收起版本面板，讓小螢幕只顯示一組操作。
function toggleNav() {
  navOpen.value = !navOpen.value
  if (navOpen.value) versionsOpen.value = false
}

// 開關版本面板時收起主導覽，避免兩組控制互相覆蓋。
function toggleVersions() {
  versionsOpen.value = !versionsOpen.value
  if (versionsOpen.value) navOpen.value = false
}

// Escape 可關閉目前面板，供鍵盤使用者快速回到導覽起點。
function onEscape(event) {
  if (event.key !== 'Escape') return
  if (versionsOpen.value) closePanel('versions')
  else if (navOpen.value) closePanel('nav')
}

// 按下錯誤摘要項目時直接聚焦對應欄位，讓修正路徑清楚。
function focusField(name) {
  document.getElementById(name)?.focus()
}

// 每次錯誤都先聚焦摘要，使螢幕閱讀器先聽到整體結果。
async function showError(message, nextErrors = {}, trace = '', code = '') {
  summary.value = message
  summaryCode.value = code
  errors.value = nextErrors
  logId.value = trace
  copyState.value = ''
  await nextTick()
  errorSummary.value?.focus()
}

// 實際 POST 到目前 Laravel API；只有確認 201 成功 envelope 才宣告註冊完成。
async function submitRegistration() {
  // 同時阻擋 Enter 與按鈕重複送出；先回饋格式錯誤，保留三欄原輸入。
  if (pending.value) return
  const checked = validateRegistration({ email: email.value, password: password.value, nickname: nickname.value })
  if (Object.keys(checked.errors).length) {
    await showError('欄位資料不正確，請確認後再送出', checked.errors)
    return
  }

  pending.value = true
  summary.value = ''
  summaryCode.value = ''
  errors.value = {}
  logId.value = ''
  requestController = new AbortController()
  // 逾時只終止前端等待，不能據此推斷伺服器未寫入。
  timeout = setTimeout(() => requestController?.abort(), 20000)
  try {
    // 只送 API 契約欄位；後端仍是唯一性及實際建立結果的權威。
    const response = await fetch('http://localhost:8081/api/auth/register', {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: checked.email, password: password.value, nickname: nickname.value }),
      signal: requestController.signal,
    })
    const body = await response.json()
    if (!alive || !requestController) return
    const result = interpretRegistrationResponse(response.status, body)
    // 只有完整成功回應才能轉頁；失敗時保留欄位與 log_id 供修正或查證。
    if (result.ok) {
      submitted = true
      password.value = ''
      window.location.assign('/vue/login?registered=1')
      return
    }
    await showError(result.message, result.errors, result.logId)
  } catch {
    if (alive) await showError('連線中斷或伺服器回應不完整，無法確認註冊結果；請先確認帳號狀態，再決定是否重試。')
  } finally {
    stopRequest()
    if (alive) pending.value = false
  }
}

// 停用或憑證失效會立刻隱藏已授權畫面，避免下次重載再沿用舊 JWT。
function forgetCredential() {
  account.value = null
  credential.value = null
  try { clearSession(sessionStorage) } catch { /* 儲存不可用時，仍先撤銷畫面授權。 */ }
}

// 重新整理只能從保存的 token 嘗試驗證，GET /api/me 成功前不顯示帳號權限。
async function verifyAccount() {
  // 認證共用取消機制；重驗不得中斷登入或登出，否則舊操作的等待狀態無法正常結束。
  if (!credential.value || authChecking.value || pending.value || logoutPending.value) return
  authChecking.value = true
  account.value = null
  summary.value = ''
  const request = startAuthRequest()
  const token = credential.value.accessToken
  try {
    const response = await fetch(`${API_BASE}/api/me`, { headers: { Accept: 'application/json', Authorization: `Bearer ${token}` }, signal: request.controller.signal })
    const body = await response.json()
    if (!currentAuthRequest(request) || credential.value?.accessToken !== token) return
    const result = interpretMe(response.status, body)
    if (result.ok) {
      account.value = result.account
      if (page === 'login') window.location.assign('/vue/')
    } else {
      if ((response.status === 401 && result.code === 'E0002') || (response.status === 403 && result.code === 'E0102')) forgetCredential()
      await showError(result.message, {}, result.logId)
    }
  } catch {
    if (currentAuthRequest(request)) await showError('目前無法確認登入狀態，憑證已保留；請手動重新驗證。')
  } finally {
    if (currentAuthRequest(request)) {
      authChecking.value = false
      finishAuthRequest(request)
    }
  }
}

// 只有格式合法、伺服器完整成功且憑證已保存，才進入已登入入口。
async function submitLogin() {
  if (pending.value || locked.value || authChecking.value) return
  const checked = validateLogin({ email: email.value, password: password.value })
  if (Object.keys(checked.errors).length) {
    await showError('欄位資料不正確，請確認後再送出', checked.errors)
    return
  }
  pending.value = true
  summary.value = ''
  summaryCode.value = ''
  lockNotice.value = ''
  errors.value = {}
  logId.value = ''
  const request = startAuthRequest()
  const submittedPassword = password.value
  try {
    // 登入不攜帶舊 JWT；此請求只代表目前表單中的帳號與密碼。
    const response = await fetch(`${API_BASE}/api/auth/login`, {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: checked.email, password: submittedPassword }),
      signal: request.controller.signal,
    })
    const body = await response.json()
    if (!currentAuthRequest(request) || checked.email !== validateLogin({ email: email.value, password: password.value }).email || submittedPassword !== password.value) return
    const result = interpretLogin(response.status, body, response.headers.get('Retry-After'))
    if (result.ok) {
      try {
        saveSession(sessionStorage, result.accessToken, result.expiresAt)
      } catch {
        await showError('無法保存登入狀態，請檢查瀏覽器儲存空間後手動重試。')
        return
      }
      credential.value = { backend: API_BASE, accessToken: result.accessToken, expiresAt: result.expiresAt }
      account.value = result.account
      clearLock()
      window.location.assign('/vue/')
      return
    }
    if (response.status === 403 && result.code === 'E0102') {
      forgetCredential()
      clearLock()
    }
    if (response.status === 429 && result.code === 'E0105') setLock(result.retryAfter)
    await showError(result.message, result.errors, result.logId, result.code)
  } catch {
    if (currentAuthRequest(request)) await showError('連線中斷或伺服器回應不完整；登入結果無法確認，請手動重試。')
  } finally {
    if (currentAuthRequest(request)) {
      pending.value = false
      finishAuthRequest(request)
    }
  }
}

// 伺服器確認撤銷前保留憑證；失敗時明示未登出並讓使用者手動重試。
async function submitLogout() {
  if (logoutPending.value || !account.value || !credential.value) return
  logoutPending.value = true
  summary.value = ''
  const request = startAuthRequest()
  const token = credential.value.accessToken
  try {
    const response = await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST', headers: { Accept: 'application/json', Authorization: `Bearer ${token}` }, signal: request.controller.signal })
    const body = await response.json()
    if (!currentAuthRequest(request) || credential.value?.accessToken !== token) return
    const result = interpretLogout(response.status, body)
    if (result.ok) {
      forgetCredential()
      window.location.assign('/vue/login')
      return
    }
    const invalidCredential = (response.status === 401 && result.code === 'E0002') || (response.status === 403 && result.code === 'E0102')
    if (invalidCredential) forgetCredential()
    await showError(invalidCredential ? result.message : `登出未完成。${result.message}`, {}, result.logId)
  } catch {
    if (currentAuthRequest(request)) await showError('登出未完成：連線中斷或伺服器回應不完整；憑證已保留，請手動重試。')
  } finally {
    if (currentAuthRequest(request)) {
      logoutPending.value = false
      finishAuthRequest(request)
    }
  }
}

// log_id 是排查線索；複製失敗時仍保留完整文字供手動選取。
async function copyLogId() {
  try {
    await navigator.clipboard.writeText(logId.value)
    copyState.value = '已複製'
  } catch {
    copyState.value = '無法自動複製，請選取文字複製'
  }
}

// 掛載時註冊離頁事件並讀取保存憑證；必須先向後端驗證，才能恢復帳號權限。
onMounted(() => {
  alive = true
  window.addEventListener('beforeunload', warnOnLeave)
  window.addEventListener('pagehide', onPageHide)
  window.addEventListener('pageshow', onPageShow)
  try { credential.value = readSession(window.sessionStorage) } catch { credential.value = null }
  if (credential.value) verifyAccount()
})

// 導覽離頁時同時停止表單與認證請求，以及純記憶體鎖定倒數。
function onPageHide() {
  alive = false
  stopRequest()
  stopAuthRequest()
  if (lockTimer) clearInterval(lockTimer)
  lockTimer = null
}

// BFCache 返回時重新載入並重新向 /api/me 驗證，避免舊 pending／timer 凍結。
function onPageShow(event) {
  if (event.persisted) window.location.reload()
}

// 離開頁面時清除監聽與待處理請求，避免過期結果回寫。
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', warnOnLeave)
  window.removeEventListener('pagehide', onPageHide)
  window.removeEventListener('pageshow', onPageShow)
  onPageHide()
})
</script>

<template>
  <div class="site-shell" @keydown="onEscape">
    <header class="site-header">
      <div class="header-inner">
        <div class="brand-row">
          <a class="brand" href="/vue/">文章集</a>
          <button ref="navToggle" class="menu-toggle" type="button" :aria-expanded="navOpen" aria-controls="visitor-nav" @click="toggleNav">選單</button>
          <nav id="visitor-nav" class="visitor-nav" :class="{ 'is-open': navOpen }" aria-label="主要導覽">
            <a href="/vue/" :aria-current="page === 'articles' ? 'page' : undefined">文章</a>
            <template v-if="account"><span class="account-name"><span class="account-nickname">{{ account.nickname }}</span><span class="muted">（{{ account.role === 'admin' ? '管理員' : '會員' }}）</span></span><button type="button" class="nav-action" :disabled="logoutPending" @click="submitLogout">{{ logoutPending ? '登出處理中…' : '登出' }}</button></template>
            <template v-else><a href="/vue/login" :aria-current="page === 'login' ? 'page' : undefined">登入</a><a href="/vue/register" :aria-current="page === 'register' ? 'page' : undefined">註冊</a></template>
            <button v-if="navOpen" type="button" class="close-link" @click="closePanel('nav')">關閉選單</button>
          </nav>
        </div>
        <div class="version-area">
          <div class="current-versions"><span>目前前端 <strong>Vue 3＋Vite</strong></span><span>目前後端 <strong>Laravel</strong></span></div>
          <button ref="versionsToggle" class="version-toggle" type="button" :aria-expanded="versionsOpen" aria-controls="version-panel" @click="toggleVersions">切換版本</button>
        </div>
      </div>
      <div v-if="versionsOpen" id="version-panel" class="version-panel">
        <div class="version-panel-inner">
          <div><label for="frontend-version" class="panel-heading">前端版本</label><select id="frontend-version" aria-describedby="frontend-help"><option>Vue 3＋Vite（目前使用）</option><option disabled>Nuxt（尚未提供）</option><option disabled>React＋Vite（尚未提供）</option><option disabled>Next.js（尚未提供）</option><option disabled>Angular（尚未提供）</option><option disabled>SvelteKit（尚未提供）</option></select><p id="frontend-help" class="muted">目前只有 Vue 版本可使用。</p></div>
          <div><label for="backend-version" class="panel-heading">後端版本</label><select id="backend-version" aria-describedby="backend-help"><option>Laravel（目前使用）</option><option disabled>其他後端（尚未提供）</option></select><p id="backend-help" class="muted">切換後端需重新登入；其他版本尚未提供。</p></div>
          <button class="panel-close" type="button" @click="closePanel('versions')">關閉</button>
        </div>
      </div>
    </header>

    <main id="main-content" class="main-content">
      <section v-if="page === 'register'" class="form-page" aria-labelledby="page-title">
        <h1 id="page-title">註冊會員</h1>
        <p class="intro">建立帳號後即可登入。文章與留言功能將陸續提供。</p>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="error-title">
          <h2 id="error-title">{{ summary }}</h2>
          <ul v-if="Object.keys(errors).length">
            <li v-for="name in errorFields" :key="name"><button type="button" @click="focusField(name)">{{ fieldNames[name] }}：{{ errors[name].join('；') }}</button></li>
          </ul>
          <div v-if="logId" class="trace"><span>追蹤編號：<span class="trace-id">{{ logId }}</span></span><button type="button" @click="copyLogId">複製編號</button><span role="status">{{ copyState }}</span></div>
        </section>
        <form novalidate @submit.prevent="submitRegistration">
          <div class="field"><label for="email">Email</label><input id="email" v-model="email" type="email" inputmode="email" autocomplete="email" :aria-invalid="Boolean(errors.email)" :aria-describedby="errors.email ? 'email-error' : undefined" :disabled="pending" /><p v-if="errors.email" id="email-error" class="field-error">{{ errors.email.join('；') }}</p></div>
          <div class="field"><label for="password">密碼</label><input id="password" v-model="password" type="password" autocomplete="new-password" :aria-invalid="Boolean(errors.password)" :aria-describedby="errors.password ? 'password-error password-help' : 'password-help'" :disabled="pending" /><p id="password-help" class="field-help">4–16 碼英文字母或數字，可只用其中一種。</p><p v-if="errors.password" id="password-error" class="field-error">{{ errors.password.join('；') }}</p></div>
          <div class="field"><label for="nickname">暱稱</label><input id="nickname" v-model="nickname" type="text" autocomplete="nickname" :aria-invalid="Boolean(errors.nickname)" :aria-describedby="errors.nickname ? 'nickname-error nickname-help' : 'nickname-help'" :disabled="pending" /><p id="nickname-help" class="field-help">1–30 個字；會顯示在您日後發表的內容旁。</p><p v-if="errors.nickname" id="nickname-error" class="field-error">{{ errors.nickname.join('；') }}</p></div>
          <button class="primary-button" type="submit" :disabled="pending">{{ pending ? '註冊處理中…' : '建立帳號' }}</button>
          <p v-if="pending" class="pending-note" role="status">正在送出，請稍候；不要重複提交。</p>
        </form>
        <p class="form-footer">已有帳號？<a href="/vue/login">前往登入</a></p>
      </section>

      <section v-else-if="page === 'articles'" class="message-page" aria-labelledby="page-title">
        <h1 id="page-title">文章集</h1>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="error-title"><h2 id="error-title">{{ summary }}</h2><div v-if="logId" class="trace"><span>追蹤編號：<span class="trace-id">{{ logId }}</span></span><button type="button" @click="copyLogId">複製編號</button><span role="status">{{ copyState }}</span></div></section>
        <p v-if="authChecking" role="status">正在確認登入狀態，請稍候…</p>
        <template v-else-if="account"><p>歡迎，{{ account.nickname }}{{ account.role === 'admin' ? '（管理員）' : '' }}。</p><p>文章列表尚未提供。</p></template>
        <template v-else-if="credential"><p>目前尚未確認登入狀態，請手動重新驗證。</p><button type="button" class="secondary-button" @click="verifyAccount">重新驗證登入</button></template>
        <template v-else><p>文章列表尚未提供。您現在可以先登入或建立會員帳號。</p><a class="text-link" href="/vue/login">前往登入</a><a class="text-link" href="/vue/register">前往註冊</a></template>
      </section>
      <section v-else-if="page === 'login'" class="form-page" aria-labelledby="page-title">
        <h1 id="page-title">會員登入</h1>
        <p class="intro">登入後即可使用會員功能。文章與留言功能將陸續提供。</p>
        <p v-if="registered" class="success-note" role="status">帳號已建立，請輸入帳密登入。</p>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="error-title">
          <h2 id="error-title">{{ summary }}</h2>
          <ul v-if="errorFields.length"><li v-for="name in errorFields" :key="name"><button type="button" @click="focusField(name)">{{ fieldNames[name] }}：{{ errors[name].join('；') }}</button></li></ul>
          <div v-if="logId" class="trace"><span>追蹤編號：<span class="trace-id">{{ logId }}</span></span><button type="button" @click="copyLogId">複製編號</button><span role="status">{{ copyState }}</span></div>
        </section>
        <p v-if="authChecking" role="status">正在確認登入狀態，請稍候…</p>
        <p v-else-if="account" role="status">登入已確認，正在返回文章入口…</p>
        <template v-else>
          <p v-if="credential && !account" class="pending-note">目前登入狀態尚未確認。<button type="button" class="inline-button" :disabled="pending" @click="verifyAccount">重新驗證登入</button></p>
          <form novalidate @submit.prevent="submitLogin">
            <div class="field"><label for="email">Email</label><input id="email" v-model="email" type="email" inputmode="email" autocomplete="email" :aria-invalid="Boolean(errors.email)" :aria-describedby="errors.email ? 'email-error' : undefined" :disabled="pending || locked" /><p v-if="errors.email" id="email-error" class="field-error">{{ errors.email.join('；') }}</p></div>
            <div class="field"><label for="password">密碼</label><input id="password" v-model="password" type="password" autocomplete="current-password" :aria-invalid="Boolean(errors.password)" :aria-describedby="errors.password ? 'password-error' : undefined" :disabled="pending || locked" /><p v-if="errors.password" id="password-error" class="field-error">{{ errors.password.join('；') }}</p></div>
            <button class="primary-button" type="submit" :disabled="pending || locked">{{ pending ? '登入處理中…' : '登入' }}</button>
            <p v-if="pending" class="pending-note" role="status">正在確認帳號，請稍候。</p>
            <p v-if="lockText" class="lock-note" aria-live="off">{{ lockText }}</p>
          </form>
        </template>
        <p class="form-footer">還沒有帳號？<a href="/vue/register">註冊會員</a></p>
      </section>
      <section v-else class="message-page" aria-labelledby="page-title"><h1 id="page-title">找不到頁面</h1><p>這個路徑沒有對應頁面。</p><a class="text-link" href="/vue/">返回文章入口</a></section>
    </main>
    <footer class="site-footer"><span>文章集</span><span>Vue 3＋Vite × Laravel</span></footer>
  </div>
</template>
