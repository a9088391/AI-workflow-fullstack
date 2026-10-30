import { computed, ref, watch } from 'vue'
import {
  API_BASE, clearSession, formatCountdown, interpretLogin, interpretLogout,
  interpretMe, readBackend, readSession, remainingSeconds, saveSession, validateLogin,
} from './auth.js'
import { interpretRegistrationResponse, validateRegistration } from './registration.js'

// 持有本頁唯一 session、帳號表單與認證生命週期；內容及草稿的恢復交回各頁既有責任。
export function useAccountSession({
  page, protectedPage, returnTo, hasDraft, submitted, feedback, showError,
  isContentBusy, onBeforeVerify, onVerified, onVerifiedReady, onAccountChanged, onLogin, onForget,
}) {
  // 摘要與內容頁共用呈現；帳密、憑證及鎖定只由本次 session 建立。
  const { summary, summaryCode, errors, logId, copyState } = feedback
  const email = ref('')
  const password = ref('')
  const nickname = ref('')
  const pending = ref(false)
  const credential = ref(null)
  const backend = ref(API_BASE)
  const account = ref(null)
  const authChecking = ref(false)
  const logoutPending = ref(false)
  const reLogin = ref(false)
  const disabledAccount = ref(false)
  const lockDeadline = ref(0)
  const lockNow = ref(0)
  const lockNotice = ref('')
  // 單一認證通道同時涵蓋註冊、登入、驗證與登出，離頁可一次撤銷。
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
    if (page !== 'login' && !reLogin.value) return
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

  // 實際 POST 到目前選擇的 API；只有確認 201 成功 envelope 才宣告註冊完成。
  async function submitRegistration() {
    // 註冊與驗權／登出共用通道；先阻擋交錯提交，避免取消另一操作後留下等待旗標。
    if (pending.value || authChecking.value || logoutPending.value) return
    const checked = validateRegistration({ email: email.value, password: password.value, nickname: nickname.value })
    if (Object.keys(checked.errors).length) {
      await showError('欄位資料不正確，請確認後再送出', checked.errors)
      return
    }

    // 新嘗試只清除上次回饋；輸入保留直到收到完整成功回應。
    pending.value = true
    summary.value = ''
    summaryCode.value = ''
    errors.value = {}
    logId.value = ''
    const request = startAuthRequest()
    try {
      // 只送 API 契約欄位；後端仍是唯一性及實際建立結果的權威。
      const response = await fetch(`${backend.value}/api/auth/register`, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: checked.email, password: password.value, nickname: nickname.value }),
        signal: request.controller.signal,
      })
      const body = await response.json()
      if (!currentAuthRequest(request)) return
      const result = interpretRegistrationResponse(response.status, body)
      // 只有完整成功回應才能轉頁；失敗時保留欄位與 log_id 供修正或查證。
      if (result.ok) {
        submitted.value = true
        password.value = ''
        window.location.assign('/vue/login?registered=1')
        return
      }
      await showError(result.message, result.errors, result.logId)
    } catch {
      if (currentAuthRequest(request)) await showError('連線中斷或伺服器回應不完整，無法確認註冊結果；請先確認帳號狀態，再決定是否重試。')
    } finally {
      if (currentAuthRequest(request)) {
        pending.value = false
        finishAuthRequest(request)
      }
    }
  }

  // 失效時先通知內容頁撤下資料並記錄草稿歸屬，再撤銷記憶體與儲存憑證。
  function forgetCredential() {
    onForget()
    account.value = null
    credential.value = null
    try { clearSession(sessionStorage) } catch { /* 儲存不可用時仍立即撤銷畫面授權。 */ }
  }

  // 每次掛載只讀取同分頁的後端與原始憑證；呼叫端仍須執行 /api/me 驗權。
  function restoreSession() {
    alive = true
    try {
      backend.value = readBackend(window.sessionStorage)
      credential.value = readSession(window.sessionStorage)
    } catch { credential.value = null }
  }

  // 離頁後使所有回應過期並停止倒數；abort 不代表後端尚未完成寫入。
  function disposeSession() {
    alive = false
    stopAuthRequest()
    if (lockTimer) clearInterval(lockTimer)
    lockTimer = null
  }

  // 重新整理只能從保存的 token 嘗試驗證，GET /api/me 成功前不顯示帳號權限。
  async function verifyAccount() {
    // 認證共用取消機制；重驗不得中斷登入或登出，否則舊操作的等待狀態無法正常結束。
    if (!credential.value || authChecking.value || pending.value || logoutPending.value || isContentBusy()) return
    const previousAccount = account.value
    onBeforeVerify(previousAccount)
    // 驗權完成前撤下帳號；暫時連線失敗仍保留原憑證供手動重驗。
    authChecking.value = true
    account.value = null
    summary.value = ''
    const request = startAuthRequest()
    const token = credential.value.accessToken
    try {
      const response = await fetch(`${backend.value}/api/me`, { headers: { Accept: 'application/json', Authorization: `Bearer ${token}` }, signal: request.controller.signal })
      const body = await response.json()
      if (!currentAuthRequest(request) || credential.value?.accessToken !== token) return
      const result = interpretMe(response.status, body)
      if (result.ok) {
        await onVerified(result.account)
        if (!currentAuthRequest(request) || credential.value?.accessToken !== token) return
        account.value = result.account
        // 已登入時只使用通過本機路徑檢查的返回目標。
        if (page === 'login') {
          submitted.value = true
          window.location.assign(returnTo)
        }
      } else {
        if (response.status === 401 && result.code === 'E0002') {
          forgetCredential()
          reLogin.value = protectedPage || page === 'article' && hasDraft.value
        } else if (response.status === 403 && result.code === 'E0102') {
          forgetCredential()
          disabledAccount.value = true
        }
        await showError(result.message, {}, result.logId, result.code)
      }
    } catch {
      if (currentAuthRequest(request)) await showError('目前無法確認登入狀態，憑證已保留；請手動重新驗證。')
    } finally {
      if (currentAuthRequest(request)) {
        authChecking.value = false
        finishAuthRequest(request)
        // 登入重驗後按目前角色恢復資料；管理頁不可沿用先前的管理員資格。
        if (account.value && page !== 'login') onVerifiedReady()
      }
    }
  }

  // 只有格式合法、伺服器完整成功且憑證已保存，才進入已登入入口。
  async function submitLogin() {
    // 登入同樣等待其他認證完成，不能取代尚未確認撤銷的登出。
    if (pending.value || locked.value || authChecking.value || logoutPending.value || !((page === 'login') || ((protectedPage || page === 'article') && reLogin.value))) return
    const checked = validateLogin({ email: email.value, password: password.value })
    if (Object.keys(checked.errors).length) {
      await showError('欄位資料不正確，請確認後再送出', checked.errors)
      return
    }
    // 新嘗試只清除上次回饋；輸入保留直到收到完整成功回應。
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
      const response = await fetch(`${backend.value}/api/auth/login`, {
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
        // 儲存成功後才讓畫面取得權限，原到期時間不延長。
        credential.value = { backend: backend.value, accessToken: result.accessToken, expiresAt: result.expiresAt }
        account.value = result.account
        onAccountChanged(result.account.id)
        clearLock()
        // 登入只保存後端給的到期時間，並導向已驗證的本機返回目標。
        if (page === 'login') {
          submitted.value = true
          window.location.assign(returnTo)
        }
        else {
          reLogin.value = false
          await onLogin(result.account)
        }
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
    // 註冊、登入及驗權完成前不能開始登出，原請求才有機會走完自己的清理。
    if (pending.value || authChecking.value || logoutPending.value || isContentBusy() || !account.value || !credential.value) return
    // 先讓使用者決定是否捨棄純記憶體草稿，取消時不能先撤銷伺服器憑證。
    if (hasDraft.value && !window.confirm('尚有未送出的內容，確定登出並捨棄嗎？')) return
    logoutPending.value = true
    summary.value = ''
    const request = startAuthRequest()
    const token = credential.value.accessToken
    try {
      const response = await fetch(`${backend.value}/api/auth/logout`, { method: 'POST', headers: { Accept: 'application/json', Authorization: `Bearer ${token}` }, signal: request.controller.signal })
      const body = await response.json()
      if (!currentAuthRequest(request) || credential.value?.accessToken !== token) return
      const result = interpretLogout(response.status, body)
      if (result.ok) {
        forgetCredential()
        submitted.value = true
        window.location.assign('/vue/login')
        return
      }
      const invalidCredential = (response.status === 401 && result.code === 'E0002') || (response.status === 403 && result.code === 'E0102')
      if (invalidCredential) {
        forgetCredential()
        reLogin.value = response.status === 401 && (protectedPage || page === 'article' && hasDraft.value)
        disabledAccount.value = response.status === 403
      }
      await showError(invalidCredential ? result.message : `登出未完成。${result.message}`, {}, result.logId, result.code)
    } catch {
      if (currentAuthRequest(request)) await showError('登出未完成：連線中斷或伺服器回應不完整；憑證已保留，請手動重試。')
    } finally {
      if (currentAuthRequest(request)) {
        logoutPending.value = false
        finishAuthRequest(request)
      }
    }
  }

  return {
    email, password, nickname, pending, credential, backend, account, authChecking,
    logoutPending, reLogin, disabledAccount, locked, lockText,
    submitRegistration, submitLogin, submitLogout, verifyAccount, forgetCredential,
    restoreSession, disposeSession,
  }
}
