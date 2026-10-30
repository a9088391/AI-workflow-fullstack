<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { BACKENDS, selectBackend } from './auth.js'
import { useAccountSession } from './useAccountSession.js'
import AccountForm from './AccountForm.vue'
import { frontendDestination, loginReturnTarget, selectedBackendLabel } from './navigation.js'
import { readComment, readCommentList, validateComment } from './comments.js'
import { articleCreated, contentUpdated, articleFailure, articlePage, backendName, localPage, readArticle, readArticleList, validateArticle } from './articles.js'
import { readLockoutSettings, readMember, readMemberList, validateLockoutSettings } from './admin.js'

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
// 成功提示只由已確認建立後的轉頁參數啟用，並限制在「我的文章」頁。
const published = page === 'my-articles' && new URLSearchParams(window.location.search).get('published') === '1'
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
const initialKeyword = page === 'members' ? new URLSearchParams(window.location.search).get('keyword') || '' : ''
const keyword = ref(initialKeyword)
const searchedKeyword = initialKeyword.replace(/^ *| *$/g, '')
const memberData = ref(null)
const memberList = ref(null)
const statusPending = ref(false)
const statusUnknown = ref(false)
const statusNeedsReadback = ref(false)
const statusNotice = ref('')
let statusOwner = null
const lockoutData = ref(null)
const lockoutDraft = ref(null)
const lockoutPending = ref(false)
const lockoutUnknown = ref(false)
const lockoutNeedsReadback = ref(false)
const lockoutNotice = ref('')
let lockoutOwner = null
let lockoutApplyReadback = false
const articleTitle = ref('')
const articleBody = ref('')
const commentBody = ref('')
const commentPage = ref(listPage)
const commentPosted = ref(false)
const articleData = ref(null)
const articleList = ref(null)
const commentData = ref(null)
const commentList = ref(null)
const parentArticle = ref(null)
const relatedLoading = ref(false)
const relatedError = ref('')
const relatedLogId = ref('')
const relatedSummary = ref(null)
const contentLoading = ref(false)
const contentPending = ref(false)
const contentReady = ref(false)
const writeUnknown = ref(false)
const lifecycleUnknown = ref(false)
const editVersion = ref(null)
const editLoaded = ref(false)
const editOriginal = ref(null)
const conflict = ref(false)
const comparison = ref(null)
const updateNotice = ref('')
let draftOwner = null
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
let contentController = null
let contentTimeout = null
let contentEpoch = 0
let relatedController = null
let relatedTimeout = null
let relatedEpoch = 0

// 草稿僅存在目前頁面記憶體；任何未送出內容都應觸發離頁確認。
const hasDraft = computed(() => {
  if (submitted.value) return false
  if (page === 'register') return Boolean(email.value || password.value || nickname.value)
  if (page === 'login') return Boolean(email.value || password.value)
  if (page === 'article-new') return Boolean(articleTitle.value || articleBody.value)
  if (page === 'article') return Boolean(commentBody.value)
  // 編輯頁只有讀到原始版本後才能比較；原文不可因重讀失敗而當作空字串。
  if (editing && editLoaded.value) {
    return articleResource
      ? articleTitle.value !== editOriginal.value?.title || articleBody.value !== editOriginal.value?.body
      : commentBody.value !== editOriginal.value?.body
  }
  return page === 'lockout-settings' && hasLockoutDraft()
})

// 登入與內容頁共用同一份表單及 session；這些回呼僅處理內容／草稿整合。
const {
  email, password, nickname, pending, credential, backend, account, authChecking,
  logoutPending, reLogin, disabledAccount, locked, lockText,
  submitRegistration, submitLogin, submitLogout, verifyAccount, forgetCredential,
  restoreSession, disposeSession,
} = useAccountSession({
  page, protectedPage, returnTo, hasDraft, submitted,
  feedback: { summary, summaryCode, errors, logId, copyState }, showError,
  isContentBusy, onBeforeVerify: prepareAccountVerification,
  onVerified: acceptVerifiedAccount, onVerifiedReady: loadVerifiedContent,
  onAccountChanged: accountChanged, onLogin: resumeAfterLogin, onForget: clearCredentialContent,
})

// 內容寫入與認證共用互斥邊界；驗權或登出不得中斷尚在等待的寫入。
function isContentBusy() {
  return contentPending.value || statusPending.value || lockoutPending.value
}

// 只比較目前伺服器值與表單值，供離頁確認保護未儲存設定。
function hasLockoutDraft() {
  if (!lockoutDraft.value) return false
  // 重讀或重新驗權時舊值暫時不可用，仍須保護記憶體草稿。
  if (!lockoutData.value) return true
  for (const name of Object.keys(lockoutData.value)) if (lockoutDraft.value[name] !== lockoutData.value[name]) return true
  return false
}
// 留言寫入須確認父文章公開及目前登入有效，後端再做最終權限判斷。
const canComment = computed(() => page === 'article' && contentReady.value && articleData.value?.visibility === 'visible' && Boolean(account.value && credential.value) && !authChecking.value && !reLogin.value && !disabledAccount.value && !logoutPending.value)
// 詳情與編輯以後端最新讀取值判斷資格；管理員不能修改他人內文。
const currentResource = computed(() => articleResource ? articleData.value : commentResource ? commentData.value : null)
// 確認目前登入與資源後才顯示操作，避免重驗期間洩漏受保護內容。
const authorizedResource = computed(() => Boolean(contentReady.value && currentResource.value && account.value && credential.value && !authChecking.value && !reLogin.value && !disabledAccount.value && !logoutPending.value))
// 作者編修仍需確認資格及父文章狀態，不能只憑列表連結開放內文寫入。
const canEdit = computed(() => authorizedResource.value && currentResource.value.author.id === account.value.id && (!commentResource || parentArticle.value?.visibility === 'visible' && !relatedLoading.value))
// 按目前資源資格顯示下架操作，實際權限及版本仍由後端重驗。
const canManageVisibility = computed(() => authorizedResource.value && (currentResource.value.author.id === account.value.id || account.value.role === 'admin'))
// 管理員可再次下架他人內容以接管恢復權；自己的內容仍按作者規則。
const canHide = computed(() => canManageVisibility.value && (currentResource.value.visibility === 'visible' || account.value.role === 'admin' && currentResource.value.author.id !== account.value.id))
// 原文保留在 refs；textarea 的瀏覽器換行正規化只在真實 input 事件後採用。
function editBodyInput(event) {
  if (articleResource) articleBody.value = event.target.value
  else commentBody.value = event.target.value
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

// 切頁、登出或換帳號後取消內容與設定請求；epoch 阻擋不遵守 abort 的舊回應。
function stopContentRequest() {
  contentEpoch += 1
  if (contentTimeout) clearTimeout(contentTimeout)
  contentTimeout = null
  contentController?.abort()
  contentController = null
}

// 內容與設定讀寫只保留目前請求；逾時寫入可能已完成，須由使用者自行查證。
function startContentRequest() {
  stopContentRequest()
  contentController = new AbortController()
  const controller = contentController
  const epoch = contentEpoch
  // 定時取消前端等待；寫入是否完成仍以伺服器回應或查證結果為準。
  contentTimeout = setTimeout(() => controller.abort(), 20000)
  return { controller, epoch }
}

// 回應必須屬於目前頁面與目前帳號，避免舊資料重新出現。
function currentContentRequest(request, token) {
  return alive && contentController === request.controller && contentEpoch === request.epoch && credential.value?.accessToken === token
}

// 只清理仍屬目前操作的計時器與取消控制。
function finishContentRequest(request, token) {
  if (!currentContentRequest(request, token)) return
  if (contentTimeout) clearTimeout(contentTimeout)
  contentTimeout = null
  contentController = null
}

// 留言列表與父文章各自可重試；離頁或授權改變時撤銷舊結果。
function stopRelatedRequest() {
  relatedEpoch += 1
  if (relatedTimeout) clearTimeout(relatedTimeout)
  relatedTimeout = null
  relatedController?.abort()
  relatedController = null
  relatedLoading.value = false
}

// 獨立讀取只接受目前帳號與目前請求，避免父文章失敗或舊回應覆蓋留言。
function currentRelatedRequest(request, token) {
  return alive && relatedController === request.controller && relatedEpoch === request.epoch && credential.value?.accessToken === token && (!token || Boolean(account.value))
}

// 文章頁讀留言；留言詳情另向父文章驗權，不能從 article_id 推斷可讀內容。
async function loadRelated() {
  // 寫入結果未確認前不可插入讀取，避免讀取失效回應撤銷仍在等待的寫入。
  if (contentPending.value || relatedLoading.value || authChecking.value || disabledAccount.value || reLogin.value || !contentReady.value || credential.value && !account.value || summaryCode.value === 'E0002') return
  if (page !== 'article' && !commentResource) return
  stopRelatedRequest()
  relatedLoading.value = true
  relatedError.value = ''
  relatedLogId.value = ''
  if (page === 'article') commentList.value = null
  else parentArticle.value = null
  relatedController = new AbortController()
  const request = { controller: relatedController, epoch: relatedEpoch }
  const token = credential.value?.accessToken
  const parentId = commentResource ? commentData.value.article_id : requestedId
  const endpoint = commentResource ? `/api/articles/${parentId}` : `/api/articles/${parentId}/comments?page=${commentPage.value}&per_page=20`
  // 逾時只停止這次讀取；原留言仍保留，交由使用者手動重試。
  relatedTimeout = setTimeout(() => request.controller.abort(), 20000)
  try {
    const response = await fetch(`${backend.value}${endpoint}`, { headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, signal: request.controller.signal })
    const body = await response.json()
    if (!currentRelatedRequest(request, token)) return
    const data = commentResource ? readArticle(response.status, body, parentId) : readCommentList(response.status, body, commentPage.value, parentId)
    if (data) {
      if (commentResource) parentArticle.value = data
      else commentList.value = data
      return
    }
    const result = articleFailure(body)
    // JWT 失效或停用須撤銷整頁授權；一般父文章錯誤不影響作者自己的留言。
    if (response.status === 401 && result.code === 'E0002' || response.status === 403 && result.code === 'E0102') {
      forgetCredential()
      reLogin.value = response.status === 401 && (protectedPage || page === 'article' && hasDraft.value)
      disabledAccount.value = response.status === 403
      await showError(result.message, {}, result.logId, result.code)
      return
    }
    if (page === 'article' && response.status === 404 && result.code === 'E0004') {
      // 列表 404 已否定父文章可讀性，撤下舊內容及表單；草稿保留，須重讀文章後才恢復操作。
      articleData.value = null
      commentList.value = null
      contentReady.value = false
      stopRelatedRequest()
      await showError(result.message, {}, result.logId, result.code)
      return
    }
    relatedError.value = commentResource && response.status === 404 && result.code === 'E0004' ? '所屬文章目前無法讀取' : data === null && response.status === 200 ? '伺服器回應不完整，請手動重新讀取。' : result.message
    relatedLogId.value = result.logId
  } catch {
    if (currentRelatedRequest(request, token)) relatedError.value = '讀取失敗或伺服器回應不完整，請手動重試。'
  } finally {
    if (currentRelatedRequest(request, token)) {
      stopRelatedRequest()
      if (relatedError.value) {
        await nextTick()
        relatedSummary.value?.focus()
      }
    }
  }
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

// 內容列表、詳情與管理設定沿同一讀取流程，登入與角色驗證先於受保護資料。
async function loadContent() {
  // 排除非內容頁；管理資料仍須通過登入與角色驗證。
  if (!['articles', 'article', 'my-articles', 'my-comments', 'comment', 'article-edit', 'comment-edit', 'members', 'member', 'managed-articles', 'managed-comments', 'lockout-settings'].includes(page)
    || contentLoading.value || contentPending.value || statusPending.value || lockoutPending.value || authChecking.value
    || disabledAccount.value || summaryCode.value === 'E0002' && !credential.value
    || protectedPage && !account.value || credential.value && !account.value
    || adminPage && account.value?.role !== 'admin') return
  contentLoading.value = true
  contentReady.value = false
  articleData.value = null
  articleList.value = null
  memberData.value = null
  memberList.value = null
  commentData.value = null
  commentList.value = null
  parentArticle.value = null
  stopRelatedRequest()
  relatedError.value = ''
  relatedLogId.value = ''
  summary.value = ''
  const request = startContentRequest()
  const token = credential.value?.accessToken
  const endpoint = articleResource ? `/api/articles/${requestedId}`
    : commentResource ? `/api/comments/${requestedId}`
    : page === 'member' ? `/api/members/${requestedId}`
    : page === 'members' ? '/api/members'
    : page === 'lockout-settings' ? '/api/settings/login-lockout'
    : page === 'managed-comments' ? '/api/comments'
    : page === 'my-comments' ? '/api/me/comments'
    : page === 'my-articles' ? '/api/me/articles' : '/api/articles'
  const query = articleResource || commentResource || page === 'member' || page === 'lockout-settings' ? ''
    : `?page=${listPage}&per_page=20${page === 'managed-articles' ? '&filters%5Bscope%5D=management'
      : page === 'members' && searchedKeyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(searchedKeyword)}` : ''}`
  try {
    const response = await fetch(`${backend.value}${endpoint}${query}`, { headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, signal: request.controller.signal })
    const body = await response.json()
    if (!currentContentRequest(request, token)) return
    if (response.status === 200 && body?.success === true) {
      const data = articleResource ? readArticle(response.status, body, requestedId)
        : commentResource ? readComment(response.status, body, requestedId)
        : page === 'member' ? readMember(response.status, body, requestedId)
        : page === 'members' ? readMemberList(response.status, body, listPage)
        : page === 'lockout-settings' ? readLockoutSettings(response.status, body)
        : page === 'my-comments' || page === 'managed-comments' ? readCommentList(response.status, body, listPage)
        : readArticleList(response.status, body, listPage)
      if (!data) { await showError('伺服器回應不完整，請手動重新讀取。'); return }
      if (articleResource) articleData.value = data
      else if (commentResource) commentData.value = data
      else if (page === 'member') memberData.value = data
      else if (page === 'members') memberList.value = data
      else if (page === 'lockout-settings') {
        lockoutData.value = data
        lockoutOwner = account.value.id
        // 初次載入或已確認成功的寫入才同步草稿；結果不明時的原輸入留給管理員比較。
        if (!lockoutDraft.value || lockoutApplyReadback) lockoutDraft.value = { ...data }
        lockoutApplyReadback = false
        if (lockoutUnknown.value) {
          lockoutNeedsReadback.value = false
          lockoutNotice.value = '已重讀目前伺服器設定；先前結果仍無法單憑目前值判定，請確認後再操作。'
        }
      }
      else if (page === 'my-comments' || page === 'managed-comments') commentList.value = data
      else articleList.value = data
      contentReady.value = true
      if (page === 'member' && statusUnknown.value) {
        statusNeedsReadback.value = false
        statusNotice.value = `已重讀，目前伺服器狀態為${data.status === 'active' ? '啟用' : '停用'}。`
      }
      // 初次載入才填入編輯草稿與版本；重讀只提供比較，不靜默覆蓋草稿或版本。
      if (editing && data.author.id === account.value.id) {
        if (!editLoaded.value) {
          articleTitle.value = articleResource ? data.title : ''
          articleBody.value = articleResource ? data.body : ''
          commentBody.value = commentResource ? data.body : ''
          editOriginal.value = data
          editVersion.value = data.version
          editLoaded.value = true
          draftOwner = account.value.id
        } else comparison.value = data
      } else if (conflict.value) comparison.value = data
      loadRelated()
      return
    }
    const result = articleFailure(body)
    // 管理搜尋的巢狀欄位錯誤要連到輸入框；其他未對應錯誤仍由共用摘要顯示。
    if (page === 'members' && response.status === 422 && result.code === 'E0001' && Array.isArray(body?.errors?.['filters.keyword'])) {
      const messages = []
      for (const message of body.errors['filters.keyword']) if (typeof message === 'string') messages.push(message)
      result.errors['filters.keyword'] = messages
    }
    if (response.status === 401 && result.code === 'E0002') {
      forgetCredential()
      reLogin.value = protectedPage || page === 'article' && hasDraft.value
    } else if (response.status === 403 && result.code === 'E0102') {
      forgetCredential()
      disabledAccount.value = true
    } else if (adminPage && response.status === 403 && result.code === 'E0003' || page === 'member' && response.status === 404 && result.code === 'E0004') {
      // 權限或目標已變更，保留錯誤入口，但不再顯示舊的管理資料。
      memberData.value = null
      memberList.value = null
      lockoutData.value = null
      articleList.value = null
      commentList.value = null
      contentReady.value = false
    }
    await showError(result.message, result.errors, result.logId, result.code)
  } catch {
    if (currentContentRequest(request, token)) await showError('讀取失敗或伺服器回應不完整，請手動重試。')
  } finally {
    if (currentContentRequest(request, token)) {
      contentLoading.value = false
      finishContentRequest(request, token)
    } else if (alive && !contentController) contentLoading.value = false
  }
}

// 搜尋只提交字面字串，換頁回第一頁；搜尋條件不進入登入返回目標。
function submitMemberSearch() {
  const value = keyword.value.replace(/^ *| *$/g, '')
  if ([...value].length > 254) {
    showError('欄位資料不正確，請確認後再送出', { 'filters.keyword': ['搜尋關鍵字最多 254 個字。'] })
    return
  }
  window.location.assign(`/vue/admin/members${value ? `?keyword=${encodeURIComponent(value)}` : ''}`)
}

// 會員分頁保留字面搜尋條件；其他管理列表只保留頁碼。
function adminPageHref(nextPage) {
  const query = new URLSearchParams()
  if (page === 'members' && searchedKeyword) query.set('keyword', searchedKeyword)
  query.set('page', String(nextPage))
  return `${path}?${query.toString()}`
}

// 管理員提交完整四欄設定；未知結果需先手動重讀，避免重送可能已提交的 PUT。
async function submitLockoutSettings() {
  if (page !== 'lockout-settings' || !lockoutData.value || !lockoutDraft.value || !contentReady.value
    || contentLoading.value || lockoutPending.value || lockoutNeedsReadback.value
    || authChecking.value || logoutPending.value || !credential.value || account.value?.role !== 'admin') return
  const checked = validateLockoutSettings(lockoutDraft.value)
  if (Object.keys(checked).length) { await showError('欄位資料不正確，請確認後再送出', checked); return }
  const payload = { enabled: lockoutDraft.value.enabled, window_minutes: lockoutDraft.value.window_minutes, failure_threshold: lockoutDraft.value.failure_threshold, lock_seconds: lockoutDraft.value.lock_seconds }
  lockoutOwner = account.value.id
  lockoutPending.value = true
  lockoutNotice.value = ''
  summary.value = ''
  const request = startContentRequest()
  const token = credential.value.accessToken
  let confirmed = false
  try {
    const response = await fetch(`${backend.value}/api/settings/login-lockout`, {
      method: 'PUT',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify(payload),
      signal: request.controller.signal,
    })
    const body = await response.json()
    if (!currentContentRequest(request, token)) return
    if (contentUpdated(response.status, body)) {
      // 完整成功回應已確認欄位通過驗證，移除先前 422 的摘要、欄旁錯誤與追蹤資訊。
      summaryCode.value = ''
      errors.value = {}
      logId.value = ''
      confirmed = true
      lockoutNotice.value = '更新已送達；正在重新讀取伺服器設定。'
    } else {
      const result = articleFailure(body)
      const uncertain = response.status >= 500 || response.status === 200
      // 5xx 與不完整成功回應可能已寫入；保留草稿並要求手動查證。
      if (uncertain) { lockoutUnknown.value = true; lockoutNeedsReadback.value = true }
      const fieldErrors = {}
      if (response.status === 422 && result.code === 'E0001' && body?.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
        for (const name of ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds']) {
          if (Array.isArray(body.errors[name])) {
            fieldErrors[name] = []
            for (const message of body.errors[name]) if (typeof message === 'string') fieldErrors[name].push(message)
          }
        }
      }
      if (response.status === 401 && result.code === 'E0002') { forgetCredential(); reLogin.value = true }
      else if (response.status === 403 && result.code === 'E0102') { forgetCredential(); disabledAccount.value = true }
      else if (response.status === 403 && result.code === 'E0003') { lockoutData.value = null; contentReady.value = false }
      await showError(uncertain ? `更新結果尚未確認；請手動重讀設定查證。${result.message}` : result.message, fieldErrors, result.logId, result.code)
    }
  } catch {
    if (currentContentRequest(request, token)) {
      lockoutUnknown.value = true
      lockoutNeedsReadback.value = true
      await showError('更新結果尚未確認；請手動重讀設定查證，再決定是否重新送出。')
    }
  } finally {
    // 只結束本次有效寫入，不能讓舊回應解除後續操作的等待狀態。
    if (currentContentRequest(request, token)) {
      lockoutPending.value = false
      finishContentRequest(request, token)
    }
  }
  if (!confirmed || !alive || credential.value?.accessToken !== token) return
  // 成功 envelope 沒有設定值，重讀後才同步顯示；先前未知寫入仍保留獨立警示。
  lockoutApplyReadback = true
  await loadContent()
  lockoutApplyReadback = false
  if (lockoutData.value && contentReady.value && !lockoutUnknown.value) lockoutNotice.value = '已重新讀取並顯示目前伺服器設定。'
  else if (!lockoutData.value && account.value?.role === 'admin') lockoutNotice.value = '更新已送達，但尚未讀到伺服器設定；請手動重新讀取。'
}

// 狀態寫入只送 approved status；不根據空成功回應推測新狀態，而是重讀同一會員。
async function submitMemberStatus() {
  if (page !== 'member' || !memberData.value || !contentReady.value
    || contentLoading.value || statusPending.value || statusNeedsReadback.value
    || authChecking.value || logoutPending.value || !credential.value
    || account.value?.role !== 'admin') return
  const nextStatus = memberData.value.status === 'active' ? 'disabled' : 'active'
  statusOwner = account.value.id
  statusPending.value = true
  statusNotice.value = ''
  const request = startContentRequest()
  const token = credential.value.accessToken
  try {
    const response = await fetch(`${backend.value}/api/members/${requestedId}/status`, {
      method: 'PUT',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({ status: nextStatus }),
      signal: request.controller.signal,
    })
    const body = await response.json()
    if (!currentContentRequest(request, token)) return
    if (contentUpdated(response.status, body)) {
      statusNotice.value = '更新已送達；正在重新讀取會員狀態。'
    } else {
      const result = articleFailure(body)
      // 5xx 或不完整的 200 可能已提交；保留結果不明狀態，必須先讀回同一 ID。
      const outcomeUnknown = response.status >= 500 || response.status === 200
      if (outcomeUnknown) {
        statusUnknown.value = true
        statusNeedsReadback.value = true
      }
      if (response.status === 401 && result.code === 'E0002') {
        forgetCredential()
        reLogin.value = true
      } else if (response.status === 403 && result.code === 'E0102') {
        forgetCredential()
        disabledAccount.value = true
      } else if (response.status === 403 && result.code === 'E0003' || response.status === 404 && result.code === 'E0004') {
        memberData.value = null
        contentReady.value = false
      }
      await showError(outcomeUnknown ? `更新結果尚未確認；請先重讀同一會員查證。${result.message}` : result.message, {}, result.logId, result.code)
      return
    }
  } catch {
    if (currentContentRequest(request, token)) {
      statusUnknown.value = true
      statusNeedsReadback.value = true
      await showError('更新結果尚未確認；請先重讀同一會員查證，再決定是否手動重試。')
    }
    return
  } finally {
    // 只清掉本次仍有效的等待狀態，避免舊請求完成後解開較新的操作。
    if (currentContentRequest(request, token)) {
      statusPending.value = false
      finishContentRequest(request, token)
    }
  }
  // 成功 envelope 不含新狀態；讀回同一 ID 才能顯示伺服器值，且不替先前結果不明的寫入下結論。
  if (alive && credential.value?.accessToken === token) await loadContent()
  if (contentReady.value && !statusUnknown.value) statusNotice.value = `已重讀會員，目前狀態為${memberData.value.status === 'active' ? '啟用' : '停用'}。`
  else if (account.value?.role === 'admin' && !contentReady.value) statusNotice.value = '更新已送達，但尚未讀到最新會員狀態；請手動重新讀取。'
}

// 作者只送 title/body；確認 201 空 data 後才帶提示回我的文章第一頁，失敗保留草稿。
async function submitArticle() {
  if (page !== 'article-new' || contentPending.value || authChecking.value || !account.value || !credential.value || reLogin.value || disabledAccount.value) return
  const checked = validateArticle({ title: articleTitle.value, body: articleBody.value })
  if (Object.keys(checked).length) { await showError('欄位資料不正確，請確認後再送出', checked); return }
  contentPending.value = true
  writeUnknown.value = false
  lifecycleUnknown.value = false
  summary.value = ''
  errors.value = {}
  logId.value = ''
  const request = startContentRequest()
  const token = credential.value.accessToken
  try {
    const response = await fetch(`${backend.value}/api/articles`, { method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ title: articleTitle.value, body: articleBody.value }), signal: request.controller.signal })
    const body = await response.json()
    if (!currentContentRequest(request, token)) return
    if (articleCreated(response.status, body)) {
      submitted.value = true
      window.location.assign('/vue/me/articles?published=1')
      return
    }
    const result = articleFailure(body)
    if (response.status >= 500 || response.status === 201) writeUnknown.value = true
    if (response.status === 401 && result.code === 'E0002') {
      draftOwner = account.value.id
      forgetCredential()
      reLogin.value = true
    } else if (response.status === 403 && result.code === 'E0102') {
      forgetCredential()
      disabledAccount.value = true
    }
    await showError(result.message, result.errors, result.logId, result.code)
  } catch {
    if (currentContentRequest(request, token)) {
      writeUnknown.value = true
      await showError('無法確認發文結果；請先到我的文章查證，再決定是否手動重試。')
    }
  } finally {
    if (currentContentRequest(request, token)) {
      contentPending.value = false
      finishContentRequest(request, token)
    } else if (alive && !contentController) contentPending.value = false
  }
}

// 留言只送原始 body；確認建立後留在原文章重讀第一頁，不推測新 ID 或自動重送。
async function submitComment() {
  if (!canComment.value || contentPending.value) return
  const checked = validateComment(commentBody.value)
  if (Object.keys(checked).length) { await showError('欄位資料不正確，請確認後再送出', checked); return }
  draftOwner = account.value.id
  contentPending.value = true
  commentPosted.value = false
  // 留言與資源更新各自保留結果不明警告；新留言只重置自己的狀態。
  writeUnknown.value = false
  summary.value = ''
  summaryCode.value = ''
  errors.value = {}
  logId.value = ''
  // 送出時撤銷舊留言讀取，避免舊列表或授權回應覆蓋本次操作。
  stopRelatedRequest()
  const request = startContentRequest()
  const token = credential.value.accessToken
  try {
    const response = await fetch(`${backend.value}/api/articles/${requestedId}/comments`, { method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify({ body: commentBody.value }), signal: request.controller.signal })
    const body = await response.json()
    if (!currentContentRequest(request, token)) return
    if (articleCreated(response.status, body)) {
      commentBody.value = ''
      commentPosted.value = true
      commentPage.value = 1
      // 同步網址與畫面分頁；重整與上一頁都沿用原文章，不新增未知留言路徑。
      window.history.replaceState(null, '', `${path}#comments`)
      contentPending.value = false
      finishContentRequest(request, token)
      loadRelated()
      return
    }
    const result = articleFailure(body)
    if (response.status >= 500 || response.status === 201) writeUnknown.value = true
    if (response.status === 401 && result.code === 'E0002') {
      forgetCredential()
      reLogin.value = true
    } else if (response.status === 403 && result.code === 'E0102') {
      forgetCredential()
      disabledAccount.value = true
    } else if (response.status === 403 && result.code === 'E0003' || response.status === 404 && result.code === 'E0004') {
      // 可讀性已改變，先隱藏舊文章與表單，須手動重讀確認現況。
      articleData.value = null
      commentList.value = null
      contentReady.value = false
    }
    await showError(result.message, result.errors, result.logId, result.code)
  } catch {
    if (currentContentRequest(request, token)) {
      writeUnknown.value = true
      await showError('無法確認留言結果；請先重新讀取留言查證，再決定是否手動重試。')
    }
  } finally {
    if (currentContentRequest(request, token)) {
      contentPending.value = false
      finishContentRequest(request, token)
    } else if (alive && !contentController) contentPending.value = false
  }
}

// 換帳號時清掉版本、比較及未知結果，原帳號文字不能交給新身分。
function resetEditor() {
  editVersion.value = null
  editLoaded.value = false
  editOriginal.value = null
  conflict.value = false
  comparison.value = null
  writeUnknown.value = false
  lifecycleUnknown.value = false
  updateNotice.value = ''
}

// 人工比較後明確選擇整合或捨棄；只更新版本，不自動 PUT。
function acceptComparison(discard) {
  if (!editing || !comparison.value || !canEdit.value || contentPending.value || contentLoading.value) return
  if (!window.confirm(discard ? '確定捨棄自己的輸入，採用最新內容？' : '已比較並整合自己的輸入，確定採用最新版本供手動送出？')) return
  if (discard) {
    if (articleResource) { articleTitle.value = comparison.value.title; articleBody.value = comparison.value.body }
    else commentBody.value = comparison.value.body
  }
  editOriginal.value = comparison.value
  editVersion.value = comparison.value.version
  conflict.value = false
  comparison.value = null
  updateNotice.value = '已確認比較結果；如要儲存，請手動送出。'
}

// 作者編修保留原文與原讀取版本；衝突未人工確認前不能重送。
async function submitEdit() {
  if (!editing || !canEdit.value || contentPending.value || contentLoading.value || conflict.value || !editVersion.value) return
  const checked = articleResource ? validateArticle({ title: articleTitle.value, body: articleBody.value }) : validateComment(commentBody.value)
  if (Object.keys(checked).length) { await showError('欄位資料不正確，請確認後再送出', checked); return }
  const payload = articleResource ? { title: articleTitle.value, body: articleBody.value, version: editVersion.value } : { body: commentBody.value, version: editVersion.value }
  await updateContent(payload)
}

// 每次下架／恢復都重新確認操作；409 後必須先重讀，不能只替換版本再重送。
async function submitVisibility(visibility) {
  if (editing || !canManageVisibility.value || contentPending.value || contentLoading.value || conflict.value && !comparison.value) return
  if (visibility !== 'hidden' && visibility !== 'visible' || visibility === 'hidden' && !canHide.value || visibility === 'visible' && !currentResource.value.can_restore) return
  const action = visibility === 'hidden' ? currentResource.value.visibility === 'hidden' ? '再次下架並由管理員控制恢復' : '下架' : '恢復'
  if (!window.confirm(`確定${action}這筆內容？`)) return
  await updateContent({ visibility, version: currentResource.value.version }, true)
}

// 已知 ID 寫入共用既有 epoch；成功與同值成功都重讀同一資源，未知結果不自動重送。
async function updateContent(payload, visibility = false) {
  // 記錄送出身分並停止舊父文章讀取；後續重登不能接收他人的草稿。
  draftOwner = account.value.id
  contentPending.value = true
  // 新 PUT 只重置資源更新警告，不能替先前結果不明的留言作出結論。
  lifecycleUnknown.value = false
  updateNotice.value = ''
  summary.value = ''
  summaryCode.value = ''
  errors.value = {}
  logId.value = ''
  stopRelatedRequest()
  const request = startContentRequest()
  const token = credential.value.accessToken
  const endpoint = `/api/${articleResource ? 'articles' : 'comments'}/${requestedId}${visibility ? '/visibility' : ''}`
  try {
    const response = await fetch(`${backend.value}${endpoint}`, { method: 'PUT', headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }, body: JSON.stringify(payload), signal: request.controller.signal })
    const body = await response.json()
    if (!currentContentRequest(request, token)) return
    // 成功不樂觀改寫資源；重新讀取同 ID，才把編輯基準切到伺服器確認值。
    if (contentUpdated(response.status, body)) {
      conflict.value = false
      comparison.value = null
      editLoaded.value = false
      updateNotice.value = '更新已確認；內容以重新讀取的結果為準。'
      contentPending.value = false
      finishContentRequest(request, token)
      loadContent()
      return
    }
    const result = articleFailure(body)
    if (response.status >= 500 || response.status === 200) lifecycleUnknown.value = true
    // 409 只撤銷比較結果，不改使用者文字與舊版本，等待人工重讀及確認。
    if (response.status === 409 && result.code === 'E0201') {
      conflict.value = true
      comparison.value = null
    } else if (response.status === 401 && result.code === 'E0002') {
      forgetCredential()
      reLogin.value = true
    } else if (response.status === 403 && result.code === 'E0102') {
      forgetCredential()
      disabledAccount.value = true
    } else if (response.status === 403 && result.code === 'E0003' || response.status === 404 && result.code === 'E0004') {
      // 權限／可讀性已改變，撤下舊資源和父文章但保留草稿，重讀後才重建資格。
      articleData.value = null
      commentData.value = null
      parentArticle.value = null
      commentList.value = null
      contentReady.value = false
    }
    await showError(result.message, result.errors, result.logId, result.code)
  } catch {
    if (currentContentRequest(request, token)) {
      lifecycleUnknown.value = true
      await showError('更新結果尚未確認；請先重新讀取同一筆內容查證，再決定是否手動重試。')
    }
  } finally {
    if (currentContentRequest(request, token)) {
      contentPending.value = false
      finishContentRequest(request, token)
    } else if (alive && !contentController) contentPending.value = false
  }
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
  if (account.value && hasDraft.value && (page === 'article-new' || page === 'article' || editing)) draftOwner = account.value.id
  stopContentRequest()
  contentLoading.value = false
  contentPending.value = false
  statusPending.value = false
  lockoutPending.value = false
  lockoutData.value = null
  articleData.value = null
  articleList.value = null
  memberData.value = null
  memberList.value = null
  statusNotice.value = ''
  commentData.value = null
  commentList.value = null
  parentArticle.value = null
  stopRelatedRequest()
  relatedError.value = ''
  relatedLogId.value = ''
  contentReady.value = false
}

// 換帳號後清除前一位管理員的設定草稿與查證狀態，避免新帳號讀取時沿用舊輸入。
function clearLockoutForDifferentAccount(nextAccountId) {
  if (lockoutOwner === null || lockoutOwner === nextAccountId) return
  lockoutDraft.value = null
  lockoutUnknown.value = false
  lockoutNeedsReadback.value = false
  lockoutNotice.value = ''
  lockoutOwner = null
}

// 驗證前撤下內容授權與舊讀取；保留原帳號的記憶體草稿供同帳號恢復。
function prepareAccountVerification(previousAccount) {
  if ((page === 'article-new' || page === 'article' || editing) && previousAccount && hasDraft.value) draftOwner = previousAccount.id
  // 重驗期間停止讀取並隱藏原授權資料，成功後再向後端重新取得。
  stopContentRequest()
  stopRelatedRequest()
  contentLoading.value = false
  contentReady.value = false
  articleData.value = null
  articleList.value = null
  memberData.value = null
  memberList.value = null
  lockoutData.value = null
  commentData.value = null
  commentList.value = null
  parentArticle.value = null
}

// /me 成功後只恢復相同帳號的草稿；角色改變仍重新判斷管理頁資格。
async function acceptVerifiedAccount(nextAccount) {
  if ((page === 'article-new' || page === 'article' || editing) && draftOwner !== null && draftOwner !== nextAccount.id) {
    articleTitle.value = ''
    articleBody.value = ''
    commentBody.value = ''
    resetEditor()
    await showError('目前登入帳號已變更，原帳號草稿已清除；請重新輸入。')
  }
  if (page === 'article-new' || page === 'article' || editing) draftOwner = nextAccount.id
  accountChanged(nextAccount.id)
  if (adminPage && nextAccount.role !== 'admin') await showError('沒有權限使用此功能', {}, '', 'E0003')
}

// 成功驗權後才讀取目前角色可讀的資料，避免管理頁提前送出受保護請求。
function loadVerifiedContent() {
  if (!adminPage || account.value.role === 'admin') loadContent()
}

// 切換帳號時清除管理員專屬草稿與尚未確認操作，避免由新帳號接收。
function accountChanged(nextAccountId) {
  clearLockoutForDifferentAccount(nextAccountId)
  if (statusOwner !== null && statusOwner !== nextAccountId) {
    statusUnknown.value = false
    statusNeedsReadback.value = false
    statusNotice.value = ''
    statusOwner = null
  }
}

// 原頁重登只恢復資料；文字由使用者手動送出，換帳號必須清掉先前草稿。
async function resumeAfterLogin(nextAccount) {
  if (adminPage && nextAccount.role !== 'admin') {
    memberData.value = null
    memberList.value = null
    contentReady.value = false
    await showError('沒有權限使用此功能', {}, '', 'E0003')
    return
  }
  if (draftOwner !== null && draftOwner !== nextAccount.id) {
    articleTitle.value = ''
    articleBody.value = ''
    commentBody.value = ''
    resetEditor()
    loadContent()
    await showError('已登入不同帳號，原帳號草稿已清除；請重新輸入。')
  } else {
    summary.value = ''
    if (['article', 'my-articles', 'my-comments', 'comment', 'article-edit', 'comment-edit'].includes(page) || adminPage) loadContent()
  }
  draftOwner = nextAccount.id
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
  disposeSession()
  stopContentRequest()
  stopRelatedRequest()
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
      <section v-else-if="page === 'articles' || page === 'article' || page === 'article-new' || page === 'my-articles' || page === 'my-comments' || page === 'comment' || editing" class="reading-page" aria-labelledby="page-title">
        <div class="reading-heading"><div><p class="eyebrow">文章集 / {{ page === 'articles' ? '公開閱讀' : page === 'article' ? '文章詳情' : page === 'article-new' ? '會員發表' : page === 'my-articles' ? '會員文章' : page === 'article-edit' ? '文章編修' : page === 'comment-edit' ? '留言編修' : '會員留言' }}</p><h1 id="page-title">{{ page === 'articles' ? '最新文章' : page === 'article' ? '閱讀文章' : page === 'article-new' ? '發表文章' : page === 'my-articles' ? '我的文章' : page === 'my-comments' ? '我的留言' : page === 'article-edit' ? '編輯文章' : page === 'comment-edit' ? '編輯留言' : '留言詳情' }}</h1></div><a v-if="account && page === 'articles'" class="secondary-button" href="/vue/articles/new">發表文章</a></div>
        <p v-if="published && account" class="success-note" role="status">文章已發表，以下為我的文章。</p>
        <section v-if="summary" ref="errorSummary" class="error-summary" tabindex="-1" aria-labelledby="error-title"><h2 id="error-title">{{ summary }}</h2><ul v-if="errorFields.length"><li v-for="name in errorFields" :key="name"><button type="button" @click="focusField(name)">{{ fieldNames[name] }}：{{ errors[name].join('；') }}</button></li></ul><div v-if="logId" class="trace"><span>追蹤編號：<span class="trace-id">{{ logId }}</span></span><button type="button" @click="copyLogId">複製編號</button><span role="status">{{ copyState }}</span></div></section>
        <p v-if="authChecking" role="status">正在確認登入狀態，請稍候…</p>
        <p v-if="contentLoading" role="status">正在讀取內容，請稍候…</p>
        <p v-if="credential && !account && !authChecking && !reLogin" class="pending-note">目前無法確認登入狀態；憑證已保留。<button type="button" class="inline-button" @click="verifyAccount">重新驗證登入</button></p>
        <p v-if="!credential && !reLogin && !disabledAccount && summaryCode === 'E0002'">請重新登入後手動讀取。<a class="text-link" :href="`/vue/login?return_to=${encodeURIComponent(path)}`">前往登入</a></p>
        <template v-if="reLogin && !disabledAccount">
          <p class="pending-note">{{ page === 'article-new' || page === 'article' || editing ? '原頁內容會暫留在此頁記憶體。請重新登入；成功後由您手動送出。' : '請重新登入後讀取原頁內容。' }}</p>
          <AccountForm relogin
            v-model:email="email" v-model:password="password"
            :pending="pending" :blocked="authChecking || logoutPending" :locked="locked" :errors="errors" :lock-text="lockText"
            @submit="submitLogin" />
        </template>
        <!-- total_pages=0 也可能是第 1 頁空列表；依原頁碼區分合法超頁。 -->
        <template v-if="page === 'articles' || page === 'my-articles'">
          <div v-if="articleList && contentReady" class="article-results"><p class="result-count">共 {{ articleList.pagination.total }} 篇 · 第 {{ articleList.pagination.page }} 頁<span v-if="articleList.pagination.total_pages">／共 {{ articleList.pagination.total_pages }} 頁</span></p><p v-if="!articleList.list.length" role="status">{{ listPage > 1 && listPage > articleList.pagination.total_pages ? '此頁沒有文章，總數仍保留。' : '目前尚無文章。' }}</p><ol v-else class="article-list"><li v-for="item in articleList.list" :key="item.id" class="article-card"><p class="article-meta">{{ item.author.nickname }} · {{ item.created_at }} · 建立於 {{ backendName(item.created_backend) }}<span v-if="item.visibility === 'hidden'"> · 已下架</span></p><h2><a :href="`/vue/articles/${item.id}`">{{ item.title }}</a></h2><p class="article-excerpt">{{ item.body }}</p><a class="read-link" :href="`/vue/articles/${item.id}`">閱讀全文</a></li></ol><nav v-if="articleList.pagination.total_pages > 1 || listPage > 1" class="pagination" aria-label="文章分頁"><a v-if="listPage > 1" :href="`${path}?page=${listPage - 1}`">上一頁</a><span>第 {{ listPage }} 頁</span><a v-if="listPage < articleList.pagination.total_pages" :href="`${path}?page=${listPage + 1}`">下一頁</a><a v-if="listPage > 1 && listPage > articleList.pagination.total_pages" :href="path">回第一頁</a></nav></div>
          <button v-else-if="!contentLoading && !authChecking && !reLogin && !disabledAccount && summaryCode !== 'E0002' && (!protectedPage || account)" type="button" class="secondary-button" @click="loadContent">重新讀取</button>
        </template>
        <template v-else-if="page === 'article'"><article v-if="articleData && contentReady" class="article-detail"><p class="article-meta">{{ articleData.author.nickname }} · {{ articleData.created_at }} · 建立於 {{ backendName(articleData.created_backend) }}<span v-if="articleData.visibility === 'hidden'"> · 已下架</span></p><h2>{{ articleData.title }}</h2><div class="article-body">{{ articleData.body }}</div></article><button v-else-if="!contentLoading && !authChecking && !disabledAccount && summaryCode !== 'E0002' && (!credential || account)" type="button" class="secondary-button" @click="loadContent">重新讀取</button><p class="detail-return-links"><a class="text-link" href="/vue/">返回文章列表</a><a v-if="account?.role === 'admin'" class="text-link" href="/vue/admin/articles">返回文章管理</a></p></template>
        <!-- 我的留言也以原頁碼判斷零筆超頁，不能把第 7 頁說成尚無留言。 -->
        <template v-else-if="page === 'my-comments'">
          <div v-if="commentList && contentReady" class="article-results">
            <p class="result-count">共 {{ commentList.pagination.total }} 則 · 第 {{ commentList.pagination.page }} 頁<span v-if="commentList.pagination.total_pages">／共 {{ commentList.pagination.total_pages }} 頁</span></p>
            <p v-if="!commentList.list.length" role="status">{{ listPage > 1 && listPage > commentList.pagination.total_pages ? '此頁沒有留言，總數仍保留。' : '目前尚無留言。' }}</p>
            <ol v-else class="article-list"><li v-for="item in commentList.list" :key="item.id" class="article-card"><p class="article-meta">{{ item.author.nickname }} · {{ item.created_at }} · 建立於 {{ backendName(item.created_backend) }}<span v-if="item.visibility === 'hidden'"> · 已下架</span></p><p class="article-body comment-body">{{ item.body }}</p><a class="read-link" :href="`/vue/comments/${item.id}`">閱讀留言詳情</a></li></ol>
            <nav v-if="commentList.pagination.total_pages > 1 || listPage > 1" class="pagination" aria-label="我的留言分頁"><a v-if="listPage > 1" :href="`${path}?page=${listPage - 1}`">上一頁</a><span>第 {{ listPage }} 頁</span><a v-if="listPage < commentList.pagination.total_pages" :href="`${path}?page=${listPage + 1}`">下一頁</a><a v-if="listPage > 1 && listPage > commentList.pagination.total_pages" :href="path">回第一頁</a></nav>
          </div>
          <button v-if="!contentReady && !contentLoading && !authChecking && !reLogin && !disabledAccount && account" type="button" class="secondary-button" :disabled="contentPending" @click="loadContent">重新讀取留言</button>
        </template>
        <template v-else-if="commentResource">
          <article v-if="commentData && contentReady && !editing" class="article-detail"><p class="article-meta">{{ commentData.author.nickname }} · {{ commentData.created_at }} · 建立於 {{ backendName(commentData.created_backend) }}<span v-if="commentData.visibility === 'hidden'"> · 已下架</span></p><div class="article-body">{{ commentData.body }}</div></article>
          <button v-if="!contentReady && !contentLoading && !authChecking && !reLogin && !disabledAccount && account" type="button" class="secondary-button" :disabled="contentPending" @click="loadContent">重新讀取留言</button>
          <section v-if="commentData && contentReady" class="related-content" aria-labelledby="parent-title"><h2 id="parent-title">所屬文章</h2><p v-if="relatedLoading" role="status">正在確認所屬文章的讀取權限…</p><template v-if="parentArticle"><p class="article-meta"><span v-if="parentArticle.visibility === 'hidden'">已下架 · </span>建立於 {{ backendName(parentArticle.created_backend) }}</p><h3><a :href="`/vue/articles/${parentArticle.id}`">{{ parentArticle.title }}</a></h3></template><section v-if="relatedError" ref="relatedSummary" class="error-summary" tabindex="-1" aria-labelledby="related-error-title"><h3 id="related-error-title">{{ relatedError }}</h3><div v-if="relatedLogId" class="trace"><span>追蹤編號：<span class="trace-id">{{ relatedLogId }}</span></span><button type="button" @click="copyRelatedLogId">複製編號</button><span role="status">{{ copyState }}</span></div></section><button v-if="relatedError && !relatedLoading" class="secondary-button" type="button" :disabled="contentPending" @click="loadRelated">重新讀取所屬文章</button></section>
          <p class="detail-return-links"><a class="text-link" href="/vue/me/comments">返回我的留言</a><a v-if="account?.role === 'admin'" class="text-link" href="/vue/admin/comments">返回留言管理</a></p>
        </template>
        <template v-else-if="page === 'article-new'"><p class="intro">標題與內文會以純文字公開顯示。發表後可在我的文章查看。</p><form v-if="account && credential && !authChecking && !reLogin && !disabledAccount" novalidate @submit.prevent="submitArticle"><div class="field"><label for="title">標題</label><input id="title" v-model="articleTitle" type="text" :disabled="contentPending" :aria-invalid="Boolean(errors.title)" :aria-describedby="errors.title ? 'title-error title-help' : 'title-help'" /><p id="title-help" class="field-help">1–200 個字，不可全為空白。</p><p v-if="errors.title" id="title-error" class="field-error">{{ errors.title.join('；') }}</p></div><div class="field"><label for="body">內文</label><textarea id="body" v-model="articleBody" :disabled="contentPending" :aria-invalid="Boolean(errors.body)" :aria-describedby="errors.body ? 'body-error body-help' : 'body-help'"></textarea><p id="body-help" class="field-help">1–20,000 個字；原樣保留換行。</p><p v-if="errors.body" id="body-error" class="field-error">{{ errors.body.join('；') }}</p></div><button class="primary-button" type="submit" :disabled="contentPending">{{ contentPending ? '發表處理中…' : '發表文章' }}</button><p v-if="contentPending" class="pending-note" role="status">正在發表，請稍候；不要重複送出。</p></form><p v-if="writeUnknown" class="pending-note">發文結果尚未確認。<a class="text-link" href="/vue/me/articles">先到我的文章查證</a></p></template>
        <template v-if="editing">
          <form v-if="authorizedResource && currentResource.author.id === account.id && editLoaded" class="content-editor" novalidate @submit.prevent="submitEdit">
            <p class="article-meta">{{ currentResource.author.nickname }} · {{ currentResource.created_at }} · 建立於 {{ backendName(currentResource.created_backend) }}<span v-if="currentResource.visibility === 'hidden'"> · 已下架</span></p>
            <p class="intro">修改不會改變下架狀態或恢復權限。原文與換行會直接交給伺服器。</p>
            <p v-if="commentResource && !canEdit" class="pending-note" role="status">所屬文章尚未確認公開，目前不能編修內文；您的草稿仍留在此頁。</p>
            <div v-if="articleResource" class="field"><label for="title">標題</label><input id="title" v-model="articleTitle" type="text" :disabled="!canEdit || contentPending || contentLoading" :aria-invalid="Boolean(errors.title)" :aria-describedby="errors.title ? 'title-error' : undefined" /><p v-if="errors.title" id="title-error" class="field-error">{{ errors.title.join('；') }}</p></div>
            <div class="field"><label for="body">內文</label><textarea id="body" :value="articleResource ? articleBody : commentBody" @input="editBodyInput" :disabled="!canEdit || contentPending || contentLoading" :aria-invalid="Boolean(errors.body)" :aria-describedby="errors.body ? 'body-error body-help' : 'body-help'"></textarea><p id="body-help" class="field-help">{{ articleResource ? '1–20,000' : '1–2,000' }} 個字，不可全為空白。以純文字保留，不解析 HTML。</p><p v-if="errors.body" id="body-error" class="field-error">{{ errors.body.join('；') }}</p></div>
            <p class="muted">目前草稿使用版本 {{ editVersion }}</p>
            <button class="primary-button" type="submit" :disabled="!canEdit || contentPending || contentLoading || conflict">{{ contentPending ? '儲存處理中…' : '儲存修改' }}</button>
            <p v-if="contentPending" class="pending-note" role="status">正在送出，請稍候；不要重複提交。</p>
          </form>
          <p v-else-if="authorizedResource" class="pending-note">您不是這筆內容的作者，不能修改內文。</p>
          <div class="content-actions"><button v-if="account && !reLogin && !disabledAccount" class="secondary-button" type="button" :disabled="contentPending || contentLoading || authChecking" @click="loadContent">重新讀取最新內容</button><a class="text-link" :href="`/vue/${articleResource ? 'articles' : 'comments'}/${requestedId}`">返回詳情</a></div>
          <section v-if="comparison && authorizedResource && currentResource.author.id === account.id" class="content-comparison" aria-labelledby="comparison-title">
            <h2 id="comparison-title">最新內容（版本 {{ comparison.version }}）</h2><p>上方保留您的輸入。請先比較及整合，或決定捨棄自己的文字，再確認版本；確認不會自動儲存。</p><h3 v-if="articleResource">{{ comparison.title }}</h3><div class="article-body">{{ comparison.body }}</div><div class="content-actions"><button type="button" class="secondary-button" :disabled="!canEdit || contentPending || contentLoading" @click="acceptComparison(false)">已整合輸入，採用最新版本</button><button type="button" class="secondary-button" :disabled="!canEdit || contentPending || contentLoading" @click="acceptComparison(true)">捨棄輸入，採用最新內容</button></div>
          </section>
        </template>
        <div v-if="!editing && canManageVisibility" class="content-actions">
          <a v-if="canEdit" class="secondary-button" :href="`${path}/edit`">{{ articleResource ? '編輯文章' : '編輯留言' }}</a>
          <button v-if="canHide" type="button" class="secondary-button" :disabled="contentPending || contentLoading || conflict && !comparison" @click="submitVisibility('hidden')">{{ currentResource.visibility === 'hidden' ? '再次下架並接管恢復權' : '下架' }}</button>
          <button v-if="currentResource.visibility === 'hidden' && currentResource.can_restore" type="button" class="secondary-button" :disabled="contentPending || contentLoading || conflict && !comparison" @click="submitVisibility('visible')">恢復</button>
          <p v-if="currentResource.visibility === 'hidden' && !currentResource.can_restore" class="muted">目前無恢復資格。</p>
          <p v-if="contentPending" role="status">正在更新狀態，請稍候…</p>
        </div>
        <p v-if="conflict && (editing || articleResource || commentResource)" class="pending-note" role="status">資料已被其他操作更新。{{ editing ? '請重讀最新內容並人工比較、確認後再手動送出。' : '請先重讀最新內容，再重新確認下架或恢復操作。' }}<button v-if="!editing && account && !reLogin && !disabledAccount" type="button" class="inline-button" :disabled="contentPending || contentLoading || authChecking" @click="loadContent">重新讀取最新內容</button></p>
        <p v-if="updateNotice" class="success-note" role="status">{{ updateNotice }}</p>
        <p v-if="lifecycleUnknown" class="pending-note">更新結果尚未確認。請先依原 ID 查證；不會自動重送。<button v-if="account && !reLogin && !disabledAccount" class="inline-button" type="button" :disabled="contentPending || contentLoading || authChecking" @click="loadContent">重新讀取同一筆內容查證</button><a class="text-link" :href="`/vue/${articleResource ? 'articles' : 'comments'}/${requestedId}`">依原 ID 開啟詳情查證</a></p>
        <section v-if="page === 'article' && articleData && contentReady" id="comments" class="related-content" aria-labelledby="comments-title">
          <h2 id="comments-title">留言</h2><p v-if="relatedLoading" role="status">正在讀取留言，請稍候…</p>
          <section v-if="relatedError" ref="relatedSummary" class="error-summary" tabindex="-1" aria-labelledby="related-error-title"><h3 id="related-error-title">{{ relatedError }}</h3><div v-if="relatedLogId" class="trace"><span>追蹤編號：<span class="trace-id">{{ relatedLogId }}</span></span><button type="button" @click="copyRelatedLogId">複製編號</button><span role="status">{{ copyState }}</span></div></section>
          <button v-if="relatedError && !relatedLoading" type="button" class="secondary-button" :disabled="contentPending" @click="loadRelated">重新讀取留言</button>
          <!-- 文章留言的頁碼不因零筆回應重設，返回入口仍指向同一文章的留言區。 -->
          <template v-if="commentList"><p class="result-count">共 {{ commentList.pagination.total }} 則 · 第 {{ commentList.pagination.page }} 頁<span v-if="commentList.pagination.total_pages">／共 {{ commentList.pagination.total_pages }} 頁</span></p><p v-if="!commentList.list.length" role="status">{{ commentPage > 1 && commentPage > commentList.pagination.total_pages ? '此頁沒有留言，總數仍保留。' : '目前尚無留言。' }}</p><ol v-else class="article-list"><li v-for="item in commentList.list" :key="item.id" class="article-card"><p class="article-meta">{{ item.author.nickname }} · {{ item.created_at }} · 建立於 {{ backendName(item.created_backend) }}<span v-if="item.visibility === 'hidden'"> · 已下架</span></p><p class="article-body comment-body">{{ item.body }}</p><a v-if="account && (account.id === item.author.id || account.role === 'admin')" class="read-link" :href="`/vue/comments/${item.id}`">閱讀留言詳情</a></li></ol><nav v-if="commentList.pagination.total_pages > 1 || commentPage > 1" class="pagination" aria-label="文章留言分頁"><a v-if="commentPage > 1" :href="`${path}?page=${commentPage - 1}#comments`">上一頁</a><span>第 {{ commentPage }} 頁</span><a v-if="commentPage < commentList.pagination.total_pages" :href="`${path}?page=${commentPage + 1}#comments`">下一頁</a><a v-if="commentPage > 1 && commentPage > commentList.pagination.total_pages" :href="`${path}#comments`">回第一頁</a></nav></template>
        </section>
        <section v-if="page === 'article' && (articleData && contentReady || reLogin && commentBody)" class="comment-form" aria-labelledby="comment-form-title">
          <h2 id="comment-form-title">發表留言</h2>
          <p v-if="commentPosted" class="success-note" role="status">留言已發表。</p>
          <form v-if="canComment || reLogin && commentBody" novalidate @submit.prevent="submitComment"><div class="field"><label for="body">留言內容</label><textarea id="body" v-model="commentBody" :disabled="contentPending || !canComment" :aria-invalid="Boolean(errors.body)" :aria-describedby="errors.body ? 'body-error comment-help' : 'comment-help'"></textarea><p id="comment-help" class="field-help">1–2,000 個字，不可全為空白；原樣保留換行。</p><p v-if="errors.body" id="body-error" class="field-error" role="alert">{{ errors.body.join('；') }}</p></div><button class="primary-button" type="submit" :disabled="contentPending || !canComment">{{ contentPending ? '留言處理中…' : '發表留言' }}</button><p v-if="contentPending" class="pending-note" role="status">正在送出留言，請稍候；不要重複提交。</p></form>
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
            <h1 id="page-title">{{ page === 'members' ? '會員管理' : page === 'member' ? '會員詳情' : page === 'managed-articles' ? '文章管理' : page === 'managed-comments' ? '留言管理' : '登入鎖定設定' }}</h1>
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
          class="pending-note">目前無法確認登入狀態；憑證已保留。<button class="inline-button" type="button" @click="verifyAccount">重新驗證登入</button>
        </p>
        <p v-if="!credential && !reLogin && !disabledAccount && summaryCode === 'E0002'">請重新登入後手動讀取。<a class="text-link"
          :href="`/vue/login?return_to=${encodeURIComponent(path)}`">前往登入</a>
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
            <p v-if="lockoutUnknown" class="pending-note" role="status">先前 PUT 結果尚未確認。{{ lockoutNeedsReadback ? '請先手動重新讀取目前伺服器設定；不會自動重送。' : '目前值已重讀，但可能已有其他管理員更新，無法藉此判定先前 PUT 是否成功。' }}</p>
            <p v-if="lockoutPending" class="pending-note" role="status">正在儲存完整設定，請勿重複送出。</p>
            <button v-if="!contentLoading && !lockoutPending && !disabledAccount && summaryCode !== 'E0002'" class="secondary-button" type="button" @click="loadContent">重新讀取目前設定{{ lockoutNeedsReadback ? '並查證' : '' }}</button>
            <p v-if="lockoutData && contentReady" class="field-help">目前伺服器值：{{ lockoutData.enabled ? '啟用' : '關閉' }}；{{ lockoutData.window_minutes }} 分鐘內失敗 {{ lockoutData.failure_threshold }} 次，鎖定 {{ lockoutData.lock_seconds }} 秒。</p>
            <form v-if="lockoutDraft && lockoutData && contentReady" class="lockout-form" novalidate @submit.prevent="submitLockoutSettings">
              <div class="field">
                <label for="enabled">鎖定功能</label>
                <select id="enabled" v-model="lockoutDraft.enabled" :disabled="lockoutPending" :aria-invalid="Boolean(errors.enabled?.length)" :aria-describedby="errors.enabled?.length ? 'enabled-error enabled-help' : 'enabled-help'"><option :value="true">啟用</option><option :value="false">關閉</option></select>
                <p id="enabled-help" class="field-help">關閉後仍保存並驗證以下三個數值；重新啟用時沿用。</p>
                <p v-if="errors.enabled?.length" id="enabled-error" class="field-error">{{ errors.enabled.join('；') }}</p>
              </div>
              <div class="field">
                <label for="window_minutes">累計期間（分鐘）</label>
                <input id="window_minutes" v-model.number="lockoutDraft.window_minutes" type="number" min="1" max="60" step="1" inputmode="numeric" :disabled="lockoutPending" :aria-invalid="Boolean(errors.window_minutes?.length)" :aria-describedby="errors.window_minutes?.length ? 'window_minutes-error window_minutes-help' : 'window_minutes-help'" />
                <p id="window_minutes-help" class="field-help">可設定 1–60 分鐘；範圍調大時，仍在新範圍內的既有失敗事件可能計入。</p>
                <p v-if="errors.window_minutes?.length" id="window_minutes-error" class="field-error">{{ errors.window_minutes.join('；') }}</p>
              </div>
              <div class="field">
                <label for="failure_threshold">失敗門檻（次）</label>
                <input id="failure_threshold" v-model.number="lockoutDraft.failure_threshold" type="number" min="1" max="100" step="1" inputmode="numeric" :disabled="lockoutPending" :aria-invalid="Boolean(errors.failure_threshold?.length)" :aria-describedby="errors.failure_threshold?.length ? 'failure_threshold-error failure_threshold-help' : 'failure_threshold-help'" />
                <p id="failure_threshold-help" class="field-help">可設定 1–100 次；達到門檻的當次登入即開始鎖定。</p>
                <p v-if="errors.failure_threshold?.length" id="failure_threshold-error" class="field-error">{{ errors.failure_threshold.join('；') }}</p>
              </div>
              <div class="field">
                <label for="lock_seconds">鎖定時間（秒）</label>
                <input id="lock_seconds" v-model.number="lockoutDraft.lock_seconds" type="number" min="1" max="86400" step="1" inputmode="numeric" :disabled="lockoutPending" :aria-invalid="Boolean(errors.lock_seconds?.length)" :aria-describedby="errors.lock_seconds?.length ? 'lock_seconds-error lock_seconds-help' : 'lock_seconds-help'" />
                <p id="lock_seconds-help" class="field-help">可設定 1–86,400 秒；調整不延長已存在鎖定的到期時間。</p>
                <p v-if="errors.lock_seconds?.length" id="lock_seconds-error" class="field-error">{{ errors.lock_seconds.join('；') }}</p>
              </div>
              <div class="lockout-preview" aria-live="polite"><strong>設定預覽</strong><p>{{ lockoutDraft.enabled ? `啟用：${lockoutDraft.window_minutes} 分鐘內失敗 ${lockoutDraft.failure_threshold} 次，鎖定 ${lockoutDraft.lock_seconds} 秒。` : '關閉：會員登入失敗不會觸發暫時鎖定。' }}</p></div>
              <button class="primary-button" type="submit" :disabled="lockoutPending || lockoutNeedsReadback || contentLoading || logoutPending">{{ lockoutPending ? '儲存中…' : '儲存完整設定' }}</button>
            </form>
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
              @click="submitMemberStatus">{{ statusPending ? '更新處理中…' : memberData.status === 'active' ? '停用會員' : '恢復會員' }}</button>
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
                  <span>{{ item.author.nickname }}</span>
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
                  <span>{{ item.author.nickname }}</span>
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
            class="secondary-button" type="button" @click="loadContent">重新讀取{{ page === 'member' ? '同一會員' : '管理資料' }}</button>
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
