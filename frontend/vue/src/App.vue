<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { BACKENDS, selectBackend } from './auth.js'
import { useAccountSession } from './useAccountSession.js'
import { useContentReading } from './useContentReading.js'
import { useAuthorWriting } from './useAuthorWriting.js'
import { useManagement } from './useManagement.js'
import { useLockoutSettings } from './useLockoutSettings.js'
import LockoutSettingsForm from './LockoutSettingsForm.vue'
import AccountForm from './AccountForm.vue'
import AuthorContentForm from './AuthorContentForm.vue'
import { frontendDestination, loginReturnTarget, selectedBackendLabel } from './navigation.js'
import { articlePage, backendName, localPage } from './articles.js'

const path = window.location.pathname
const page = articlePage(path)
// 各路徑使用自己的分頁名稱，避免重載後把文章或找不到頁面誤認為註冊。
document.title = {
  articles: '文章集｜文章列表', article: '文章集｜閱讀文章',
  'article-new': '文章集｜發表文章', 'my-articles': '文章集｜我的文章',
  'my-comments': '文章集｜我的留言', comment: '文章集｜留言詳情',
  'article-edit': '文章集｜編輯文章', 'comment-edit': '文章集｜編輯留言',
  members: '文章集｜會員管理', member: '文章集｜會員詳情',
  'managed-articles': '文章集｜文章管理', 'managed-comments': '文章集｜留言管理',
  'lockout-settings': '文章集｜登入鎖定設定',
  register: '文章集｜註冊會員', login: '文章集｜會員登入',
  missing: '文章集｜找不到頁面',
}[page]
const registered = page === 'login' && new URLSearchParams(window.location.search).get('registered') === '1'
const target = new URLSearchParams(window.location.search).get('return_to')
const returnTo = loginReturnTarget(target)
// 版本選單與切換事件共用目的地判斷，避免顯示可選卻不能到達的路徑。
const canSwitchNuxt = computed(() => frontendDestination('nuxt', path, target) !== null)
const canSwitchReactPage = frontendDestination('react', path, target) !== null
const canSwitchNextPage = frontendDestination('next', path, target) !== null
const canSwitchAngularPage = frontendDestination('angular', path, target) !== null
const canSwitchSveltePage = frontendDestination('sveltekit', path, target) !== null
const editing = page === 'article-edit' || page === 'comment-edit'
const articleResource = page === 'article' || page === 'article-edit'
const commentResource = page === 'comment' || page === 'comment-edit'
const adminPage = ['members', 'member', 'managed-articles', 'managed-comments', 'lockout-settings'].includes(page)
const protectedPage = ['article-new', 'my-articles', 'my-comments', 'comment', 'article-edit', 'comment-edit'].includes(page) || adminPage
const requestedId = articleResource || commentResource
  ? Number(path.split('/')[3])
  : page === 'member' ? Number(path.split('/')[4]) : null
const rawPage = new URLSearchParams(window.location.search).get('page')
const listPage = ['articles', 'my-articles', 'my-comments', 'article', 'members', 'managed-articles', 'managed-comments'].includes(page)
  ? rawPage && /^[1-9][0-9]*$/.test(rawPage) && Number.isSafeInteger(Number(rawPage)) ? Number(rawPage) : 1
  : 1
const errors = ref({})
const summary = ref('')
const summaryCode = ref('')
const logId = ref('')
const copyState = ref('')
// 標頭與頁尾由目前唯一 session 的 API 選擇導出同一個顯示名稱。
const backendLabel = computed(() => selectedBackendLabel(backend.value))
const navOpen = ref(false)
const versionsOpen = ref(false)
const navToggle = ref(null)
const versionsToggle = ref(null)
const errorSummary = ref(null)
// 已確認完成或使用者已確認捨棄時，需立即更新離頁提示的 computed 狀態。
const submitted = ref(false)
let alive = false

// 草稿僅存在目前頁面記憶體；任何未送出內容都應觸發離頁確認。
const hasDraft = computed(() => {
  if (submitted.value) return false
  if (page === 'register') return Boolean(email.value || password.value || nickname.value)
  if (page === 'login') return Boolean(email.value || password.value)
  if (authorWriting.hasDraft.value) return true
  return Boolean(lockoutSettings?.hasDraft.value)
})

// 登入與內容頁共用同一份表單及 session；這些回呼僅處理內容／草稿整合。
const session = useAccountSession({
  page, protectedPage, returnTo, hasDraft, submitted,
  feedback: { summary, summaryCode, errors, logId, copyState }, showError,
  isContentBusy, onBeforeVerify: prepareAccountVerification,
  onVerified: acceptVerifiedAccount, onVerifiedReady: loadVerifiedContent,
  onAccountChanged: accountChanged, onLogin: resumeAfterLogin, onForget: clearCredentialContent,
})
const {
  email, password, nickname, pending, credential, backend, account, authChecking,
  logoutPending, reLogin, disabledAccount, locked, lockText,
  submitRegistration, submitLogin, submitLogout, verifyAccount, forgetCredential,
  restoreSession, disposeSession,
} = session

// 閱讀頁只取得目前頁面的 owner；外殼提供登入與寫入互斥，不持有完整讀取狀態。
const contentReader = useContentReading({
  backend, credential, account, canRead: canReadContent,
  // 離頁後所有 GET 都不能再回寫，即使瀏覽器沒有取消網路回應。
  isActive: () => alive,
  onStart: clearReadFeedback, onFailure: reportReadFailure,
}, page, requestedId, listPage)
// 管理頁按資源選擇 owner；外殼只連接 session、回饋及離頁，不再持有完整管理資料。
const management = adminPage && page !== 'lockout-settings' ? useManagement({
  page, path, requestedId, listPage, session, canRead: canReadContent,
  // 導覽離頁後管理 GET／PUT 與讀回續段都不能回寫。
  isActive: () => alive,
  showError, feedback: { summary },
}) : null
const lockoutSettings = page === 'lockout-settings' ? useLockoutSettings({
  session, canRead: canReadContent,
  // 設定 owner 同樣以本頁生命週期為界，不把離頁取消視為伺服器回滾。
  isActive: () => alive,
  showError, feedback: { summary, summaryCode, errors, logId },
}) : null
const managementReader = lockoutSettings || management
const articleData = articleResource ? contentReader.data : ref(null)
const articleList = page === 'articles' || page === 'my-articles' ? contentReader.data
  : page === 'managed-articles' ? management.data : ref(null)
const commentData = commentResource ? contentReader.data : ref(null)
const commentList = page === 'my-comments' ? contentReader.data : page === 'article' ? contentReader.related.data
  : page === 'managed-comments' ? management.data : ref(null)
const parentArticle = commentResource ? contentReader.related.data : ref(null)
const memberData = page === 'member' ? management.data : ref(null)
const memberList = page === 'members' ? management.data : ref(null)
const contentLoading = (contentReader || managementReader)?.loading || ref(false)
const contentReady = (contentReader || managementReader)?.ready || ref(false)
const keyword = management?.keyword || ref('')
const searchedKeyword = management?.searchedKeyword || ''
const statusPending = management?.statusPending || ref(false)
const statusUnknown = management?.statusUnknown || ref(false)
const statusNeedsReadback = management?.statusNeedsReadback || ref(false)
const statusNotice = management?.statusNotice || ref('')
const submitMemberSearch = management?.submitMemberSearch
const adminPageHref = management?.adminPageHref
const submitMemberStatus = management?.submitMemberStatus
const lockoutData = lockoutSettings?.data || ref(null)
const lockoutDraft = lockoutSettings?.draft || ref(null)
const lockoutPending = lockoutSettings?.pending || ref(false)
const lockoutUnknown = lockoutSettings?.unknown || ref(false)
const lockoutNeedsReadback = lockoutSettings?.needsReadback || ref(false)
const lockoutNotice = lockoutSettings?.notice || ref('')
const submitLockoutSettings = lockoutSettings?.submitLockoutSettings
const updateLockoutDraft = lockoutSettings?.updateDraft
const relatedLoading = contentReader?.related?.loading || ref(false)
const relatedError = contentReader?.related?.error || ref('')
const relatedLogId = contentReader?.related?.logId || ref('')
const relatedSummary = contentReader?.related?.summary || ref(null)
const commentPage = contentReader?.commentPage || ref(listPage)

// 作者操作只接收 session 與 reader 的既有 seams；外殼摘要不接管表單或寫入控制。
const authorWriting = useAuthorWriting({
  page, path, requestedId, session, reader: contentReader,
  feedback: { summary, summaryCode, errors, logId }, showError, loadContent, submitted,
  // 離頁後作者回應必須失效，包含未遵守 abort 的網路層。
  isActive: () => alive,
})
const {
  articleTitle, articleBody, commentBody, commentPosted, contentPending, createPending, updatePending,
  writeUnknown, lifecycleUnknown, editVersion, editLoaded,
  conflict, comparison, updateNotice, published,
  canComment, currentResource, authorizedResource, canEdit, canManageVisibility, canHide,
  updateEditorBody, submitArticle, submitComment, submitEdit, submitVisibility, acceptComparison,
} = authorWriting

// 所有內容 GET 先等待登入驗證與寫入完成，管理頁另外要求管理員資格。
function canReadContent() {
  return alive && !contentPending.value && !statusPending.value && !lockoutPending.value && !authChecking.value
    && !disabledAccount.value && !reLogin.value && !(summaryCode.value === 'E0002' && !credential.value)
    && (!protectedPage || Boolean(account.value)) && (!credential.value || Boolean(account.value))
    && (!adminPage || account.value?.role === 'admin')
}

// 新的主要讀取只清掉舊摘要，獨立寫入結果不明與草稿仍由各寫入流程保留。
function clearReadFeedback() {
  summary.value = ''
}

// 讀取認證失效交回唯一 session 撤權；一般讀取錯誤保留原頁的人工重試入口。
async function reportReadFailure(status, result) {
  if (status === 401 && result.code === 'E0002') {
    forgetCredential()
    reLogin.value = protectedPage || page === 'article' && hasDraft.value
  } else if (status === 403 && result.code === 'E0102') {
    forgetCredential()
    disabledAccount.value = true
  }
  await showError(result.message, result.errors, result.logId, result.code)
}

// 內容寫入與認證共用互斥邊界；驗權或登出不得中斷尚在等待的寫入。
function isContentBusy() {
  return contentPending.value || statusPending.value || lockoutPending.value
}

const fieldOrder = ['email', 'password', 'nickname', 'title', 'body', 'filters.keyword', 'enabled', 'window_minutes', 'failure_threshold', 'lock_seconds']
const fieldNames = { email: 'Email', password: '密碼', nickname: '暱稱', title: '標題', body: '內文', 'filters.keyword': '搜尋關鍵字', enabled: '鎖定功能', window_minutes: '累計期間', failure_threshold: '失敗門檻', lock_seconds: '鎖定時間' }

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

// 模板與寫入成功共用頁面 owner 的重試入口，不把父文章及留言狀態交回外殼。
function loadRelated() {
  return contentReader?.loadRelated?.()
}

// 撤權、重新驗證與離頁一併通知當頁 owner，資料及請求由各 owner 自行回收。
function clearContentReading() {
  contentReader?.clear()
  management?.clear()
  lockoutSettings?.clear()
}

// 複製獨立讀取的追蹤編號，父文章或留言錯誤不覆蓋主要錯誤摘要。
async function copyRelatedLogId() {
  try {
    await navigator.clipboard.writeText(relatedLogId.value)
    copyState.value = '已複製'
  } catch {
    copyState.value = '無法自動複製，請選取文字複製'
  }
}

// 依頁面呼叫唯一讀取 owner；作者讀回仍以原身分與資源檢查後交回作者流程。
async function loadContent() {
  if (contentReader) {
    const token = credential.value?.accessToken
    const sourceBackend = backend.value
    const owner = account.value?.id
    const data = await contentReader.load()
    // 讀取完成後仍須屬於原身分與資源，才能更新編輯比較；草稿不由閱讀 owner 接管。
    if (!data || !contentReady.value || credential.value?.accessToken !== token
      || backend.value !== sourceBackend || account.value?.id !== owner) return
    authorWriting.acceptRead(data)
    return
  }
  return managementReader?.load()
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

// 只在目標已有同類頁面時保留語意路徑與原始 ID；其他頁保持目前頁面。
function chooseFrontend(event) {
  if (event.target.value === 'vue') return
  const next = event.target.value
  const destination = frontendDestination(next, path, target)
  if (!destination || logoutPending.value) {
    event.target.value = 'vue'
    return
  }
  if (hasDraft.value && !window.confirm('尚有未儲存文字，確定捨棄並切換前端嗎？')) {
    event.target.value = 'vue'
    return
  }
  // 使用者確認捨棄後停止本頁等待；既送出的寫入結果仍須在原後端查證。
  submitted.value = true
  onPageHide()
  window.location.assign(destination)
}

// 換後端只更換 API 基底並撤銷舊 JWT；保留安全的本機返回目標。
async function chooseBackend(event) {
  const next = event.target.value
  if (next === backend.value) return
  if (hasDraft.value && !window.confirm('尚有未儲存文字，確定捨棄並切換後端嗎？')) {
    event.target.value = backend.value
    return
  }
  try {
    if (!selectBackend(sessionStorage, next)) {
      event.target.value = backend.value
      return
    }
  } catch {
    event.target.value = backend.value
    await showError('無法保存後端選擇；目前仍使用原後端。')
    return
  }
  // 先作廢所有舊回應，再導航至登入；不自動重送可能已到達舊後端的寫入。
  submitted.value = true
  onPageHide()
  credential.value = null
  account.value = null
  backend.value = next
  // 先取得安全的本機目標；作者或管理頁可在重新登入後讀取。
  const destination = page === 'login' ? returnTo : page === 'register' ? '/vue/' : localPage(path) ? path : '/vue/'
  window.location.assign(`/vue/login?return_to=${encodeURIComponent(destination)}`)
}

// Escape 可關閉目前面板，供鍵盤使用者快速回到導覽起點。
function onEscape(event) {
  if (event.key !== 'Escape') return
  if (versionsOpen.value) closePanel('versions')
  else if (navOpen.value) closePanel('nav')
}

// 按下錯誤摘要項目時直接聚焦對應欄位，讓修正路徑清楚。
function focusField(name) {
  document.getElementById(name === 'filters.keyword' ? 'member-keyword' : name)?.focus()
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

// 停用或憑證失效會立刻隱藏已授權畫面，避免下次重載再沿用舊 JWT。
function clearCredentialContent() {
  // 失效前記錄草稿所屬帳號；重新登入其他帳號時不可接收原文字。
  authorWriting.rememberOwner(account.value)
  authorWriting.stopWriting()
  clearContentReading()
}

// 驗證前撤下內容授權與舊讀取；保留原帳號的記憶體草稿供同帳號恢復。
function prepareAccountVerification(previousAccount) {
  authorWriting.rememberOwner(previousAccount)
  // 重驗期間停止讀取並隱藏原授權資料，成功後再向後端重新取得。
  clearContentReading()
}

// /me 成功後只恢復相同帳號的草稿；角色改變仍重新判斷管理頁資格。
async function acceptVerifiedAccount(nextAccount) {
  if (authorWriting.acceptAccount(nextAccount.id)) {
    await showError('目前登入帳號已變更，原帳號草稿已清除；請重新輸入。')
  }
  management?.acceptAccount(nextAccount)
  lockoutSettings?.acceptAccount(nextAccount)
  if (adminPage && nextAccount.role !== 'admin') await showError('沒有權限使用此功能', {}, '', 'E0003')
}

// 成功驗權後才讀取目前角色可讀的資料，避免管理頁提前送出受保護請求。
function loadVerifiedContent() {
  authorWriting.acceptPublishedNotice()
  if (!adminPage || account.value.role === 'admin') loadContent()
}

// 切換帳號時清除管理員專屬草稿與尚未確認操作，避免由新帳號接收。
function accountChanged() {
  management?.acceptAccount(account.value)
  lockoutSettings?.acceptAccount(account.value)
}

// 原頁重登只恢復資料；文字由使用者手動送出，換帳號必須清掉先前草稿。
async function resumeAfterLogin(nextAccount) {
  if (adminPage && nextAccount.role !== 'admin') {
    clearContentReading()
    await showError('沒有權限使用此功能', {}, '', 'E0003')
    return
  }
  if (authorWriting.acceptAccount(nextAccount.id)) {
    loadContent()
    await showError('已登入不同帳號，原帳號草稿已清除；請重新輸入。')
  } else {
    summary.value = ''
    if (['article', 'my-articles', 'my-comments', 'comment', 'article-edit', 'comment-edit'].includes(page) || adminPage) loadContent()
  }
  authorWriting.acceptPublishedNotice()
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

// 掛載時讀取保存憑證；受保護深連結先登入驗權，再讀取內容。
onMounted(() => {
  alive = true
  window.addEventListener('beforeunload', warnOnLeave)
  window.addEventListener('pagehide', onPageHide)
  window.addEventListener('pageshow', onPageShow)
  restoreSession()
  // 深連結沿用共同驗權流程，訪客先登入，避免提前讀取管理資料。
  if (credential.value) verifyAccount()
  else if (protectedPage) window.location.assign(`/vue/login?return_to=${encodeURIComponent(path)}`)
  else loadContent()
})

// 導覽離頁時同時停止表單與認證請求，以及純記憶體鎖定倒數。
function onPageHide() {
  alive = false
  authorWriting.stopWriting()
  disposeSession()
  clearContentReading()
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
          <button
            ref="navToggle"
            class="menu-toggle"
            type="button"
            :aria-expanded="navOpen"
            aria-controls="visitor-nav"
            @click="toggleNav"
          >
            選單
          </button>
          <nav id="visitor-nav" class="visitor-nav" :class="{ 'is-open': navOpen }" aria-label="主要導覽">
            <a href="/vue/" :aria-current="page === 'articles' ? 'page' : undefined">文章</a>
            <template v-if="account">
              <a href="/vue/articles/new" :aria-current="page === 'article-new' ? 'page' : undefined">發文</a>
              <a href="/vue/me/articles" :aria-current="page === 'my-articles' ? 'page' : undefined">我的文章</a>
              <a href="/vue/me/comments" :aria-current="page === 'my-comments' ? 'page' : undefined">我的留言</a>
              <template v-if="account.role === 'admin'">
                <a href="/vue/admin/members" :aria-current="page === 'members' || page === 'member' ? 'page' : undefined">會員管理</a>
                <a href="/vue/admin/articles" :aria-current="page === 'managed-articles' ? 'page' : undefined">文章管理</a>
                <a href="/vue/admin/comments" :aria-current="page === 'managed-comments' ? 'page' : undefined">留言管理</a>
                <a href="/vue/admin/settings/login-lockout" :aria-current="page === 'lockout-settings' ? 'page' : undefined">鎖定設定</a>
              </template>
              <span class="account-name">
                <span class="account-nickname">{{ account.nickname }}</span>
                <span class="muted">（{{ account.role === 'admin' ? '管理員' : '會員' }}）</span>
              </span>
              <button
                type="button"
                class="nav-action"
                :disabled="pending || authChecking || logoutPending || contentPending || statusPending || lockoutPending"
                @click="submitLogout"
              >
                {{ logoutPending ? '登出處理中…' : '登出' }}
              </button>
            </template>
            <template v-else>
              <a href="/vue/login" :aria-current="page === 'login' ? 'page' : undefined">登入</a>
              <a href="/vue/register" :aria-current="page === 'register' ? 'page' : undefined">註冊</a>
            </template>
            <button v-if="navOpen" type="button" class="close-link" @click="closePanel('nav')">關閉選單</button>
          </nav>
        </div>
        <div class="version-area">
          <div class="current-versions">
            <span>
              目前前端
              <strong>Vue 3＋Vite</strong>
            </span>
            <span>
              目前後端
              <strong>{{ backendLabel }}</strong>
            </span>
          </div>
          <button
            ref="versionsToggle"
            class="version-toggle"
            type="button"
            :aria-expanded="versionsOpen"
            aria-controls="version-panel"
            @click="toggleVersions"
          >
            切換版本
          </button>
        </div>
      </div>
      <div v-if="versionsOpen" id="version-panel" class="version-panel">
        <div class="version-panel-inner">
          <div>
            <label for="frontend-version" class="panel-heading">前端版本</label>
            <select id="frontend-version" aria-describedby="frontend-help" value="vue" @change="chooseFrontend">
              <option value="vue">Vue 3＋Vite（目前使用）</option>
              <option value="nuxt" :disabled="!canSwitchNuxt">Nuxt{{ canSwitchNuxt ? '' : '（此頁尚未提供）' }}</option>
              <option value="react" :disabled="!canSwitchReactPage">React＋Vite{{ canSwitchReactPage ? '' : '（此頁尚未提供）' }}</option>
              <option value="next" :disabled="!canSwitchNextPage">Next.js{{ canSwitchNextPage ? '' : '（此頁尚未提供）' }}</option>
              <option value="angular" :disabled="!canSwitchAngularPage">Angular{{ canSwitchAngularPage ? '' : '（此頁尚未提供）' }}</option>
              <option value="sveltekit" :disabled="!canSwitchSveltePage">SvelteKit{{ canSwitchSveltePage ? '' : '（此頁尚未提供）' }}</option>
            </select>
            <p id="frontend-help" class="muted">已知頁面可切換至 Nuxt；目前後端與登入到期時間會保留。</p>
          </div>
          <div>
            <label for="backend-version" class="panel-heading">後端版本</label>
            <select id="backend-version" aria-describedby="backend-help" :value="backend" @change="chooseBackend">
              <option :value="BACKENDS.laravel">Laravel</option>
              <option :value="BACKENDS.symfony">Symfony</option>
              <option :value="BACKENDS.aspnet">ASP.NET Core Controllers</option>
              <option :value="BACKENDS.django">Django</option>
              <option :value="BACKENDS.flask">Flask</option>
              <option :value="BACKENDS.fastapi">FastAPI</option>
              <option :value="BACKENDS.gin">Gin</option>
              <option :value="BACKENDS.echo">Echo</option>
              <option :value="BACKENDS.fiber">Fiber</option>
              <option :value="BACKENDS.express">Express</option>
              <option :value="BACKENDS.fastify">Fastify</option>
              <option :value="BACKENDS.nestjs">NestJS</option>
              <option :value="BACKENDS.rails">Ruby on Rails</option>
            </select>
            <p id="backend-help" class="muted">切換後端會清除目前登入並要求重新登入。</p>
          </div>
          <button class="panel-close" type="button" @click="closePanel('versions')">關閉</button>
        </div>
      </div>
    </header>

    <main id="main-content" class="main-content">
      <section v-if="page === 'register'" class="form-page" aria-labelledby="page-title">
        <h1 id="page-title">註冊會員</h1>
        <p class="intro">建立帳號後即可登入並發表文章。</p>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="error-title">
          <h2 id="error-title">{{ summary }}</h2>
          <ul v-if="Object.keys(errors).length">
            <li v-for="name in errorFields" :key="name">
              <button type="button" @click="focusField(name)">{{ fieldNames[name] }}：{{ errors[name].join('；') }}</button>
            </li>
          </ul>
          <div v-if="logId" class="trace">
            <span>
              追蹤編號：
              <span class="trace-id">{{ logId }}</span>
            </span>
            <button type="button" @click="copyLogId">複製編號</button>
            <span role="status">{{ copyState }}</span>
          </div>
        </section>
        <p v-if="authChecking" role="status">正在確認登入狀態，請稍候…</p>
        <p v-if="logoutPending" role="status">登出處理中，請等待結果後再註冊。</p>
        <AccountForm
          registration
          v-model:email="email"
          v-model:password="password"
          v-model:nickname="nickname"
          :pending="pending" :blocked="authChecking || logoutPending"
          :locked="locked"
          :errors="errors"
          :lock-text="lockText"
          @submit="submitRegistration"
        />
        <p class="form-footer">
          已有帳號？
          <a href="/vue/login">前往登入</a>
        </p>
      </section>
      <section
        v-else-if="page === 'articles' || page === 'article' || page === 'article-new' || page === 'my-articles' || page === 'my-comments' || page === 'comment' || editing"
        class="reading-page"
        :class="{ 'article-index': page === 'articles' }"
        aria-labelledby="page-title"
      >
        <div class="reading-heading">
          <div>
            <h1 id="page-title">{{
              page === 'articles' ? '所有文章' : page === 'article' ? '閱讀文章'
                : page === 'article-new' ? '發表文章' : page === 'my-articles' ? '我的文章'
                  : page === 'my-comments' ? '我的留言' : page === 'article-edit' ? '編輯文章'
                    : page === 'comment-edit' ? '編輯留言' : '留言詳情'
            }}</h1>
          </div>
          <a v-if="account && page === 'articles'" class="secondary-button" href="/vue/articles/new">發表文章</a>
        </div>
        <p v-if="published" class="success-note" role="status">文章已發表，以下為我的文章。</p>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="error-title">
          <h2 id="error-title">{{ summary }}</h2>
          <ul v-if="errorFields.length">
            <li v-for="name in errorFields" :key="name">
              <button type="button" @click="focusField(name)">{{ fieldNames[name] }}：{{ errors[name].join('；') }}</button>
            </li>
          </ul>
          <div v-if="logId" class="trace">
            <span>追蹤編號：<span class="trace-id">{{ logId }}</span></span>
            <button type="button" @click="copyLogId">複製編號</button>
            <span role="status">{{ copyState }}</span>
          </div>
        </section>
        <p v-if="authChecking" role="status">正在確認登入狀態，請稍候…</p>
        <p v-if="contentLoading" role="status">正在讀取內容，請稍候…</p>
        <p v-if="credential && !account && !authChecking && !reLogin" class="pending-note">
          目前無法確認登入狀態；憑證已保留。
          <button type="button" class="inline-button" @click="verifyAccount">重新驗證登入</button>
        </p>
        <p v-if="!credential && !reLogin && !disabledAccount && summaryCode === 'E0002'">
          請重新登入後手動讀取。
          <a class="text-link" :href="`/vue/login?return_to=${encodeURIComponent(path)}`">前往登入</a>
        </p>
        <template v-if="reLogin && !disabledAccount">
          <p class="pending-note">{{ page === 'article-new' || page === 'article' || editing ? '原頁內容會暫留在此頁記憶體。請重新登入；成功後由您手動送出。' : '請重新登入後讀取原頁內容。' }}</p>
          <AccountForm relogin
            v-model:email="email" v-model:password="password"
            :pending="pending" :blocked="authChecking || logoutPending" :locked="locked" :errors="errors" :lock-text="lockText"
            @submit="submitLogin" />
        </template>
        <!-- total_pages=0 也可能是第 1 頁空列表；依原頁碼區分合法超頁。 -->
        <template v-if="page === 'articles' || page === 'my-articles'">
          <div v-if="articleList && contentReady" class="article-results">
            <p class="result-count">共 {{ articleList.pagination.total }} 篇 · 第 {{ articleList.pagination.page }} 頁<span v-if="articleList.pagination.total_pages">／共 {{ articleList.pagination.total_pages }} 頁</span></p>
            <p v-if="!articleList.list.length" role="status">{{
              listPage > 1 && listPage > articleList.pagination.total_pages
                ? '此頁沒有文章，總數仍保留。' : '目前尚無文章。'
            }}</p>
            <ol v-else class="article-list">
              <li v-for="item in articleList.list" :key="item.id" class="article-card">
                <p class="article-meta">
                  {{ item.author.nickname }} · {{ item.created_at }} · 建立於 {{ backendName(item.created_backend) }}<span v-if="item.visibility === 'hidden'"> · 已下架</span>
                </p>
                <h2><a :href="`/vue/articles/${item.id}`">{{ item.title }}</a></h2>
                <p class="article-excerpt">{{ item.body }}</p>
                <a class="read-link" :href="`/vue/articles/${item.id}`">閱讀全文</a>
              </li>
            </ol>
            <nav v-if="articleList.pagination.total_pages > 1 || listPage > 1" class="pagination" aria-label="文章分頁">
              <a v-if="listPage > 1" :href="`${path}?page=${listPage - 1}`">上一頁</a>
              <span>第 {{ listPage }} 頁</span>
              <a v-if="listPage < articleList.pagination.total_pages" :href="`${path}?page=${listPage + 1}`">下一頁</a>
              <a v-if="listPage > 1 && listPage > articleList.pagination.total_pages" :href="path">回第一頁</a>
            </nav>
          </div>
          <button
            v-else-if="!contentLoading && !authChecking && !reLogin && !disabledAccount && summaryCode !== 'E0002' && (!protectedPage || account)"
            type="button" class="secondary-button" @click="loadContent"
          >重新讀取</button>
        </template>
        <template v-else-if="page === 'article'">
          <article v-if="articleData && contentReady" class="article-detail">
            <p class="article-meta">
              {{ articleData.author.nickname }} · {{ articleData.created_at }} · 建立於 {{ backendName(articleData.created_backend) }}<span v-if="articleData.visibility === 'hidden'"> · 已下架</span>
            </p>
            <h2>{{ articleData.title }}</h2>
            <div class="article-body">{{ articleData.body }}</div>
          </article>
          <button
            v-else-if="!contentLoading && !authChecking && !disabledAccount && summaryCode !== 'E0002' && (!credential || account)"
            type="button" class="secondary-button" @click="loadContent"
          >重新讀取</button>
          <p class="detail-return-links">
            <a class="text-link" href="/vue/">返回文章列表</a>
            <a v-if="account?.role === 'admin'" class="text-link" href="/vue/admin/articles">返回文章管理</a>
          </p>
        </template>
        <!-- 我的留言也以原頁碼判斷零筆超頁，不能把第 7 頁說成尚無留言。 -->
        <template v-else-if="page === 'my-comments'">
          <div v-if="commentList && contentReady" class="article-results">
            <p class="result-count">共 {{ commentList.pagination.total }} 則 · 第 {{ commentList.pagination.page }} 頁<span v-if="commentList.pagination.total_pages">／共 {{ commentList.pagination.total_pages }} 頁</span></p>
            <p v-if="!commentList.list.length" role="status">{{ listPage > 1 && listPage > commentList.pagination.total_pages ? '此頁沒有留言，總數仍保留。' : '目前尚無留言。' }}</p>
            <ol v-else class="article-list">
              <li v-for="item in commentList.list" :key="item.id" class="article-card">
                <p class="article-meta">
                  {{ item.author.nickname }} · {{ item.created_at }} · 建立於 {{ backendName(item.created_backend) }}<span v-if="item.visibility === 'hidden'"> · 已下架</span>
                </p>
                <p class="article-body comment-body">{{ item.body }}</p>
                <a class="read-link" :href="`/vue/comments/${item.id}`">閱讀留言詳情</a>
              </li>
            </ol>
            <nav v-if="commentList.pagination.total_pages > 1 || listPage > 1" class="pagination" aria-label="我的留言分頁">
              <a v-if="listPage > 1" :href="`${path}?page=${listPage - 1}`">上一頁</a>
              <span>第 {{ listPage }} 頁</span>
              <a v-if="listPage < commentList.pagination.total_pages" :href="`${path}?page=${listPage + 1}`">下一頁</a>
              <a v-if="listPage > 1 && listPage > commentList.pagination.total_pages" :href="path">回第一頁</a>
            </nav>
          </div>
          <button v-if="!contentReady && !contentLoading && !authChecking && !reLogin && !disabledAccount && account" type="button" class="secondary-button" :disabled="contentPending" @click="loadContent">重新讀取留言</button>
        </template>
        <template v-else-if="commentResource">
          <article v-if="commentData && contentReady && !editing" class="article-detail">
            <p class="article-meta">
              {{ commentData.author.nickname }} · {{ commentData.created_at }} · 建立於 {{ backendName(commentData.created_backend) }}<span v-if="commentData.visibility === 'hidden'"> · 已下架</span>
            </p>
            <div class="article-body">{{ commentData.body }}</div>
          </article>
          <button v-if="!contentReady && !contentLoading && !authChecking && !reLogin && !disabledAccount && account" type="button" class="secondary-button" :disabled="contentPending" @click="loadContent">重新讀取留言</button>
          <section v-if="commentData && contentReady" class="related-content" aria-labelledby="parent-title">
            <h2 id="parent-title">所屬文章</h2>
            <p v-if="relatedLoading" role="status">正在確認所屬文章的讀取權限…</p>
            <template v-if="parentArticle">
              <p class="article-meta"><span v-if="parentArticle.visibility === 'hidden'">已下架 · </span>建立於 {{ backendName(parentArticle.created_backend) }}</p>
              <h3><a :href="`/vue/articles/${parentArticle.id}`">{{ parentArticle.title }}</a></h3>
            </template>
            <section v-if="relatedError" ref="relatedSummary" class="error-summary" tabindex="-1" aria-labelledby="related-error-title">
              <h3 id="related-error-title">{{ relatedError }}</h3>
              <div v-if="relatedLogId" class="trace">
                <span>追蹤編號：<span class="trace-id">{{ relatedLogId }}</span></span>
                <button type="button" @click="copyRelatedLogId">複製編號</button>
                <span role="status">{{ copyState }}</span>
              </div>
            </section>
            <button
              v-if="relatedError && !relatedLoading" class="secondary-button" type="button"
              :disabled="contentPending" @click="loadRelated"
            >重新讀取所屬文章</button>
          </section>
          <p class="detail-return-links">
            <a class="text-link" href="/vue/me/comments">返回我的留言</a>
            <a v-if="account?.role === 'admin'" class="text-link" href="/vue/admin/comments">返回留言管理</a>
          </p>
        </template>
        <template v-else-if="page === 'article-new'">
          <p class="intro">標題與內文會以純文字公開顯示。發表後可在我的文章查看。</p>
          <AuthorContentForm v-if="account && credential && !authChecking && !reLogin && !disabledAccount"
            v-model:title="articleTitle" v-model:body="articleBody" mode="article-new"
            :errors="errors" :disabled="contentPending || logoutPending || submitted" :pending="createPending" @submit="submitArticle" />
          <p v-if="writeUnknown" class="pending-note">
            發文結果尚未確認。<a class="text-link" href="/vue/me/articles">先到我的文章查證</a>
          </p>
        </template>
        <template v-if="editing">
          <AuthorContentForm v-if="authorizedResource && currentResource.author.id === account.id && editLoaded"
            v-model:title="articleTitle" :body="articleResource ? articleBody : commentBody"
            :mode="page" :errors="errors" :disabled="!canEdit || contentPending || contentLoading"
            :pending="updatePending" :conflict="conflict" :version="editVersion"
            @update:body="updateEditorBody" @submit="submitEdit">
            <p class="article-meta">
              {{ currentResource.author.nickname }} · {{ currentResource.created_at }} · 建立於 {{ backendName(currentResource.created_backend) }}
              <span v-if="currentResource.visibility === 'hidden'"> · 已下架</span>
            </p>
            <p class="intro">修改不會改變下架狀態或恢復權限。原文與換行會直接交給伺服器。</p>
            <p v-if="commentResource && !canEdit" class="pending-note" role="status">所屬文章尚未確認公開，目前不能編修內文；您的草稿仍留在此頁。</p>
          </AuthorContentForm>
          <p v-else-if="authorizedResource" class="pending-note">您不是這筆內容的作者，不能修改內文。</p>
          <div class="content-actions">
            <button v-if="account && !reLogin && !disabledAccount" class="secondary-button" type="button"
              :disabled="contentPending || contentLoading || authChecking" @click="loadContent">重新讀取最新內容</button>
            <a class="text-link" :href="`/vue/${articleResource ? 'articles' : 'comments'}/${requestedId}`">返回詳情</a>
          </div>
          <section v-if="comparison && authorizedResource && currentResource.author.id === account.id" class="content-comparison" aria-labelledby="comparison-title">
            <h2 id="comparison-title">最新內容（版本 {{ comparison.version }}）</h2>
            <p>上方保留您的輸入。請先比較及整合，或決定捨棄自己的文字，再確認版本；確認不會自動儲存。</p>
            <h3 v-if="articleResource">{{ comparison.title }}</h3>
            <div class="article-body">{{ comparison.body }}</div>
            <div class="content-actions">
              <button type="button" class="secondary-button" :disabled="!canEdit || contentPending || contentLoading"
                @click="acceptComparison(false)">已整合輸入，採用最新版本</button>
              <button type="button" class="secondary-button" :disabled="!canEdit || contentPending || contentLoading"
                @click="acceptComparison(true)">捨棄輸入，採用最新內容</button>
            </div>
          </section>
        </template>
        <div v-if="!editing && canManageVisibility" class="content-actions">
          <a v-if="canEdit" class="secondary-button" :href="`${path}/edit`">{{ articleResource ? '編輯文章' : '編輯留言' }}</a>
          <button v-if="canHide" type="button" class="secondary-button"
            :disabled="contentPending || contentLoading || conflict && !comparison" @click="submitVisibility('hidden')">
            {{ currentResource.visibility === 'hidden' ? '再次下架並接管恢復權' : '下架' }}
          </button>
          <button v-if="currentResource.visibility === 'hidden' && currentResource.can_restore" type="button" class="secondary-button"
            :disabled="contentPending || contentLoading || conflict && !comparison" @click="submitVisibility('visible')">恢復</button>
          <p v-if="currentResource.visibility === 'hidden' && !currentResource.can_restore" class="muted">目前無恢復資格。</p>
          <p v-if="updatePending" role="status">正在更新狀態，請稍候…</p>
        </div>
        <p v-if="conflict && (editing || articleResource || commentResource)" class="pending-note" role="status">
          資料已被其他操作更新。{{ editing ? '請重讀最新內容並人工比較、確認後再手動送出。' : '請先重讀最新內容，再重新確認下架或恢復操作。' }}
          <button v-if="!editing && account && !reLogin && !disabledAccount" type="button" class="inline-button"
            :disabled="contentPending || contentLoading || authChecking" @click="loadContent">重新讀取最新內容</button>
        </p>
        <p v-if="updateNotice" class="success-note" role="status">{{ updateNotice }}</p>
        <p v-if="lifecycleUnknown" class="pending-note">
          更新結果尚未確認。請先依原 ID 查證；不會自動重送。
          <button v-if="account && !reLogin && !disabledAccount" class="inline-button" type="button"
            :disabled="contentPending || contentLoading || authChecking" @click="loadContent">重新讀取同一筆內容查證</button>
          <a class="text-link" :href="`/vue/${articleResource ? 'articles' : 'comments'}/${requestedId}`">依原 ID 開啟詳情查證</a>
        </p>
        <section v-if="page === 'article' && articleData && contentReady" id="comments" class="related-content" aria-labelledby="comments-title">
          <h2 id="comments-title">留言</h2>
          <p v-if="relatedLoading" role="status">正在讀取留言，請稍候…</p>
          <section v-if="relatedError" ref="relatedSummary" class="error-summary" tabindex="-1" aria-labelledby="related-error-title">
            <h3 id="related-error-title">{{ relatedError }}</h3>
            <div v-if="relatedLogId" class="trace">
              <span>追蹤編號：<span class="trace-id">{{ relatedLogId }}</span></span>
              <button type="button" @click="copyRelatedLogId">複製編號</button>
              <span role="status">{{ copyState }}</span>
            </div>
          </section>
          <button v-if="relatedError && !relatedLoading" type="button" class="secondary-button" :disabled="contentPending" @click="loadRelated">重新讀取留言</button>
          <!-- 文章留言的頁碼不因零筆回應重設，返回入口仍指向同一文章的留言區。 -->
          <template v-if="commentList">
            <p class="result-count">共 {{ commentList.pagination.total }} 則 · 第 {{ commentList.pagination.page }} 頁<span v-if="commentList.pagination.total_pages">／共 {{ commentList.pagination.total_pages }} 頁</span></p>
            <p v-if="!commentList.list.length" role="status">{{
              commentPage > 1 && commentPage > commentList.pagination.total_pages
                ? '此頁沒有留言，總數仍保留。' : '目前尚無留言。'
            }}</p>
            <ol v-else class="article-list">
              <li v-for="item in commentList.list" :key="item.id" class="article-card">
                <p class="article-meta">
                  {{ item.author.nickname }} · {{ item.created_at }} · 建立於 {{ backendName(item.created_backend) }}<span v-if="item.visibility === 'hidden'"> · 已下架</span>
                </p>
                <p class="article-body comment-body">{{ item.body }}</p>
                <a
                  v-if="account && (account.id === item.author.id || account.role === 'admin')"
                  class="read-link" :href="`/vue/comments/${item.id}`"
                >閱讀留言詳情</a>
              </li>
            </ol>
            <nav v-if="commentList.pagination.total_pages > 1 || commentPage > 1" class="pagination" aria-label="文章留言分頁">
              <a v-if="commentPage > 1" :href="`${path}?page=${commentPage - 1}#comments`">上一頁</a>
              <span>第 {{ commentPage }} 頁</span>
              <a v-if="commentPage < commentList.pagination.total_pages" :href="`${path}?page=${commentPage + 1}#comments`">下一頁</a>
              <a v-if="commentPage > 1 && commentPage > commentList.pagination.total_pages" :href="`${path}#comments`">回第一頁</a>
            </nav>
          </template>
        </section>
        <section v-if="page === 'article' && (articleData && contentReady || reLogin && commentBody)" class="comment-form" aria-labelledby="comment-form-title">
          <h2 id="comment-form-title">發表留言</h2>
          <p v-if="commentPosted" class="success-note" role="status">留言已發表。</p>
          <AuthorContentForm v-if="canComment || reLogin && commentBody" v-model:body="commentBody" mode="comment-new"
            :errors="errors" :disabled="contentPending || !canComment" :pending="createPending" @submit="submitComment" />
          <p v-else-if="articleData?.visibility === 'hidden'">所屬文章已下架，目前不能新增留言。</p>
          <p v-else-if="!account && !credential && !disabledAccount && summaryCode !== 'E0002'">登入後即可發表留言。<a class="text-link" :href="`/vue/login?return_to=${encodeURIComponent(path)}`">前往登入</a></p>

        </section>
        <!-- 留言結果不明與資源更新、父文章可讀性獨立；其他操作不能清除查證入口。 -->
        <p v-if="page === 'article' && writeUnknown" class="pending-note">留言結果尚未確認；請先查證，避免重複留言。<button type="button" class="inline-button" :disabled="relatedLoading || contentPending || !canComment" @click="loadRelated">重新讀取留言查證</button><a class="text-link" href="/vue/me/comments">到我的留言查證</a></p>
      </section>
      <section v-else-if="adminPage" class="reading-page admin-page" aria-labelledby="page-title">
        <div class="reading-heading">
          <div>
            <p class="eyebrow">文章集 / 管理</p>
            <h1 id="page-title">{{
              page === 'members' ? '會員管理' : page === 'member' ? '會員詳情'
                : page === 'managed-articles' ? '文章管理' : page === 'managed-comments' ? '留言管理' : '登入鎖定設定'
            }}</h1>
          </div>
        </div>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="admin-error-title">
          <h2 id="admin-error-title">{{ summary }}</h2>
          <ul v-if="errorFields.length">
            <li v-for="name in errorFields" :key="name">
              <button type="button" @click="focusField(name)">{{ fieldNames[name] }}：{{ errors[name].join('；') }}</button>
            </li>
          </ul>
          <div v-if="logId" class="trace">
            <span>追蹤編號：<span class="trace-id">{{ logId }}</span>
            </span>
            <button type="button" @click="copyLogId">複製編號</button>
            <span role="status">{{ copyState }}</span>
          </div>
        </section>
        <p v-if="authChecking" role="status">正在確認登入狀態，請稍候…</p>
        <p v-if="contentLoading" role="status">正在讀取管理資料，請稍候…</p>
        <p v-if="credential && !account && !authChecking && !reLogin"
          class="pending-note">
          目前無法確認登入狀態；憑證已保留。
          <button class="inline-button" type="button" @click="verifyAccount">重新驗證登入</button>
        </p>
        <p v-if="!credential && !reLogin && !disabledAccount && summaryCode === 'E0002'">
          請重新登入後手動讀取。
          <a class="text-link" :href="`/vue/login?return_to=${encodeURIComponent(path)}`">前往登入</a>
        </p>
        <template v-if="reLogin && !disabledAccount">
          <p>請重新登入後讀取原管理頁。</p>
          <AccountForm relogin
            v-model:email="email" v-model:password="password"
            :pending="pending" :blocked="authChecking || logoutPending" :locked="locked" :errors="errors" :lock-text="lockText"
            @submit="submitLogin" />
        </template>
        <template v-if="account && account.role !== 'admin' && !authChecking">
          <p>此頁僅供管理員使用。</p>
          <a class="text-link" href="/vue/">返回文章列表</a>
        </template>
        <template v-if="account?.role === 'admin' && !authChecking">
          <div v-if="page === 'lockout-settings'" class="lockout-settings">
            <p class="intro">設定會員登入連續失敗時的鎖定規則。關閉鎖定會清除會員的既有鎖定與失敗計數；再次啟用時從零開始計算。</p>
            <p v-if="lockoutNotice" class="success-note" role="status">{{ lockoutNotice }}</p>
            <p v-if="lockoutUnknown" class="pending-note" role="status">
              先前 PUT 結果尚未確認。{{ lockoutNeedsReadback
                ? '請先手動重新讀取目前伺服器設定；不會自動重送。'
                : '目前值已重讀，但可能已有其他管理員更新，無法藉此判定先前 PUT 是否成功。' }}
            </p>
            <p v-if="lockoutPending" class="pending-note" role="status">正在儲存完整設定，請勿重複送出。</p>
            <button v-if="!contentLoading && !lockoutPending && !disabledAccount && summaryCode !== 'E0002'" class="secondary-button" type="button" @click="loadContent">
              重新讀取目前設定{{ lockoutNeedsReadback ? '並查證' : '' }}
            </button>
            <p v-if="lockoutData && contentReady" class="field-help">
              目前伺服器值：{{ lockoutData.enabled ? '啟用' : '關閉' }}；{{ lockoutData.window_minutes }} 分鐘內失敗 {{ lockoutData.failure_threshold }} 次，鎖定 {{ lockoutData.lock_seconds }} 秒。
            </p>
            <LockoutSettingsForm v-if="lockoutDraft && lockoutData && contentReady"
              :draft="lockoutDraft" :errors="errors" :pending="lockoutPending"
              :blocked="lockoutNeedsReadback || contentLoading || logoutPending"
              @update:draft="updateLockoutDraft" @submit="submitLockoutSettings" />
          </div>
          <form v-if="page === 'members'" class="admin-search" @submit.prevent="submitMemberSearch">
            <label for="member-keyword">搜尋會員暱稱或 Email</label>
            <div>
              <input id="member-keyword" v-model="keyword" type="search"
                :aria-invalid="Boolean(errors['filters.keyword']?.length)"
                :aria-describedby="errors['filters.keyword']?.length ? 'member-keyword-error member-keyword-help' : 'member-keyword-help'" />
              <button class="secondary-button" type="submit">搜尋</button>
            </div>
            <p id="member-keyword-help" class="field-help">以字面包含搜尋；不使用萬用字元。</p>
            <p v-if="errors['filters.keyword']?.length" id="member-keyword-error" class="field-error">{{ errors['filters.keyword'].join('；') }}</p>
          </form>
          <p v-if="page === 'member' && statusNotice" class="success-note" role="status">{{ statusNotice }}</p>
          <p v-if="page === 'member' && statusUnknown" class="pending-note" role="status">先前送出的操作是否完成仍無法判定。{{
            statusNeedsReadback ? '請重新讀取同一會員，查證目前狀態後再決定是否操作。' : '目前狀態已重讀；再次操作仍需手動確認。'
          }}</p>
          <article v-if="page === 'member' && memberData && contentReady" class="admin-detail">
            <dl>
              <div>
                <dt>會員 ID</dt>
                <dd>{{ memberData.id }}</dd>
              </div>
              <div>
                <dt>暱稱</dt>
                <dd>{{ memberData.nickname }}</dd>
              </div>
              <div>
                <dt>Email</dt>
                <dd>{{ memberData.email }}</dd>
              </div>
              <div>
                <dt>狀態</dt>
                <dd>{{ memberData.status === 'active' ? '啟用' : '停用' }}</dd>
              </div>
              <div>
                <dt>建立時間</dt>
                <dd>{{ memberData.created_at }}</dd>
              </div>
            </dl>
            <button class="secondary-button" type="button"
              :disabled="statusPending || statusNeedsReadback || contentLoading || logoutPending"
              @click="submitMemberStatus">{{
                statusPending ? '更新處理中…' : memberData.status === 'active' ? '停用會員' : '恢復會員'
              }}</button>
            <p v-if="statusPending" class="pending-note" role="status">正在更新會員；請勿重複送出。</p>
          </article>
          <!-- 管理搜尋首頁零筆仍顯示無符合結果，其他頁零筆則保留原頁碼。 -->
          <div v-if="page === 'members' && memberList && contentReady">
            <p class="result-count">共 {{ memberList.pagination.total }} 位 · 第 {{ memberList.pagination.page }} 頁<span
              v-if="memberList.pagination.total_pages">／共 {{ memberList.pagination.total_pages }} 頁</span>
            </p>
            <p v-if="!memberList.list.length" role="status">{{
              listPage > 1 && listPage > memberList.pagination.total_pages
                ? '此頁沒有會員，總數仍保留。' : searchedKeyword ? '沒有符合搜尋的會員。' : '目前尚無會員。'
            }}</p>
            <ol v-else class="admin-list">
              <li v-for="item in memberList.list" :key="item.id" class="admin-row">
                <div>
                  <span class="admin-label">暱稱</span>
                  <strong>{{ item.nickname }}</strong>
                </div>
                <div>
                  <span class="admin-label">Email</span>
                  <span>{{ item.email }}</span>
                </div>
                <div>
                  <span class="admin-label">狀態</span>
                  <span>{{ item.status === 'active' ? '啟用' : '停用' }}</span>
                </div>
                <a class="read-link" :href="`/vue/admin/members/${item.id}`">查看會員</a>
              </li>
            </ol>
          </div>
          <div v-if="page === 'managed-articles' && articleList && contentReady">
            <p class="result-count">共 {{ articleList.pagination.total }} 篇 · 第 {{ articleList.pagination.page }} 頁</p>
            <p v-if="!articleList.list.length" role="status">{{
              listPage > 1 && listPage > articleList.pagination.total_pages
                ? '此頁沒有文章，總數仍保留。' : '目前尚無文章。'
            }}</p>
            <ol v-else class="admin-list">
              <li v-for="item in articleList.list" :key="item.id" class="admin-row">
                <div>
                  <span class="admin-label">標題</span>
                  <strong>{{ item.title }}</strong>
                </div>
                <div>
                  <span class="admin-label">作者</span>
                  <span>{{ item.author.nickname }} · 建立於 {{ backendName(item.created_backend) }}</span>
                </div>
                <div>
                  <span class="admin-label">狀態</span>
                  <span>{{ item.visibility === 'hidden' ? '已下架' : '公開' }}</span>
                </div>
                <a class="read-link" :href="`/vue/articles/${item.id}`">閱讀文章詳情</a>
              </li>
            </ol>
          </div>
          <div v-if="page === 'managed-comments' && commentList && contentReady">
            <p class="result-count">共 {{ commentList.pagination.total }} 則 · 第 {{ commentList.pagination.page }} 頁</p>
            <p v-if="!commentList.list.length" role="status">{{
              listPage > 1 && listPage > commentList.pagination.total_pages
                ? '此頁沒有留言，總數仍保留。' : '目前尚無留言。'
            }}</p>
            <ol v-else class="admin-list">
              <li v-for="item in commentList.list" :key="item.id" class="admin-row">
                <div>
                  <span class="admin-label">留言</span>
                  <span class="admin-excerpt">{{ item.body }}</span>
                </div>
                <div>
                  <span class="admin-label">作者</span>
                  <span>{{ item.author.nickname }} · 建立於 {{ backendName(item.created_backend) }}</span>
                </div>
                <div>
                  <span class="admin-label">狀態</span>
                  <span>{{ item.visibility === 'hidden' ? '已下架' : '公開' }}</span>
                </div>
                <a class="read-link" :href="`/vue/comments/${item.id}`">閱讀留言詳情</a>
              </li>
            </ol>
          </div>
          <nav
            v-if="(page === 'members' && memberList || page === 'managed-articles' && articleList || page === 'managed-comments' && commentList) && contentReady"
            class="pagination" aria-label="管理列表分頁">
            <a v-if="listPage > 1" :href="adminPageHref(listPage - 1)">上一頁</a>
            <span>第 {{ listPage }} 頁</span>
            <a v-if="listPage < (memberList || articleList || commentList).pagination.total_pages" :href="adminPageHref(listPage + 1)">下一頁</a>
            <a v-if="listPage > 1 && listPage > (memberList || articleList || commentList).pagination.total_pages"
              :href="adminPageHref(1)">回第一頁</a>
          </nav>
          <button
            v-if="page !== 'lockout-settings' && !contentLoading && !statusPending && (!contentReady || page === 'member' && statusNeedsReadback) && !disabledAccount && summaryCode !== 'E0002'"
            class="secondary-button" type="button" @click="loadContent">
            重新讀取{{ page === 'member' ? '同一會員' : '管理資料' }}
          </button>
          <p v-if="page === 'member'">
            <a class="text-link" href="/vue/admin/members">返回會員列表</a>
          </p>
        </template>
      </section>
      <section v-else-if="page === 'login'" class="form-page" aria-labelledby="page-title">
        <h1 id="page-title">會員登入</h1>
        <p class="intro">登入後即可發表文章與查看自己的文章。</p>
        <p v-if="registered" class="success-note" role="status">帳號已建立，請輸入帳密登入。</p>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="error-title">
          <h2 id="error-title">{{ summary }}</h2>
          <ul v-if="errorFields.length">
            <li v-for="name in errorFields" :key="name">
              <button type="button" @click="focusField(name)">{{ fieldNames[name] }}：{{ errors[name].join('；') }}</button>
            </li>
          </ul>
          <div v-if="logId" class="trace">
            <span>
              追蹤編號：
              <span class="trace-id">{{ logId }}</span>
            </span>
            <button type="button" @click="copyLogId">複製編號</button>
            <span role="status">{{ copyState }}</span>
          </div>
        </section>
        <p v-if="authChecking" role="status">正在確認登入狀態，請稍候…</p>
        <p v-else-if="account" role="status">登入已確認，正在返回文章入口…</p>
        <template v-else>
          <p v-if="credential && !account" class="pending-note">
            目前登入狀態尚未確認。
            <button type="button" class="inline-button" :disabled="pending || logoutPending" @click="verifyAccount">重新驗證登入</button>
          </p>
          <AccountForm
            v-model:email="email"
            v-model:password="password"
            :pending="pending" :blocked="authChecking || logoutPending"
            :locked="locked"
            :errors="errors"
            :lock-text="lockText"
            @submit="submitLogin"
          />
        </template>
        <p class="form-footer">
          還沒有帳號？
          <a href="/vue/register">註冊會員</a>
        </p>
      </section>
      <section v-else class="message-page" aria-labelledby="page-title">
        <h1 id="page-title">找不到頁面</h1>
        <p>這個路徑沒有對應頁面。</p>
        <a class="text-link" href="/vue/">返回文章入口</a>
      </section>
    </main>
    <footer class="site-footer">
      <span>文章集</span>
      <span>Vue 3＋Vite × {{ backendLabel }}</span>
    </footer>
  </div>
</template>
