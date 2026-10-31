import { API_BASE, apiFailure, apiRequest, clearSession, emptySuccess, isAccount, readBackend, readSession, saveSession, selectBackend } from '../utils/contracts.js'

// 共用登入狀態以 null 起始；SSR 不預填瀏覽器憑證。
function initialNull() { return null }

// 共用狀態欄位以 false 起始；ready 在驗證前保持未完成。
function initialFalse() { return false }

// 連續請求編號避免較慢的 /me 回應覆蓋後來登入或登出結果。
function initialZero() { return 0 }

// 尚未讀取同分頁選擇前只提供首次預設，掛載後才從瀏覽器儲存更新。
function initialBackend() { return API_BASE }

// 共用導覽列與頁面的登入狀態；sessionStorage 僅在瀏覽器讀取。
export function useSite() {
  // 同名 useState 由外殼與各頁共享；帳號、憑證與頁面摘要不另建第二份真相。
  const credential = useState('site-credential', initialNull)
  const account = useState('site-account', initialNull)
  const ready = useState('site-ready', initialFalse)
  const checking = useState('site-checking', initialFalse)
  const authError = useState('site-auth-error', initialNull)
  const sequence = useState('site-sequence', initialZero)
  const backend = useState('site-backend', initialBackend)
  const dirty = useState('site-dirty', initialFalse)
  const adminPending = useState('site-admin-pending', initialFalse)
  const leaving = useState('site-leaving', initialFalse)
  // 控制器屬於這次 useSite 呼叫；跨呼叫的舊回應另由共用 sequence 作廢。
  let verifyController = null
  let logoutController = null
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
    // 避免同時重驗或離頁後啟動請求；沒有憑證時直接完成訪客初始化。
    if (checking.value || leaving.value) return
    const saved = credential.value
    if (!saved) { ready.value = true; return }
    // 保存原憑證與遞增序號，登入、登出或換來源後即可拒收這次舊結果。
    const current = ++sequence.value
    const request = new AbortController()
    verifyController = request
    checking.value = true
    authError.value = null
    try {
      const response = await apiRequest('/api/me', { token: saved.accessToken, signal: request.signal })
      // abort 只是盡力取消；採用回應仍須核對共用序號與憑證物件。
      if (current !== sequence.value || credential.value !== saved) return
      // 完整的成功身分才恢復授權；暫時錯誤不等於憑證已失效。
      if (response.status === 200 && response.body?.success === true && response.body.message === 'OK' && isAccount(response.body.data)) {
        account.value = response.body.data
      } else {
        // 只有明確失效或停用才撤銷登入，其餘錯誤保留憑證供手動重驗。
        const failure = apiFailure(response.status, response.body)
        if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') forget(failure.message)
        else authError.value = failure.message
      }
      // 連線失敗不自動登入或重送，也不讓舊請求蓋掉新狀態的錯誤。
    } catch {
      if (current === sequence.value) authError.value = '無法驗證登入狀態；請手動重試。'
    } finally {
      // 控制器／checking 只由仍持有者收尾；ready 依現行流程在 finally 一律結束初始化。
      if (verifyController === request) verifyController = null
      if (current === sequence.value) checking.value = false
      ready.value = true
    }
  }

  // 初次掛載才讀取同源共用憑證；跨前端切換仍須向目前後端重驗。
  async function initialize() {
    if (ready.value || checking.value) return
    // 連取得 sessionStorage 屬性都可能遭拒；改以訪客載入公開內容並顯示原因。
    try { backend.value = readBackend(sessionStorage); credential.value = readSession(sessionStorage) } catch {
      credential.value = null
      account.value = null
      authError.value = '無法讀取登入狀態；目前以訪客瀏覽公開內容。'
      ready.value = true
      return
    }
    // 共用儲存只提供待驗證憑證，必須向目前後端確認後才能顯示身分。
    if (credential.value) await verify()
    else ready.value = true
  }

  // 登入成功後只存原 token 與到期時間；帳號資料維持在記憶體。
  function acceptLogin(data) {
    if (leaving.value) return false
    // 先作廢舊驗證，再保存這次原始期限；儲存例外交由登入表單顯示結果未確認。
    sequence.value++
    saveSession(sessionStorage, data.access_token, data.expires_at)
    credential.value = readSession(sessionStorage)
    // 儲存完成後才更新記憶體身分，不額外延長登入或複製帳號到 Storage。
    account.value = data.user
    authError.value = null
    checking.value = false
    ready.value = true
    return true
  }

  // 跨前端離頁只作廢本頁請求和授權回應，不清除共用 JWT 或改寫原到期時間。
  function leaveFrontend() {
    if (leaving.value) return
    leaving.value = true
    sequence.value++
    verifyController?.abort()
    logoutController?.abort()
    dirty.value = false
  }

  // 儲存先撤銷舊 JWT 才寫新選擇，成功後作廢本頁身分與舊 /me 回應。
  function switchBackend(next) {
    // 儲存新選擇成功才切換畫面身分；儲存拒絕時回傳 false 讓選單回復。
    try { if (!selectBackend(sessionStorage, next)) return false } catch { return false }
    // 舊後端的授權與在途回應同時失效，下一頁必須重新登入。
    sequence.value++
    verifyController?.abort()
    logoutController?.abort()
    backend.value = next
    credential.value = null
    account.value = null
    checking.value = false
    authError.value = null
    dirty.value = false
    return true
  }

  // 僅在伺服器確認登出後清除憑證，失敗保留登入並提供手動重試。
  async function logout() {
    if (!token.value || leaving.value) return { ok: false, message: '尚未完成登入驗證。' }
    // 固定送出的 token 與序號，避免舊登出回應清掉後來的新登入。
    const sentToken = token.value
    const current = sequence.value
    const request = new AbortController()
    logoutController = request
    try {
      const response = await apiRequest('/api/auth/logout', { method: 'POST', token: sentToken, signal: request.signal })
      // 伺服器可能已完成撤銷，但來源已變時不能替新畫面宣告登出結果。
      if (leaving.value || current !== sequence.value || token.value !== sentToken) return { ok: false, message: '登入狀態已改變，請重新確認。' }
      // 完整成功才清除登入；已知驗證拒絕也撤銷本頁對舊身分的信任。
      if (emptySuccess(response.status, response.body, 200)) {
        forget(null)
        return { ok: true }
      }
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') forget(failure.message)
      return { ok: false, message: `登出未完成：${failure.message}`, logId: failure.logId }
      // 連線中斷無法證明撤銷結果，保留手動查證／重試提示而不自動重送。
    } catch {
      return { ok: false, message: '登出結果尚未確認；請先確認登入狀態，再手動重試。' }
    } finally {
      // 較晚的舊請求不能清除新請求的控制器。
      if (logoutController === request) logoutController = null
    }
  }

  return { account, adminPending, authError, backend, checking, credential, dirty, leaving, ready, token, initialize, verify, forget, acceptLogin, leaveFrontend, logout, switchBackend }
}
