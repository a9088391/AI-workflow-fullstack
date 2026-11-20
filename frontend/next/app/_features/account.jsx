import { useEffect, useRef, useState } from 'react'
import { apiFailure, apiRequest, emptySuccess, formatCountdown, loginData, readBackend, remainingSeconds, retryAfterSeconds, saveSession, validateAccount } from '../../lib/contracts.js'
import { ErrorNotice } from '../_shared/ui.jsx'

// 註冊與登入使用相同單欄表單；鎖定與伺服器錯誤只套在當次登入。
export function AccountForm({ mode, backend, returnTo, registered, formNavigation, onLogin }) {
  const register = mode === 'register'
  const [fields, setFields] = useState({ email: '', password: '', nickname: '' })
  const [error, setError] = useState(null)
  const [pending, setPending] = useState(false)
  const [deadline, setDeadline] = useState(null)
  const [now, setNow] = useState(Date.now())
  const [unlocked, setUnlocked] = useState(false)
  const [created, setCreated] = useState(false)
  const requestRef = useRef(null)
  const attemptRef = useRef(0)
  const errorRef = useRef(null)
  const remaining = deadline === null ? 0 : remainingSeconds(deadline, now)
  const locked = remaining > 0
  // 真實時鐘決定倒數，歸零後只恢復輸入而不自動送出。
  useEffect(() => {
    if (deadline === null) return undefined
    // 計時器節流後仍按實際時間計算，結束時只更新目前表單。
    function tick() {
      const current = Date.now()
      setNow(current)
      if (current >= deadline) { setDeadline(null); setUnlocked(true) }
    }
    const timer = setInterval(tick, 250)
    // 表單卸載或期限改變時清掉舊計時器，避免重複倒數。
    return () => clearInterval(timer)
  }, [deadline])
  // 父頁切換前可同步作廢請求；卸載時也防止舊回應寫入新畫面。
  useEffect(() => {
    const navigation = formNavigation.current
    // 先使序號失效再中止請求，即使請求忽略 AbortSignal 也不採用舊回應。
    function cancelPending() { attemptRef.current += 1; requestRef.current?.abort() }
    navigation.cancel = cancelPending
    // 表單卸載後移除父頁可呼叫的取消入口，避免指向舊表單。
    return () => { cancelPending(); navigation.dirty = false; navigation.pending = false; navigation.cancel = null }
  }, [formNavigation])
  // 錯誤摘要可聚焦，讓表單驗證結果對鍵盤與讀屏使用者可達。
  useEffect(() => { if (error) errorRef.current?.focus() }, [error])
  // 改動欄位後清除舊錯誤，Email 更換也清除已結束的鎖定提示。
  function changeField(event) {
    const { name, value } = event.target
    // 導覽選單在同一事件週期讀取草稿狀態，取消切換時仍保留原欄位。
    formNavigation.current.dirty = Boolean(value || Object.entries(fields).some(
      // 只檢查未被本次輸入取代的欄位。
      ([field, text]) => field !== name && text,
    ))
    // 使用最新欄位值，連續輸入不覆蓋尚未畫出的變更。
    setFields((current) => ({ ...current, [name]: value }))
    setError(null)
    if (name === 'email') setUnlocked(false)
  }
  // 本地先檢查已知格式；送出結果仍以目前後端和當次請求為準。
  async function submit(event) {
    event.preventDefault()
    if (pending || locked || created || requestRef.current || !backend) return
    // 本地錯誤不送 API，避免格式錯誤被算成伺服器登入失敗。
    const validated = validateAccount(fields, register)
    if (Object.keys(validated.errors).length) { setError({ message: '請修正下列欄位後再送出。', errors: validated.errors }); return }
    // 當次表單持有序號與 controller，切換後即使收到回應也不得保存或導頁。
    const sentBackend = backend
    const sequence = ++attemptRef.current
    const controller = new AbortController()
    requestRef.current = controller
    formNavigation.current.pending = true
    setPending(true)
    setError(null)
    setUnlocked(false)
    try {
      const response = await apiRequest(sentBackend, `/api/auth/${register ? 'register' : 'login'}`, {
        method: 'POST', body: register ? { email: validated.email, password: fields.password, nickname: fields.nickname } : { email: validated.email, password: fields.password }, signal: controller.signal,
      })
      if (controller.signal.aborted || sequence !== attemptRef.current) return
      // 儲存可讀時再核對後端；切換已同步中止請求並作廢序號，儲存被拒不能遮住已知回應。
      try {
        if (readBackend(sessionStorage) !== sentBackend) return
      } catch { /* getter 被拒時仍判讀伺服器結果；登入保存失敗由下方專用分支明示。 */ }
      if (register && emptySuccess(response.status, response.body, 201)) {
        setCreated(true)
        // 伺服器已確認建立帳號，成功導頁不應再被原表單的離頁保護攔下。
        formNavigation.current.dirty = false
        formNavigation.current.pending = false
        window.location.assign('/next/login?registered=1')
        return
      }
      const login = !register && loginData(response.status, response.body)
      if (login) {
        // 取得完整登入回應仍須保存成功；儲存被拒不表示帳密錯誤或登入已逾期。
        try {
          if (!saveSession(sessionStorage, sentBackend, login.access_token, login.expires_at)) throw new Error('Unable to save session')
        } catch {
          setError({ message: '無法儲存登入，請檢查瀏覽器儲存設定後再手動嘗試。' })
          return
        }
        // 成功登入後原欄位不再是未儲存草稿，避免導頁出現第二次確認。
        formNavigation.current.dirty = false
        formNavigation.current.pending = false
        if (onLogin) { setFields({ email: '', password: '', nickname: '' }); onLogin(login); return }
        window.location.assign(returnTo)
        return
      }
      const failure = apiFailure(response.status, response.body, register ? ['email', 'password', 'nickname'] : ['email', 'password'])
      // 倒數僅依有效 Retry-After；其他錯誤立即清除舊倒數，不推算伺服器狀態。
      if (!register && response.status === 429 && failure.code === 'E0105') {
        const seconds = retryAfterSeconds(response.retryAfter)
        if (seconds !== null) { const current = Date.now(); setNow(current); setDeadline(current + seconds * 1000) }
        else failure.message += '（缺少有效 Retry-After，無法顯示倒數）'
      } else setDeadline(null)
      if (register && (response.status >= 500 || response.status === 201)) failure.message = `註冊結果尚未確認；請先確認帳號狀態，再決定是否重試。${failure.message}`
      setError(failure)
    } catch {
      if (!controller.signal.aborted && sequence === attemptRef.current) setError({ message: register ? '註冊結果尚未確認；請先確認帳號狀態，再決定是否重試。' : '登入結果尚未確認；請先檢查登入狀態，再決定是否重試。' })
    } finally {
      if (requestRef.current === controller) { requestRef.current = null; formNavigation.current.pending = false; setPending(false) }
    }
  }
  return <div className="form-page">
    <div className="form-intro">
      <p className="eyebrow">{register ? 'JOIN THE COLLECTION / 加入讀者' : 'MEMBER ACCESS / 會員入口'}</p>
      <h1>{register ? '註冊會員' : '會員登入'}</h1>
      <p>{register ? '建立帳號後，請再登入閱讀與參與。' : '登入後可使用已開放的會員功能。'}</p>
    </div>
    <form className="account-form" noValidate onSubmit={submit}>
      {registered && !register && <p className="notice success" role="status">註冊成功，請登入。</p>}
      {created ? <p className="notice success" role="status">
        註冊成功，正在前往登入頁。<a href="/next/login?registered=1">手動前往登入</a>
      </p> : <>
        <ErrorNotice error={error} focusRef={errorRef} />
        {locked && <p className="lock-note">登入暫時鎖定，約剩 {formatCountdown(remaining)}</p>}
        {unlocked && <p className="notice success" role="status">可再次嘗試登入</p>}
        <label htmlFor="email">Email</label>
        <input id="email" name="email" value={fields.email} onChange={changeField}
          autoComplete="email" inputMode="email" disabled={pending || locked}
          aria-invalid={Boolean(error?.errors?.email)}
          aria-describedby={error?.errors?.email ? 'email-error' : undefined} />
        {error?.errors?.email && <small id="email-error" className="field-error">{error.errors.email.join('；')}</small>}
        <label htmlFor="password">密碼</label>
        <input id="password" name="password" value={fields.password} onChange={changeField} type="password"
          autoComplete={register ? 'new-password' : 'current-password'} disabled={pending || locked}
          aria-invalid={Boolean(error?.errors?.password)}
          aria-describedby={error?.errors?.password ? 'password-error' : undefined} />
        {error?.errors?.password && <small id="password-error" className="field-error">{error.errors.password.join('；')}</small>}
        <p className="hint">4–16 碼英文字母或數字</p>
        {register && <>
          <label htmlFor="nickname">暱稱</label>
          <input id="nickname" name="nickname" value={fields.nickname} onChange={changeField}
            autoComplete="nickname" disabled={pending} aria-invalid={Boolean(error?.errors?.nickname)}
            aria-describedby={error?.errors?.nickname ? 'nickname-error' : undefined} />
          {error?.errors?.nickname && <small id="nickname-error" className="field-error">{error.errors.nickname.join('；')}</small>}
        </>}
        <button type="submit" className="red-button" disabled={pending || locked}>
          {pending ? '處理中…' : register ? '建立帳號' : '登入'} <span aria-hidden="true">↗</span>
        </button>
        <p className="form-foot">{register
          ? <>已有帳號？ <a href="/next/login">前往登入</a></>
          : <>還沒有帳號？ <a href="/next/register">註冊會員</a></>}
        </p>
      </>}
    </form>
  </div>
}
