import { useCallback, useEffect, useRef, useState } from 'react'
import { BACKENDS, FRONTENDS, clearSessionIfOwned, emptyCommentMessage, failure, formatCountdown, frontendTarget, ownsAttempt, ownsSession, positiveId, readBackend, readSession, remainingSeconds, request, retryAfterSeconds, saveSession, selectBackend, showCommentPagination, validAccount, validContent, validList, validReturn, validateAccount } from './contracts.js'

const pathname = window.location.pathname
const params = new URLSearchParams(window.location.search)
const articleMatch = pathname.match(/^\/react\/articles\/([0-9]+)$/)
const articleId = articleMatch && positiveId(articleMatch[1]) !== null ? articleMatch[1] : null
const page = pathname === '/react/' ? 'articles' : pathname === '/react/login' ? 'login' : pathname === '/react/register' ? 'register' : articleId ? 'article' : 'missing'
const rawPage = params.get('page')
const listPage = rawPage && /^[1-9][0-9]*$/.test(rawPage) && Number.isSafeInteger(Number(rawPage)) ? Number(rawPage) : 1
const rawReturn = params.get('return_to')
const returnTo = validReturn(rawReturn) ? rawReturn : '/react/'

// 選定本頁標題以支援直接開啟深層網址，其他路徑明示不存在。
document.title = { articles: '文章集｜所有文章', article: '文章集｜閱讀文章', register: '文章集｜註冊會員', login: '文章集｜會員登入', missing: '文章集｜找不到頁面' }[page]

// API 失敗保持欄位錯誤與追蹤編號，避免使用者只能看到泛用失敗訊息。
function ErrorNotice({ error, retry, focusRef }) {
  if (!error) return null
  // 可聚焦摘要讓鍵盤使用者取得錯誤，欄位連結可直接回到需修正的輸入。
  function focusField(event) { document.getElementById(event.currentTarget.dataset.field)?.focus() }
  return <div className="notice error" role="alert" tabIndex={-1} ref={focusRef}>
    <strong>{error.message}</strong>
    {error.errors && Object.entries(error.errors).length > 0 && <ul className="error-fields">{Object.entries(error.errors).map(
      // 已知欄位可直接聚焦；未知欄位仍完整呈現，避免遺失後端明細。
      ([name, messages]) => <li key={name}>{['email', 'password', 'nickname'].includes(name) ? <button type="button" className="text-button" data-field={name} onClick={focusField}>{name}：{messages.join('；')}</button> : `${name}：${messages.join('；')}`}</li>,
    )}</ul>}
    {error.logId && <p>追蹤編號：<span className="break">{error.logId}</span></p>}
    {retry && <button className="secondary" type="button" onClick={retry}>重新讀取</button>}
  </div>
}

// 每次後端、憑證或分頁變動都取消舊讀取，避免過時資料覆寫新選擇。
function useRead(backend, credential, path, currentPage, enabled, onAuthFailure, switchEpoch, kind = 'article') {
  const [result, setResult] = useState({ loading: true, data: null, error: null })
  const [revision, setRevision] = useState(0)
  // 每個讀取生命週期只擁有自己的一次請求，依賴變動時清理。
  useEffect(() => {
    if (!enabled) { setResult({ loading: false, data: null, error: null }); return undefined }
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    setResult({ loading: true, data: null, error: null })
    // 公開 API 若帶 JWT 就必須驗證；此處不在錯誤後靜默改以訪客重試。
    request(backend, path, { signal: controller.signal, headers: credential ? { Authorization: `Bearer ${credential.accessToken}` } : {} })
      .then(
        // 只接受本次路徑預期的成功 envelope；不完整資料不顯示為空列表。
        ({ status, body }) => {
          if (controller.signal.aborted || startedEpoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential)) return
          if (status === 200 && body?.success === true && body.message === 'OK' && (currentPage ? validList(body.data, currentPage, kind) : body.data?.id === Number(articleId) && validContent(body.data))) setResult({ loading: false, data: body.data, error: null })
          else {
            const problem = failure(body)
            if (credential && (problem.code === 'E0002' || problem.code === 'E0102')) onAuthFailure(problem, backend, credential)
            setResult({ loading: false, data: null, error: problem })
          }
        },
        // 網路中斷保留登入狀態並提供手動重讀。
        () => { if (!controller.signal.aborted && startedEpoch === switchEpoch.current && ownsSession(sessionStorage, backend, credential)) setResult({ loading: false, data: null, error: { message: '暫時無法連線，請稍後手動重試。' } }) },
      )
    // 來源變動或元件離開時中止舊請求，避免舊回應落到新畫面。
    return () => controller.abort()
  }, [backend, credential, path, currentPage, enabled, revision, onAuthFailure, switchEpoch, kind])
  // 手動重讀保留相同路徑，只增加請求世代；連線錯誤不自動重送。
  function retry() {
    setRevision(
      // 使用最新世代，連續點擊也不會因過時閉包漏掉重讀。
      (value) => value + 1,
    )
  }
  return { ...result, retry }
}

// 顯示文章原始時間與後端來源；純文字由 React 轉義，不解讀 HTML。
function Metadata({ item }) {
  const source = BACKENDS[item.created_backend]?.[0] || item.created_backend
  return <div className="metadata"><span>作者　{item.author?.nickname}</span><span>{item.created_at}</span><span>建立於　<b>{source}</b></span></div>
}

// 分頁使用 API 回傳的總頁數；超頁仍保留原頁碼及返回第一頁入口。
function Pagination({ pagination, base }) {
  if (!pagination || pagination.total_pages <= 1 && pagination.page === 1) return null
  const { page: number, total_pages: totalPages, total } = pagination
  return <nav className="pagination" aria-label="分頁">
    <span>第 {number} 頁／共 {totalPages} 頁，{total} 筆</span>
    {number > 1 && <a href={`${base}?page=${number - 1}`}>上一頁</a>}
    {number < totalPages && <a href={`${base}?page=${number + 1}`}>下一頁</a>}
    {number > totalPages && <a href={base}>返回第一頁</a>}
  </nav>
}

// 公開列表只顯示已核准欄位，保留空資料、超頁與載入失敗區別。
function Articles({ backend, credential, enabled, onAuthFailure, switchEpoch }) {
  const read = useRead(backend, credential, `/api/articles?page=${listPage}&per_page=20`, listPage, enabled, onAuthFailure, switchEpoch)
  return <section className="content" aria-labelledby="articles-title">
    <p className="eyebrow">ARTICLE INDEX / REACT</p><h1 id="articles-title">所有文章</h1>
    <p className="intro">在這裡閱讀來自各個後端的文章。</p>
    {read.loading && <p role="status">正在載入文章…</p>}
    <ErrorNotice error={read.error} retry={read.retry} />
    {read.data && <>
      {read.data.list.length === 0 ? <p className="notice">{read.data.pagination.total === 0 ? '目前尚無文章。' : '此頁沒有文章。'}</p> : <div className="article-list">{read.data.list.map(
        // ID 是後端資源穩定鍵，跨頁更新不依賴陣列位置。
        (item) => <article className="article-row" key={item.id}>
          <h2><a href={`/react/articles/${item.id}`}>{item.title}</a></h2>
          <p className="excerpt">{item.body}</p><Metadata item={item} />
        </article>,
      )}</div>}
      <Pagination pagination={read.data.pagination} base="/react/" />
    </>}
  </section>
}

// 文章本體與留言各自有載入／錯誤狀態；留言只提供唯讀內容。
function Article({ backend, credential, enabled, onAuthFailure, switchEpoch }) {
  const article = useRead(backend, credential, `/api/articles/${articleId}`, null, enabled, onAuthFailure, switchEpoch)
  const [commentPage, setCommentPage] = useState(listPage)
  const comments = useRead(backend, credential, `/api/articles/${articleId}/comments?page=${commentPage}&per_page=20`, commentPage, enabled && Boolean(article.data), onAuthFailure, switchEpoch, 'comment')
  // 留言分頁只影響留言讀取，不搬移文章或其他列表狀態。
  function previousComments() { setCommentPage(commentPage - 1) }
  // 下一頁由後端 total_pages 決定，不猜測更多資料。
  function nextComments() { setCommentPage(commentPage + 1) }
  // 合法超頁返回第一頁，保留 API 實際總筆數的判斷。
  function firstComments() { setCommentPage(1) }
  return <section className="content reading">
    <a className="back" href="/react/">← 返回文章列表</a>
    {article.loading && <p role="status">正在載入文章…</p>}
    <ErrorNotice error={article.error} retry={article.retry} />
    {article.data && <>
      <article><p className="eyebrow">ARTICLE / {article.data.id}</p><h1>{article.data.title}</h1><Metadata item={article.data} /><div className="prose">{article.data.body}</div></article>
      <section className="comments" aria-labelledby="comments-title"><h2 id="comments-title">留言</h2>
        {comments.loading && <p role="status">正在載入留言…</p>}
        <ErrorNotice error={comments.error} retry={comments.retry} />
        {comments.data && <>
          {/* 指定超頁先顯示此頁無資料，避免把第 99 頁誤說成首頁尚無留言。 */}
          {comments.data.list.length === 0 ? <p className="notice">{emptyCommentMessage(comments.data.pagination)}</p> : comments.data.list.map(
            // 使用留言 ID 保留穩定身份，全文依純文字呈現。
            (item) => <article className="comment" key={item.id}><Metadata item={item} /><p className="prose">{item.body}</p></article>,
          )}
          {showCommentPagination(comments.data.pagination) && <nav className="pagination" aria-label="留言分頁"><span>第 {commentPage} 頁／共 {comments.data.pagination.total_pages} 頁</span>{commentPage > 1 && <button className="text-button" onClick={previousComments}>上一頁</button>}{commentPage < comments.data.pagination.total_pages && <button className="text-button" onClick={nextComments}>下一頁</button>}{commentPage > comments.data.pagination.total_pages && <button className="text-button" onClick={firstComments}>返回第一頁</button>}</nav>}
        </>}
      </section>
    </>}
  </section>
}

// 帳號表單只在本機記憶體保存輸入；送出時先檢查欄位再呼叫目前後端。
function AccountForm({ mode, backend, onLogin, onDraftChange, switchEpoch }) {
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
    if (deadline && !remaining) { setDeadline(0); setUnlocked(true) }
  }, [deadline, remaining])
  // 元件切走時使舊帳號回應失效，並終止仍在途的請求。
  useEffect(() => {
    // 清理屬於這次表單的請求，避免切換後重新顯示舊鎖定。
    return () => { latest.current++; controller.current?.abort(); onDraftChange(false, false) }
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
    const checked = validateAccount({ email, password, nickname }, register)
    setErrors(checked.errors)
    if (Object.keys(checked.errors).length) { setNotice({ message: '請修正標示欄位後再送出。' }); return }
    const sequence = ++latest.current
    const startingEpoch = switchEpoch.current
    const attempt = { email: checked.email, password, ...(register ? { nickname } : {}) }
    const abort = new AbortController()
    controller.current = abort
    setPending(true)
    onDraftChange(true, true)
    setNotice(null)
    try {
      const { status, body, retryAfter } = await request(backend, `/api/auth/${register ? 'register' : 'login'}`, { method: 'POST', signal: abort.signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(attempt) })
      // 已確認切換會同步增加世代；卸載前抵達的舊成功也不得存 JWT 或導覽。
      if (!ownsAttempt(abort.signal, sequence, latest.current, startingEpoch, switchEpoch.current)) return
      if (register && status === 201 && body?.success === true && body.message === 'OK' && body.data && !Array.isArray(body.data) && Object.keys(body.data).length === 0) { window.location.assign('/react/login?registered=1'); return }
      if (!register && status === 200 && body?.success === true && body.message === 'OK' && body.data?.token_type === 'Bearer' && validAccount(body.data.user) && saveSession(sessionStorage, backend, body.data.access_token, body.data.expires_at)) { setPending(false); onDraftChange(false, false); onLogin(); window.location.assign(returnTo); return }
      const problem = failure(body)
      if (register && (status >= 500 || status === 201)) problem.message += '。無法確認註冊結果；請先確認帳號狀態，再決定是否重試。'
      setErrors(problem.errors)
      if (!register && status === 429 && problem.code === 'E0105') {
        const seconds = retryAfterSeconds(retryAfter)
        if (seconds !== null) { setNow(Date.now()); setDeadline(Date.now() + seconds * 1000); setUnlocked(false) }
        else console.warn('Login lockout response has invalid Retry-After header')
      } else if (problem.code === 'E0102') setDeadline(0)
      setNotice(problem)
    } catch {
      if (ownsAttempt(abort.signal, sequence, latest.current, startingEpoch, switchEpoch.current)) setNotice({ message: register ? '無法確認註冊結果；請先確認帳號狀態，再決定是否重試。' : '無法確認登入結果；請稍後手動重試。' })
    } finally { if (ownsAttempt(abort.signal, sequence, latest.current, startingEpoch, switchEpoch.current)) { setPending(false); onDraftChange(true, false) } }
  }

  return <section className="account"><p className="eyebrow">{register ? 'CREATE ACCOUNT' : 'MEMBER ACCESS'}</p><h1>{register ? '註冊會員' : '會員登入'}</h1><p className="intro">{register ? '建立帳號後，請再登入。' : '登入後可跨前端保留同一次登入期限。'}</p>
    {!register && params.get('registered') === '1' && <p className="notice success">註冊完成，請登入。</p>}
    <ErrorNotice error={notice} focusRef={summary} />
    {locked && <p className="lock-message">登入暫時鎖定，約剩 {formatCountdown(remaining)}</p>}
    {unlocked && !locked && <p className="notice" role="status">可再次嘗試登入</p>}
    <form noValidate onSubmit={submit}>
      <label htmlFor="email">Email</label><input id="email" type="email" autoComplete="email" value={email} disabled={pending || locked} aria-invalid={Boolean(errors.email)} aria-describedby={errors.email ? 'email-error' : undefined} onChange={editEmail} />{errors.email && <p className="field-error" id="email-error">{errors.email.join('；')}</p>}
      {register && <><label htmlFor="nickname">暱稱</label><input id="nickname" value={nickname} disabled={pending} aria-invalid={Boolean(errors.nickname)} aria-describedby={errors.nickname ? 'nickname-error' : undefined} onChange={editNickname} />{errors.nickname && <p className="field-error" id="nickname-error">{errors.nickname.join('；')}</p>}</>}
      <label htmlFor="password">密碼</label><input id="password" type="password" autoComplete={register ? 'new-password' : 'current-password'} value={password} disabled={pending || locked} aria-invalid={Boolean(errors.password)} aria-describedby={errors.password ? 'password-error' : undefined} onChange={editPassword} />{errors.password && <p className="field-error" id="password-error">{errors.password.join('；')}</p>}
      <button className="primary" type="submit" disabled={pending || locked}>{pending ? '處理中…' : register ? '建立帳號' : '登入'}</button>
    </form><p className="account-link">{register ? <>已有帳號？ <a href="/react/login">前往登入</a></> : <>還沒有帳號？ <a href="/react/register">註冊會員</a></>}</p>
  </section>
}

// 工具列保持目前版本與後端可見，切換只導覽同源已知路徑。
function Header({ backend, onBackend, account, authStatus, credential, onLogout, draft, switchEpoch }) {
  const [busy, setBusy] = useState(false)
  const [logoutError, setLogoutError] = useState(null)
  const [front, setFront] = useState('react')
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const logoutController = useRef(null)
  const navToggle = useRef(null)
  // 工具列在後端變更時重建，舊登出請求不得撤銷新後端畫面狀態。
  useEffect(() => {
    // 離開舊後端或頁面時取消仍在途的登出請求。
    return () => logoutController.current?.abort()
  }, [])
  const name = Object.values(BACKENDS).find(
    // 固定表只用於顯示，不將使用者輸入拼成 API URL。
    (entry) => entry[1] === backend,
  )?.[0]

  // 有未送出文字或結果未定的寫入時，讓使用者決定留在表單或捨棄離開。
  function mayLeave() {
    return !draft.dirty || window.confirm(draft.pending ? '請求已送出，切換後仍可能完成。要捨棄目前表單並切換嗎？' : '目前表單有未儲存文字。要捨棄並切換嗎？')
  }

  // 目前前端已知頁面轉成同語意網址；Nuxt 不支援某些後端時保持選單原值。
  function switchFrontend(event) {
    const next = event.target.value
    if (next === 'react') return
    if (!mayLeave()) { event.target.value = 'react'; return }
    const target = frontendTarget(pathname, next, returnTo)
    if (target) {
      // 在導覽卸載前立即讓帳號及登出舊回應失去寫入資格。
      switchEpoch.current++
      logoutController.current?.abort()
      window.location.assign(target)
    }
    else setFront('react')
  }

  // 後端變更只清除本次 JWT 並重開本頁；受保護目標需要重新登入。
  function switchBackend(event) {
    if (!mayLeave()) { event.target.value = backend; return }
    if (onBackend(event.target.value)) logoutController.current?.abort()
    else event.target.value = backend
  }

  // 窄螢幕按鈕展開或收合導覽，當前前後端仍由固定工具列顯示。
  function toggleMobileNav() { setMobileNavOpen(!mobileNavOpen) }
  // 關閉後把焦點送回觸發按鈕，避免焦點停在被隱藏的連結上。
  function closeMobileNav() { setMobileNavOpen(false); navToggle.current?.focus() }
  // Escape 可從導覽任何位置離開，鍵盤不必倒退穿過所有連結。
  function mobileNavKeyDown(event) { if (event.key === 'Escape') closeMobileNav() }

  // 只有伺服器確認撤銷才回報登出；連線失敗保留 JWT 供手動重試。
  async function logout() {
    if (!credential || busy) return
    setBusy(true)
    setLogoutError(null)
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    logoutController.current = controller
    try {
      const { status, body } = await request(backend, '/api/auth/logout', { method: 'POST', signal: controller.signal, headers: { Authorization: `Bearer ${credential.accessToken}` } })
      // 舊登出縱使伺服器已完成，也不可清除另一個前端剛保存的 JWT。
      if (controller.signal.aborted || startedEpoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential)) return
      if (status === 200 && body?.success === true && body.message === 'OK' && body.data && !Array.isArray(body.data) && Object.keys(body.data).length === 0) onLogout(null, backend, credential)
      else if (body?.error_code === 'E0002' || body?.error_code === 'E0102') onLogout(failure(body), backend, credential)
      else setLogoutError(failure(body))
    } catch { if (!controller.signal.aborted && startedEpoch === switchEpoch.current && ownsSession(sessionStorage, backend, credential)) setLogoutError({ message: '登出尚未確認；請稍後手動重試。' }) }
    finally { if (!controller.signal.aborted && startedEpoch === switchEpoch.current) setBusy(false) }
  }

  return <><header className="site-header"><a className="brand" href="/react/">文章集<span> / REACT</span></a><div className="current">目前前端　<strong>React＋Vite</strong><br />目前後端　<strong>{name}</strong></div><details className="switches"><summary>切換版本</summary><div className="switch-panel"><label htmlFor="frontend">目前前端</label><select id="frontend" value={front} onChange={switchFrontend}>{Object.entries(FRONTENDS).map(
    // 固定六版仍列在選單；尚未完成版本禁止誤導為可操作頁面。
    ([id, label]) => <option key={id} value={id} disabled={['next', 'angular', 'sveltekit'].includes(id) || id === 'nuxt' && !Object.values(BACKENDS).slice(0, 4).some(
      // Nuxt 現階段只支援四個已驗證後端，其他來源不強制改選。
      (entry) => entry[1] === backend,
    )}>{label}{['next', 'angular', 'sveltekit'].includes(id) ? '（尚未提供）' : ''}</option>,
  )}</select><label htmlFor="backend">目前後端</label><select id="backend" value={backend} onChange={switchBackend}>{Object.entries(BACKENDS).map(
    // 十二個後端共享 API 路徑，只需更換固定基底。
    ([id, [label, url]]) => <option key={id} value={url}>{label}</option>,
  )}</select><p className="switch-help">切換後端會清除登入，請重新登入。{!Object.values(BACKENDS).slice(0, 4).some(
    // 提醒目前後端無法在 Nuxt 延續，避免選單停用而無說明。
    (entry) => entry[1] === backend,
  ) && '目前後端尚未支援 Nuxt。'}</p></div></details></header>
    <button className="mobile-nav-toggle" ref={navToggle} type="button" aria-controls="react-main-nav" aria-expanded={mobileNavOpen} onClick={toggleMobileNav}>{mobileNavOpen ? '關閉導覽' : '開啟導覽'}</button>
    <nav id="react-main-nav" className={`site-nav${mobileNavOpen ? ' open' : ''}`} aria-label="主導覽" onKeyDown={mobileNavKeyDown}><a href="/react/" aria-current={page === 'articles' || page === 'article' ? 'page' : undefined}>所有文章</a>{account ? <><span className="account-name">{account.nickname}</span><button className="nav-button" type="button" disabled={busy} onClick={logout}>{busy ? '登出中…' : '登出'}</button></> : authStatus === 'checking' ? <span>驗證登入中…</span> : <><a href="/react/login" aria-current={page === 'login' ? 'page' : undefined}>登入</a><a href="/react/register" aria-current={page === 'register' ? 'page' : undefined}>註冊</a></>}<button className="nav-close" type="button" onClick={closeMobileNav}>關閉導覽</button></nav>
    {logoutError && <div className="shell-error"><ErrorNotice error={logoutError} /></div>}
  </>
}

// 入口只信任本分頁 schema，重新載入時請目前後端 /api/me 確認帳號。
export default function App() {
  // 初始值只讀同分頁儲存，避免渲染過程反覆解析已損壞的 session。
  const [backend, setBackend] = useState(() => readBackend(sessionStorage))
  // 跨前端僅傳 JWT、後端與原期限；帳號資料必須重新驗證。
  const [credential, setCredential] = useState(() => readSession(sessionStorage))
  const [account, setAccount] = useState(null)
  const [authStatus, setAuthStatus] = useState(credential ? 'checking' : 'guest')
  const [authError, setAuthError] = useState(null)
  const [draft, setDraft] = useState({ dirty: false, pending: false })
  const switchEpoch = useRef(0)

  // BFCache 凍結期間不容許舊回應更新畫面；返回時重載並向目前後端重驗 JWT。
  useEffect(() => {
    // 真正離頁才撤銷請求世代，取消切換仍保有目前表單的回應資格。
    function onPageHide() { switchEpoch.current++ }
    // BFCache 會保留舊 React state，必須從共用儲存重新初始化。
    function onPageShow(event) { if (event.persisted) window.location.reload() }
    window.addEventListener('pagehide', onPageHide)
    window.addEventListener('pageshow', onPageShow)
    // 卸載時移除監聽，避免重複註冊造成多次重載。
    return () => { window.removeEventListener('pagehide', onPageHide); window.removeEventListener('pageshow', onPageShow) }
  }, [])

  // 依目前後端驗證本分頁憑證，並清理切換前的請求。
  useEffect(() => {
    if (!credential) {
      setAccount(null)
      setAuthStatus(
        // 失效／停用提示保留到使用者處理，避免清理憑證後被誤寫成訪客。
        (current) => current === 'expired' || current === 'disabled' ? current : 'guest',
      )
      return undefined
    }
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    setAuthStatus('checking')
    // 回應需同時屬於目前頁面世代及儲存的同一份憑證。
    const ownsCurrent = () => !controller.signal.aborted && startedEpoch === switchEpoch.current && ownsSession(sessionStorage, backend, credential)
    // 重驗不改寫 token 或 expiresAt；舊後端回應在切換時中止。
    request(backend, '/api/me', { signal: controller.signal, headers: { Authorization: `Bearer ${credential.accessToken}` } }).then(
      // 只接受目前憑證對應的有效 Account。
      ({ status, body }) => {
        if (!ownsCurrent()) return
        if (status === 200 && body?.success === true && body.message === 'OK' && validAccount(body.data)) { setAccount(body.data); setAuthError(null); setAuthStatus('ready'); return }
        const problem = failure(body)
        if (problem.code === 'E0002' || problem.code === 'E0102') { if (!clearSessionIfOwned(sessionStorage, backend, credential)) return; setCredential(null); setAccount(null); setAuthStatus(problem.code === 'E0102' ? 'disabled' : 'expired') }
        else setAuthStatus('unverified')
        setAuthError(problem)
      },
      // 暫時故障不清除憑證，亦不先顯示受保護帳號資料。
      () => { if (ownsCurrent()) { setAuthStatus('unverified'); setAuthError({ message: '目前無法驗證登入；請稍後重新載入。' }) } },
    )
    // 憑證或後端變更時舊 me 結果沒有權力更新新身份。
    return () => controller.abort()
  }, [backend, credential])

  // 後端變更立即移除舊憑證與帳號，並讓依賴它的請求取消。
  function changeBackend(next) {
    if (next === backend) return false
    const hadCredential = Boolean(credential)
    try { if (!selectBackend(sessionStorage, next)) return false } catch { setAuthError({ message: '無法儲存後端選擇，請檢查瀏覽器設定。' }); return false }
    // 儲存後端選擇成功才同步撤銷舊表單回應的擁有權；取消切換則保留。
    switchEpoch.current++
    setBackend(next); setCredential(null); setAccount(null); setAuthStatus('guest'); setAuthError({ message: '已切換後端，請重新登入。' })
    if (hadCredential && page !== 'login' && page !== 'register') window.location.assign(`/react/login?return_to=${encodeURIComponent(page === 'article' ? pathname : '/react/')}`)
    return true
  }

  // 新 JWT 一保存就替換本頁請求身份，舊 me 不可再覆寫，並重新驗證帳號。
  function acceptLogin() {
    // 即使後端重簽得到相同字串，較早請求也不得繼承這次登入的寫入資格。
    switchEpoch.current++
    const current = readSession(sessionStorage)
    setCredential(current); setAccount(null); setAuthStatus(current ? 'checking' : 'guest'); setAuthError(null)
  }

  // 登出成功或已知失效才清除憑證，保留目前後端選擇。
  function completeLogout(problem, sourceBackend, sourceCredential) {
    if (!clearSessionIfOwned(sessionStorage, sourceBackend, sourceCredential)) return
    setCredential(null); setAccount(null); setAuthStatus(problem?.code === 'E0102' ? 'disabled' : 'guest'); setAuthError(problem || { message: '已登出。' })
  }

  // 公開讀取若遇憑證失效也立即停止授權狀態，不降級為訪客後暗中重試。
  const invalidateSession = useCallback((problem, sourceBackend, sourceCredential) => {
    if (!clearSessionIfOwned(sessionStorage, sourceBackend, sourceCredential)) return
    setCredential(null); setAccount(null)
    setAuthStatus(problem.code === 'E0102' ? 'disabled' : 'expired'); setAuthError(problem)
  }, [])

  // 只追蹤目前帳號表單是否有草稿或在途寫入，供前後端切換確認。
  const changeDraft = useCallback((dirty, pending) => { setDraft({ dirty, pending }) }, [])

  // 只有使用者主動點擊才重驗暫時故障的 me，不背景無限重試。
  function reloadPage() { window.location.reload() }

  const enabled = authStatus === 'guest' || authStatus === 'ready'
  return <div className="app-shell"><Header key={backend} backend={backend} onBackend={changeBackend} account={account} authStatus={authStatus} credential={credential} onLogout={completeLogout} draft={draft} switchEpoch={switchEpoch} /><main>
    {authError && <div className="auth-notice"><ErrorNotice error={authError} />{authStatus === 'expired' && <a href={`/react/login?return_to=${encodeURIComponent(page === 'article' ? pathname : '/react/')}`}>重新登入</a>}{authStatus === 'unverified' && <button className="secondary" type="button" onClick={reloadPage}>重新驗證</button>}</div>}
    {page === 'articles' && (authStatus === 'checking' ? <p role="status">正在驗證登入…</p> : <Articles key={backend} backend={backend} credential={credential} enabled={enabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />)}
    {page === 'article' && (authStatus === 'checking' ? <p role="status">正在驗證登入…</p> : <Article key={backend} backend={backend} credential={credential} enabled={enabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />)}
    {(page === 'login' || page === 'register') && <AccountForm key={`${page}-${backend}`} mode={page} backend={backend} onLogin={acceptLogin} onDraftChange={changeDraft} switchEpoch={switchEpoch} />}
    {page === 'missing' && <section className="content"><h1>找不到頁面</h1><p>這個 React 頁面目前不存在。</p><a href="/react/">返回文章列表</a></section>}
  </main><footer>文章集　·　React＋Vite　·　公開閱讀與帳號頁</footer></div>
}
