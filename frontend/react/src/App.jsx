import { useCallback, useEffect, useRef, useState } from 'react'
import { BACKENDS, FRONTENDS, canEditContent, canHideContent, canRestoreContent, clearSessionIfOwned, emptyCommentMessage, failure, formatCountdown, frontendTarget, ownsAttempt, ownsRead, ownsSession, positiveId, readBackend, readSession, remainingSeconds, request, retryAfterSeconds, saveSession, selectBackend, showCommentPagination, supportsAngularRoute, supportsNextRoute, validAccount, validContent, validList, validLockout, validMember, validReturn, validateAccount, validateContent, validateLockout } from './contracts.js'

const pathname = window.location.pathname
const params = new URLSearchParams(window.location.search)
const articleMatch = pathname.match(/^\/react\/articles\/([0-9]+)$/)
const articleId = articleMatch && positiveId(articleMatch[1]) !== null ? articleMatch[1] : null
const articleEditMatch = pathname.match(/^\/react\/articles\/([0-9]+)\/edit$/)
const editingArticleId = articleEditMatch && positiveId(articleEditMatch[1]) !== null ? articleEditMatch[1] : null
const commentMatch = pathname.match(/^\/react\/comments\/([0-9]+)$/)
const commentId = commentMatch && positiveId(commentMatch[1]) !== null ? commentMatch[1] : null
const commentEditMatch = pathname.match(/^\/react\/comments\/([0-9]+)\/edit$/)
const editingCommentId = commentEditMatch && positiveId(commentEditMatch[1]) !== null ? commentEditMatch[1] : null
const memberMatch = pathname.match(/^\/react\/admin\/members\/([0-9]+)$/)
const memberId = memberMatch && positiveId(memberMatch[1]) !== null ? memberMatch[1] : null
const page = pathname === '/react/' ? 'articles' : pathname === '/react/login' ? 'login' : pathname === '/react/register' ? 'register' : pathname === '/react/articles/new' ? 'article-new' : pathname === '/react/me/articles' ? 'my-articles' : pathname === '/react/me/comments' ? 'my-comments' : pathname === '/react/admin/members' ? 'members' : memberId ? 'member' : pathname === '/react/admin/articles' ? 'managed-articles' : pathname === '/react/admin/comments' ? 'managed-comments' : pathname === '/react/admin/settings/login-lockout' ? 'lockout-settings' : articleId ? 'article' : editingArticleId ? 'article-edit' : commentId ? 'comment' : editingCommentId ? 'comment-edit' : 'missing'
const rawPage = params.get('page')
const listPage = rawPage && /^[1-9][0-9]*$/.test(rawPage) && Number.isSafeInteger(Number(rawPage)) ? Number(rawPage) : 1
const rawReturn = params.get('return_to')
const returnTo = validReturn(rawReturn) ? rawReturn : '/react/'

// 選定本頁標題以支援直接開啟深層網址，其他路徑明示不存在。
document.title = { articles: '文章集｜所有文章', article: '文章集｜閱讀文章', 'article-new': '文章集｜發表文章', 'article-edit': '文章集｜編輯文章', 'my-articles': '文章集｜我的文章', 'my-comments': '文章集｜我的留言', comment: '文章集｜留言詳情', 'comment-edit': '文章集｜編輯留言', members: '文章集｜會員管理', member: '文章集｜會員詳情', 'managed-articles': '文章集｜文章管理', 'managed-comments': '文章集｜留言管理', 'lockout-settings': '文章集｜登入鎖定設定', register: '文章集｜註冊會員', login: '文章集｜會員登入', missing: '文章集｜找不到頁面' }[page]

// API 失敗保持欄位錯誤與追蹤編號，避免使用者只能看到泛用失敗訊息。
function ErrorNotice({ error, retry, focusRef }) {
  if (!error) return null
  // 可聚焦摘要讓鍵盤使用者取得錯誤，欄位連結可直接回到需修正的輸入。
  function focusField(event) { document.getElementById(event.currentTarget.dataset.field)?.focus() }
  return <div className="notice error" role="alert" tabIndex={-1} ref={focusRef}>
    <strong>{error.message}</strong>
    {error.errors && Object.entries(error.errors).length > 0 && <ul className="error-fields">{Object.entries(error.errors).map(
      // 已知欄位可直接聚焦；未知欄位仍完整呈現，避免遺失後端明細。
      ([name, messages]) => <li key={name}>{['email', 'password', 'nickname', 'title', 'body', 'enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(name) ? <button type="button" className="text-button" data-field={name} onClick={focusField}>{name}：{messages.join('；')}</button> : `${name}：${messages.join('；')}`}</li>,
    )}</ul>}
    {error.logId && <p>追蹤編號：<span className="break">{error.logId}</span> <button type="button" className="text-button" onClick={copyLogId}>複製</button></p>}
    {retry && <button className="secondary" type="button" onClick={retry}>重新讀取</button>}
  </div>
  // 使用者可複製後端追蹤編號回報問題，複製失敗仍保留可選取原文。
  async function copyLogId() { try { await navigator.clipboard?.writeText(error.logId) } catch { /* 保留可選取的原文供手動複製。 */ } }
}

// 每次後端、憑證或分頁變動都取消舊讀取，避免過時資料覆寫新選擇。
function useRead(backend, credential, path, currentPage, enabled, onAuthFailure, switchEpoch, kind = 'article', expectedId = articleId) {
  const [result, setResult] = useState({ loading: true, data: null, error: null })
  const [revision, setRevision] = useState(0)
  // 結果附原請求身分，渲染時立即隔離切換前的私有資料，不等 effect 清理。
  const owner = { backend, credential, path, currentPage, enabled, kind, expectedId, revision }
  const current = ownsRead(result, owner)
  // 每個讀取生命週期只擁有自己的一次請求，依賴變動時清理。
  useEffect(() => {
    if (!enabled) { setResult({ ...owner, loading: false, data: null, error: null }); return undefined }
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    setResult({ ...owner, loading: true, data: null, error: null })
    // 公開 API 若帶 JWT 就必須驗證；此處不在錯誤後靜默改以訪客重試。
    request(backend, path, { signal: controller.signal, headers: credential ? { Authorization: `Bearer ${credential.accessToken}` } : {} })
      .then(
        // 只接受本次路徑預期的成功 envelope；不完整資料不顯示為空列表。
        ({ status, body }) => {
          if (controller.signal.aborted || startedEpoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential)) return
          if (status === 200 && body?.success === true && body.message === 'OK' && (currentPage ? validList(body.data, currentPage, kind) : kind === 'settings' ? validLockout(body.data) : body.data?.id === Number(expectedId) && (kind === 'member' ? validMember(body.data) : validContent(body.data, kind)))) setResult({ ...owner, loading: false, data: body.data, error: null })
          else {
            const problem = failure(body)
            if (credential && (problem.code === 'E0002' || problem.code === 'E0102')) onAuthFailure(problem, backend, credential)
            setResult({ ...owner, loading: false, data: null, error: problem })
          }
        },
        // 網路中斷保留登入狀態並提供手動重讀。
        () => { if (!controller.signal.aborted && startedEpoch === switchEpoch.current && ownsSession(sessionStorage, backend, credential)) setResult({ ...owner, loading: false, data: null, error: { message: '暫時無法連線，請稍後手動重試。' } }) },
      )
    // 來源變動或元件離開時中止舊請求，避免舊回應落到新畫面。
    return () => controller.abort()
  }, [backend, credential, path, currentPage, enabled, revision, onAuthFailure, switchEpoch, kind, expectedId])
  // 手動重讀保留相同路徑，只增加請求世代；連線錯誤不自動重送。
  function retry() {
    // 同步清空舊資料，衝突比較不會把上一版誤當成剛讀回的新版本。
    setResult({ ...owner, loading: true, data: null, error: null })
    setRevision(
      // 使用最新世代，連續點擊也不會因過時閉包漏掉重讀。
      (value) => value + 1,
    )
  }
  return { loading: current ? result.loading : enabled, data: current ? result.data : null, error: current ? result.error : null, retry }
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

// 每次寫入只接受原 JWT 與原頁面世代的回應；未知結果須先查證再由人決定。
function useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange) {
  const [state, setState] = useState({ pending: false, error: null, uncertain: false })
  const active = useRef(null)
  const pendingRef = useRef(false)
  // 卸載時撤銷 UI 更新資格；伺服器可能已收到寫入，不能當作未送出。
  useEffect(() => {
    // 卸載的舊元件只中止自己的請求，不更新即將重建的畫面。
    return () => { active.current?.controller.abort(); active.current = null; pendingRef.current = false }
  }, [])
  // 只替目前這筆寫入收尾；舊 POST 不得清除新 POST 的等待狀態。
  const markUnknown = useCallback((operation) => {
    if (active.current !== operation) return false
    active.current = null
    pendingRef.current = false
    setState({ pending: false, error: { message: '結果尚未確認；請先查證資料，再決定是否手動重試。' }, uncertain: true })
    onDraftChange(true, false)
    return true
  }, [onDraftChange])
  // GET 先回 401 或重新登入會換掉原憑證；立即結束舊寫入等待並保留查證入口。
  useEffect(() => {
    const operation = active.current
    if (operation && (operation.backend !== backend || operation.credential !== credential || operation.epoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential))) {
      operation.controller.abort()
      markUnknown(operation)
    }
  }, [backend, credential, switchEpoch, markUnknown])
  // 呼叫者只有收到完整成功 envelope 才能導覽或重讀，其他結果保留表單。
  async function send(path, method, payload, successStatus) {
    if (!credential || pendingRef.current || state.uncertain) return { ok: false }
    pendingRef.current = true
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    const operation = { controller, backend, credential, epoch: startedEpoch }
    active.current = operation
    setState({ pending: true, error: null, uncertain: false })
    try {
      const { status, body } = await request(backend, path, { method, signal: controller.signal, headers: { Authorization: `Bearer ${credential.accessToken}`, 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      // 已撤銷的原請求即使晚到 2xx，也不能當作新登入後的寫入成功。
      if (active.current !== operation) return { ok: false, uncertain: true, obsolete: true }
      if (controller.signal.aborted || startedEpoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential)) { markUnknown(operation); return { ok: false, uncertain: true, obsolete: true } }
      if (status === successStatus && body?.success === true && body.message === 'OK' && body.data && !Array.isArray(body.data) && Object.keys(body.data).length === 0) { setState({ pending: false, error: null, uncertain: false }); return { ok: true } }
      const problem = failure(body)
      if (problem.code === 'E0002' || problem.code === 'E0102') onAuthFailure(problem, backend, credential)
      // 已發送的 5xx 或損壞成功回應不證明未寫入；鎖住重送直到手動查證。
      const uncertain = status >= 500 || status === successStatus || !body || body.success !== false
      setState({ pending: false, error: problem, uncertain })
      return { ok: false, problem, uncertain }
    } catch {
      const settled = markUnknown(operation)
      return { ok: false, uncertain: true, obsolete: !settled }
    } finally { if (active.current === operation) { active.current = null; pendingRef.current = false } }
  }
  // 只有使用者查證後才能解鎖未知結果；重讀本身不會重送寫入。
  const checked = useCallback(() => { setState({ pending: false, error: null, uncertain: false }) }, [])
  return { ...state, send, checked }
}

// 下架／恢復共用版本比對與查證規則；權限提示只供 UI，後端再次判定。
function VisibilityActions({ item, kind, account, backend, credential, onAuthFailure, switchEpoch, onRefresh, enabled, onDraftChange }) {
  const write = useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange)
  const [conflict, setConflict] = useState(false)
  // 離頁撤銷目前操作的離頁標記，不影響同頁留言草稿。
  useEffect(() => () => onDraftChange(false, false), [onDraftChange])
  const canHide = canHideContent(item, account)
  const canRestore = canRestoreContent(item, account)
  // 操作前再次確認動作，避免無意下架；成功後僅採信重新讀取的狀態。
  async function change(visibility) {
    if (!enabled || conflict || write.uncertain || !(visibility === 'hidden' ? canHide : canRestore)) return
    const action = visibility === 'hidden' ? item.visibility === 'hidden' ? '再次下架並接管恢復權' : '下架' : '恢復'
    if (!window.confirm(`確定要${action}嗎？`)) return
    onDraftChange(true, true)
    const result = await write.send(`/api/${kind}s/${item.id}/visibility`, 'PUT', { visibility, version: item.version }, 200)
    if (result.obsolete) return
    onDraftChange(Boolean(result.uncertain), false)
    if (result.ok) onRefresh()
    else if (result.problem?.code === 'E0201') setConflict(true)
    else if (result.problem?.code === 'E0004') onRefresh()
  }
  // 父層重讀會卸載本元件；只在同 ID 詳情重新驗證成功後顯示新操作。
  function checkLatest() { onRefresh() }
  // 明確按鈕避免匿名事件處理器缺乏用途說明。
  function hide() { change('hidden') }
  // 恢復操作由最新 can_restore 及後端再次判斷。
  function restore() { change('visible') }
  return <div className="content-actions">
    {canHide && <button type="button" className="secondary" disabled={!enabled || write.pending || write.uncertain || conflict} onClick={hide}>{item.visibility === 'hidden' ? '再次下架並接管恢復權' : '下架'}</button>}
    {canRestore && <button type="button" className="secondary" disabled={!enabled || write.pending || write.uncertain || conflict} onClick={restore}>恢復</button>}
    {item.visibility === 'hidden' && !canRestore && <p>目前無恢復資格。</p>}
    {write.pending && <p role="status">正在處理狀態…</p>}
    <ErrorNotice error={write.error} />
    {(conflict || write.uncertain) && <div className="notice"><p>{conflict ? '內容版本已變更；請讀取最新狀態並重新確認操作。' : '結果尚未確認；請先查證目前狀態。'}</p><button type="button" className="secondary" onClick={checkLatest}>讀取最新狀態</button></div>}
  </div>
}

// 我的內容以既有完整資源列表呈現，隱藏項也保留詳情入口供重新驗權。
function MyContent({ kind, backend, credential, enabled, onAuthFailure, switchEpoch }) {
  const isArticle = kind === 'article'
  const path = isArticle ? '/me/articles' : '/me/comments'
  const read = useRead(backend, credential, `/api${path}?page=${listPage}&per_page=20`, listPage, enabled, onAuthFailure, switchEpoch, kind)
  return <section className="content"><p className="eyebrow">MY CONTENT / REACT</p><h1>{isArticle ? '我的文章' : '我的留言'}</h1>
    {read.loading && enabled && <p role="status">正在載入…</p>}<ErrorNotice error={read.error} retry={read.retry} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{read.data.pagination.total === 0 ? '目前尚無內容。' : '此頁沒有資料。'}</p> : <div className="article-list">{read.data.list.map(
      // 穩定 ID 對應個別內容，留言的 article_id 不授予父文章讀取權。
      (item) => <article className="article-row" key={item.id}><h2><a href={`/react/${isArticle ? 'articles' : 'comments'}/${item.id}`}>{isArticle ? item.title : `留言 #${item.id}`}</a></h2><p className="excerpt">{item.body}</p><Metadata item={item} /><p>{item.visibility === 'hidden' ? '已下架' : '公開'}</p></article>,
    )}</div>}<Pagination pagination={read.data.pagination} base={`/react${path}`} /></>}
  </section>
}

// 發文與編修共用純文字表單；草稿只留在本頁記憶體，不隨切換傳送。
function AuthorEditor({ kind, resourceId, backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange }) {
  const isArticle = kind === 'article'
  const creating = !resourceId
  const resourcePath = isArticle ? `/api/articles/${resourceId}` : `/api/comments/${resourceId}`
  const original = useRef(null)
  const verifiedCredential = useRef(null)
  const [blocked, setBlocked] = useState(false)
  const read = useRead(backend, credential, resourcePath, null, enabled && !creating, onAuthFailure, switchEpoch, kind, resourceId)
  // 拒絕讀取時隱藏舊快照；表單文字仍留在本頁記憶體供原帳號處理。
  const unreadable = ['E0003', 'E0004', 'E0102'].includes(read.error?.code)
  const sourceItem = read.data || (!unreadable && !blocked ? original.current : null)
  const parent = useRead(backend, credential, `/api/articles/${sourceItem?.article_id || 0}`, null, enabled && !isArticle && Boolean(sourceItem), onAuthFailure, switchEpoch, 'article', sourceItem?.article_id)
  const write = useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange)
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [version, setVersion] = useState(null)
  const [errors, setErrors] = useState({})
  const [notice, setNotice] = useState(null)
  const [conflict, setConflict] = useState(false)
  const [checked, setChecked] = useState(false)
  const [checkingUnknown, setCheckingUnknown] = useState(false)
  const initial = useRef(false)
  const summary = useRef(null)
  const verification = useRead(backend, credential, '/api/me/articles?page=1&per_page=20', 1, enabled && creating && checkingUnknown, onAuthFailure, switchEpoch)
  // 第一次讀取填入原文；後續重讀只供比較，不能覆蓋使用者草稿。
  useEffect(() => {
    if (read.data) verifiedCredential.current = credential
    if (read.data && !initial.current) { original.current = read.data; if (isArticle) setTitle(read.data.title); setBody(read.data.body); setVersion(read.data.version); initial.current = true }
    if (read.data && blocked) setBlocked(false)
  }, [read.data, isArticle, credential, blocked])
  // 錯誤摘要取得焦點，讓鍵盤使用者能立即前往欄位修正。
  useEffect(() => { if (notice) summary.current?.focus() }, [notice])
  // 卸載後清除本頁草稿標記，不讓其他頁面誤繼承確認對話。
  useEffect(() => () => onDraftChange(false, false), [onDraftChange])
  const allowed = Boolean(account && !unreadable && !blocked && (creating || canEditContent(sourceItem, account) && (read.data || verifiedCredential.current === credential)))
  const parentVisible = isArticle || parent.data?.visibility === 'visible'
  // 文字變動使舊錯誤失效，但不靜默更換版本或重送內容。
  function editTitle(event) { setTitle(event.target.value); setErrors({}); setNotice(null); onDraftChange(true, false) }
  // 保留 CR/LF 與 Unicode 原文，React 只以文字顯示。
  function editBody(event) { setBody(event.target.value); setErrors({}); setNotice(null); onDraftChange(true, false) }
  // 衝突或未知結果時單獨重讀；成功的最新版本仍須手動採用。
  function checkLatest() { setChecked(true); read.retry(); if (!isArticle) parent.retry() }
  // 新文章沒有 ID；只讀自己的第一頁供人工查證，不能推測哪筆是這次建立。
  function checkCreated() { setCheckingUnknown(true); verification.retry() }
  // 完成可見資料查證後才解除重送鎖，是否再送由使用者決定。
  function acceptCreatedCheck() { write.checked(); setCheckingUnknown(false); setNotice(null) }
  // 看過最新內容才允許用新版本手動提交，不會自動取代草稿。
  function acceptLatest() { if (!read.data || read.loading || read.error) return; setVersion(read.data.version); setConflict(false); setChecked(false); write.checked(); setNotice(null) }
  // 送出前先驗證本地欄位與目前身份；後端仍執行授權及版本原子比對。
  async function submit(event) {
    event.preventDefault()
    if (!enabled || !allowed || !parentVisible || write.pending || write.uncertain || conflict) return
    const input = isArticle ? { title, body } : { body }
    const nextErrors = validateContent(input, kind)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length) { setNotice({ message: '請修正標示欄位後再送出。', errors: nextErrors }); return }
    onDraftChange(true, true)
    const result = await write.send(creating ? '/api/articles' : resourcePath, creating ? 'POST' : 'PUT', creating ? input : { ...input, version }, creating ? 201 : 200)
    if (result.obsolete) return
    if (result.ok) { onDraftChange(false, false); window.location.assign(creating ? '/react/me/articles' : `/react/${isArticle ? 'articles' : 'comments'}/${resourceId}`); return }
    onDraftChange(true, false)
    if (result.problem) { setNotice(result.problem); setErrors(result.problem.errors || {}) }
    if (result.problem?.code === 'E0201') setConflict(true)
    if (result.problem?.code === 'E0003' || result.problem?.code === 'E0004') { setBlocked(true); read.retry(); if (!isArticle) parent.retry() }
  }
  return <section className="content editor"><p className="eyebrow">AUTHOR / REACT</p><h1>{creating ? '發表文章' : isArticle ? '編輯文章' : '編輯留言'}</h1>
    {creating && <p className="intro">文章會公開發表，建立來源由目前後端記錄。</p>}
    {!creating && read.loading && enabled && <p role="status">正在讀取內容…</p>}
    {!creating && <ErrorNotice error={read.error} retry={read.retry} />}
    {!isArticle && sourceItem && <section className="related"><h2>所屬文章</h2>{parent.loading && <p role="status">正在確認文章可見性…</p>}{parent.data && <p><a href={`/react/articles/${parent.data.id}`}>{parent.data.title}</a> · {parent.data.visibility === 'visible' ? '公開' : '已下架'}</p>}{parent.error && <><ErrorNotice error={{ ...parent.error, message: parent.error.code === 'E0004' ? '所屬文章目前無法讀取。' : parent.error.message }} /><button type="button" className="secondary" onClick={parent.retry}>單獨重試文章讀取</button></>}</section>}
    {!creating && read.data && !allowed && <p className="notice error">只有內容作者可編修；管理員自己的內容也依作者規則。</p>}
    {notice && <ErrorNotice error={notice} focusRef={summary} />}
    {(creating || initial.current) && allowed && <>
      {creating ? <p>發表後由目前後端記錄建立來源、作者與時間。</p> : <Metadata item={sourceItem} />}
      {write.uncertain && <div className="notice"><p>結果尚未確認；請先查證資料，不會自動重送。</p><a href={creating ? '/react/me/articles' : `/react/${isArticle ? 'articles' : 'comments'}/${resourceId}`}>前往查證</a>{!creating && <button type="button" className="secondary" onClick={checkLatest}>在此重讀</button>}</div>}
      {creating && write.uncertain && <div className="related"><button type="button" className="secondary" onClick={checkCreated}>查證我的文章第一頁</button>{checkingUnknown && verification.loading && <p role="status">正在讀取我的文章…</p>}{checkingUnknown && <ErrorNotice error={verification.error} retry={verification.retry} />}{checkingUnknown && verification.data && <><p>此列表供人工查證，不能只憑第一頁判定這次是否建立成功。</p><ul>{verification.data.list.map(
        // 顯示資源 ID 與時間協助人工比較，不替使用者推定建立結果。
        (item) => <li key={item.id}><a href={`/react/articles/${item.id}`}>{item.title}</a> · {item.created_at}</li>,
      )}</ul><button type="button" className="secondary" onClick={acceptCreatedCheck}>已查證，允許手動再送</button></>}</div>}
      {conflict && <div className="notice"><p>版本衝突。請先讀取最新內容，比較後自行整合；不會自動重送。</p><button type="button" className="secondary" onClick={checkLatest}>讀取最新版本</button></div>}
      {checked && read.data && !read.loading && !read.error && <div className="comparison"><h2>最新版本（僅供比較）</h2>{isArticle && <p className="prose">{read.data.title}</p>}<p className="prose">{read.data.body}</p><p>版本 {read.data.version}</p><button type="button" className="secondary" onClick={acceptLatest}>已比較，採用最新版本號</button></div>}
      <form noValidate onSubmit={submit}>{isArticle && <><label htmlFor="title">標題</label><input id="title" value={title} onChange={editTitle} disabled={!enabled || write.pending} aria-invalid={Boolean(errors.title)} aria-describedby={errors.title ? 'title-error' : undefined} />{errors.title && <p className="field-error" id="title-error">{errors.title.join('；')}</p>}</>}
        <label htmlFor="body">{isArticle ? '文章內文' : '留言內容'}</label><textarea id="body" rows={10} value={body} onChange={editBody} disabled={!enabled || write.pending} aria-invalid={Boolean(errors.body)} aria-describedby={errors.body ? 'body-error' : undefined} />{errors.body && <p className="field-error" id="body-error">{errors.body.join('；')}</p>}
        {!parentVisible && !isArticle && <p className="notice">確認所屬文章公開後才能送出留言編修。</p>}
        <button className="primary" type="submit" disabled={!enabled || write.pending || write.uncertain || conflict || !parentVisible}>{write.pending ? '處理中…' : creating ? '發表文章' : '儲存修改'}</button>
      </form></>}
    <a className="back" href={creating ? '/react/me/articles' : isArticle ? `/react/articles/${resourceId}` : `/react/comments/${resourceId}`}>返回{creating ? '我的文章' : '詳情'}</a>
  </section>
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

// 留言發表只在已確認文章公開及目前帳號可用時送出，成功後重讀第一頁。
function CommentComposer({ article, backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange, onCreated, onArticleRefresh, commentsRead }) {
  const [body, setBody] = useState('')
  const [notice, setNotice] = useState(null)
  const [errors, setErrors] = useState({})
  const [checkingUnknown, setCheckingUnknown] = useState(false)
  const summary = useRef(null)
  const write = useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange)
  // 只有目前帳號和公開文章都確認後才顯示可送出表單。
  const allowed = Boolean(enabled && account && article.visibility === 'visible')
  // 欄位錯誤時將焦點移至摘要，方便鍵盤使用者修正。
  useEffect(() => { if (notice) summary.current?.focus() }, [notice])
  // 離頁清除本表單的草稿標記，避免阻擋下一頁。
  useEffect(() => () => onDraftChange(false, false), [onDraftChange])
  // 保留純文字原值，僅清理這次輸入的錯誤顯示。
  function edit(event) { setBody(event.target.value); setNotice(null); setErrors({}); onDraftChange(true, false) }
  // 新留言沒有 ID；只讀文章留言第一頁供人工查證，不猜最新留言就是本次新增。
  function checkCreated() { setCheckingUnknown(true); onCreated() }
  // 人工看過重讀結果後才解鎖，原留言文字仍由使用者自行決定是否重送。
  function acceptCreatedCheck() { write.checked(); setCheckingUnknown(false); setNotice(null) }
  // 新留言沒有回傳 ID；成功留在文章頁並從第一頁重讀，不猜新資源。
  async function submit(event) {
    event.preventDefault()
    if (!allowed || write.pending || write.uncertain) return
    const found = validateContent({ body }, 'comment')
    setErrors(found)
    if (Object.keys(found).length) { setNotice({ message: '請修正標示欄位後再送出。', errors: found }); return }
    onDraftChange(true, true)
    const result = await write.send(`/api/articles/${article.id}/comments`, 'POST', { body }, 201)
    if (result.obsolete) return
    if (result.ok) { setBody(''); onDraftChange(false, false); setNotice({ message: '留言已送出，正在重讀第一頁。' }); onCreated(); return }
    onDraftChange(true, false)
    if (result.problem) { setNotice(result.problem); setErrors(result.problem.errors || {}) }
    if (result.problem?.code === 'E0003' || result.problem?.code === 'E0004') onArticleRefresh()
  }
  return <section className="comment-composer" aria-labelledby="comment-compose-title"><h3 id="comment-compose-title">發表留言</h3>
    {article.visibility === 'hidden' ? <p>文章已下架，暫時無法新增留言。</p> : !account ? <p>登入後可以留言。<a href={`/react/login?return_to=${encodeURIComponent(pathname)}`}>前往登入</a></p> : <>
      {notice && <ErrorNotice error={notice} focusRef={summary} />}
      {write.uncertain && <div className="notice">結果尚未確認，請先查看留言第一頁，確認後才決定是否重送。<button type="button" className="secondary" onClick={checkCreated}>重讀留言第一頁</button>{checkingUnknown && commentsRead.data && !commentsRead.loading && !commentsRead.error && <button type="button" className="secondary" onClick={acceptCreatedCheck}>已查證，允許手動再送</button>}</div>}
      <form noValidate onSubmit={submit}><label htmlFor="body">留言內容</label><textarea id="body" rows={5} value={body} onChange={edit} disabled={!allowed || write.pending} aria-invalid={Boolean(errors.body)} aria-describedby={errors.body ? 'body-error' : undefined} />{errors.body && <p id="body-error" className="field-error">{errors.body.join('；')}</p>}<button className="primary" type="submit" disabled={!allowed || write.pending || write.uncertain}>{write.pending ? '送出中…' : '發表留言'}</button></form>
    </>}
  </section>
}

// 文章本體與留言各自有載入／錯誤狀態，作者動作仍以目前後端授權為準。
function Article({ backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange }) {
  const article = useRead(backend, credential, `/api/articles/${articleId}`, null, enabled, onAuthFailure, switchEpoch)
  const [snapshot, setSnapshot] = useState(null)
  const [commentPage, setCommentPage] = useState(listPage)
  const comments = useRead(backend, credential, `/api/articles/${articleId}/comments?page=${commentPage}&per_page=20`, commentPage, enabled && Boolean(article.data), onAuthFailure, switchEpoch, 'comment')
  // 留言草稿與可見性寫入分別追蹤，互不清除對方的離頁保護。
  const changeCommentDraft = useCallback((dirty, pending) => onDraftChange(dirty, pending, 'comment'), [onDraftChange])
  // 短暫的下架請求不會把尚未送出的留言當作已儲存。
  const changeVisibilityDraft = useCallback((dirty, pending) => onDraftChange(dirty, pending, 'visibility'), [onDraftChange])
  // 舊文章只供維持尚未送出的留言元件生命週期；失去授權時不再顯示內容。
  useEffect(() => { if (article.data) setSnapshot(article.data) }, [article.data])
  // 留言分頁只影響留言讀取，不搬移文章或其他列表狀態。
  function previousComments() { setCommentPage(commentPage - 1) }
  // 下一頁由後端 total_pages 決定，不猜測更多資料。
  function nextComments() { setCommentPage(commentPage + 1) }
  // 合法超頁返回第一頁，保留 API 實際總筆數的判斷。
  function firstComments() { setCommentPage(1) }
  // 新留言後先回第一頁再重讀，避免以當前超頁錯認建立失敗。
  function reloadFirstComments() { setCommentPage(1); comments.retry() }
  return <section className="content reading">
    <a className="back" href={account?.role === 'admin' ? '/react/admin/articles' : '/react/'}>← 返回{account?.role === 'admin' ? '文章管理' : '文章列表'}</a>
    {article.loading && <p role="status">正在載入文章…</p>}
    <ErrorNotice error={article.error} retry={article.retry} />
    {article.data && <>
      <article><p className="eyebrow">ARTICLE / {article.data.id}</p><h1>{article.data.title}</h1><Metadata item={article.data} /><p>{article.data.visibility === 'hidden' ? '已下架' : '公開'}</p><div className="prose">{article.data.body}</div></article>
      {canEditContent(article.data, account) && <a className="back" href={`/react/articles/${article.data.id}/edit`}>編輯文章</a>}
      {account && <VisibilityActions item={article.data} kind="article" account={account} backend={backend} credential={credential} onAuthFailure={onAuthFailure} switchEpoch={switchEpoch} onRefresh={article.retry} enabled={enabled} onDraftChange={changeVisibilityDraft} />}
      <section className="comments" aria-labelledby="comments-title"><h2 id="comments-title">留言</h2>
        {comments.loading && <p role="status">正在載入留言…</p>}
        <ErrorNotice error={comments.error} retry={comments.retry} />
        {comments.data && <>
          {/* 指定超頁先顯示此頁無資料，避免把第 99 頁誤說成首頁尚無留言。 */}
          {comments.data.list.length === 0 ? <p className="notice">{emptyCommentMessage(comments.data.pagination)}</p> : comments.data.list.map(
            // 使用留言 ID 保留穩定身份，全文依純文字呈現。
            (item) => <article className="comment" key={item.id}><Metadata item={item} /><p className="prose">{item.body}</p>{account && (account.id === item.author.id || account.role === 'admin') && <a href={`/react/comments/${item.id}`}>留言詳情</a>}</article>,
          )}
          {showCommentPagination(comments.data.pagination) && <nav className="pagination" aria-label="留言分頁"><span>第 {commentPage} 頁／共 {comments.data.pagination.total_pages} 頁</span>{commentPage > 1 && <button className="text-button" onClick={previousComments}>上一頁</button>}{commentPage < comments.data.pagination.total_pages && <button className="text-button" onClick={nextComments}>下一頁</button>}{commentPage > comments.data.pagination.total_pages && <button className="text-button" onClick={firstComments}>返回第一頁</button>}</nav>}
        </>}
      </section>
    </>}
    {snapshot && <div hidden={!article.data}><CommentComposer article={article.data || snapshot} backend={backend} credential={credential} account={account} enabled={enabled && Boolean(article.data)} onAuthFailure={onAuthFailure} switchEpoch={switchEpoch} onDraftChange={changeCommentDraft} onCreated={reloadFirstComments} onArticleRefresh={article.retry} commentsRead={comments} /></div>}
  </section>
}

// 留言詳情先依留言權限讀取；父文章獨立讀取，404 不推測其隱藏原因。
function CommentDetail({ backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange }) {
  const comment = useRead(backend, credential, `/api/comments/${commentId}`, null, enabled, onAuthFailure, switchEpoch, 'comment', commentId)
  const parent = useRead(backend, credential, `/api/articles/${comment.data?.article_id || 0}`, null, enabled && Boolean(comment.data), onAuthFailure, switchEpoch, 'article', comment.data?.article_id)
  return <section className="content reading"><a className="back" href={account?.role === 'admin' ? '/react/admin/comments' : '/react/me/comments'}>← 返回{account?.role === 'admin' ? '留言管理' : '我的留言'}</a><p className="eyebrow">COMMENT / REACT</p><h1>留言詳情</h1>
    {comment.loading && enabled && <p role="status">正在讀取留言…</p>}<ErrorNotice error={comment.error} retry={comment.retry} />
    {comment.data && <><article><Metadata item={comment.data} /><p>{comment.data.visibility === 'hidden' ? '已下架' : '公開'}</p><div className="prose">{comment.data.body}</div></article>
      <section className="related"><h2>所屬文章</h2>{parent.loading && <p role="status">正在讀取所屬文章…</p>}{parent.data && <p><a href={`/react/articles/${parent.data.id}`}>{parent.data.title}</a> · {parent.data.visibility === 'hidden' ? '已下架' : '公開'}</p>}{parent.error && <><ErrorNotice error={{ ...parent.error, message: parent.error.code === 'E0004' ? '所屬文章目前無法讀取。' : parent.error.message }} /><button type="button" className="secondary" onClick={parent.retry}>單獨重試文章讀取</button></>}</section>
      {canEditContent(comment.data, account, parent.data?.visibility === 'visible') && <a className="back" href={`/react/comments/${comment.data.id}/edit`}>編輯留言</a>}
      {account && <VisibilityActions item={comment.data} kind="comment" account={account} backend={backend} credential={credential} onAuthFailure={onAuthFailure} switchEpoch={switchEpoch} onRefresh={comment.retry} enabled={enabled} onDraftChange={onDraftChange} />}
    </>}
  </section>
}

// 管理頁在 /api/me 確認角色前不送私有請求，也不呈現上次登入的資料。
function AdminGate({ status, account }) {
  return <section className="content"><h1>管理介面</h1>{status === 'ready' && account?.role !== 'admin' ? <p className="notice error" role="alert">沒有權限使用此功能（403）。</p> : status === 'checking' ? <p role="status">正在驗證管理員身分…</p> : status === 'unverified' ? <p>目前無法驗證登入，請使用上方重新驗證。</p> : status === 'disabled' ? <p>此帳號已停用，請聯絡系統管理員。</p> : <p>請在原頁重新登入管理員帳號。</p>}</section>
}

// 會員列表只送已核准的字面 keyword 與分頁參數；網址只保存本頁搜尋與頁碼。
function Members({ backend, credential, enabled, status, account, onAuthFailure, switchEpoch }) {
  const search = params.get('filters[keyword]') || ''
  const [keyword, setKeyword] = useState(search)
  const [keywordError, setKeywordError] = useState(null)
  const ownerAccount = useRef(null)
  const accountChanged = enabled && ownerAccount.current && ownerAccount.current !== account?.id
  const query = search ? `&filters%5Bkeyword%5D=${encodeURIComponent(search)}` : ''
  const read = useRead(backend, credential, `/api/members?page=${listPage}&per_page=20${query}`, listPage, enabled && !accountChanged, onAuthFailure, switchEpoch, 'member')
  // 換成其他帳號時先隱藏舊搜尋，清除網址後才以新身分讀取。
  useEffect(() => { if (!enabled || !account) return; if (accountChanged) { window.location.replace('/react/admin/members'); return } ownerAccount.current = account.id }, [enabled, account, accountChanged])
  // 搜尋只提交本頁 keyword；重新開啟第一頁，讓返回與刷新還原對應查詢。
  function submit(event) {
    event.preventDefault()
    const value = keyword.replace(/^ +| +$/g, '')
    if ([...value].length > 254) { setKeywordError('關鍵字最多 254 個字。'); return }
    const target = new URL('/react/admin/members', window.location.origin)
    target.searchParams.set('page', '1')
    if (value) target.searchParams.set('filters[keyword]', value)
    window.location.assign(target.pathname + target.search)
  }
  if (!enabled) return <AdminGate status={status} account={account} />
  if (accountChanged) return <section className="content"><p role="status">正在載入會員管理…</p></section>
  const pagination = read.data?.pagination
  return <section className="content admin-page"><p className="eyebrow">ADMIN / MEMBERS</p><h1>會員管理</h1><p className="intro">查詢一般會員並管理帳號狀態。</p>
    <form className="admin-toolbar" onSubmit={submit}><label htmlFor="member-keyword">暱稱或 Email 關鍵字</label><input id="member-keyword" value={keyword} onChange={editKeyword} aria-invalid={Boolean(keywordError)} aria-describedby={keywordError ? 'member-keyword-error' : undefined} /><button type="submit" className="secondary">搜尋</button>{keywordError && <p className="field-error" id="member-keyword-error">{keywordError}</p>}</form>
    {read.loading && <p role="status">正在載入會員…</p>}<ErrorNotice error={read.error} retry={read.retry} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{listPage > 1 || pagination.total > 0 ? '此頁沒有會員。' : search ? '找不到符合關鍵字的會員。' : '目前尚無會員。'}</p> : <div className="admin-list">{read.data.list.map(
      // 每筆會員使用後端 ID 作穩定鍵，狀態以當次讀取為準。
      (item) => <article className="admin-row" key={item.id}><div><span className="admin-label">會員</span><h2><a href={`/react/admin/members/${item.id}`}>{item.nickname}</a></h2></div><p><span className="admin-label">Email</span>{item.email}</p><p><span className="admin-label">狀態</span>{item.status === 'active' ? '啟用' : '停用'}</p><p><span className="admin-label">建立時間</span>{item.created_at}</p></article>,
    )}</div>}
      {(pagination.total_pages > 1 || listPage > 1) && <nav className="pagination" aria-label="會員分頁"><span>第 {listPage} 頁／共 {pagination.total_pages} 頁，{pagination.total} 筆</span>{listPage > 1 && <a href={`/react/admin/members?page=${listPage - 1}${query}`}>上一頁</a>}{listPage < pagination.total_pages && <a href={`/react/admin/members?page=${listPage + 1}${query}`}>下一頁</a>}{listPage > pagination.total_pages && <a href={`/react/admin/members?page=1${query}`}>返回第一頁</a>}</nav>}</>}
  </section>
  // 搜尋輸入不在每個按鍵發出 API 請求，亦保留原字供使用者修正。
  function editKeyword(event) { setKeyword(event.target.value); setKeywordError(null) }
}

// 會員狀態更新後只採信重新 GET 的結果；未知結果須由管理員手動查證。
function Member({ backend, credential, enabled, status, account, onAuthFailure, switchEpoch, onDraftChange }) {
  const read = useRead(backend, credential, `/api/members/${memberId}`, null, enabled, onAuthFailure, switchEpoch, 'member', memberId)
  const write = useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange)
  const [checking, setChecking] = useState(false)
  const [saved, setSaved] = useState(false)
  // 已完成的伺服器重讀才解除成功後的等待；未知結果仍由人決定。
  useEffect(() => { if (saved && read.data) { setSaved(false); onDraftChange(false, false) } }, [saved, read.data, onDraftChange])
  // 離頁時清掉本管理操作的保護標記。
  useEffect(() => () => onDraftChange(false, false), [onDraftChange])
  // 只對目前讀到的一般會員提出狀態變更，後端仍重驗目標與權限。
  async function change() {
    if (!enabled || !read.data || write.pending || write.uncertain || checking) return
    const next = read.data.status === 'active' ? 'disabled' : 'active'
    if (!window.confirm(`確定要${next === 'disabled' ? '停用' : '恢復'}此會員嗎？`)) return
    onDraftChange(true, true)
    const result = await write.send(`/api/members/${memberId}/status`, 'PUT', { status: next }, 200)
    if (result.obsolete) return
    // 已知成功仍等同一會員 GET 完成，才解除離頁保護。
    onDraftChange(Boolean(result.uncertain || result.ok), false)
    if (result.ok) { setSaved(true); read.retry() }
    else if (result.problem?.code === 'E0004') read.retry()
  }
  // 未知結果只重讀會員，不會再次送出 PUT。
  function checkLatest() { setChecking(true); read.retry() }
  // 看到同一會員最新狀態後才解除未知寫入的重送鎖。
  function finishCheck() { if (!read.data || read.loading || read.error) return; write.checked(); setChecking(false); onDraftChange(false, false) }
  if (!enabled) return <AdminGate status={status} account={account} />
  return <section className="content admin-page"><a className="back" href="/react/admin/members">← 返回會員管理</a><p className="eyebrow">ADMIN / MEMBER</p><h1>會員詳情</h1>
    {read.loading && <p role="status">正在讀取會員…</p>}<ErrorNotice error={read.error} retry={read.retry} />
    {read.data && <div className="admin-detail"><p><strong>暱稱</strong>{read.data.nickname}</p><p><strong>Email</strong>{read.data.email}</p><p><strong>狀態</strong>{read.data.status === 'active' ? '啟用' : '停用'}</p><p><strong>建立時間</strong>{read.data.created_at}</p><button type="button" className="secondary" onClick={change} disabled={write.pending || write.uncertain || checking || saved}>{read.data.status === 'active' ? '停用會員' : '恢復會員'}</button></div>}
    {write.pending && <p role="status">正在更新狀態…</p>}{saved && <p role="status">已提交，正在重讀會員狀態…</p>}<ErrorNotice error={write.error} />
    {write.uncertain && <div className="notice"><p>寫入結果尚未確認。請重讀目前狀態，再決定是否手動操作。</p><button type="button" className="secondary" onClick={checkLatest}>讀取最新狀態</button>{checking && read.data && !read.loading && !read.error && <button type="button" className="secondary" onClick={finishCheck}>已查證狀態</button>}</div>}
  </section>
}

// 管理內容清單沿用完整資源和既有詳情／可見性操作，不提供他人內文編修。
function ManagedContent({ kind, backend, credential, enabled, status, account, onAuthFailure, switchEpoch }) {
  const isArticle = kind === 'article'
  const path = isArticle ? `/api/articles?page=${listPage}&per_page=20&filters%5Bscope%5D=management` : `/api/comments?page=${listPage}&per_page=20`
  const read = useRead(backend, credential, path, listPage, enabled, onAuthFailure, switchEpoch, kind)
  if (!enabled) return <AdminGate status={status} account={account} />
  return <section className="content admin-page"><p className="eyebrow">ADMIN / {isArticle ? 'ARTICLES' : 'COMMENTS'}</p><h1>{isArticle ? '文章管理' : '留言管理'}</h1>
    {read.loading && <p role="status">正在載入內容…</p>}<ErrorNotice error={read.error} retry={read.retry} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{listPage > 1 || read.data.pagination.total > 0 ? '此頁沒有資料。' : '目前尚無內容。'}</p> : <div className="admin-list">{read.data.list.map(
      // 詳情會再次驗權並讀取最新版；清單只提供入口與當次狀態。
      (item) => <article className="admin-row" key={item.id}><div><span className="admin-label">{isArticle ? '文章' : '留言'}</span><h2><a href={`/react/${isArticle ? 'articles' : 'comments'}/${item.id}`}>{isArticle ? item.title : `留言 #${item.id}`}</a></h2></div><p className="excerpt">{item.body}</p><p><span className="admin-label">作者</span>{item.author.nickname}</p><p><span className="admin-label">狀態</span>{item.visibility === 'visible' ? '公開' : '已下架'}</p><p><span className="admin-label">建立來源</span>{BACKENDS[item.created_backend]?.[0]}</p></article>,
    )}</div>}<Pagination pagination={read.data.pagination} base={`/react/admin/${isArticle ? 'articles' : 'comments'}`} /></>}
  </section>
}

// 設定保留本頁草稿；成功後重讀四欄，未知寫入只允許人工讀回確認。
function LockoutSettings({ backend, credential, enabled, status, account, onAuthFailure, switchEpoch, onDraftChange }) {
  const ownerAccount = useRef(null)
  const accountChanged = enabled && ownerAccount.current && ownerAccount.current !== account?.id
  const read = useRead(backend, credential, '/api/settings/login-lockout', null, enabled && !accountChanged, onAuthFailure, switchEpoch, 'settings')
  const write = useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange)
  const [draft, setDraft] = useState(null)
  const [errors, setErrors] = useState({})
  const [notice, setNotice] = useState(null)
  const [saved, setSaved] = useState(false)
  const [checking, setChecking] = useState(false)
  const summary = useRef(null)
  // 管理員身分實際改變時清掉舊帳號草稿；同帳號重新登入則保留。
  useEffect(() => {
    if (!enabled || !account) return
    if (accountChanged) { setDraft(null); setErrors({}); setNotice(null); onDraftChange(false, false) }
    ownerAccount.current = account.id
  }, [enabled, account, accountChanged, onDraftChange])
  // 初次 GET 或成功寫入後的 GET 才能更新表單；一般重讀不覆寫本地草稿。
  useEffect(() => {
    if (!read.data) return
    if (!draft || saved) {
      setDraft({ enabled: read.data.enabled, window_minutes: String(read.data.window_minutes), failure_threshold: String(read.data.failure_threshold), lock_seconds: String(read.data.lock_seconds) })
      if (saved) { setSaved(false); setErrors({}); setNotice({ message: '設定已儲存，並以伺服器重讀結果更新。' }); onDraftChange(false, false) }
    }
  }, [read.data, draft, saved, onDraftChange])
  // 驗證摘要出現時給鍵盤焦點，修正後再儲存不保留舊錯誤。
  useEffect(() => { if (notice?.errors) summary.current?.focus() }, [notice])
  // 離頁解除本設定頁的草稿標記。
  useEffect(() => () => onDraftChange(false, false), [onDraftChange])
  // 布林開關與數值欄位只更新本地草稿，失效登入時仍不外送。
  function edit(event) { const { name, type, checked, value } = event.target; setDraft({ ...draft, [name]: type === 'checkbox' ? checked : value }); setErrors({}); setNotice(null); onDraftChange(true, false) }
  // 本地邊界先提示；後端仍完整驗證並原子更新四欄。
  async function submit(event) {
    event.preventDefault()
    if (!enabled || !draft || write.pending || write.uncertain || saved || checking) return
    const found = validateLockout(draft)
    setErrors(found)
    if (Object.keys(found).length) { setNotice({ message: '請修正標示欄位後再送出。', errors: found }); return }
    onDraftChange(true, true)
    const payload = { enabled: draft.enabled, window_minutes: Number(draft.window_minutes), failure_threshold: Number(draft.failure_threshold), lock_seconds: Number(draft.lock_seconds) }
    const result = await write.send('/api/settings/login-lockout', 'PUT', payload, 200)
    if (result.obsolete) return
    onDraftChange(true, false)
    if (result.ok) { setSaved(true); read.retry() }
    else if (result.problem) { setErrors(result.problem.errors || {}); setNotice(result.problem) }
  }
  // 寫入結果不明時只讀伺服器目前完整設定，不自動再次提交。
  function checkLatest() { setChecking(true); read.retry() }
  // 顯示伺服器四欄後才讓管理員自行選擇是否重新送出保留草稿。
  function finishCheck() { if (!read.data || read.loading || read.error) return; write.checked(); setChecking(false); setNotice(null) }
  if (!enabled) return <AdminGate status={status} account={account} />
  if (accountChanged) return <section className="content"><p role="status">正在載入管理設定…</p></section>
  return <section className="content admin-page settings"><p className="eyebrow">ADMIN / SECURITY</p><h1>登入鎖定設定</h1><p className="intro">四項設定一併儲存，請以重讀結果確認生效值。</p>
    {read.loading && <p role="status">正在讀取設定…</p>}<ErrorNotice error={read.error} retry={read.retry} />
    {draft && <form noValidate onSubmit={submit}>
      {notice && <ErrorNotice error={notice} focusRef={summary} />}
      <label className="check-row" htmlFor="enabled"><input id="enabled" name="enabled" type="checkbox" checked={draft.enabled} onChange={edit} disabled={write.pending || saved} />啟用會員登入錯誤鎖定</label>
      {[["window_minutes", "累計失敗時間（分鐘）", 60], ["failure_threshold", "觸發鎖定失敗次數", 100], ["lock_seconds", "鎖定時間（秒）", 86400]].map(
        // 各欄保留可見標籤與錯誤，手機也能逐欄操作。
        ([name, label, max]) => <div className="setting-field" key={name}><label htmlFor={name}>{label}</label><input id={name} name={name} type="number" min="1" max={max} step="1" inputMode="numeric" value={draft[name]} onChange={edit} disabled={write.pending || saved} aria-invalid={Boolean(errors[name])} aria-describedby={errors[name] ? `${name}-error` : undefined} />{errors[name] && <p className="field-error" id={`${name}-error`}>{errors[name].join('；')}</p>}</div>,
      )}
      <button className="primary" type="submit" disabled={write.pending || write.uncertain || saved || checking}>{write.pending ? '儲存中…' : '儲存設定'}</button>
    </form>}
    {saved && <p role="status">已提交，正在重讀設定…</p>}{write.uncertain && write.error && !notice && <ErrorNotice error={write.error} />}
    {write.uncertain && <div className="notice"><p>寫入結果尚未確認。草稿已保留；請先讀取目前伺服器設定，人工比較後再決定是否送出。</p><button type="button" className="secondary" onClick={checkLatest}>讀取目前設定</button>{checking && read.data && !read.loading && !read.error && <><div className="admin-detail"><p>伺服器目前值：{read.data.enabled ? '啟用' : '停用'}；{read.data.window_minutes} 分鐘；{read.data.failure_threshold} 次；{read.data.lock_seconds} 秒。</p></div><button type="button" className="secondary" onClick={finishCheck}>已比較，允許手動再送</button></>}</div>}
  </section>
}

// 帳號表單只在本機記憶體保存輸入；送出時先檢查欄位再呼叫目前後端。
function AccountForm({ mode, backend, onLogin, onDraftChange, switchEpoch, inline = false, expectedOwnerId = null }) {
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
      if (!register && status === 200 && body?.success === true && body.message === 'OK' && body.data?.token_type === 'Bearer' && validAccount(body.data.user)) {
        // 原頁恢復只接受原作者；其他帳號不能接收舊草稿或沿用編修版本。
        if (inline && expectedOwnerId && body.data.user.id !== expectedOwnerId) { setNotice({ message: '登入帳號與草稿作者不同；請使用原帳號重新登入，或離開並捨棄草稿。' }); return }
        if (saveSession(sessionStorage, backend, body.data.access_token, body.data.expires_at)) { setPending(false); onDraftChange(false, false); onLogin(); if (!inline) window.location.assign(returnTo); return }
      }
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

  return <section className="account"><p className="eyebrow">{register ? 'CREATE ACCOUNT' : 'MEMBER ACCESS'}</p><h1>{register ? '註冊會員' : inline ? '在原頁重新登入' : '會員登入'}</h1><p className="intro">{register ? '建立帳號後，請再登入。' : inline ? '登入成功後會留在目前頁面；若有保留的草稿，請確認後手動送出。' : '登入後可跨前端保留同一次登入期限。'}</p>
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
function Header({ backend, onBackend, account, authStatus, credential, onLogout, draft, switchEpoch, approvedNavigation }) {
  const [busy, setBusy] = useState(false)
  const [logoutError, setLogoutError] = useState(null)
  const [front, setFront] = useState('react')
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const logoutController = useRef(null)
  const navToggle = useRef(null)
  // 目前頁面若無 Next 對應路由，保留選項可見但不可切換。
  const nextReady = supportsNextRoute(pathname)
  // Angular 已提供全部語意頁；只啟用目標版確實可承接的合法路由。
  const angularReady = supportsAngularRoute(pathname)
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

  // 目前前端已完成頁面轉成同語意網址；未完成目標路由保持選單原值。
  function switchFrontend(event) {
    const next = event.target.value
    if (next === 'react') return
    if (next === 'angular' && !angularReady) { event.target.value = 'react'; return }
    if (!mayLeave()) { event.target.value = 'react'; return }
    const target = frontendTarget(pathname, next, returnTo)
    if (target) {
      // 在導覽卸載前立即讓帳號及登出舊回應失去寫入資格。
      approvedNavigation.current = true
      switchEpoch.current++
      logoutController.current?.abort()
      window.location.assign(target)
    }
    else setFront('react')
  }

  // 後端變更只清除本次 JWT 並重開本頁；受保護目標需要重新登入。
  function switchBackend(event) {
    if (!mayLeave()) { event.target.value = backend; return }
    approvedNavigation.current = true
    if (onBackend(event.target.value)) logoutController.current?.abort()
    else { approvedNavigation.current = false; event.target.value = backend }
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
    if (draft.dirty && !window.confirm(draft.pending ? '請求已送出，登出後仍可能完成。要捨棄目前表單並登出嗎？' : '目前表單有未儲存文字。要捨棄並登出嗎？')) return
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
    // 六版入口保持可見；只啟用目前頁面在目標版已完成且可用的選項。
    ([id, label]) => {
      const unavailable = id === 'sveltekit' || id === 'angular' && !angularReady || id === 'next' && !nextReady
      const nuxtDisabled = id === 'nuxt' && !Object.values(BACKENDS).slice(0, 4).some(
        // Nuxt 現階段只支援四個已驗證後端，其他來源不強制改選。
        (entry) => entry[1] === backend,
      )
      return <option key={id} value={id} disabled={unavailable || nuxtDisabled}>{label}{(id === 'next' && !nextReady || id === 'angular' && !angularReady) ? '（此頁尚未提供）' : unavailable ? '（尚未提供）' : ''}</option>
    },
  )}</select><label htmlFor="backend">目前後端</label><select id="backend" value={backend} onChange={switchBackend}>{Object.entries(BACKENDS).map(
    // 十二個後端共享 API 路徑，只需更換固定基底。
    ([id, [label, url]]) => <option key={id} value={url}>{label}</option>,
  )}</select><p className="switch-help">切換後端會清除登入，請重新登入。{!Object.values(BACKENDS).slice(0, 4).some(
    // 提醒目前後端無法在 Nuxt 延續，避免選單停用而無說明。
    (entry) => entry[1] === backend,
  ) && '目前後端尚未支援 Nuxt。'}</p></div></details></header>
    <button className="mobile-nav-toggle" ref={navToggle} type="button" aria-controls="react-main-nav" aria-expanded={mobileNavOpen} onClick={toggleMobileNav}>{mobileNavOpen ? '關閉導覽' : '開啟導覽'}</button>
    <nav id="react-main-nav" className={`site-nav${mobileNavOpen ? ' open' : ''}`} aria-label="主導覽" onKeyDown={mobileNavKeyDown}><a href="/react/" aria-current={page === 'articles' || page === 'article' ? 'page' : undefined}>所有文章</a>{account ? <><a href="/react/articles/new" aria-current={page === 'article-new' ? 'page' : undefined}>發表文章</a><a href="/react/me/articles" aria-current={page === 'my-articles' ? 'page' : undefined}>我的文章</a><a href="/react/me/comments" aria-current={page === 'my-comments' ? 'page' : undefined}>我的留言</a>{account.role === 'admin' && <details className="admin-nav" open={page.startsWith('managed-') || ['members', 'member', 'lockout-settings'].includes(page)}><summary>管理介面</summary><a href="/react/admin/members" aria-current={page === 'members' || page === 'member' ? 'page' : undefined}>會員管理</a><a href="/react/admin/articles" aria-current={page === 'managed-articles' ? 'page' : undefined}>文章管理</a><a href="/react/admin/comments" aria-current={page === 'managed-comments' ? 'page' : undefined}>留言管理</a><a href="/react/admin/settings/login-lockout" aria-current={page === 'lockout-settings' ? 'page' : undefined}>鎖定設定</a></details>}<span className="account-name">{account.nickname}</span><button className="nav-button" type="button" disabled={busy} onClick={logout}>{busy ? '登出中…' : '登出'}</button></> : authStatus === 'checking' ? <span>驗證登入中…</span> : <><a href="/react/login" aria-current={page === 'login' ? 'page' : undefined}>登入</a><a href="/react/register" aria-current={page === 'register' ? 'page' : undefined}>註冊</a></>}<button className="nav-close" type="button" onClick={closeMobileNav}>關閉導覽</button></nav>
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
  const draftRef = useRef({ dirty: false, pending: false })
  const draftSources = useRef(new Map())
  const approvedNavigation = useRef(false)
  const ownerId = useRef(null)
  const switchEpoch = useRef(0)

  // 一般連結、上一頁與重新整理同樣保護本機草稿；已送出的寫入可能仍完成。
  useEffect(() => {
    // beforeunload 的文字由瀏覽器顯示；只有真要離頁才發出提示。
    function confirmUnload(event) { if (draftRef.current.dirty && !approvedNavigation.current) { event.preventDefault(); event.returnValue = '' } }
    window.addEventListener('beforeunload', confirmUnload)
    return () => window.removeEventListener('beforeunload', confirmUnload)
  }, [])

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
        if (status === 200 && body?.success === true && body.message === 'OK' && validAccount(body.data)) {
          // 重登後仍以 /api/me 確認原帳號，避免登入回應與實際 JWT 身分不一致。
          if (ownerId.current && ownerId.current !== body.data.id) { clearSessionIfOwned(sessionStorage, backend, credential); setCredential(null); setAuthStatus('expired'); setAuthError({ message: '登入身分與原草稿作者不同；請使用原帳號重新登入。' }); return }
          ownerId.current = body.data.id; setAccount(body.data); setAuthError(null); setAuthStatus('ready'); return
        }
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
    ownerId.current = null
    setBackend(next); setCredential(null); setAccount(null); setAuthStatus('guest'); setAuthError({ message: '已切換後端，請重新登入。' })
    if (hadCredential && page !== 'login' && page !== 'register') window.location.assign(`/react/login?return_to=${encodeURIComponent(validReturn(pathname) ? pathname : '/react/')}`)
    else approvedNavigation.current = false
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
    // 已確認登出後結束這個帳號的草稿生命週期，不讓新帳號接收舊內容。
    if (draftRef.current.dirty) { changeDraft(false, false); approvedNavigation.current = true; window.location.assign('/react/'); return }
    ownerId.current = null
    setCredential(null); setAccount(null); setAuthStatus(problem?.code === 'E0102' ? 'disabled' : 'guest'); setAuthError(problem || { message: '已登出。' })
  }

  // 公開讀取若遇憑證失效也立即停止授權狀態，不降級為訪客後暗中重試。
  const invalidateSession = useCallback((problem, sourceBackend, sourceCredential) => {
    if (!clearSessionIfOwned(sessionStorage, sourceBackend, sourceCredential)) return
    setCredential(null); setAccount(null)
    setAuthStatus(problem.code === 'E0102' ? 'disabled' : 'expired'); setAuthError(problem)
  }, [])

  // 只追蹤目前帳號表單是否有草稿或在途寫入，供前後端切換確認。
  const changeDraft = useCallback((dirty, pending, source = 'page') => {
    // 文章頁可同時有留言草稿與下架請求；聚合後再更新全頁確認狀態。
    if (dirty || pending) draftSources.current.set(source, { dirty, pending })
    else draftSources.current.delete(source)
    const values = [...draftSources.current.values()]
    draftRef.current = { dirty: values.some((item) => item.dirty), pending: values.some((item) => item.pending) }
    setDraft(draftRef.current)
  }, [])
  // 原頁重登表單不擁有作者草稿；其帳密輸入不得清掉離頁保護。
  const ignoreInlineDraft = useCallback(() => {}, [])

  // 只有使用者主動點擊才重驗暫時故障的 me，不背景無限重試。
  function reloadPage() { window.location.reload() }

  const enabled = authStatus === 'guest' || authStatus === 'ready'
  const authorEnabled = authStatus === 'ready'
  const authorPage = ['article-new', 'article-edit', 'my-articles', 'my-comments', 'comment', 'comment-edit', 'members', 'member', 'managed-articles', 'managed-comments', 'lockout-settings'].includes(page)
  const inlineLogin = (authorPage || page === 'article' && draft.dirty) && (authStatus === 'guest' || authStatus === 'expired')
  const adminEnabled = authStatus === 'ready' && account?.role === 'admin'
  return <div className="app-shell"><Header key={backend} backend={backend} onBackend={changeBackend} account={account} authStatus={authStatus} credential={credential} onLogout={completeLogout} draft={draft} switchEpoch={switchEpoch} approvedNavigation={approvedNavigation} /><main>
    {authError && <div className="auth-notice"><ErrorNotice error={authError} />{authStatus === 'expired' && !inlineLogin && <a href={`/react/login?return_to=${encodeURIComponent(validReturn(pathname) ? pathname : '/react/')}`}>重新登入</a>}{authStatus === 'unverified' && <button className="secondary" type="button" onClick={reloadPage}>重新驗證</button>}</div>}
    {inlineLogin && <AccountForm key={`inline-${backend}`} mode="login" backend={backend} onLogin={acceptLogin} onDraftChange={ignoreInlineDraft} switchEpoch={switchEpoch} inline expectedOwnerId={ownerId.current} />}
    {page === 'articles' && (authStatus === 'checking' ? <p role="status">正在驗證登入…</p> : <Articles key={backend} backend={backend} credential={credential} enabled={enabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />)}
    {page === 'article' && <Article key={backend} backend={backend} credential={credential} account={account} enabled={enabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
    {page === 'article-new' && <AuthorEditor key={backend} kind="article" backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
    {page === 'article-edit' && <AuthorEditor key={backend} kind="article" resourceId={editingArticleId} backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
    {page === 'my-articles' && <MyContent key={backend} kind="article" backend={backend} credential={credential} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
    {page === 'my-comments' && <MyContent key={backend} kind="comment" backend={backend} credential={credential} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
    {page === 'comment' && <CommentDetail key={backend} backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
    {page === 'comment-edit' && <AuthorEditor key={backend} kind="comment" resourceId={editingCommentId} backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
    {page === 'members' && <Members key={backend} backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
    {page === 'member' && <Member key={backend} backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
    {page === 'managed-articles' && <ManagedContent key={backend} kind="article" backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
    {page === 'managed-comments' && <ManagedContent key={backend} kind="comment" backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
    {page === 'lockout-settings' && <LockoutSettings key={backend} backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
    {(page === 'login' || page === 'register') && <AccountForm key={`${page}-${backend}`} mode={page} backend={backend} onLogin={acceptLogin} onDraftChange={changeDraft} switchEpoch={switchEpoch} />}
    {page === 'missing' && <section className="content"><h1>找不到頁面</h1><p>這個 React 頁面目前不存在。</p><a href="/react/">返回文章列表</a></section>}
  </main><footer>文章集　·　React＋Vite　·　公開閱讀、作者與管理頁</footer></div>
}
