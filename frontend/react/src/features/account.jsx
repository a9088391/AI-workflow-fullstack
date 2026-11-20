import { useEffect, useRef, useState } from 'react'
import { failure, formatCountdown, ownsAttempt, remainingSeconds, request, retryAfterSeconds, saveSession, validAccount, validateAccount } from '../contracts.js'
import { ErrorNotice } from '../shared/ui.jsx'

// 帳號表單只在本機記憶體保存輸入；送出時先檢查欄位再呼叫目前後端。
export function AccountForm({ returnTo, registered, mode, backend, onLogin, onDraftChange, switchEpoch, inline = false, expectedOwnerId = null }) {
  const register = mode === 'register'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [nickname, setNickname] = useState('')
  const [errors, setErrors] = useState({})
  const [notice, setNotice] = useState(null)
  const [pending, setPending] = useState(false)
  const [deadline, setDeadline] = useState(0)
  const [now, setNow] = useState(Date.now())
  const [unlocked, setUnlocked] = useState(false)
  const latest = useRef(0)
  const controller = useRef(null)
  const summary = useRef(null)
  const remaining = deadline ? remainingSeconds(deadline, now) : 0
  const locked = remaining > 0

  // 本表單鎖定只存在記憶體，依真實時鐘刷新顯示並在卸載時清理。
  useEffect(() => {
    if (!deadline) return undefined
    // 計時器只更新目前表單顯示；實際解鎖仍由後端下次登入判定。
    const timer = setInterval(
      // 背景分頁節流後仍由實際時間差計算剩餘秒數。
      () => setNow(Date.now()), 250,
    )
    // 離開登入表單後清掉定時器，切換前端不搬移倒數。
    return () => clearInterval(timer)
  }, [deadline])
  // 倒數歸零只恢復表單與提示，不自動送出帳密。
  useEffect(() => {
    if (deadline && !remaining) {
      setDeadline(0)
      setUnlocked(true)
    }
  }, [deadline, remaining])
  // 元件切走時使舊帳號回應失效，並終止仍在途的請求。
  useEffect(() => {
    // 清理屬於這次表單的請求，避免切換後重新顯示舊鎖定。
    return () => {
      latest.current++
      controller.current?.abort()
      onDraftChange(false, false)
    }
  }, [onDraftChange])
  // 每次欄位或伺服器錯誤更新，將鍵盤焦點放回可聚焦摘要。
  useEffect(() => { if (notice) summary.current?.focus() }, [notice])

  // 編輯帳密就讓先前請求失效，舊帳號的鎖定回應不得禁用新帳號表單。
  function edit(setter, value) {
    latest.current++
    controller.current?.abort()
    setPending(false)
    setNotice(null)
    setErrors({})
    setUnlocked(false)
    setter(value)
    onDraftChange(true, false)
  }

  // 三個輸入分別更新當前表單，且都使舊登入嘗試作廢。
  function editEmail(event) { edit(setEmail, event.target.value) }
  // 暱稱變更不搬移至其他前端或後端。
  function editNickname(event) { edit(setNickname, event.target.value) }
  // 密碼僅保存在本頁記憶體，任何修改都須重新確認回應所屬帳號。
  function editPassword(event) { edit(setPassword, event.target.value) }

  // 寫入只處理最新的有效表單請求；不明結果要求手動查證，絕不自動重送。
  async function submit(event) {
    event.preventDefault()
    if (pending || locked) return
    // 先在本機檢查格式並呈現欄位錯誤，避免無效帳密送出登入請求。
    const checked = validateAccount({ email, password, nickname }, register)
    setErrors(checked.errors)
    if (Object.keys(checked.errors).length) { setNotice({ message: '請修正標示欄位後再送出。' }); return }
    // 固定本次帳號、表單序號與頁面世代並標記等待，舊回應不能接管後續嘗試。
    const sequence = ++latest.current
    const startingEpoch = switchEpoch.current
    /** @type {import('../contracts.js').LoginInputDto | import('../contracts.js').RegisterInputDto} */
    const attempt = { email: checked.email, password, ...(register ? { nickname } : {}) }
    const abort = new AbortController()
    controller.current = abort
    setPending(true)
    onDraftChange(true, true)
    setNotice(null)
    // 註冊成功導頁時仍會執行 finally，須避免重新把已完成表單標成草稿。
    let registered = false
    try {
      const { status, body, retryAfter } = await request(backend, `/api/auth/${register ? 'register' : 'login'}`, { method: 'POST', signal: abort.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(attempt) })
      // 已確認切換會同步增加世代；卸載前抵達的舊成功也不得存 JWT 或導覽。
      if (!ownsAttempt(abort.signal, sequence, latest.current, startingEpoch, switchEpoch.current)) return
      if (register && status === 201 && body?.success === true && body.message === 'OK' && body.data && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0) {
        registered = true
        onDraftChange(false, false)
        window.location.assign('/react/login?registered=1')
        return
      }
      if (!register && status === 200 && body?.success === true && body.message === 'OK' && body.data?.token_type === 'Bearer' && validAccount(body.data.user)) {
        // 原頁恢復只接受原作者；其他帳號不能接收舊草稿或沿用編修版本。
        if (inline && expectedOwnerId && body.data.user.id !== expectedOwnerId) {
          setNotice({ message: '登入帳號與草稿作者不同；請使用原帳號重新登入，或離開並捨棄草稿。' })
          return
        }
        // 儲存完整憑證成功才通知外殼並導覽，儲存被拒絕仍留在原表單。
        if (saveSession(sessionStorage, backend, body.data.access_token, body.data.expires_at)) {
          setPending(false)
          onDraftChange(false, false)
          onLogin()
          if (!inline) window.location.assign(returnTo)
          return
        }
      }
      const problem = failure(body)
      if (register && (status >= 500 || status === 201)) problem.message += '。無法確認註冊結果；請先確認帳號狀態，再決定是否重試。'
      setErrors(problem.errors)
      if (!register && status === 429 && problem.code === 'E0105') {
        const seconds = retryAfterSeconds(retryAfter)
        if (seconds !== null) {
          setNow(Date.now())
          setDeadline(Date.now() + seconds * 1000)
          setUnlocked(false)
        } else console.warn('Login lockout response has invalid Retry-After header')
      } else if (problem.code === 'E0102') setDeadline(0)
      setNotice(problem)
    } catch {
      if (ownsAttempt(abort.signal, sequence, latest.current, startingEpoch, switchEpoch.current)) setNotice({ message: register ? '無法確認註冊結果；請先確認帳號狀態，再決定是否重試。' : '無法確認登入結果；請稍後手動重試。' })
    } finally {
      // 只結束仍屬目前表單的等待，不讓過時回應重設新嘗試的狀態。
      if (ownsAttempt(abort.signal, sequence, latest.current, startingEpoch, switchEpoch.current)) {
        setPending(false)
        onDraftChange(!registered, false)
      }
    }
  }

  return (
    <section className="account">
      <p className="eyebrow">{register ? 'CREATE ACCOUNT' : 'MEMBER ACCESS'}</p>
      <h1>{register ? '註冊會員' : inline ? '在原頁重新登入' : '會員登入'}</h1>
      <p className="intro">{register ? '建立帳號後，請再登入。' : inline ? '登入成功後會留在目前頁面；若有保留的草稿，請確認後手動送出。' : '登入後可跨前端保留同一次登入期限。'}</p>
      {!register && registered && <p className="notice success">註冊完成，請登入。</p>}
      <ErrorNotice error={notice} focusRef={summary} />
      {locked && <p className="lock-message">登入暫時鎖定，約剩 {formatCountdown(remaining)}</p>}
      {unlocked && !locked && <p className="notice" role="status">可再次嘗試登入</p>}
      <form noValidate onSubmit={submit}>
        <label htmlFor="email">Email</label>
        <input id="email" type="email" autoComplete="email" value={email} disabled={pending || locked} aria-invalid={Boolean(errors.email)} aria-describedby={errors.email ? 'email-error' : undefined} onChange={editEmail} />
        {errors.email && <p className="field-error" id="email-error">{errors.email.join('；')}</p>}
        {register && <>
          <label htmlFor="nickname">暱稱</label>
          <input id="nickname" value={nickname} disabled={pending} aria-invalid={Boolean(errors.nickname)} aria-describedby={errors.nickname ? 'nickname-error' : undefined} onChange={editNickname} />
          {errors.nickname && <p className="field-error" id="nickname-error">{errors.nickname.join('；')}</p>}
        </>}
        <label htmlFor="password">密碼</label>
        <input id="password" type="password" autoComplete={register ? 'new-password' : 'current-password'} value={password} disabled={pending || locked} aria-invalid={Boolean(errors.password)} aria-describedby={errors.password ? 'password-error' : undefined} onChange={editPassword} />
        {errors.password && <p className="field-error" id="password-error">{errors.password.join('；')}</p>}
        <button className="primary" type="submit" disabled={pending || locked}>{pending ? '處理中…' : register ? '建立帳號' : '登入'}</button>
      </form>
      <p className="account-link">{register ? <>已有帳號？ <a href="/react/login">前往登入</a></> : <>還沒有帳號？ <a href="/react/register">註冊會員</a></>}</p>
    </section>
  )
}
