'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFailure, apiRequest, BACKENDS, canEditContent, canHideContent, canRestoreContent, canSwitchFrontend, emptySuccess, formatCountdown, FRONTENDS, frontendTarget, loginData, memberKeyword, ownsSession, pageNumber, parseLockoutDraft, positiveId, readBackend, readSession, remainingSeconds, retryAfterSeconds, saveSession, selectBackend, SESSION_KEY, successData, validReturn, validateAccount, validateContent, clearSessionIfOwned } from '../lib/contracts.js'

const CREATED_ARTICLE_NOTICE_KEY = 'articleCollection.next.createdArticleNotice'

// 保留錯誤摘要、欄位導覽與 log_id，讓鍵盤使用者能修正或回報問題。
function ErrorNotice({ error, retry, focusRef }) {
  if (!error) return null
  // 點選欄位明細會移至輸入，不以顏色單獨傳達錯誤。
  function focusField(event) { (document.getElementById(event.currentTarget.dataset.field) || document.getElementById('comment-body'))?.focus() }
  // 複製失敗仍可手動選取原文，不阻斷錯誤處理。
  async function copyLogId() { try { await navigator.clipboard?.writeText(error.logId) } catch { /* 原文仍可選取。 */ } }
  return <div className="notice error" role="alert" tabIndex={-1} ref={focusRef}>
    <strong>{error.message}</strong>
    {error.errors && Object.keys(error.errors).length > 0 && <ul>{Object.entries(error.errors).map(
      // 已知欄位可聚焦，其他伺服器明細仍須完整顯示。
      ([name, messages]) => <li key={name}>{['email', 'password', 'nickname', 'title', 'body', 'keyword', 'enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(name)
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
function Pagination({ pagination, base, fragment = '', query = '' }) {
  if (!pagination || (pagination.total_pages <= 1 && pagination.page <= 1)) return null
  const number = pagination.page
  // 保留目前資源路徑，只更換列表頁碼。
  function href(page) { return `${base}?page=${page}${query ? `&${query}` : ''}${fragment}` }
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

// 每個作者寫入各自追蹤請求與草稿，切換時作廢回應但不聲稱取消伺服器寫入。
function useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, formKey) {
  const [state, setState] = useState({ pending: false, error: null, uncertain: false, conflict: false })
  const request = useRef(null)
  const sequence = useRef(0)
  const failedAuth = useRef(null)
  const stateRef = useRef(state)
  stateRef.current = state
  // 登記每份表單的生命週期，文章頁的留言與可見性操作可同時受離頁保護。
  useEffect(() => {
    const entry = { dirty: false, pending: false, cancel: () => { sequence.current += 1; request.current?.abort() } }
    formNavigation.current.authorForms.set(formKey, entry)
    return () => { entry.cancel(); formNavigation.current.authorForms.delete(formKey) }
  }, [formNavigation, formKey])
  // 只在原帳號取得新憑證後移除舊認證提示；草稿、版本及其他恢復鎖維持原狀。
  useEffect(() => {
    const failed = failedAuth.current
    if (!failed || !account || account.id !== failed.accountId || backend !== failed.backend || !credential
      || credential.accessToken === failed.accessToken || !ownsSession(sessionStorage, backend, credential)) return
    failedAuth.current = null
    setState((current) => ['E0002', 'E0102'].includes(current.error?.code) ? { ...current, error: null } : current)
  }, [account, backend, credential])
  // 同步標記目前草稿，讓選單在 React 下一次 render 前仍能阻止誤切換。
  function markDirty(dirty) {
    const entry = formNavigation.current.authorForms.get(formKey)
    if (entry) entry.dirty = dirty
  }
  // 未知結果鎖住自動重送；明確 409 留待人工讀取最新版後才可解鎖。
  async function send(path, method, body, expectedStatus, fields) {
    if (!backend || !credential || request.current || stateRef.current.uncertain || stateRef.current.conflict) return { obsolete: true }
    const controller = new AbortController()
    const turn = ++sequence.current
    request.current = controller
    const entry = formNavigation.current.authorForms.get(formKey)
    if (entry) entry.pending = true
    setState({ pending: true, error: null, uncertain: false, conflict: false })
    try {
      const response = await apiRequest(backend, path, { method, body, token: credential.accessToken, signal: controller.signal })
      if (controller.signal.aborted || turn !== sequence.current) return { obsolete: true }
      if (!ownsSession(sessionStorage, backend, credential)) {
        // 另一個 401 可能已清除原 JWT；已送出的寫入仍可能完成，必須人工讀回確認。
        const error = { message: '寫入結果尚未確認；請重新登入並讀回查證後，才決定是否手動再送。' }
        setState({ pending: false, error, uncertain: true, conflict: false })
        return { error, uncertain: true }
      }
      if (emptySuccess(response.status, response.body, expectedStatus)) { setState({ pending: false, error: null, uncertain: false, conflict: false }); return { ok: true } }
      const error = apiFailure(response.status, response.body, fields)
      if (response.status === 401 && error.code === 'E0002' || response.status === 403 && error.code === 'E0102') {
        failedAuth.current = { backend, accessToken: credential.accessToken, accountId: account?.id }
        onAuthFailure(backend, credential, error)
      }
      const conflict = response.status === 409 && error.code === 'E0201'
      const uncertain = response.status >= 500 || response.status === expectedStatus
      setState({ pending: false, error, uncertain, conflict })
      return { error, conflict, uncertain }
    } catch {
      if (controller.signal.aborted || turn !== sequence.current) return { obsolete: true }
      const error = { message: !ownsSession(sessionStorage, backend, credential)
        ? '寫入結果尚未確認；請重新登入並讀回查證後，才決定是否手動再送。'
        : '寫入結果尚未確認；請先重讀查證，不會自動重送。' }
      setState({ pending: false, error, uncertain: true, conflict: false })
      return { error, uncertain: true }
    } finally {
      if (request.current === controller) { request.current = null; if (entry) entry.pending = false }
    }
  }
  // 只能在使用者明確比較或查證後解除送出鎖，不自動再次呼叫 API。
  function checked() { setState({ pending: false, error: null, uncertain: false, conflict: false }) }
  // 使用者修改欄位時移除已過期的 422 提示，保留衝突或未知結果鎖。
  function clearError() { setState((current) => ({ ...current, error: null })) }
  return { ...state, markDirty, send, checked, clearError }
}

// 下架與恢復共用版本寫入，衝突或結果不明時要求重新讀取目前資源。
function VisibilityAction({ item, kind, account, backend, credential, onAuthFailure, formNavigation, onRefresh }) {
  const write = useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, `${kind}-visibility-${item.id}`)
  const [readback, setReadback] = useState({ loading: false, data: null, error: null })
  const readbackRequest = useRef(null)
  const canHide = canHideContent(item, account)
  const canRestore = canRestoreContent(item, account)
  // 切頁或切換來源時中止查證，過時回應不能解鎖目前帳號的操作。
  useEffect(() => () => readbackRequest.current?.abort(), [])
  // 後端再次判定權限與版本；成功只讀回已知 ID，絕不猜測更新後內容。
  async function change() {
    if (!canHide && !canRestore || write.pending || write.conflict || write.uncertain) return
    const visibility = canRestore ? 'visible' : 'hidden'
    if (!window.confirm(`確定${visibility === 'hidden' ? '下架' : '恢復'}這筆${kind === 'article' ? '文章' : '留言'}嗎？`)) return
    const result = await write.send(`/api/${kind === 'article' ? 'articles' : 'comments'}/${item.id}/visibility`, 'PUT', { visibility, version: item.version }, 200, ['visibility', 'version'])
    if (result.ok) onRefresh()
  }
  // 衝突與未知結果單獨讀回已知 ID；讀取失敗或換帳號不解除寫入鎖。
  async function check() {
    if (readbackRequest.current || !credential) return
    const controller = new AbortController()
    readbackRequest.current = controller
    setReadback({ loading: true, data: null, error: null })
    try {
      const response = await apiRequest(backend, `/api/${kind === 'article' ? 'articles' : 'comments'}/${item.id}`, { token: credential.accessToken, signal: controller.signal })
      if (controller.signal.aborted || !ownsSession(sessionStorage, backend, credential)) return
      const data = successData(response.status, response.body, kind, item.id)
      if (data) { setReadback({ loading: false, data, error: null }); return }
      const error = apiFailure(response.status, response.body)
      if (response.status === 401 && error.code === 'E0002' || response.status === 403 && error.code === 'E0102') onAuthFailure(backend, credential, error)
      setReadback({ loading: false, data: null, error })
    } catch {
      if (!controller.signal.aborted && ownsSession(sessionStorage, backend, credential)) setReadback({ loading: false, data: null, error: { message: '查證失敗，請確認目前後端後手動重試。' } })
    } finally {
      if (readbackRequest.current === controller) readbackRequest.current = null
    }
  }
  // 看過新版本與可見性後再重讀父頁；下一次操作仍由使用者按下並經後端重驗。
  function acceptReadback() {
    if (!readback.data || readback.loading || readback.error || !ownsSession(sessionStorage, backend, credential)) return
    onRefresh()
  }
  if (!canHide && !canRestore && !write.error) return null
  return <div className="author-actions"><ErrorNotice error={write.error} />
    {(write.conflict || write.uncertain) && <div className="notice"><p>{write.conflict ? '版本已變更，請重讀並重新確認操作。' : '結果尚未確認，請重讀查證目前狀態。'}</p>
      <button className="outline-button" type="button" disabled={readback.loading} onClick={check}>{readback.loading ? '查證中…' : '重讀目前狀態'}</button>
      <ErrorNotice error={readback.error} />
      {readback.data && <div><p>最新版本 {readback.data.version} · {readback.data.visibility === 'hidden' ? '已下架' : '公開'} · {readback.data.can_restore ? '可恢復' : '目前不可恢復'}</p><button className="outline-button" type="button" onClick={acceptReadback}>已比對，使用最新狀態</button></div>}
    </div>}
    {(canHide || canRestore) && <button className="outline-button" type="button" disabled={write.pending || write.conflict || write.uncertain} onClick={change}>{write.pending ? '處理中…' : canRestore ? '恢復' : '下架'}</button>}
  </div>
}

// 私有列表只在目前帳號已向 /api/me 驗證後讀取，包含自己下架內容。
function MyContent({ kind, backend, credential, account, authStatus, ready, page, onAuthFailure }) {
  const article = kind === 'article'
  const route = article ? '/me/articles' : '/me/comments'
  const [createdNotice, setCreatedNotice] = useState(null)
  const read = useApiRead(backend, credential, `/api${route}?page=${page}&per_page=20`, article ? 'articles' : 'my-comments', page, null, ready, onAuthFailure)
  // 驗證中、暫時故障或過期時保留標記；訪客、停權或完成驗證後消費，僅原帳號與後端的第一頁顯示提示。
  useEffect(() => {
    if (!article || ['checking', 'error', 'expired'].includes(authStatus) || (authStatus === 'active' && !ready)) { setCreatedNotice(null); return }
    let marked = null
    try {
      const raw = sessionStorage.getItem(CREATED_ARTICLE_NOTICE_KEY)
      sessionStorage.removeItem(CREATED_ARTICLE_NOTICE_KEY)
      if (raw) marked = JSON.parse(raw)
    } catch { /* 儲存不可讀時不推測曾經成功發文。 */ }
    setCreatedNotice(ready && page === 1 && marked?.backend === backend && marked?.accountId === account?.id ? marked : null)
  }, [article, authStatus, ready, page, backend, account?.id])
  return <div className="editorial-layout"><ArticleRail /><main className="article-column"><div className="section-heading"><h1>{article ? '我的文章' : '我的留言'}</h1></div>
    {ready && createdNotice?.backend === backend && createdNotice?.accountId === account?.id && <p className="notice success" role="status">文章已發表，正在顯示我的文章第一頁。</p>}
    {ready && read.loading && <p role="status" className="notice">正在載入…</p>}<ErrorNotice error={read.error} retry={read.reload} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有內容。' : '目前尚無內容。'}</p> : <ol className="article-list">{read.data.list.map(
      // 以資源 ID 作穩定鍵；留言的 article_id 不代表父文章可讀。
      (item) => <li className="article-item" key={item.id}>
        <span className="item-number">#{item.id}</span>
        <div>
          <h2><a href={`/next/${article ? 'articles' : 'comments'}/${item.id}`}>{article ? item.title : `留言 #${item.id}`}</a></h2>
          <p className="excerpt">{item.body}</p>
          <ArticleMeta item={item} />
          <p>{item.visibility === 'hidden' ? '已下架' : '公開'}</p>
        </div>
      </li>,
    )}</ol>}<Pagination pagination={read.data.pagination} base={`/next${route}`} /></>}
  </main></div>
}

// 發文與編修保持原始草稿；重讀資料只供人工比較，不覆蓋輸入欄位。
function ContentEditor({ kind, id, backend, credential, account, ready, onAuthFailure, formNavigation }) {
  const article = kind === 'article'
  const creating = article && !id
  const resourcePath = `/api/${article ? 'articles' : 'comments'}/${id || 0}`
  const [checking, setChecking] = useState(false)
  const read = useApiRead(backend, credential, resourcePath, kind, id ? positiveId(id) : null, null, ready && !creating, onAuthFailure)
  const parent = useApiRead(backend, credential, `/api/articles/${read.data?.article_id || 0}`, 'article', read.data?.article_id, null, ready && !article && Boolean(read.data), onAuthFailure)
  const verification = useApiRead(backend, credential, '/api/me/articles?page=1&per_page=20', 'articles', 1, null, ready && creating && checking, onAuthFailure)
  const write = useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, `editor-${kind}-${id || 'new'}`)
  const [fields, setFields] = useState({ title: '', body: '' })
  const [version, setVersion] = useState(null)
  const [errors, setErrors] = useState({})
  const owner = useRef(null)
  const initialized = useRef(false)
  const summary = useRef(null)
  // 初讀填入伺服器內容；之後的重讀保留輸入，讓衝突可逐字比較。
  useEffect(() => {
    if (account && owner.current === null && creating) owner.current = account.id
    if (read.data && !initialized.current) { owner.current = read.data.author.id; setFields({ title: read.data.title || '', body: read.data.body }); setVersion(read.data.version); initialized.current = true }
  }, [read.data, account, creating])
  // 錯誤摘要取得鍵盤焦點，欄位連結可前往精確輸入。
  useEffect(() => { if (write.error || Object.keys(errors).length) summary.current?.focus() }, [write.error, errors])
  const sameAccount = Boolean(account && (owner.current === null || account.id === owner.current))
  const allowed = ready && sameAccount && (creating || Boolean(read.data && canEditContent(read.data, account)))
  const parentVisible = article || parent.data?.visibility === 'visible'
  // 保留使用者輸入原文，清除已過期的欄位提示並更新離頁標記。
  function change(event) { setFields((current) => ({ ...current, [event.target.name]: event.target.value })); setErrors({}); write.clearError(); write.markDirty(true) }
  // 人工重讀可處理衝突或未知結果，需明確採用新版本才可再次送出。
  function checkLatest() { setChecking(true); if (creating) verification.reload(); else read.reload(); if (!article) parent.reload() }
  // 最新資料不會覆蓋草稿；按鈕只更新送出用版本。
  function acceptLatest() { if (!read.data || read.loading || read.error) return; setVersion(read.data.version); setChecking(false); write.checked() }
  // 建立沒有回傳 ID，列表只供人工查證，不把第一筆當作剛建立的內容。
  function acceptCreatedCheck() { if (!verification.data || verification.loading || verification.error) return; write.checked(); setChecking(false) }
  // 送出前做共同文字驗證與目前作者檢查，後端仍原子檢查權限及版本。
  async function submit(event) {
    event.preventDefault()
    if (!allowed || !parentVisible || write.pending || write.uncertain || write.conflict) return
    const input = article ? { title: fields.title, body: fields.body } : { body: fields.body }
    const found = validateContent(input, kind)
    setErrors(found)
    if (Object.keys(found).length) return
    const result = await write.send(creating ? '/api/articles' : resourcePath, creating ? 'POST' : 'PUT', creating ? input : { ...input, version }, creating ? 201 : 200, article ? ['title', 'body', 'version'] : ['body', 'version'])
    if (result.ok) {
      write.markDirty(false)
      if (creating) {
        // 只有已確認的 201 可留下單次提示；儲存失敗仍先告知成功，避免重送已建立的文章。
        try { sessionStorage.setItem(CREATED_ARTICLE_NOTICE_KEY, JSON.stringify({ backend, accountId: account.id })) }
        catch { window.alert('文章已發表，接著前往我的文章第一頁。') }
      }
      window.location.assign(creating ? '/next/me/articles' : `/next/${article ? 'articles' : 'comments'}/${id}`)
    }
    if (result.error?.errors) setErrors(result.error.errors)
  }
  return <div className="editorial-layout"><ArticleRail /><main className="article-column editor-page"><a className="back-link" href={creating ? '/next/me/articles' : `/next/${article ? 'articles' : 'comments'}/${id}`}>← 返回{creating ? '我的文章' : '詳情'}</a><div className="section-heading"><h1>{creating ? '發表文章' : article ? '編輯文章' : '編輯留言'}</h1></div>
    {creating && <p>文章發表後立即公開，由目前後端記錄建立來源。</p>}
    {!creating && ready && read.loading && <p role="status" className="notice">正在讀取內容…</p>}{!creating && <ErrorNotice error={read.error} retry={read.reload} />}
    {!article && read.data && <section className="related"><h2>所屬文章</h2>{parent.loading && <p role="status">正在確認文章…</p>}{parent.data && <p><a href={`/next/articles/${parent.data.id}`}>{parent.data.title}</a> · {parent.data.visibility === 'visible' ? '公開' : '已下架'}</p>}{parent.error && <><p className="notice">所屬文章目前無法讀取。</p><button className="outline-button" type="button" onClick={parent.reload}>單獨重試文章讀取</button></>}</section>}
    {account && !sameAccount && <p className="notice error" role="alert">目前登入帳號不同，這份草稿不能交給新帳號。</p>}
    {read.data && sameAccount && !creating && !canEditContent(read.data, account) && <p className="notice error">只有內容作者可編修。</p>}
    {allowed && <><ErrorNotice error={write.error || (Object.keys(errors).length ? { message: '請修正標示欄位後再送出。', errors } : null)} focusRef={summary} />
      {(write.conflict || write.uncertain) && <div className="notice"><p>{write.conflict ? '內容版本已變更，請比較最新內容後自行整合。' : '寫入結果尚未確認，請查證後自行決定是否再送。'}</p><button className="outline-button" type="button" onClick={checkLatest}>{creating ? '查證我的文章第一頁' : '讀取最新內容'}</button></div>}
      {checking && !creating && read.data && !read.loading && !read.error && <div className="comparison"><h2>最新伺服器內容（供比較）</h2>{article && <p className="body-text">{read.data.title}</p>}<p className="body-text">{read.data.body}</p><p>版本 {read.data.version}</p><button className="outline-button" type="button" onClick={acceptLatest}>已比較，採用最新版本號</button></div>}
      {checking && creating && verification.loading && <p role="status" className="notice">正在查證我的文章…</p>}{checking && creating && <ErrorNotice error={verification.error} retry={verification.reload} />}
      {checking && creating && verification.data && !verification.loading && !verification.error && <div className="comparison"><p>請自行核對是否已建立；第一筆不一定是本次文章。</p><ul>{verification.data.list.map(
        // 顯示伺服器資料供核對，不推測本次建立的 ID。
        (item) => <li key={item.id}><a href={`/next/articles/${item.id}`}>{item.title}</a> · {item.created_at}</li>,
      )}</ul><button className="outline-button" type="button" onClick={acceptCreatedCheck}>已查證，允許手動送出</button></div>}
      <form className="account-form" noValidate onSubmit={submit}>{article && <><label htmlFor="title">標題</label><input id="title" name="title" value={fields.title} onChange={change} disabled={write.pending} aria-invalid={Boolean(errors.title)} />{errors.title && <small className="field-error">{errors.title.join('；')}</small>}</>}
        <label htmlFor="body">{article ? '文章內文' : '留言內容'}</label><textarea id="body" name="body" rows={10} value={fields.body} onChange={change} disabled={write.pending} aria-invalid={Boolean(errors.body)} />{errors.body && <small className="field-error">{errors.body.join('；')}</small>}
        {!parentVisible && !article && <p className="notice">確認所屬文章公開後才能送出留言編修。</p>}
        <button className="red-button" type="submit" disabled={write.pending || write.conflict || write.uncertain || !parentVisible}>{write.pending ? '處理中…' : creating ? '發表文章' : '儲存修改'}</button></form></>}
  </main></div>
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
        (article, index) => <li key={article.id} className="article-item">
          <span className="item-number">{String((page - 1) * 20 + index + 1).padStart(2, '0')}</span>
          <div>
            <h2><a href={`/next/articles/${article.id}`}>{article.title}</a></h2>
            <p className="excerpt">{article.body}</p>
            <ArticleMeta item={article} />
          </div>
        </li>,
      )}</ol>}
      <Pagination pagination={read.data.pagination} base="/next/" /></>}
  </section></div>
}

// 留言與文章分開載入，避免任一讀取失敗被誤當成沒有留言。
function CommentList({ backend, credential, account, ready, articleId, page, onAuthFailure, onRead, firstPage = 0 }) {
  const read = useApiRead(backend, credential, `/api/articles/${articleId}/comments?page=${page}&per_page=20`, 'comments', page, articleId, ready, onAuthFailure)
  // 留言建立或人工查證後，重讀目前第一頁而不猜本次新 ID。
  useEffect(() => { if (firstPage) read.reload() }, [firstPage])
  // 將讀取狀態交給同頁表單，未知寫入結果只能在列表完成後人工解鎖。
  useEffect(() => { onRead?.(read) }, [read.data, read.loading, read.error])
  return <section className="comments" id="comments" aria-labelledby="comments-title"><div className="subsection-heading"><h2 id="comments-title">留言</h2></div>
    {read.loading && <p className="notice" role="status">正在載入留言…</p>}
    <ErrorNotice error={read.error} retry={read.reload} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有留言。' : '目前尚無留言。'}</p>
      : <ol className="comment-list">{read.data.list.map(
        // 留言 ID 固定列項，文章切換時不沿用舊文章內容。
        (comment, index) => <li key={comment.id}>
          <span className="item-number">{String((page - 1) * 20 + index + 1).padStart(2, '0')}</span>
          <div>
            <div className="meta">
              <strong>{comment.author.nickname}</strong><span>{comment.created_at.slice(0, 10)}</span>
              <span>建立於 <b>{BACKENDS[comment.created_backend][0]}</b></span>
            </div>
            <p className="body-text">{comment.body}</p>
            {account && (comment.author.id === account.id || account.role === 'admin') && <a href={`/next/comments/${comment.id}`}>留言詳情</a>}
          </div>
        </li>,
      )}</ol>}
      <Pagination pagination={read.data.pagination} base={`/next/articles/${articleId}`} fragment="#comments" /></>}
  </section>
}

// 留言草稿只在本頁記憶體，結果不明時用原文章第一頁供人工核對。
function CommentComposer({ article, backend, credential, account, ready, onAuthFailure, formNavigation, comments, reloadComments, reloadArticle }) {
  const [body, setBody] = useState('')
  const [errors, setErrors] = useState({})
  const [checking, setChecking] = useState(false)
  const owner = useRef(null)
  const summary = useRef(null)
  const write = useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, `comment-create-${article.id}`)
  // 首位使用表單者擁有草稿；重登成不同帳號不接收原文字。
  useEffect(() => { if (account && owner.current === null) owner.current = account.id }, [account])
  // 驗證錯誤或寫入失敗時把焦點放回摘要，避免鍵盤使用者漏看結果。
  useEffect(() => { if (write.error || Object.keys(errors).length) summary.current?.focus() }, [write.error, errors])
  const sameAccount = Boolean(account && (owner.current === null || owner.current === account.id))
  const allowed = ready && sameAccount && article.visibility === 'visible'
  // 更新輸入後清除舊錯誤，維持換行與純文字原值。
  function change(event) { setBody(event.target.value); setErrors({}); write.clearError(); write.markDirty(true) }
  // 查證只重讀第一頁，不能將列表第一筆認定為這次建立。
  function check() { setChecking(true); reloadComments() }
  // 必須看到已完成的列表讀取，才能自行決定是否再次送出。
  function acceptCheck() { if (!comments?.data || comments.loading || comments.error) return; setChecking(false); write.checked() }
  // 父文章須由目前後端確認公開；成功後清草稿並重讀留言第一頁。
  async function submit(event) {
    event.preventDefault()
    if (!allowed || write.pending || write.uncertain) return
    const found = validateContent({ body }, 'comment')
    setErrors(found)
    if (Object.keys(found).length) return
    const result = await write.send(`/api/articles/${article.id}/comments`, 'POST', { body }, 201, ['body'])
    if (result.ok) { setBody(''); write.markDirty(false); reloadComments() }
    if (result.error?.errors) setErrors(result.error.errors)
    if (['E0003', 'E0004'].includes(result.error?.code)) reloadArticle()
  }
  return <section className="composer"><h3>發表留言</h3>{article.visibility === 'hidden' ? <p className="notice">文章已下架，不能新增留言。</p> : !account ? <p>登入後可以留言。<a href={`/next/login?return_to=${encodeURIComponent(`/next/articles/${article.id}`)}`}>前往登入</a></p> : !sameAccount ? <p className="notice error">目前登入帳號不同，原留言草稿不能交給新帳號。</p> : <>
    <ErrorNotice error={write.error || (Object.keys(errors).length ? { message: '請修正標示欄位後再送出。', errors } : null)} focusRef={summary} />
    {write.uncertain && <div className="notice"><p>結果尚未確認；請查證留言第一頁，不會自動重送。</p><button className="outline-button" type="button" onClick={check}>重讀留言第一頁</button>{checking && comments?.data && !comments.loading && !comments.error && <button className="outline-button" type="button" onClick={acceptCheck}>已查證，允許手動送出</button>}</div>}
    <form className="account-form" noValidate onSubmit={submit}><label htmlFor="comment-body">留言內容</label><textarea id="comment-body" name="body" rows={5} value={body} onChange={change} disabled={!allowed || write.pending} aria-invalid={Boolean(errors.body)} />{errors.body && <small className="field-error">{errors.body.join('；')}</small>}<button className="red-button" type="submit" disabled={!allowed || write.pending || write.uncertain}>{write.pending ? '送出中…' : '發表留言'}</button></form></>}
  </section>
}

// 文章先確認可讀才載入留言，404 不揭露下架或不存在的差別。
function ArticlePage({ backend, credential, account, ready, id, page, onAuthFailure, formNavigation }) {
  const numericId = positiveId(id)
  const read = useApiRead(backend, credential, `/api/articles/${id}`, 'article', numericId, null, ready, onAuthFailure)
  const [commentPage, setCommentPage] = useState(page)
  const [commentRevision, setCommentRevision] = useState(0)
  const [comments, setComments] = useState(null)
  const [snapshot, setSnapshot] = useState(null)
  // 暫時失去登入時保留同頁留言草稿元件，但隱藏尚未重新驗權的文章快照。
  useEffect(() => { if (read.data) setSnapshot(read.data) }, [read.data])
  // 新留言後導覽至同篇第一頁並觸發重讀，不依 API 空回應推測留言 ID。
  function reloadComments() {
    setComments(null)
    if (commentPage === 1) {
      // 第一頁不改路徑，用讀取版次讓連續查證仍各自生效。
      setCommentRevision((value) => value + 1)
    }
    else setCommentPage(1)
    if (page !== 1) window.history.replaceState(null, '', `/next/articles/${id}?page=1#comments`)
  }
  return <div className="editorial-layout"><ArticleRail /><article className="article-column detail">
    <a className="back-link" href="/next/">← 返回文章列表</a>
    {account?.role === 'admin' && <a className="back-link admin-return" href="/next/admin/articles">← 返回文章管理</a>}
    {read.loading && <p className="notice" role="status">正在載入文章…</p>}
    <ErrorNotice error={read.error} retry={read.reload} />
    {read.data && <><header><p className="eyebrow">ARTICLE / 文章閱讀</p><h1>{read.data.title}</h1><ArticleMeta item={read.data} /><p>{read.data.visibility === 'hidden' ? '已下架' : '公開'}</p></header>
      <div className="article-body body-text">{read.data.body}</div>
      {canEditContent(read.data, account) && <a className="back-link" href={`/next/articles/${id}/edit`}>編輯文章</a>}
      {account && <VisibilityAction item={read.data} kind="article" account={account} backend={backend} credential={credential} onAuthFailure={onAuthFailure} formNavigation={formNavigation} onRefresh={read.reload} />}
      <CommentList backend={backend} credential={credential} account={account} ready={ready} articleId={numericId} page={commentPage} onAuthFailure={onAuthFailure} onRead={setComments} firstPage={commentRevision} />
      </>}
    {snapshot && <div hidden={!read.data}><CommentComposer article={read.data || snapshot} backend={backend} credential={credential} account={account} ready={ready && Boolean(read.data)} onAuthFailure={onAuthFailure} formNavigation={formNavigation} comments={comments} reloadComments={reloadComments} reloadArticle={read.reload} /></div>}
  </article></div>
}

// 留言先獨立授權讀取，再嘗試父文章；父文章 404 不洩漏其標題或原因。
function CommentDetail({ id, backend, credential, account, ready, onAuthFailure, formNavigation }) {
  const read = useApiRead(backend, credential, `/api/comments/${id}`, 'comment', positiveId(id), null, ready, onAuthFailure)
  const parent = useApiRead(backend, credential, `/api/articles/${read.data?.article_id || 0}`, 'article', read.data?.article_id, null, ready && Boolean(read.data), onAuthFailure)
  return <div className="editorial-layout">
    <ArticleRail />
    <main className="article-column detail">
      <a className="back-link" href="/next/me/comments">← 返回我的留言</a>
      {account?.role === 'admin' && <a className="back-link admin-return" href="/next/admin/comments">← 返回留言管理</a>}
      <div className="section-heading"><h1>留言詳情</h1></div>
      {ready && read.loading && <p role="status" className="notice">正在讀取留言…</p>}
      <ErrorNotice error={read.error} retry={read.reload} />
      {read.data && <>
        <ArticleMeta item={read.data} />
        <p>{read.data.visibility === 'hidden' ? '已下架' : '公開'}</p>
        <p className="article-body body-text">{read.data.body}</p>
        <section className="related">
          <h2>所屬文章</h2>
          {parent.loading && <p role="status">正在讀取文章…</p>}
          {parent.data && <p>
            <a href={`/next/articles/${parent.data.id}`}>{parent.data.title}</a>
            {' · '}{parent.data.visibility === 'visible' ? '公開' : '已下架'}
          </p>}
          {parent.error && <>
            <p className="notice">所屬文章目前無法讀取。</p>
            <button className="outline-button" type="button" onClick={parent.reload}>單獨重試文章讀取</button>
          </>}
        </section>
        {canEditContent(read.data, account, parent.data?.visibility === 'visible') && <a className="back-link" href={`/next/comments/${id}/edit`}>編輯留言</a>}
        {account && <VisibilityAction item={read.data} kind="comment" account={account} backend={backend}
          credential={credential} onAuthFailure={onAuthFailure} formNavigation={formNavigation} onRefresh={read.reload} />}
      </>}
    </main>
  </div>
}

// 管理入口沿用同一組語意頁，避免在窄螢幕塞入過長的單列導覽。
function AdminMenu() {
  return <details className="admin-menu"><summary>管理</summary><nav aria-label="管理導覽"><a href="/next/admin/members">會員</a><a href="/next/admin/articles">文章</a><a href="/next/admin/comments">留言</a><a href="/next/admin/settings/login-lockout">鎖定設定</a></nav></details>
}

// 瑞士式側欄與清楚的操作區延伸到管理頁，資料仍由各頁獨立讀取。
function AdminFrame({ title, children }) {
  return <div className="editorial-layout admin-layout"><aside className="article-rail" aria-label="管理介面"><p className="rail-kicker">THE ARTICLE COLLECTION</p><p className="rail-title">管理<br />中心</p><p className="rail-foot">會員・文章・留言</p></aside><main className="article-column admin-page"><div className="section-heading"><h1>{title}</h1><p className="eyebrow">ADMINISTRATION / 管理</p></div><a className="back-link" href="/next/">← 返回文章列表</a>{children}</main></div>
}

// 管理員身分只控制前端顯示與請求時機，實際授權仍由各 API 重驗。
function AdminAccess({ ready, authStatus, account }) {
  if (ready) return null
  return <p className="notice" role="status">{account && account.role !== 'admin' ? '沒有權限使用管理功能。' : authStatus === 'checking' ? '正在驗證登入…' : authStatus === 'error' ? '目前無法確認登入，請先重新驗證。' : authStatus === 'disabled' ? '此帳號已停用，不能使用管理功能。' : authStatus === 'expired' ? '請重新登入以繼續管理操作。' : '請先登入以使用管理功能。'}</p>
}

// 會員搜尋只保留核准的 keyword；網址頁碼在搜尋時回第一頁。
function AdminMembers({ backend, credential, account, authStatus, ready, page, keyword: rawKeyword, onAuthFailure }) {
  const keyword = memberKeyword(rawKeyword)
  const [input, setInput] = useState(rawKeyword)
  const [searchError, setSearchError] = useState(null)
  const searchRef = useRef(null)
  const filter = keyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(keyword)}` : ''
  const read = useApiRead(backend, credential, `/api/members?page=${page}&per_page=20${filter}`, 'members', page, null, ready && keyword !== null, onAuthFailure)
  // 搜尋驗證失敗時聚焦摘要，可由其中的欄位按鈕回到輸入。
  useEffect(() => { if (searchError) searchRef.current?.focus() }, [searchError])
  // 只將修剪後的字面值放進網址，未知字元由 URL 編碼而非自行解讀。
  function search(event) {
    event.preventDefault()
    const next = memberKeyword(input)
    if (next === null) { setSearchError({ message: '搜尋關鍵字不得超過 254 個字。', errors: { keyword: ['最多 254 個 Unicode 字元'] } }); return }
    window.location.assign(`/next/admin/members${next ? `?keyword=${encodeURIComponent(next)}` : ''}`)
  }
  // 搜尋字串變動後移除舊的長度錯誤，保留輸入直到送出。
  function changeSearch(event) { setInput(event.target.value); setSearchError(null) }
  // 指定超頁優先顯示此頁無資料，首頁才區分搜尋無結果與尚無會員。
  return <AdminFrame title="會員管理"><AdminAccess ready={ready} authStatus={authStatus} account={account} />{ready && <>
    <form className="admin-toolbar" onSubmit={search}><label htmlFor="keyword">搜尋暱稱或 Email</label><input id="keyword" value={input} onChange={changeSearch} aria-invalid={Boolean(searchError)} aria-describedby={searchError ? 'keyword-error' : undefined} /><button className="outline-button" type="submit">搜尋會員</button>{searchError && <p id="keyword-error" className="field-error">最多 254 個 Unicode 字元</p>}</form>
    <ErrorNotice error={searchError || (keyword === null ? { message: '網址中的搜尋關鍵字過長，請修正後再搜尋。' } : null)} focusRef={searchRef} />
    {keyword !== null && <>{read.loading && <p className="notice" role="status">正在載入會員…</p>}<ErrorNotice error={read.error} retry={read.reload} />{read.data && <>
      {read.data.list.length ? <ol className="admin-list">{read.data.list.map(
        // 列表只連到一般會員詳情，不在前端推測狀態寫入結果。
        (member) => <li className="admin-row" key={member.id}><div><small>會員 #{member.id}</small><h2><a href={`/next/admin/members/${member.id}`}>{member.nickname}</a></h2></div><p className="break">{member.email}</p><p>{member.status === 'active' ? '啟用' : '停用'}</p><a href={`/next/admin/members/${member.id}`}>查看詳情</a></li>,
      )}</ol> : <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有會員資料。' : keyword ? '沒有符合搜尋條件的會員。' : '目前尚無會員。'}</p>}
      <Pagination pagination={read.data.pagination} base="/next/admin/members" query={keyword ? `keyword=${encodeURIComponent(keyword)}` : ''} />
    </>}</>}
  </>}</AdminFrame>
}

// 停用／恢復後只相信重新讀取的伺服器狀態；不把空成功回應當成新狀態。
function AdminMember({ id, backend, credential, account, authStatus, ready, onAuthFailure, formNavigation }) {
  const numericId = positiveId(id)
  const read = useApiRead(backend, credential, `/api/members/${id}`, 'member', numericId, null, ready, onAuthFailure)
  const write = useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, `member-status-${id}`)
  const [checking, setChecking] = useState(false)
  // 寫入未確認時須由人重新讀回，不能自動重送同一狀態。
  async function change() {
    if (!read.data || write.pending || write.uncertain || write.conflict || !window.confirm(`確定${read.data.status === 'active' ? '停用' : '恢復'}這位會員嗎？`)) return
    const status = read.data.status === 'active' ? 'disabled' : 'active'
    const result = await write.send(`/api/members/${id}/status`, 'PUT', { status }, 200, ['status'])
    if (result.ok) { setChecking(false); read.reload() }
  }
  // 查證啟動時清除先前資料，讀取完成後才可解鎖下一次手動操作。
  function check() { setChecking(true); read.reload() }
  // 使用者親自比對最新狀態後解鎖，下一次點擊仍須再次確認。
  function accept() { if (read.data && !read.loading && !read.error) { write.checked(); setChecking(false) } }
  return <AdminFrame title="會員詳情"><a className="back-link" href="/next/admin/members">← 返回會員列表</a><AdminAccess ready={ready} authStatus={authStatus} account={account} />{ready && <>
    {read.loading && <p className="notice" role="status">正在讀取會員…</p>}<ErrorNotice error={read.error} retry={read.reload} />{read.data && <div className="admin-detail"><p><strong>會員 ID</strong><span>{read.data.id}</span></p><p><strong>暱稱</strong><span>{read.data.nickname}</span></p><p><strong>Email</strong><span>{read.data.email}</span></p><p><strong>角色</strong><span>一般會員</span></p><p><strong>狀態</strong><span>{read.data.status === 'active' ? '啟用' : '停用'}</span></p><p><strong>加入時間</strong><span>{read.data.created_at}</span></p></div>}
    <ErrorNotice error={write.error} />{(write.uncertain || write.conflict) && <div className="notice"><p>結果尚未確認。請重新讀取並核對會員狀態，不會自動重送。</p><button type="button" className="outline-button" onClick={check} disabled={read.loading}>重讀會員狀態</button>{checking && read.data && <button type="button" className="outline-button" onClick={accept}>已核對最新狀態</button>}</div>}
    {read.data && <button type="button" className="red-button" onClick={change} disabled={write.pending || write.uncertain || write.conflict}>{write.pending ? '處理中…' : read.data.status === 'active' ? '停用會員' : '恢復會員'}</button>}
  </>}</AdminFrame>
}

// 管理列表使用既有內容詳情與可見性控制，避免開放修改其他作者內文。
function AdminContent({ kind, backend, credential, account, authStatus, ready, page, onAuthFailure }) {
  const article = kind === 'article'
  const route = article ? 'articles' : 'comments'
  const path = article ? `/api/articles?page=${page}&per_page=20&filters%5Bscope%5D=management` : `/api/comments?page=${page}&per_page=20`
  const read = useApiRead(backend, credential, path, article ? 'articles' : 'managed-comments', page, null, ready, onAuthFailure)
  // 零筆超頁仍保留 API 原頁碼與返回第一頁入口，不宣稱整個管理列表為空。
  return <AdminFrame title={article ? '文章管理' : '留言管理'}><AdminAccess ready={ready} authStatus={authStatus} account={account} />{ready && <>
    {read.loading && <p className="notice" role="status">正在載入{article ? '文章' : '留言'}…</p>}<ErrorNotice error={read.error} retry={read.reload} />{read.data && <>
      {read.data.list.length ? <ol className="admin-list">{read.data.list.map(
        // 完整內文和版本操作留在已知 ID 詳情，列表只呈現可辨識摘要。
        (item) => <li className="admin-row" key={item.id}><div><small>{article ? '文章' : '留言'} #{item.id}</small><h2><a href={`/next/${route}/${item.id}`}>{article ? item.title : `留言 #${item.id}`}</a></h2></div><p>作者 {item.author.nickname}<br />建立於 {BACKENDS[item.created_backend][0]}</p><p>{item.visibility === 'hidden' ? '已下架' : '公開'}</p><a href={`/next/${route}/${item.id}`}>查看詳情</a><p className="excerpt">{item.body}</p></li>,
      )}</ol> : <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有資料。' : `目前尚無${article ? '文章' : '留言'}。`}</p>}
      <Pagination pagination={read.data.pagination} base={`/next/admin/${route}`} />
    </>}
  </>}</AdminFrame>
}

// 設定表單保留未送出的值；PUT 期間停用輸入，成功後才以讀回四欄取代草稿。
function AdminSettings({ backend, credential, account, authStatus, ready, onAuthFailure, formNavigation }) {
  const read = useApiRead(backend, credential, '/api/settings/login-lockout', 'lockout-settings', null, null, ready, onAuthFailure)
  const write = useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, 'lockout-settings')
  const [draft, setDraft] = useState(null)
  const [ownerId, setOwnerId] = useState(null)
  const [localError, setLocalError] = useState(null)
  const [checking, setChecking] = useState(false)
  const [saved, setSaved] = useState(false)
  const errorRef = useRef(null)
  const sameAccount = ownerId === null || ownerId === account?.id
  // 首次讀入與成功寫入後使用伺服器值；其他情況保留待查證的草稿。
  useEffect(() => {
    if (!read.data || !account || draft) return
    setDraft({ enabled: read.data.enabled, window_minutes: String(read.data.window_minutes), failure_threshold: String(read.data.failure_threshold), lock_seconds: String(read.data.lock_seconds) })
    setOwnerId(account.id)
  }, [read.data, account, draft])
  // 送出驗證失敗後把焦點交給摘要，讓鍵盤使用者可選欄位並保留草稿。
  useEffect(() => { if (ready && sameAccount && (localError || write.error)) errorRef.current?.focus() }, [ready, sameAccount, localError, write.error])
  // 本地欄位更新只影響草稿，切換選單仍會提示捨棄未儲存設定。
  function change(name, value) {
    // 函式式更新保留其他三欄，快速輸入時不以過期草稿覆蓋它們。
    setDraft((current) => ({ ...current, [name]: value }))
    setLocalError(null); setSaved(false); write.clearError(); write.markDirty(true)
  }
  // 勾選變更只更新布林欄，不清除停用期間仍需保存的數值。
  function changeEnabled(event) { change('enabled', event.target.checked) }
  // 數值欄保留原字串供錯誤修正，送出時才驗證並轉換。
  function changeNumber(event) { change(event.target.id, event.target.value) }
  // 三個數值須為界限內的十進位整數；停用開關仍一同送出原數值。
  async function save(event) {
    event.preventDefault()
    if (!ready || !sameAccount || !draft || write.pending || write.uncertain) return
    const { body, errors } = parseLockoutDraft(draft)
    if (Object.keys(errors).length) { setLocalError({ message: '欄位資料不正確，請確認後再送出。', errors }); return }
    const result = await write.send('/api/settings/login-lockout', 'PUT', body, 200, Object.keys(body))
    if (result.ok) { setDraft(null); write.markDirty(false); setSaved(true); read.reload() }
  }
  // 已送出但結果不明時只查證完整設定，不替使用者重送 PUT。
  function check() { setChecking(true); read.reload() }
  // 手動核對伺服器完整值後才解除鎖，保留原草稿讓使用者自行決定。
  function accept() { if (read.data && !read.loading && !read.error && sameAccount) { write.checked(); setChecking(false) } }
  return <AdminFrame title="登入鎖定設定"><AdminAccess ready={ready} authStatus={authStatus} account={account} />{ready && <>
    {read.loading && <p className="notice" role="status">正在讀取設定…</p>}<ErrorNotice error={read.error} retry={read.reload} />{saved && !read.loading && read.data && <p className="notice success" role="status">已儲存；以下數值已重新向伺服器讀取。</p>}
    {!sameAccount && <p className="notice error">登入帳號已變更，舊帳號的設定草稿不可由目前帳號送出。請重新開啟此頁。</p>}
    {draft && sameAccount && <form className="account-form settings-form" onSubmit={save}><ErrorNotice error={localError || write.error} focusRef={errorRef} /><label className="setting-toggle"><input id="enabled" type="checkbox" checked={draft.enabled} onChange={changeEnabled} disabled={write.pending} aria-invalid={Boolean((localError || write.error)?.errors?.enabled)} aria-describedby={(localError || write.error)?.errors?.enabled ? 'enabled-error' : undefined} />啟用會員登入錯誤鎖定</label>
      {(localError || write.error)?.errors?.enabled && <p id="enabled-error" className="field-error">{(localError || write.error).errors.enabled.join('；')}</p>}
      <p className="hint">停用仍保留以下三個數值；重新啟用時沿用。</p>
      {[['window_minutes', '累計失敗時間（分鐘）', 60], ['failure_threshold', '觸發鎖定次數', 100], ['lock_seconds', '鎖定秒數', 86400]].map(
        // 各欄保留可見標籤、上限與 422 明細的焦點目標。
        ([name, label, max]) => <div className="setting-field" key={name}><label htmlFor={name}>{label}</label><input id={name} inputMode="numeric" value={draft[name]} onChange={changeNumber} disabled={write.pending} aria-invalid={Boolean((localError || write.error)?.errors?.[name])} aria-describedby={(localError || write.error)?.errors?.[name] ? `${name}-error` : undefined} /><p className="hint">1–{max}</p>{(localError || write.error)?.errors?.[name]?.map(
          // 伺服器欄位錯誤保持在相應輸入旁，同時於摘要可聚焦。
          (message, index) => <p id={index === 0 ? `${name}-error` : undefined} className="field-error" key={message}>{message}</p>,
        )}</div>,
      )}
      {(write.uncertain || write.conflict) && <div className="notice"><p>結果尚未確認。請重讀並核對目前設定，不會自動重送。</p><button className="outline-button" type="button" onClick={check} disabled={read.loading}>重讀目前設定</button>{checking && read.data && <><p>伺服器目前：{read.data.enabled ? '啟用' : '停用'}，{read.data.window_minutes} 分鐘／{read.data.failure_threshold} 次／{read.data.lock_seconds} 秒。</p><button className="outline-button" type="button" onClick={accept}>已核對目前設定</button></>}</div>}
      <button className="red-button" type="submit" disabled={write.pending || write.uncertain || write.conflict}>{write.pending ? '儲存中…' : '儲存設定'}</button>
    </form>}
  </>}</AdminFrame>
}

// 註冊與登入使用相同單欄表單；鎖定與伺服器錯誤只套在當次登入。
function AccountForm({ mode, backend, returnTo, registered, formNavigation, onLogin }) {
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
  return <main className="form-page">
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
  </main>
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
  const formNavigation = useRef({ dirty: false, pending: false, cancel: null, authorForms: new Map() })
  // 路由只解析核准的語意與安全 ID，返回目標不攜帶任意網址。
  const page = pageNumber(query.page)
  const returnTo = validReturn(query.return_to) ? query.return_to : '/next/'
  const article = path.match(/^\/next\/articles\/([0-9]+)$/)
  const articleId = article && positiveId(article[1]) !== null ? article[1] : null
  const articleEdit = path.match(/^\/next\/articles\/([0-9]+)\/edit$/)
  const articleEditId = articleEdit && positiveId(articleEdit[1]) !== null ? articleEdit[1] : null
  const comment = path.match(/^\/next\/comments\/([0-9]+)$/)
  const commentId = comment && positiveId(comment[1]) !== null ? comment[1] : null
  const commentEdit = path.match(/^\/next\/comments\/([0-9]+)\/edit$/)
  const commentEditId = commentEdit && positiveId(commentEdit[1]) !== null ? commentEdit[1] : null
  const member = path.match(/^\/next\/admin\/members\/([0-9]+)$/)
  const memberId = member && positiveId(member[1]) !== null ? member[1] : null
  const adminPath = validReturn(path) && path.startsWith('/next/admin/')
  const authorPath = path === '/next/articles/new' || path === '/next/me/articles' || path === '/next/me/comments' || Boolean(articleId || articleEditId || commentId || commentEditId)
  const authorReady = authStatus === 'active' && Boolean(account)
  const adminReady = authorReady && account.role === 'admin'
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
  // 瀏覽器導覽也需攔住未儲存或仍在送出的草稿；切換選單另提供明確文案。
  useEffect(() => {
    // 同一分頁離開時交由瀏覽器顯示標準確認；不把草稿寫入儲存空間。
    function beforeUnload(event) {
      if (!formNavigation.current.dirty && !formNavigation.current.pending && ![...formNavigation.current.authorForms.values()].some(
        // 文章頁可同時有留言與可見性表單，任一未完成都需提示。
        (entry) => entry.dirty || entry.pending,
      )) return
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', beforeUnload)
    // 卸載外殼時解除離頁保護，避免舊表單繼續攔截新頁。
    return () => window.removeEventListener('beforeunload', beforeUnload)
  }, [])
  // 初次進頁及手動重驗都以同分頁儲存狀態呼叫 /api/me，暫時故障不刪 JWT。
  useEffect(() => {
    const controller = new AbortController()
    let storage
    try {
      storage = sessionStorage
      // 也檢查 getItem 被拒；共用讀取 helper 會把損壞或不可讀內容當作無憑證。
      storage.getItem(SESSION_KEY)
    } catch {
      setBackend(BACKENDS.laravel[1])
      setAuthStatus('guest')
      setNotice({ message: '無法讀取瀏覽器儲存，登入延續與版本選擇無法保存；請檢查瀏覽器設定。' })
      return undefined
    }
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
    const dirty = formNavigation.current.dirty || [...formNavigation.current.authorForms.values()].some(
      // 任一作者表單仍有文字都需先取得捨棄同意。
      (entry) => entry.dirty,
    )
    const pending = formNavigation.current.pending || [...formNavigation.current.authorForms.values()].some(
      // 等待中仍可能在後端完成，不承諾切換就回滾。
      (entry) => entry.pending,
    )
    if (!dirty && !pending) return true
    const warning = pending
      ? path === '/next/register' ? '註冊請求已送出，切換後伺服器仍可能完成建立帳號。' : path === '/next/login' ? '登入請求已送出，切換後伺服器仍可能完成處理。' : '內容請求已送出，切換後伺服器仍可能完成寫入。'
      : '目前表單有未儲存文字。'
    return window.confirm(`${warning}要捨棄目前表單並切換${destination}嗎？`)
  }
  // 使用者已同意切換後同步作廢表單請求，避免 beforeunload 再問一次。
  function discardFormsForSwitch() {
    formNavigation.current.cancel?.()
    formNavigation.current.dirty = false
    formNavigation.current.pending = false
    for (const entry of formNavigation.current.authorForms.values()) {
      entry.cancel()
      entry.dirty = false
      entry.pending = false
    }
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
    discardFormsForSwitch()
    const target = validReturn(path) ? path : '/next/'
    window.location.assign(hadCredential && path !== '/next/login' && path !== '/next/register' ? `/next/login?return_to=${encodeURIComponent(target)}` : window.location.pathname + window.location.search)
  }
  // 跨前端只導覽相同語意頁，不修改同分頁的後端或 JWT 原期限。
  function changeFrontend(event) {
    const target = canSwitchFrontend(event.target.value, backend) && frontendTarget(path, event.target.value, event.target.value === 'sveltekit' ? query.return_to ?? '/next/' : returnTo)
    if (!target) { event.target.value = 'next'; return }
    if (logoutPending || logoutRef.current) { event.target.value = 'next'; setNotice({ message: '登出處理中，請等待結果後再切換。' }); return }
    if (!confirmFormSwitch('前端')) { event.target.value = 'next'; return }
    // 確認後、導覽前同步取消來源表單，避免延遲成功回應搶先改址。
    discardFormsForSwitch()
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
  // 原頁重登後只設定當次登入者；作者表單自行核對原帳號才可繼續送出。
  function inlineLogin(login) {
    const saved = readSession(sessionStorage)
    setCredential(saved)
    setAccount(login.user)
    setAuthStatus('active')
    setAuthError(null)
  }
  return <div className="site-shell"><a className="skip-link" href="#main-content">跳至主要內容</a><header className="site-header">
    <div className="brand-row"><a className="brand" href="/next/">文章集<span aria-hidden="true">.</span></a><nav className="desktop-nav" aria-label="主要導覽"><a aria-current={path === '/next/' ? 'page' : undefined} href="/next/">所有文章</a>{account ? <><a href="/next/articles/new">發表文章</a><a href="/next/me/articles">我的文章</a><a href="/next/me/comments">我的留言</a>{account.role === 'admin' && <AdminMenu />}</> : <><a href="/next/login">登入</a><a href="/next/register">註冊</a></>}</nav></div>
    <div className="header-controls"><div className="current-labels"><span>目前前端 <strong>Next.js</strong></span><span>目前後端 <strong>{backend ? Object.values(BACKENDS).find(
      // 從固定清單找顯示名稱，不把不可信儲存值直接印到畫面。
      (entry) => entry[1] === backend,
    )?.[0] : '讀取中…'}</strong></span></div>
      <label className="select-wrap">切換版本<select value="next" onChange={changeFrontend} aria-label="切換前端版本">{Object.entries(FRONTENDS).map(
        // 固定後端與目標路由都須合法；SvelteKit 另檢查原始返回目標。
        ([id, name]) => {
          const backendReady = canSwitchFrontend(id, backend)
          const routeReady = frontendTarget(path, id, id === 'sveltekit' ? query.return_to ?? '/next/' : returnTo) !== null
          return <option key={id} value={id} disabled={!backendReady || !routeReady}>{name}{!routeReady ? '（此頁尚未提供）' : ''}</option>
        },
      )}</select></label>
      <label className="select-wrap">切換後端<select value={backend || ''} onChange={changeBackend} disabled={!backend} aria-label="切換目前後端">{!backend && <option value="">讀取中…</option>}{Object.entries(BACKENDS).map(
        // API 來源只取固定清單，顯示文字不影響實際位址。
        ([id, entry]) => <option key={id} value={entry[1]}>{entry[0]}</option>,
      )}</select></label>
      {account && <span className="account-name">{account.nickname}</span>}
      {account && <button type="button" className="text-button logout" onClick={logout} disabled={logoutPending}>{logoutPending ? '登出中…' : '登出'}</button>}
    </div>
    <details className="mobile-nav"><summary>選單</summary><nav aria-label="行動導覽"><a href="/next/">所有文章</a>{account ? <><a href="/next/articles/new">發表文章</a><a href="/next/me/articles">我的文章</a><a href="/next/me/comments">我的留言</a>{account.role === 'admin' && <AdminMenu />}</> : <><a href="/next/login">登入</a><a href="/next/register">註冊</a></>}</nav></details>
  </header>
  <div className="status-area">
    <ErrorNotice error={notice} />
    {authStatus === 'checking' && <p className="notice" role="status">正在驗證登入…</p>}
    {authError && <ErrorNotice error={authError} retry={authStatus === 'error' ? retryAuth : null} />}
    {(authorPath || adminPath) && authStatus === 'expired'
      ? <AccountForm mode="login" backend={backend} returnTo={path} formNavigation={formNavigation} onLogin={inlineLogin} />
      : authStatus === 'expired' && <a href={`/next/login?return_to=${encodeURIComponent(validReturn(path) ? path : '/next/')}`}>重新登入</a>}
    {(authorPath || adminPath) && authStatus === 'guest' && <p className="notice">請先<a href={`/next/login?return_to=${encodeURIComponent(path)}`}>登入</a>。</p>}
  </div>
  <div id="main-content">{path === '/next/' || path === '/next' ? <ArticleList backend={backend} credential={account ? credential : null} ready={ready} page={page} onAuthFailure={handleAuthFailure} />
    : articleId ? <ArticlePage backend={backend} credential={account ? credential : null} account={account} ready={ready} id={articleId} page={page} onAuthFailure={handleAuthFailure} formNavigation={formNavigation} />
      : path === '/next/articles/new' ? <ContentEditor kind="article" backend={backend} credential={credential} account={account} ready={authorReady} onAuthFailure={handleAuthFailure} formNavigation={formNavigation} />
      : articleEditId ? <ContentEditor kind="article" id={articleEditId} backend={backend} credential={credential} account={account} ready={authorReady} onAuthFailure={handleAuthFailure} formNavigation={formNavigation} />
      : commentEditId ? <ContentEditor kind="comment" id={commentEditId} backend={backend} credential={credential} account={account} ready={authorReady} onAuthFailure={handleAuthFailure} formNavigation={formNavigation} />
      : commentId ? <CommentDetail id={commentId} backend={backend} credential={credential} account={account} ready={authorReady} onAuthFailure={handleAuthFailure} formNavigation={formNavigation} />
      : path === '/next/me/articles' || path === '/next/me/comments' ? <MyContent kind={path.endsWith('articles') ? 'article' : 'comment'} backend={backend} credential={credential} account={account} authStatus={authStatus} ready={authorReady} page={page} onAuthFailure={handleAuthFailure} />
      : path === '/next/admin/members' ? <AdminMembers backend={backend} credential={credential} account={account} authStatus={authStatus} ready={adminReady} page={page} keyword={typeof query.keyword === 'string' ? query.keyword : ''} onAuthFailure={handleAuthFailure} />
      : memberId ? <AdminMember id={memberId} backend={backend} credential={credential} account={account} authStatus={authStatus} ready={adminReady} onAuthFailure={handleAuthFailure} formNavigation={formNavigation} />
      : path === '/next/admin/articles' || path === '/next/admin/comments' ? <AdminContent kind={path.endsWith('articles') ? 'article' : 'comment'} backend={backend} credential={credential} account={account} authStatus={authStatus} ready={adminReady} page={page} onAuthFailure={handleAuthFailure} />
      : path === '/next/admin/settings/login-lockout' ? <AdminSettings backend={backend} credential={credential} account={account} authStatus={authStatus} ready={adminReady} onAuthFailure={handleAuthFailure} formNavigation={formNavigation} />
      : path === '/next/login' || path === '/next/register' ? <AccountForm mode={path === '/next/register' ? 'register' : 'login'} backend={backend} returnTo={returnTo} registered={query.registered === '1'} formNavigation={formNavigation} />
        : <main className="missing"><p className="eyebrow">404 / PAGE NOT FOUND</p><h1>找不到頁面</h1><p>此路徑尚未提供可閱讀的頁面。</p><a className="red-button" href="/next/">返回文章列表 <span aria-hidden="true">↗</span></a></main>}</div>
  <footer className="site-footer"><span>文章集 / NEXT.JS EDITION</span><span>閱讀是另一種記錄。</span></footer></div>
}
