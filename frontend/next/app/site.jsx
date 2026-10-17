'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFailure, apiRequest, BACKENDS, canSwitchFrontend, emptySuccess, formatCountdown, FRONTENDS, frontendTarget, loginData, ownsSession, pageNumber, positiveId, readBackend, readSession, remainingSeconds, retryAfterSeconds, saveSession, selectBackend, successData, validReturn, validateAccount, clearSessionIfOwned } from '../lib/contracts.js'

// 保留錯誤摘要、欄位導覽與 log_id，讓鍵盤使用者能修正或回報問題。
function ErrorNotice({ error, retry, focusRef }) {
  if (!error) return null
  // 點選欄位明細會移至輸入，不以顏色單獨傳達錯誤。
  function focusField(event) { document.getElementById(event.currentTarget.dataset.field)?.focus() }
  // 複製失敗仍可手動選取原文，不阻斷錯誤處理。
  async function copyLogId() { try { await navigator.clipboard?.writeText(error.logId) } catch { /* 原文仍可選取。 */ } }
  return <div className="notice error" role="alert" tabIndex={-1} ref={focusRef}>
    <strong>{error.message}</strong>
    {error.errors && Object.keys(error.errors).length > 0 && <ul>{Object.entries(error.errors).map(
      // 已知欄位可聚焦，其他伺服器明細仍須完整顯示。
      ([name, messages]) => <li key={name}>{['email', 'password', 'nickname'].includes(name)
        ? <button type="button" className="text-button" data-field={name} onClick={focusField}>{name}：{messages.join('；')}</button>
        : `${name}：${messages.join('；')}`}</li>,
    )}</ul>}
    {error.logId && <p>追蹤編號：<span className="break">{error.logId}</span> <button type="button" className="text-button" onClick={copyLogId}>複製</button></p>}
    {retry && <button type="button" className="outline-button" onClick={retry}>重新讀取</button>}
  </div>
}

// 每次路徑、頁碼或登入變動都取消舊讀取，並只呈現目前來源的結果。
function useApiRead(backend, credential, path, kind, expected, articleId, enabled, onAuthFailure) {
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState({ key: '', loading: true, data: null, error: null })
  const failureRef = useRef(onAuthFailure)
  failureRef.current = onAuthFailure
  const token = credential?.accessToken || ''
  const key = `${backend}|${token}|${path}|${kind}|${expected}|${articleId}|${enabled}|${revision}`
  // 讀取生命週期只擁有自己的 AbortController；清理後的回應不得改畫面。
  useEffect(() => {
    if (!enabled || !backend) return undefined
    const controller = new AbortController()
    setResult({ key, loading: true, data: null, error: null })
    // 只有完整 envelope 與預期 ID／頁碼才作為成功資料。
    async function load() {
      try {
        const response = await apiRequest(backend, path, { token, signal: controller.signal })
        if (controller.signal.aborted) return
        const data = successData(response.status, response.body, kind, expected, articleId)
        if (data) { setResult({ key, loading: false, data, error: null }); return }
        const failure = apiFailure(response.status, response.body)
        if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') failureRef.current(backend, credential, failure)
        setResult({ key, loading: false, data: null, error: failure })
      } catch {
        if (!controller.signal.aborted) setResult({ key, loading: false, data: null, error: { message: '連線失敗，請確認目前後端後手動重試。' } })
      }
    }
    load()
    // 離開此讀取條件即中止請求，避免舊結果覆蓋目前資料。
    return () => controller.abort()
  }, [backend, token, path, kind, expected, articleId, enabled, revision, key])
  // 依當前請求識別隔離切換瞬間的舊資料，不等待 effect 才清空。
  const current = result.key === key ? result : { loading: true, data: null, error: null }
  // 手動重試只遞增讀取版次，讓原條件重新請求且舊回應仍被隔離。
  function reload() {
    // 用先前值遞增，避免快速連續重試遺失更新。
    setRevision((value) => value + 1)
  }
  return { ...current, reload }
}

// 以 API 原頁碼及總數導覽，超頁仍提供第一頁入口。
function Pagination({ pagination, base, fragment = '' }) {
  if (!pagination || (pagination.total_pages <= 1 && pagination.page <= 1)) return null
  const number = pagination.page
  // 保留目前資源路徑，只更換列表頁碼。
  function href(page) { return `${base}?page=${page}${fragment}` }
  return <nav className="pagination" aria-label="分頁">
    <span>第 {number} 頁／共 {pagination.total_pages} 頁，{pagination.total} 筆</span>
    {number > 1 && <a href={href(number - 1)}>上一頁</a>}
    {number < pagination.total_pages && <a href={href(number + 1)}>下一頁</a>}
    {number > pagination.total_pages && <a href={href(1)}>返回第一頁</a>}
  </nav>
}

// 固定的文章側欄建立瑞士式非對稱格線，行動版縮為一般標題區。
function ArticleRail() {
  return <aside className="article-rail" aria-label="文章集"><p className="rail-kicker">THE ARTICLE COLLECTION</p><p className="rail-title">所有<br />文章</p><p className="rail-foot">閱讀・記錄・分享</p></aside>
}

// 保留建立來源、作者與日期，列表和詳情共用同一資料語意。
function ArticleMeta({ item }) {
  return <div className="meta"><span>作者 <strong>{item.author.nickname}</strong></span><span>發佈日期 {item.created_at.slice(0, 10)}</span><span>建立於 <b>{BACKENDS[item.created_backend][0]}</b></span></div>
}

// 公開文章列表直接用目前後端資料，分別處理載入、真空與超頁。
function ArticleList({ backend, credential, ready, page, onAuthFailure }) {
  const read = useApiRead(backend, credential, `/api/articles?page=${page}&per_page=20`, 'articles', page, null, ready, onAuthFailure)
  return <div className="editorial-layout"><ArticleRail /><section className="article-column" aria-labelledby="list-title">
    <div className="section-heading"><p className="eyebrow">PUBLIC ARTICLES / 文章索引</p><h1 id="list-title">所有文章</h1></div>
    {read.loading && <p className="notice" role="status">正在載入文章…</p>}
    <ErrorNotice error={read.error} retry={read.reload} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有文章。' : '目前尚無文章。'}</p>
      : <ol className="article-list">{read.data.list.map(
        // 以資料 ID 固定列項，跨頁與重新讀取都不重用錯誤內容。
        (article, index) => <li key={article.id} className="article-item"><span className="item-number">{String((page - 1) * 20 + index + 1).padStart(2, '0')}</span><div><h2><a href={`/next/articles/${article.id}`}>{article.title}</a></h2><p className="excerpt">{article.body}</p><ArticleMeta item={article} /></div></li>,
      )}</ol>}
      <Pagination pagination={read.data.pagination} base="/next/" /></>}
  </section></div>
}

// 留言與文章分開載入，避免任一讀取失敗被誤當成沒有留言。
function CommentList({ backend, credential, ready, articleId, page, onAuthFailure }) {
  const read = useApiRead(backend, credential, `/api/articles/${articleId}/comments?page=${page}&per_page=20`, 'comments', page, articleId, ready, onAuthFailure)
  return <section className="comments" id="comments" aria-labelledby="comments-title"><div className="subsection-heading"><h2 id="comments-title">留言</h2><span>READ ONLY / 唯讀</span></div>
    {read.loading && <p className="notice" role="status">正在載入留言…</p>}
    <ErrorNotice error={read.error} retry={read.reload} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有留言。' : '目前尚無留言。'}</p>
      : <ol className="comment-list">{read.data.list.map(
        // 留言 ID 固定列項，文章切換時不沿用舊文章內容。
        (comment, index) => <li key={comment.id}><span className="item-number">{String((page - 1) * 20 + index + 1).padStart(2, '0')}</span><div><div className="meta"><strong>{comment.author.nickname}</strong><span>{comment.created_at.slice(0, 10)}</span><span>建立於 <b>{BACKENDS[comment.created_backend][0]}</b></span></div><p className="body-text">{comment.body}</p></div></li>,
      )}</ol>}
      <Pagination pagination={read.data.pagination} base={`/next/articles/${articleId}`} fragment="#comments" /></>}
    <p className="read-only-note">此頁目前提供留言閱讀；留言發表與管理將在後續功能開放。</p>
  </section>
}

// 文章先確認可讀才載入留言，404 不揭露下架或不存在的差別。
function ArticlePage({ backend, credential, ready, id, page, onAuthFailure }) {
  const numericId = positiveId(id)
  const read = useApiRead(backend, credential, `/api/articles/${id}`, 'article', numericId, null, ready, onAuthFailure)
  return <div className="editorial-layout"><ArticleRail /><article className="article-column detail">
    <a className="back-link" href="/next/">← 返回文章列表</a>
    {read.loading && <p className="notice" role="status">正在載入文章…</p>}
    <ErrorNotice error={read.error} retry={read.reload} />
    {read.data && <><header><p className="eyebrow">ARTICLE / 文章閱讀</p><h1>{read.data.title}</h1><ArticleMeta item={read.data} /></header>
      <div className="article-body body-text">{read.data.body}</div>
      <CommentList backend={backend} credential={credential} ready={ready} articleId={numericId} page={page} onAuthFailure={onAuthFailure} /></>}
  </article></div>
}

// 註冊與登入使用相同單欄表單；鎖定與伺服器錯誤只套在當次登入。
function AccountForm({ mode, backend, returnTo, registered, formNavigation }) {
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
    // 先使序號失效再中止請求，忽略即使忽略 AbortSignal 的舊回應。
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
    const validated = validateAccount(fields, register)
    if (Object.keys(validated.errors).length) { setError({ message: '請修正下列欄位後再送出。', errors: validated.errors }); return }
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
      if (controller.signal.aborted || sequence !== attemptRef.current || readBackend(sessionStorage) !== sentBackend) return
      if (register && emptySuccess(response.status, response.body, 201)) {
        setCreated(true)
        window.location.assign('/next/login?registered=1')
        return
      }
      const login = !register && loginData(response.status, response.body)
      if (login) {
        if (!saveSession(sessionStorage, sentBackend, login.access_token, login.expires_at)) throw new Error('Unable to save session')
        window.location.assign(returnTo)
        return
      }
      const failure = apiFailure(response.status, response.body, register ? ['email', 'password', 'nickname'] : ['email', 'password'])
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
  return <main className="form-page"><div className="form-intro"><p className="eyebrow">{register ? 'JOIN THE COLLECTION / 加入讀者' : 'MEMBER ACCESS / 會員入口'}</p><h1>{register ? '註冊會員' : '會員登入'}</h1><p>{register ? '建立帳號後，請再登入閱讀與參與。' : '登入後可使用已開放的會員功能。'}</p></div>
    <form className="account-form" noValidate onSubmit={submit}>
      {registered && !register && <p className="notice success" role="status">註冊成功，請登入。</p>}
      {created ? <p className="notice success" role="status">註冊成功，正在前往登入頁。<a href="/next/login?registered=1">手動前往登入</a></p> : <>
        <ErrorNotice error={error} focusRef={errorRef} />
        {locked && <p className="lock-note">登入暫時鎖定，約剩 {formatCountdown(remaining)}</p>}
        {unlocked && <p className="notice success" role="status">可再次嘗試登入</p>}
        <label htmlFor="email">Email</label><input id="email" name="email" value={fields.email} onChange={changeField} autoComplete="email" inputMode="email" disabled={pending || locked} aria-invalid={Boolean(error?.errors?.email)} aria-describedby={error?.errors?.email ? 'email-error' : undefined} />
        {error?.errors?.email && <small id="email-error" className="field-error">{error.errors.email.join('；')}</small>}
        <label htmlFor="password">密碼</label><input id="password" name="password" value={fields.password} onChange={changeField} type="password" autoComplete={register ? 'new-password' : 'current-password'} disabled={pending || locked} aria-invalid={Boolean(error?.errors?.password)} aria-describedby={error?.errors?.password ? 'password-error' : undefined} />
        {error?.errors?.password && <small id="password-error" className="field-error">{error.errors.password.join('；')}</small>}
        <p className="hint">4–16 碼英文字母或數字</p>
        {register && <><label htmlFor="nickname">暱稱</label><input id="nickname" name="nickname" value={fields.nickname} onChange={changeField} autoComplete="nickname" disabled={pending} aria-invalid={Boolean(error?.errors?.nickname)} aria-describedby={error?.errors?.nickname ? 'nickname-error' : undefined} />
          {error?.errors?.nickname && <small id="nickname-error" className="field-error">{error.errors.nickname.join('；')}</small>}</>}
        <button type="submit" className="red-button" disabled={pending || locked}>{pending ? '處理中…' : register ? '建立帳號' : '登入'} <span aria-hidden="true">↗</span></button>
        <p className="form-foot">{register ? <>已有帳號？ <a href="/next/login">前往登入</a></> : <>還沒有帳號？ <a href="/next/register">註冊會員</a></>}</p>
      </>}
    </form></main>
}

// 共用頁面控制目前前後端與登入；業務資料只在瀏覽器讀取目前後端。
export default function Site({ path, query }) {
  const [backend, setBackend] = useState(null)
  const [credential, setCredential] = useState(null)
  const [account, setAccount] = useState(null)
  const [authStatus, setAuthStatus] = useState('checking')
  const [authError, setAuthError] = useState(null)
  const [logoutPending, setLogoutPending] = useState(false)
  const [notice, setNotice] = useState(null)
  const [authRevision, setAuthRevision] = useState(0)
  const logoutRef = useRef(null)
  const formNavigation = useRef({ dirty: false, pending: false, cancel: null })
  const page = pageNumber(query.page)
  const returnTo = validReturn(query.return_to) ? query.return_to : '/next/'
  const article = path.match(/^\/next\/articles\/([0-9]+)$/)
  const articleId = article && positiveId(article[1]) !== null ? article[1] : null
  // 本人驗證暫時失敗仍可嘗試公開文章；只在已確認登入時附上 JWT。
  const ready = backend !== null && ['guest', 'active', 'expired', 'disabled', 'error'].includes(authStatus)
  // BFCache 恢復的舊元件可能持有過期後端或 JWT，重載後重新讀取並驗證。
  useEffect(() => {
    // 只對已快取的舊文件重載，一般初次顯示保留原狀。
    function restoreFromCache(event) { if (event.persisted) window.location.reload() }
    window.addEventListener('pageshow', restoreFromCache)
    // 元件卸載時移除監聽，避免重複觸發重載。
    return () => window.removeEventListener('pageshow', restoreFromCache)
  }, [])
  // 初次進頁及手動重驗都以同分頁儲存狀態呼叫 /api/me，暫時故障不刪 JWT。
  useEffect(() => {
    const controller = new AbortController()
    let storage
    try { storage = sessionStorage } catch { setBackend(BACKENDS.laravel[1]); setAuthStatus('guest'); return undefined }
    const selected = readBackend(storage)
    const saved = readSession(storage)
    setBackend(selected)
    setCredential(saved)
    setAccount(null)
    setAuthError(null)
    if (!saved) { setAuthStatus('guest'); return undefined }
    setAuthStatus('checking')
    // 未通過本次驗證前不顯示私有資料；只有確認失效才清除原憑證。
    async function verify() {
      try {
        const response = await apiRequest(selected, '/api/me', { token: saved.accessToken, signal: controller.signal })
        if (controller.signal.aborted || !ownsSession(storage, selected, saved)) return
        const user = successData(response.status, response.body, 'account')
        if (user) { setAccount(user); setAuthStatus('active'); return }
        const failure = apiFailure(response.status, response.body)
        if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') {
          clearSessionIfOwned(storage, selected, saved)
          setCredential(null)
          setAuthStatus(failure.code === 'E0102' ? 'disabled' : 'expired')
          setAuthError(failure)
          return
        }
        setAuthStatus('error')
        setAuthError(failure)
      } catch {
        if (!controller.signal.aborted) { setAuthStatus('error'); setAuthError({ message: '暫時無法驗證登入，請確認目前後端後重新驗證。' }) }
      }
    }
    verify()
    // 頁面離開時中止驗證，讓舊後端結果不能改寫目前登入狀態。
    return () => controller.abort()
  }, [authRevision])
  // 公開 API 回報已知失效時，只移除原請求的 JWT，讓新登入不受舊回應影響。
  function handleAuthFailure(sentBackend, sentCredential, failure) {
    try { if (!clearSessionIfOwned(sessionStorage, sentBackend, sentCredential)) return } catch { return }
    setCredential(null)
    setAccount(null)
    setAuthStatus(failure.code === 'E0102' ? 'disabled' : 'expired')
    setAuthError(failure)
  }
  // 有草稿或送出中的表單先取得捨棄同意，並告知註冊可能已在伺服器完成。
  function confirmFormSwitch(destination) {
    const { dirty, pending } = formNavigation.current
    if (!dirty && !pending) return true
    const warning = pending
      ? path === '/next/register' ? '註冊請求已送出，切換後伺服器仍可能完成建立帳號。' : '登入請求已送出，切換後伺服器仍可能完成處理。'
      : '目前表單有未儲存文字。'
    return window.confirm(`${warning}要捨棄目前表單並切換${destination}嗎？`)
  }
  // 只在目前後端更新選擇，舊登入清除後轉登入並保留合法閱讀目的地。
  function changeBackend(event) {
    const next = event.target.value
    // 未完成登出不能先清 JWT 或改後端，等伺服器結果明確後再試。
    if (logoutPending || logoutRef.current) { event.target.value = backend || ''; setNotice({ message: '登出處理中，請等待結果後再切換。' }); return }
    if (!confirmFormSwitch('後端')) { event.target.value = backend || ''; return }
    const hadCredential = Boolean(credential)
    try { if (!selectBackend(sessionStorage, next)) { event.target.value = backend || ''; return } } catch { event.target.value = backend || ''; setNotice({ message: '無法儲存後端選擇，請檢查瀏覽器設定。' }); return }
    // 儲存成功後、導覽前立即作廢來源表單，舊登入回應不能寫回 JWT。
    formNavigation.current.cancel?.()
    const target = validReturn(path) ? path : '/next/'
    window.location.assign(hadCredential && path !== '/next/login' && path !== '/next/register' ? `/next/login?return_to=${encodeURIComponent(target)}` : window.location.pathname + window.location.search)
  }
  // 跨前端只導覽相同語意頁，不修改同分頁的後端或 JWT 原期限。
  function changeFrontend(event) {
    const target = canSwitchFrontend(event.target.value, backend) && frontendTarget(path, event.target.value, returnTo)
    if (!target) { event.target.value = 'next'; return }
    if (logoutPending || logoutRef.current) { event.target.value = 'next'; setNotice({ message: '登出處理中，請等待結果後再切換。' }); return }
    if (!confirmFormSwitch('前端')) { event.target.value = 'next'; return }
    // 確認後、導覽前同步取消來源表單，避免延遲成功回應搶先改址。
    formNavigation.current.cancel?.()
    window.location.assign(target)
  }
  // 伺服器確認撤銷才清除本次 JWT；未知結果保留登入並提示手動核對。
  async function logout() {
    if (logoutPending || !credential || !backend || logoutRef.current) return
    const sentBackend = backend
    const sentCredential = credential
    const controller = new AbortController()
    logoutRef.current = controller
    setLogoutPending(true)
    setNotice(null)
    try {
      const response = await apiRequest(sentBackend, '/api/auth/logout', { method: 'POST', token: sentCredential.accessToken, signal: controller.signal })
      if (controller.signal.aborted || !ownsSession(sessionStorage, sentBackend, sentCredential)) return
      if (emptySuccess(response.status, response.body, 200)) {
        clearSessionIfOwned(sessionStorage, sentBackend, sentCredential)
        setCredential(null)
        setAccount(null)
        setAuthStatus('guest')
        setNotice({ message: '已登出。' })
        return
      }
      const failure = apiFailure(response.status, response.body)
      if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') handleAuthFailure(sentBackend, sentCredential, failure)
      setNotice({ ...failure, message: `登出未完成：${failure.message}` })
    } catch {
      if (!controller.signal.aborted) setNotice({ message: '登出結果尚未確認；請先確認登入狀態，再手動重試。' })
    } finally {
      if (logoutRef.current === controller) { logoutRef.current = null; setLogoutPending(false) }
    }
  }
  // 身分驗證故障時手動重新查核，不重新簽發原登入。
  function retryAuth() {
    // 以最新驗證版次重新查核，不依賴舊畫面的數值。
    setAuthRevision((value) => value + 1)
  }
  return <div className="site-shell"><a className="skip-link" href="#main-content">跳至主要內容</a><header className="site-header">
    <div className="brand-row"><a className="brand" href="/next/">文章集<span aria-hidden="true">.</span></a><nav className="desktop-nav" aria-label="主要導覽"><a aria-current={path === '/next/' ? 'page' : undefined} href="/next/">所有文章</a>{account ? <span className="coming-soon">會員功能逐步開放</span> : <><a href="/next/login">登入</a><a href="/next/register">註冊</a></>}</nav></div>
    <div className="header-controls"><div className="current-labels"><span>目前前端 <strong>Next.js</strong></span><span>目前後端 <strong>{backend ? Object.values(BACKENDS).find(
      // 從固定清單找顯示名稱，不把不可信儲存值直接印到畫面。
      (entry) => entry[1] === backend,
    )?.[0] : '讀取中…'}</strong></span></div>
      <label className="select-wrap">切換版本<select value="next" onChange={changeFrontend} aria-label="切換前端版本">{Object.entries(FRONTENDS).map(
        // Nuxt 不接受其餘八個後端；入口是否啟動仍由 NGINX 呈現。
        ([id, name]) => <option key={id} value={id} disabled={!canSwitchFrontend(id, backend)}>{name}{id === 'nuxt' && !canSwitchFrontend(id, backend) ? '（此後端尚未提供）' : ''}</option>,
      )}</select></label>
      <label className="select-wrap">切換後端<select value={backend || ''} onChange={changeBackend} disabled={!backend} aria-label="切換目前後端">{!backend && <option value="">讀取中…</option>}{Object.entries(BACKENDS).map(
        // API 來源只取固定清單，顯示文字不影響實際位址。
        ([id, entry]) => <option key={id} value={entry[1]}>{entry[0]}</option>,
      )}</select></label>
      {account && <span className="account-name">{account.nickname}</span>}
      {account && <button type="button" className="text-button logout" onClick={logout} disabled={logoutPending}>{logoutPending ? '登出中…' : '登出'}</button>}
    </div>
    <details className="mobile-nav"><summary>選單</summary><nav aria-label="行動導覽"><a href="/next/">所有文章</a>{!account && <><a href="/next/login">登入</a><a href="/next/register">註冊</a></>}</nav></details>
  </header>
  <div className="status-area"><ErrorNotice error={notice} />{authStatus === 'checking' && <p className="notice" role="status">正在驗證登入…</p>}{authError && <ErrorNotice error={authError} retry={authStatus === 'error' ? retryAuth : null} />}{authStatus === 'expired' && <a href={`/next/login?return_to=${encodeURIComponent(validReturn(path) ? path : '/next/')}`}>重新登入</a>}</div>
  <div id="main-content">{path === '/next/' || path === '/next' ? <ArticleList backend={backend} credential={account ? credential : null} ready={ready} page={page} onAuthFailure={handleAuthFailure} />
    : articleId ? <ArticlePage backend={backend} credential={account ? credential : null} ready={ready} id={articleId} page={page} onAuthFailure={handleAuthFailure} />
      : path === '/next/login' || path === '/next/register' ? <AccountForm mode={path === '/next/register' ? 'register' : 'login'} backend={backend} returnTo={returnTo} registered={query.registered === '1'} formNavigation={formNavigation} />
        : <main className="missing"><p className="eyebrow">404 / PAGE NOT FOUND</p><h1>找不到頁面</h1><p>此路徑尚未提供可閱讀的頁面。</p><a className="red-button" href="/next/">返回文章列表 <span aria-hidden="true">↗</span></a></main>}</div>
  <footer className="site-footer"><span>文章集 / NEXT.JS EDITION</span><span>閱讀是另一種記錄。</span></footer></div>
}
