<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
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
const logId = ref('')
const copyState = ref('')
const pending = ref(false)
const navOpen = ref(false)
const versionsOpen = ref(false)
const navToggle = ref(null)
const versionsToggle = ref(null)
const errorSummary = ref(null)
let requestController = null
let timeout = null
let submitted = false

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
  requestController?.abort()
  requestController = null
}

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
async function showError(message, nextErrors = {}, trace = '') {
  summary.value = message
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
    await showError('連線中斷或伺服器回應不完整，無法確認註冊結果；請先確認帳號狀態，再決定是否重試。')
  } finally {
    stopRequest()
    pending.value = false
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

// 只在頁面存活時註冊事件，避免重新掛載後重複確認。
onMounted(() => {
  window.addEventListener('beforeunload', warnOnLeave)
  window.addEventListener('pagehide', stopRequest)
})

// 離開頁面時清除監聽與待處理請求，避免過期結果回寫。
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', warnOnLeave)
  window.removeEventListener('pagehide', stopRequest)
  stopRequest()
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
            <a href="/vue/login" :aria-current="page === 'login' ? 'page' : undefined">登入</a>
            <a href="/vue/register" :aria-current="page === 'register' ? 'page' : undefined">註冊</a>
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
        <p class="intro">建立帳號後，您可在登入功能開放時使用文章與留言服務。</p>
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
        <p class="form-footer">已有帳號？<a href="/vue/login">前往登入</a><span class="muted footer-note">登入功能尚未提供</span></p>
      </section>

      <section v-else-if="page === 'articles'" class="message-page" aria-labelledby="page-title"><h1 id="page-title">文章集</h1><p>文章列表尚未提供。您現在可以先建立會員帳號。</p><a class="text-link" href="/vue/register">前往註冊</a></section>
      <section v-else-if="page === 'login'" class="message-page" aria-labelledby="page-title"><h1 id="page-title">會員登入</h1><p v-if="registered" class="success-note" role="status">帳號已建立，註冊成功。</p><p>登入功能尚未提供。註冊不會讓您自動登入。</p><a class="text-link" href="/vue/">返回文章入口</a></section>
      <section v-else class="message-page" aria-labelledby="page-title"><h1 id="page-title">找不到頁面</h1><p>這個路徑沒有對應頁面。</p><a class="text-link" href="/vue/">返回文章入口</a></section>
    </main>
    <footer class="site-footer"><span>文章集</span><span>Vue 3＋Vite × Laravel</span></footer>
  </div>
</template>
