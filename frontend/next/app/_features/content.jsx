import { useEffect, useRef, useState } from 'react'
import { apiFailure, apiRequest, BACKENDS, canEditContent, canHideContent, canRestoreContent, positiveId, successData, validateContent } from '../../lib/contracts.js'
import { ownsWriteSession, useApiRead, useAuthorWrite } from '../_shared/api-hooks.js'
import { ErrorNotice, Pagination } from '../_shared/ui.jsx'

const CREATED_ARTICLE_NOTICE_KEY = 'articleCollection.next.createdArticleNotice'

// 建築照片只提供展廳構圖；頁面容器決定照片位置，資料閱讀順序留在語意內容中。
function ArticleRail() {
  return <aside className="article-rail" aria-hidden="true" />
}

// 保留建立來源、作者與日期，列表和詳情共用同一資料語意。
function ArticleMeta({ item }) {
  return <div className="meta"><span>作者 <strong>{item.author.nickname}</strong></span><span>發佈日期 {item.created_at.slice(0, 10)}</span><span>建立於 <b>{BACKENDS[item.created_backend][0]}</b></span></div>
}

// 下架與恢復共用版本寫入，衝突或結果不明時要求重新讀取目前資源。
function VisibilityAction({ item, kind, account, backend, credential, active, onAuthFailure, formNavigation, onRefresh }) {
  const write = useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, `${kind}-visibility-${item.id}`)
  const [readback, setReadback] = useState({ loading: false, data: null, error: null })
  const readbackRequest = write.readbackRequest
  const readbackKey = `${backend}|${credential?.accessToken || ''}`
  const currentReadback = readback.key === readbackKey ? readback : { loading: false, data: null, error: null }
  const canHide = canHideContent(item, account)
  const canRestore = canRestoreContent(item, account)
  // 後端再次判定權限與版本；成功只讀回已知 ID，絕不猜測更新後內容。
  async function change() {
    if (!active || !canHide && !canRestore || write.pending || write.conflict || write.uncertain) return
    const visibility = canRestore ? 'visible' : 'hidden'
    if (!window.confirm(`確定${visibility === 'hidden' ? '下架' : '恢復'}這筆${kind === 'article' ? '文章' : '留言'}嗎？`)) return
    setReadback({ loading: false, data: null, error: null })
    const result = await write.send(`/api/${kind === 'article' ? 'articles' : 'comments'}/${item.id}/visibility`, 'PUT', { visibility, version: item.version }, 200, ['visibility', 'version'])
    if (result.ok) onRefresh()
  }
  // 衝突與未知結果單獨讀回已知 ID；讀取失敗或換帳號不解除寫入鎖。
  async function check() {
    if (!active || readbackRequest.current || !credential) return
    const controller = new AbortController()
    readbackRequest.current = controller
    setReadback({ key: readbackKey, loading: true, data: null, error: null })
    try {
      const response = await apiRequest(backend, `/api/${kind === 'article' ? 'articles' : 'comments'}/${item.id}`, { token: credential.accessToken, signal: controller.signal })
      if (controller.signal.aborted) return
      if (!ownsWriteSession(backend, credential)) {
        setReadback({ key: readbackKey, loading: false, data: null, error: { message: '目前無法確認登入，請重新驗證後再查證。' } })
        return
      }
      const data = successData(response.status, response.body, kind, item.id)
      if (data) { setReadback({ key: readbackKey, loading: false, data, error: null }); return }
      const error = apiFailure(response.status, response.body)
      if (response.status === 401 && error.code === 'E0002' || response.status === 403 && error.code === 'E0102') onAuthFailure(backend, credential, error)
      setReadback({ key: readbackKey, loading: false, data: null, error })
    } catch {
      if (!controller.signal.aborted) setReadback({ key: readbackKey, loading: false, data: null, error: { message: '查證失敗，請確認目前後端後手動重試。' } })
    } finally {
      if (readbackRequest.current === controller) readbackRequest.current = null
    }
  }
  // 看過新版本與可見性後再重讀父頁；下一次操作仍由使用者按下並經後端重驗。
  function acceptReadback() {
    if (!active || !currentReadback.data || currentReadback.loading || currentReadback.error || !ownsWriteSession(backend, credential)) return
    write.checked()
    setReadback({ loading: false, data: null, error: null })
    onRefresh()
  }
  if (!write.sameAccount) return <p className="notice">登入帳號已變更，請重新開啟此頁再操作內容狀態。</p>
  if (!canHide && !canRestore && !write.error && !write.uncertain && !write.conflict) return null
  return <div className="author-actions">
    <ErrorNotice error={write.error} />
    {(write.conflict || write.uncertain) && <div className="notice">
      <p>{write.conflict ? '版本已變更，請重讀並重新確認操作。' : '結果尚未確認，請重讀查證目前狀態。'}</p>
      <button className="outline-button" type="button" disabled={currentReadback.loading} onClick={check}>
        {currentReadback.loading ? '查證中…' : '重讀目前狀態'}
      </button>
      <ErrorNotice error={currentReadback.error} />
      {currentReadback.data && <div>
        <p>最新版本 {currentReadback.data.version} · {currentReadback.data.visibility === 'hidden' ? '已下架' : '公開'} · {currentReadback.data.can_restore ? '可恢復' : '目前不可恢復'}</p>
        <button className="outline-button" type="button" onClick={acceptReadback}>已比對，使用最新狀態</button>
      </div>}
    </div>}
    {(canHide || canRestore) && <button className="outline-button" type="button"
      disabled={!active || write.pending || write.conflict || write.uncertain} onClick={change}>
      {write.pending ? '處理中…' : canRestore ? '恢復' : '下架'}
    </button>}
  </div>
}

// 私有列表只在目前帳號已向 /api/me 驗證後讀取，包含自己下架內容。
export function MyContent({ kind, backend, credential, account, authStatus, ready, page, onAuthFailure }) {
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
  return <div className="editorial-layout"><ArticleRail /><div className="article-column"><div className="section-heading"><h1>{article ? '我的文章' : '我的留言'}</h1></div>
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
  </div></div>
}

// 發文與編修保持原始草稿；重讀資料只供人工比較，不覆蓋輸入欄位。
export function ContentEditor({ kind, id, backend, credential, account, ready, onAuthFailure, formNavigation }) {
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
    if (read.data && !initialized.current) {
      owner.current = read.data.author.id
      setFields({ title: read.data.title || '', body: read.data.body })
      setVersion(read.data.version)
      initialized.current = true
    }
  }, [read.data, account, creating])
  // 錯誤摘要取得鍵盤焦點，欄位連結可前往精確輸入。
  useEffect(() => { if (write.error || Object.keys(errors).length) summary.current?.focus() }, [write.error, errors])
  const sameAccount = Boolean(account && (owner.current === null || account.id === owner.current))
  const allowed = ready && sameAccount && (creating || Boolean(read.data && canEditContent(read.data, account)))
  const parentVisible = article || parent.data?.visibility === 'visible'
  // 保留使用者輸入原文，清除已過期的欄位提示並更新離頁標記。
  function change(event) {
    const { name, value } = event.target
    // 合併最新欄位值，連續輸入不覆蓋另一欄草稿。
    setFields((current) => ({ ...current, [name]: value }))
    setErrors({})
    write.clearError()
    write.markDirty(true)
  }
  // 人工重讀可處理衝突或未知結果，需明確採用新版本才可再次送出。
  function checkLatest() {
    setChecking(true)
    if (creating) verification.reload()
    else read.reload()
    if (!article) parent.reload()
  }
  // 最新資料不會覆蓋草稿；按鈕只更新送出用版本。
  function acceptLatest() {
    if (!read.data || read.loading || read.error || !sameAccount) return
    setVersion(read.data.version)
    setChecking(false)
    write.checked()
  }
  // 建立沒有回傳 ID，列表只供人工查證，不把第一筆當作剛建立的內容。
  function acceptCreatedCheck() {
    if (!verification.data || verification.loading || verification.error || !sameAccount) return
    write.checked()
    setChecking(false)
  }
  // 送出前做共同文字驗證與目前作者檢查，後端仍原子檢查權限及版本。
  async function submit(event) {
    event.preventDefault()
    if (!allowed || !parentVisible || write.pending || write.uncertain || write.conflict) return
    /** @type {import('../../lib/contracts.js').ArticleInputDto | import('../../lib/contracts.js').CommentInputDto} */
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
  return <div className={`editorial-layout${creating ? ' gallery-writing' : ''}`}>
    {creating ? <section className="writing-intro" aria-labelledby="editor-title">
      <a className="back-link" href="/next/me/articles">← 返回我的文章</a>
      <div className="section-heading"><h1 id="editor-title">發表文章</h1></div>
      <ArticleRail />
      <p>文章發表後立即公開，由目前後端記錄建立來源。</p>
    </section> : <ArticleRail />}
    <div className={`article-column editor-page${creating ? ' writing-controls' : ''}`}>
      {!creating && <a className="back-link" href={`/next/${article ? 'articles' : 'comments'}/${id}`}>← 返回詳情</a>}
      {!creating && <div className="section-heading"><h1>{article ? '編輯文章' : '編輯留言'}</h1></div>}
      {!creating && ready && read.loading && <p role="status" className="notice">正在讀取內容…</p>}
      {!creating && <ErrorNotice error={read.error} retry={read.reload} />}
      {!article && read.data && <section className="related">
        <h2>所屬文章</h2>
        {parent.loading && <p role="status">正在確認文章…</p>}
        {parent.data && <p>
          <a href={`/next/articles/${parent.data.id}`}>{parent.data.title}</a> · {parent.data.visibility === 'visible' ? '公開' : '已下架'}
        </p>}
        {parent.error && <>
          <p className="notice">所屬文章目前無法讀取。</p>
          <button className="outline-button" type="button" onClick={parent.reload}>單獨重試文章讀取</button>
        </>}
      </section>}
      {account && !sameAccount && <p className="notice error" role="alert">目前登入帳號不同，這份草稿不能交給新帳號。</p>}
      {read.data && sameAccount && !creating && !canEditContent(read.data, account) && <p className="notice error">只有內容作者可編修。</p>}
      {allowed && <>
        <ErrorNotice error={write.error || (Object.keys(errors).length ? { message: '請修正標示欄位後再送出。', errors } : null)} focusRef={summary} />
        {(write.conflict || write.uncertain) && <div className="notice">
          <p>{write.conflict ? '內容版本已變更，請比較最新內容後自行整合。' : '寫入結果尚未確認，請查證後自行決定是否再送。'}</p>
          <button className="outline-button" type="button" onClick={checkLatest}>{creating ? '查證我的文章第一頁' : '讀取最新內容'}</button>
        </div>}
        {checking && !creating && read.data && !read.loading && !read.error && <div className="comparison">
          <h2>最新伺服器內容（供比較）</h2>
          {article && <p className="body-text">{read.data.title}</p>}
          <p className="body-text">{read.data.body}</p>
          <p>版本 {read.data.version}</p>
          <button className="outline-button" type="button" onClick={acceptLatest}>已比較，採用最新版本號</button>
        </div>}
        {checking && creating && verification.loading && <p role="status" className="notice">正在查證我的文章…</p>}
        {checking && creating && <ErrorNotice error={verification.error} retry={verification.reload} />}
        {checking && creating && verification.data && !verification.loading && !verification.error && <div className="comparison">
          <p>請自行核對是否已建立；第一筆不一定是本次文章。</p>
          <ul>{verification.data.list.map(
            // 顯示伺服器資料供核對，不推測本次建立的 ID。
            (item) => <li key={item.id}><a href={`/next/articles/${item.id}`}>{item.title}</a> · {item.created_at}</li>,
          )}</ul>
          <button className="outline-button" type="button" onClick={acceptCreatedCheck}>已查證，允許手動送出</button>
        </div>}
        <form className="account-form" noValidate onSubmit={submit}>
          {article && <>
            <label htmlFor="title">標題</label>
            <input id="title" name="title" value={fields.title} onChange={change} disabled={write.pending}
              aria-invalid={Boolean(errors.title)} aria-describedby={errors.title ? 'title-error' : undefined} />
            {errors.title && <small id="title-error" className="field-error">{errors.title.join('；')}</small>}
          </>}
          <label htmlFor="body">{article ? '文章內文' : '留言內容'}</label>
          <textarea id="body" name="body" rows={10} value={fields.body} onChange={change} disabled={write.pending}
            aria-invalid={Boolean(errors.body)} aria-describedby={errors.body ? 'body-error' : undefined} />
          {errors.body && <small id="body-error" className="field-error">{errors.body.join('；')}</small>}
          {!parentVisible && !article && <p className="notice">確認所屬文章公開後才能送出留言編修。</p>}
          <button className="red-button" type="submit" disabled={write.pending || write.conflict || write.uncertain || !parentVisible}>
            {write.pending ? '處理中…' : creating ? '發表文章' : '儲存修改'}
          </button>
        </form>
      </>}
    </div>
  </div>
}

// 展廳列表以自然文件順序左右排列；照片不改變文章次序，載入、真空與超頁仍各自呈現。
export function ArticleList({ backend, credential, ready, page, onAuthFailure }) {
  const read = useApiRead(backend, credential, `/api/articles?page=${page}&per_page=20`, 'articles', page, null, ready, onAuthFailure)
  return <div className="editorial-layout gallery-index"><ArticleRail /><section className="article-column" aria-labelledby="list-title">
    <div className="section-heading"><h1 id="list-title">所有文章</h1></div>
    {read.loading && <p className="notice" role="status">正在載入文章…</p>}
    <ErrorNotice error={read.error} retry={read.reload} />
    {read.data && <>{read.data.list.length === 0 ? <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有文章。' : '目前尚無文章。'}</p>
      : <ol className="article-list">{read.data.list.map(
        // 以資料 ID 固定列項，跨頁與重新讀取都不重用錯誤內容。
        (article) => <li key={article.id} className="article-item">
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
  function change(event) {
    setBody(event.target.value)
    setErrors({})
    write.clearError()
    write.markDirty(true)
  }
  // 查證只重讀第一頁，不能將列表第一筆認定為這次建立。
  function check() { setChecking(true); reloadComments() }
  // 必須看到已完成的列表讀取，才能自行決定是否再次送出。
  function acceptCheck() {
    if (!comments?.data || comments.loading || comments.error || !sameAccount) return
    setChecking(false)
    write.checked()
  }
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
  return <section className="composer">
    <h3>發表留言</h3>
    {account && sameAccount && <>
      <ErrorNotice error={write.error || (Object.keys(errors).length ? { message: '請修正標示欄位後再送出。', errors } : null)} focusRef={summary} />
      {write.uncertain && <div className="notice">
        <p>結果尚未確認；請查證留言第一頁，不會自動重送。</p>
        <button className="outline-button" type="button" onClick={check}>重讀留言第一頁</button>
        {checking && comments?.data && !comments.loading && !comments.error && <button className="outline-button" type="button" onClick={acceptCheck}>已查證，允許手動送出</button>}
      </div>}
    </>}
    {article.visibility === 'hidden' ? <p className="notice">文章已下架，不能新增留言。</p>
      : !account ? <p>登入後可以留言。<a href={`/next/login?return_to=${encodeURIComponent(`/next/articles/${article.id}`)}`}>前往登入</a></p>
        : !sameAccount ? <p className="notice error">目前登入帳號不同，原留言草稿不能交給新帳號。</p>
          : <>
            <form className="account-form" noValidate onSubmit={submit}>
              <label htmlFor="comment-body">留言內容</label>
              <textarea id="comment-body" name="body" rows={5} value={body} onChange={change} disabled={!allowed || write.pending}
                aria-invalid={Boolean(errors.body)} aria-describedby={errors.body ? 'comment-body-error' : undefined} />
              {errors.body && <small id="comment-body-error" className="field-error">{errors.body.join('；')}</small>}
              <button className="red-button" type="submit" disabled={!allowed || write.pending || write.uncertain}>
                {write.pending ? '送出中…' : '發表留言'}
              </button>
            </form>
          </>}
  </section>
}

// 文章先確認可讀才載入留言，404 不揭露下架或不存在的差別。
export function ArticlePage({ backend, credential, account, ready, id, page, onAuthFailure, formNavigation }) {
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
  return <div className="editorial-layout gallery-reading">
    <article className="article-column detail reading-story">
      <a className="back-link" href="/next/">← 返回文章列表</a>
      {account?.role === 'admin' && <a className="back-link admin-return" href="/next/admin/articles">← 返回文章管理</a>}
      {read.loading && <p className="notice" role="status">正在載入文章…</p>}
      <ErrorNotice error={read.error} retry={read.reload} />
      {read.data && <>
        <header>
          <p className="eyebrow">ARTICLE / 文章閱讀</p>
          <h1>{read.data.title}</h1>
          <ArticleMeta item={read.data} />
          <p>{read.data.visibility === 'hidden' ? '已下架' : '公開'}</p>
        </header>
        <div className="article-body body-text">{read.data.body}</div>
        {canEditContent(read.data, account) && <a className="back-link" href={`/next/articles/${id}/edit`}>編輯文章</a>}
      </>}
      {snapshot && <div hidden={!read.data || !account}>
        <VisibilityAction item={read.data || snapshot} kind="article" account={account} backend={backend}
          credential={credential} active={Boolean(read.data && account)} onAuthFailure={onAuthFailure}
          formNavigation={formNavigation} onRefresh={read.reload} />
      </div>}
    </article>
    <ArticleRail />
    {/* 留言及輸入移到同一側欄；DOM 仍先呈現清單再呈現表單。 */}
    <section className="article-discussion" aria-label="文章留言與輸入">
      {read.data && <CommentList backend={backend} credential={credential} account={account} ready={ready}
        articleId={numericId} page={commentPage} onAuthFailure={onAuthFailure} onRead={setComments} firstPage={commentRevision} />}
      {snapshot && <div hidden={!read.data}>
        <CommentComposer article={read.data || snapshot} backend={backend} credential={credential} account={account}
          ready={ready && Boolean(read.data)} onAuthFailure={onAuthFailure} formNavigation={formNavigation}
          comments={comments} reloadComments={reloadComments} reloadArticle={read.reload} />
      </div>}
    </section>
  </div>
}

// 留言先獨立授權讀取，再嘗試父文章；父文章 404 不洩漏其標題或原因。
export function CommentDetail({ id, backend, credential, account, ready, onAuthFailure, formNavigation }) {
  const read = useApiRead(backend, credential, `/api/comments/${id}`, 'comment', positiveId(id), null, ready, onAuthFailure)
  const parent = useApiRead(backend, credential, `/api/articles/${read.data?.article_id || 0}`, 'article', read.data?.article_id, null, ready && Boolean(read.data), onAuthFailure)
  const [snapshot, setSnapshot] = useState(null)
  // 暫時失權或重讀時保留可見性操作，舊內容只留記憶體且隱藏。
  useEffect(() => { if (read.data) setSnapshot(read.data) }, [read.data])
  return <div className="editorial-layout">
    <ArticleRail />
    <div className="article-column detail">
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
      </>}
      {snapshot && <div hidden={!read.data || !account}>
        <VisibilityAction item={read.data || snapshot} kind="comment" account={account} backend={backend}
          credential={credential} active={ready && Boolean(read.data && account)} onAuthFailure={onAuthFailure}
          formNavigation={formNavigation} onRefresh={read.reload} />
      </div>}
    </div>
  </div>
}
