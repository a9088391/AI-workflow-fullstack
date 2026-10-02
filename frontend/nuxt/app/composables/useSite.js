import { apiFailure, apiRequest, clearSession, emptySuccess, isAccount, readSession, saveSession } from '../utils/contracts.js'

// 每個 Nuxt useState 欄位使用相同空值；SSR 外殼不持有任何使用者憑證。
function initialNull() { return null }

// 每次頁面實例起始為尚未驗證，避免先顯示受保護身分。
function initialFalse() { return false }

// 連續請求編號避免較慢的 /me 回應覆蓋後來登入或登出結果。
function initialZero() { return 0 }

// 共用導覽列與頁面的登入狀態；sessionStorage 僅在瀏覽器讀取。
export function useSite() {
  const credential = useState('site-credential', initialNull)
  const account = useState('site-account', initialNull)
  const ready = useState('site-ready', initialFalse)
  const checking = useState('site-checking', initialFalse)
  const authError = useState('site-auth-error', initialNull)
  const sequence = useState('site-sequence', initialZero)
  const token = computed(
    // 只有 /me 已確認的身分可讓頁面帶出現有 JWT。
    () => account.value && credential.value ? credential.value.accessToken : null,
  )

  // 失效或停用時撤銷本頁對舊身分的信任，其他失敗不誤清除憑證。
  function forget(message) {
    sequence.value++
    account.value = null
    credential.value = null
    checking.value = false
    authError.value = message
    try { clearSession(sessionStorage) } catch { /* 儲存不可用時仍撤銷本頁權限。 */ }
  }

  // 使用原 token 呼叫 /me；暫時連線失敗保留憑證供手動重驗。
  async function verify() {
    if (checking.value) return
    const saved = credential.value
    if (!saved) { ready.value = true; return }
    const current = ++sequence.value
    checking.value = true
    authError.value = null
    try {
      const response = await apiRequest('/api/me', { token: saved.accessToken })
      if (current !== sequence.value || credential.value !== saved) return
      if (response.status === 200 && response.body?.success === true && response.body.message === 'OK' && isAccount(response.body.data)) {
        account.value = response.body.data
      } else {
        const failure = apiFailure(response.status, response.body)
        if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') forget(failure.message)
        else authError.value = failure.message
      }
    } catch {
      if (current === sequence.value) authError.value = '無法驗證登入狀態；請手動重試。'
    } finally {
      if (current === sequence.value) checking.value = false
      ready.value = true
    }
  }

  // 初次掛載才讀取同源共用憑證；跨前端切換仍須向目前後端重驗。
  async function initialize() {
    if (ready.value || checking.value) return
    // 連取得 sessionStorage 屬性都可能遭拒；改以訪客載入公開內容並顯示原因。
    try { credential.value = readSession(sessionStorage) } catch {
      credential.value = null
      account.value = null
      authError.value = '無法讀取登入狀態；目前以訪客瀏覽公開內容。'
      ready.value = true
      return
    }
    if (credential.value) await verify()
    else ready.value = true
  }

  // 登入成功後只存原 token 與到期時間；帳號資料維持在記憶體。
  function acceptLogin(data) {
    sequence.value++
    saveSession(sessionStorage, data.access_token, data.expires_at)
    credential.value = readSession(sessionStorage)
    account.value = data.user
    authError.value = null
    checking.value = false
    ready.value = true
  }

  // 僅在伺服器確認登出後清除憑證，失敗保留登入並提供手動重試。
  async function logout() {
    if (!token.value) return { ok: false, message: '尚未完成登入驗證。' }
    const sentToken = token.value
    try {
      const response = await apiRequest('/api/auth/logout', { method: 'POST', token: sentToken })
      if (token.value !== sentToken) return { ok: false, message: '登入狀態已改變，請重新確認。' }
      if (emptySuccess(response.status, response.body, 200)) {
        forget(null)
        return { ok: true }
      }
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') forget(failure.message)
      return { ok: false, message: `登出未完成：${failure.message}`, logId: failure.logId }
    } catch {
      return { ok: false, message: '登出結果尚未確認；請先確認登入狀態，再手動重試。' }
    }
  }

  return { account, authError, checking, credential, ready, token, initialize, verify, forget, acceptLogin, logout }
}
