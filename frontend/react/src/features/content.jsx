import { useCallback, useEffect, useRef, useState } from 'react'
import { BACKENDS, canEditContent, canHideContent, canRestoreContent, emptyCommentMessage, showCommentPagination, validateContent } from '../contracts.js'
import { useRead, useWrite } from '../shared/useApi.js'
import { ErrorNotice, Pagination } from '../shared/ui.jsx'

// 顯示文章原始時間與後端來源；純文字由 React 轉義，不解讀 HTML。
function Metadata({ item }) {
  const source = BACKENDS[item.created_backend]?.[0] || item.created_backend
  return (
    <div className="metadata">
      <span>作者　{item.author?.nickname}</span>
      <span>{item.created_at}</span>
      <span>建立於　<b>{source}</b></span>
    </div>
  )
}

// 下架／恢復共用版本比對與查證規則；權限提示只供 UI，後端再次判定。
function VisibilityActions({ item, read, kind, account, backend, credential, onAuthFailure, switchEpoch, onRefresh, enabled, onDraftChange }) {
  const write = useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange)
  const [conflict, setConflict] = useState(false)
  const [checking, setChecking] = useState(false)
  // 離頁撤銷目前操作的離頁標記，不影響同頁留言草稿。
  useEffect(() => {
    // 僅清理這個元件擁有的草稿來源，其他操作仍保留自己的保護。
    return () => onDraftChange(false, false)
  }, [onDraftChange])
  const canHide = canHideContent(item, account)
  const canRestore = canRestoreContent(item, account)
  // 操作前再次確認動作，避免無意下架；成功後僅採信重新讀取的狀態。
  async function change(visibility) {
    if (!enabled || checking || write.pending || conflict || write.uncertain || !(visibility === 'hidden' ? canHide : canRestore)) return
    const action = visibility === 'hidden' ? item.visibility === 'hidden' ? '再次下架並接管恢復權' : '下架' : '恢復'
    if (!window.confirm(`確定要${action}嗎？`)) return
    // 確認後固定這次資源版本並標記在途；取消請求不代表伺服器回滾。
    onDraftChange(true, true)
    const result = await write.send(`/api/${kind}s/${item.id}/visibility`, 'PUT', { visibility, version: item.version }, 200)
    // 舊憑證或舊世代沒有收尾資格，避免清掉重登後的新操作。
    if (result.obsolete) return
    onDraftChange(Boolean(result.uncertain), false)
    // 成功重新讀取目前狀態；衝突與未知結果保留人工確認入口。
    if (result.ok) onRefresh()
    else if (result.problem?.code === 'E0201') setConflict(true)
    else if (result.problem?.code === 'E0004') onRefresh()
  }
  // 重讀只更新同一資源的查證資料；請求失敗時仍保留未知結果與重送鎖。
  function checkLatest() { setChecking(true); onRefresh() }
  // 原操作的查證由使用者確認；同頁留言成功不能替這筆狀態寫入解鎖。
  function finishCheck() {
    if (!enabled || !checking || !read.data || read.loading || read.error) return
    write.checked()
    setConflict(false)
    setChecking(false)
    onDraftChange(false, false)
  }
  // 下架仍走同一次確認與版本檢查，避免另一路徑漏掉保護。
  function hide() { change('hidden') }
  // 恢復操作由最新 can_restore 及後端再次判斷。
  function restore() { change('visible') }
  return (
    <div className="content-actions">
      {canHide && (
        <button type="button" className="secondary" disabled={!enabled || write.pending || write.uncertain || conflict || checking} onClick={hide}>
          {item.visibility === 'hidden' ? '再次下架並接管恢復權' : '下架'}
        </button>
      )}
      {canRestore && <button type="button" className="secondary" disabled={!enabled || write.pending || write.uncertain || conflict || checking} onClick={restore}>恢復</button>}
      {item.visibility === 'hidden' && !canRestore && <p>目前無恢復資格。</p>}
      {write.pending && <p role="status">正在處理狀態…</p>}
      <ErrorNotice error={write.error} />
      {(conflict || write.uncertain) && (
        <div className="notice">
          <p>{conflict ? '內容版本已變更；請讀取最新狀態並重新確認操作。' : '結果尚未確認；請先查證目前狀態。'}</p>
          <button type="button" className="secondary" onClick={checkLatest}>讀取最新狀態</button>
          {checking && read.data && !read.loading && !read.error && (
            <button type="button" className="secondary" onClick={finishCheck}>已查證狀態</button>
          )}
        </div>
      )}
    </div>
  )
}

// 我的內容以既有完整資源列表呈現，隱藏項也保留詳情入口供重新驗權。
export function MyContent({ listPage, kind, backend, credential, enabled, onAuthFailure, switchEpoch }) {
  const isArticle = kind === 'article'
  const path = isArticle ? '/me/articles' : '/me/comments'
  const read = useRead(backend, credential, `/api${path}?page=${listPage}&per_page=20`, listPage, enabled, onAuthFailure, switchEpoch, kind)
  return (
    <section className="content">
      <p className="eyebrow">MY CONTENT / REACT</p>
      <h1>{isArticle ? '我的文章' : '我的留言'}</h1>
      {read.loading && enabled && <p role="status">正在載入…</p>}
      <ErrorNotice error={read.error} retry={read.retry} />
      {read.data && <>
        {/* 指定超頁即使總數為零，也只表示這一頁沒有資料。 */}
        {read.data.list.length === 0
          ? <p className="notice">{read.data.pagination.page > 1 || read.data.pagination.total > 0 ? '此頁沒有資料。' : '目前尚無內容。'}</p>
          : <div className="article-list">
            {read.data.list.map(
              // 穩定 ID 對應個別內容，留言的 article_id 不授予父文章讀取權。
              (item) => (
                <article className="article-row" key={item.id}>
                  <h2><a href={`/react/${isArticle ? 'articles' : 'comments'}/${item.id}`}>{isArticle ? item.title : `留言 #${item.id}`}</a></h2>
                  <p className="excerpt">{item.body}</p>
                  <Metadata item={item} />
                  <p>{item.visibility === 'hidden' ? '已下架' : '公開'}</p>
                </article>
              ),
            )}
          </div>}
        <Pagination pagination={read.data.pagination} base={`/react${path}`} />
      </>}
    </section>
  )
}

// 發文與編修共用純文字表單；草稿只留在本頁記憶體，不隨切換傳送。
export function AuthorEditor({ kind, resourceId, backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange }) {
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
    // 初次快照只初始化一次；後續查證不覆蓋輸入或自動採用新版本。
    if (read.data && !initial.current) {
      original.current = read.data
      if (isArticle) setTitle(read.data.title)
      setBody(read.data.body)
      setVersion(read.data.version)
      initial.current = true
    }
    if (read.data && blocked) setBlocked(false)
  }, [read.data, isArticle, credential, blocked])
  // 錯誤摘要取得焦點，讓鍵盤使用者能立即前往欄位修正。
  useEffect(() => { if (notice) summary.current?.focus() }, [notice])
  // 卸載後清除本頁草稿標記，不讓其他頁面誤繼承確認對話。
  useEffect(() => {
    // 僅清理這個元件擁有的草稿來源，其他操作仍保留自己的保護。
    return () => onDraftChange(false, false)
  }, [onDraftChange])
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
  function acceptLatest() {
    if (!read.data || read.loading || read.error) return
    setVersion(read.data.version)
    setConflict(false)
    setChecked(false)
    write.checked()
    setNotice(null)
  }
  // 送出前先驗證本地欄位與目前身份；後端仍執行授權及版本原子比對。
  async function submit(event) {
    event.preventDefault()
    // 目前身分、父文章與版本恢復狀態都允許後，才驗證本地純文字。
    if (!enabled || !allowed || !parentVisible || write.pending || write.uncertain || conflict) return
    /** @type {import('../contracts.js').ArticleInputDto | import('../contracts.js').CommentInputDto} */
    const input = isArticle ? { title, body } : { body }
    const nextErrors = validateContent(input, kind)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length) { setNotice({ message: '請修正標示欄位後再送出。', errors: nextErrors }); return }
    // 只提交本次文字與使用者採用的版本，不因衝突或連線失敗自動再送。
    onDraftChange(true, true)
    const result = await write.send(creating ? '/api/articles' : resourcePath, creating ? 'POST' : 'PUT', creating ? input : { ...input, version }, creating ? 201 : 200)
    // 新登入或切換已接管畫面時，不讓舊回應清稿或導覽。
    if (result.obsolete) return
    // 完整成功先解除離頁保護再導覽，其餘分支保留輸入供修正或查證。
    if (result.ok) { onDraftChange(false, false); window.location.assign(creating ? '/react/me/articles' : `/react/${isArticle ? 'articles' : 'comments'}/${resourceId}`); return }
    onDraftChange(true, false)
    if (result.problem) { setNotice(result.problem); setErrors(result.problem.errors || {}) }
    // 版本衝突必須手動比較；失去可讀性則隱藏快照並重新驗權。
    if (result.problem?.code === 'E0201') setConflict(true)
    if (result.problem?.code === 'E0003' || result.problem?.code === 'E0004') { setBlocked(true); read.retry(); if (!isArticle) parent.retry() }
  }
  return (
    <section className="content editor">
      <p className="eyebrow">AUTHOR / REACT</p>
      <h1>{creating ? '發表文章' : isArticle ? '編輯文章' : '編輯留言'}</h1>
      {creating && <p className="intro">文章會公開發表，建立來源由目前後端記錄。</p>}
      {!creating && read.loading && enabled && <p role="status">正在讀取內容…</p>}
      {!creating && <ErrorNotice error={read.error} retry={read.retry} />}
      {!isArticle && sourceItem && (
        <section className="related">
          <h2>所屬文章</h2>
          {parent.loading && <p role="status">正在確認文章可見性…</p>}
          {parent.data && <p><a href={`/react/articles/${parent.data.id}`}>{parent.data.title}</a> · {parent.data.visibility === 'visible' ? '公開' : '已下架'}</p>}
          {parent.error && <>
            <ErrorNotice error={{ ...parent.error, message: parent.error.code === 'E0004' ? '所屬文章目前無法讀取。' : parent.error.message }} />
            <button type="button" className="secondary" onClick={parent.retry}>單獨重試文章讀取</button>
          </>}
        </section>
      )}
      {!creating && read.data && !allowed && <p className="notice error">只有內容作者可編修；管理員自己的內容也依作者規則。</p>}
      {notice && <ErrorNotice error={notice} focusRef={summary} />}
      {(creating || initial.current) && allowed && <>
        {creating ? <p>發表後由目前後端記錄建立來源、作者與時間。</p> : <Metadata item={sourceItem} />}
        {write.uncertain && (
          <div className="notice">
            <p>結果尚未確認；請先查證資料，不會自動重送。</p>
            <a href={creating ? '/react/me/articles' : `/react/${isArticle ? 'articles' : 'comments'}/${resourceId}`}>前往查證</a>
            {!creating && <button type="button" className="secondary" onClick={checkLatest}>在此重讀</button>}
          </div>
        )}
        {creating && write.uncertain && (
          <div className="related">
            <button type="button" className="secondary" onClick={checkCreated}>查證我的文章第一頁</button>
            {checkingUnknown && verification.loading && <p role="status">正在讀取我的文章…</p>}
            {checkingUnknown && <ErrorNotice error={verification.error} retry={verification.retry} />}
            {checkingUnknown && verification.data && <>
              <p>此列表供人工查證，不能只憑第一頁判定這次是否建立成功。</p>
              <ul>{verification.data.list.map(
                // 顯示資源 ID 與時間協助人工比較，不替使用者推定建立結果。
                (item) => <li key={item.id}><a href={`/react/articles/${item.id}`}>{item.title}</a> · {item.created_at}</li>,
              )}</ul>
              <button type="button" className="secondary" onClick={acceptCreatedCheck}>已查證，允許手動再送</button>
            </>}
          </div>
        )}
        {conflict && (
          <div className="notice">
            <p>版本衝突。請先讀取最新內容，比較後自行整合；不會自動重送。</p>
            <button type="button" className="secondary" onClick={checkLatest}>讀取最新版本</button>
          </div>
        )}
        {checked && read.data && !read.loading && !read.error && (
          <div className="comparison">
            <h2>最新版本（僅供比較）</h2>
            {isArticle && <p className="prose">{read.data.title}</p>}
            <p className="prose">{read.data.body}</p>
            <p>版本 {read.data.version}</p>
            <button type="button" className="secondary" onClick={acceptLatest}>已比較，採用最新版本號</button>
          </div>
        )}
        <form noValidate onSubmit={submit}>
          {isArticle && <>
            <label htmlFor="title">標題</label>
            <input id="title" value={title} onChange={editTitle} disabled={!enabled || write.pending} aria-invalid={Boolean(errors.title)} aria-describedby={errors.title ? 'title-error' : undefined} />
            {errors.title && <p className="field-error" id="title-error">{errors.title.join('；')}</p>}
          </>}
          <label htmlFor="body">{isArticle ? '文章內文' : '留言內容'}</label>
          <textarea id="body" rows={10} value={body} onChange={editBody} disabled={!enabled || write.pending} aria-invalid={Boolean(errors.body)} aria-describedby={errors.body ? 'body-error' : undefined} />
          {errors.body && <p className="field-error" id="body-error">{errors.body.join('；')}</p>}
          {!parentVisible && !isArticle && <p className="notice">確認所屬文章公開後才能送出留言編修。</p>}
          <button className="primary" type="submit" disabled={!enabled || write.pending || write.uncertain || conflict || !parentVisible}>
            {write.pending ? '處理中…' : creating ? '發表文章' : '儲存修改'}
          </button>
        </form>
      </>}
      <a className="back" href={creating ? '/react/me/articles' : isArticle ? `/react/articles/${resourceId}` : `/react/comments/${resourceId}`}>返回{creating ? '我的文章' : '詳情'}</a>
    </section>
  )
}

// 夜景舞台只裝飾公開列表；文章仍依後端順序呈現，保留空資料、超頁與讀取失敗區別。
export function Articles({ listPage, backend, credential, enabled, onAuthFailure, switchEpoch }) {
  const read = useRead(backend, credential, `/api/articles?page=${listPage}&per_page=20`, listPage, enabled, onAuthFailure, switchEpoch)
  return <section className="content cinema-index" aria-labelledby="articles-title">
    <div className="cinema-stage"><h1 id="articles-title">所有文章</h1></div>
    {read.loading && <p role="status">正在載入文章…</p>}
    <ErrorNotice error={read.error} retry={read.retry} />
    {read.data && <>
      {/* 只有首頁總數為零才是整個列表尚無文章；超頁保留原分頁資訊。 */}
      {read.data.list.length === 0 ? <p className="notice">{read.data.pagination.page > 1 || read.data.pagination.total > 0 ? '此頁沒有文章。' : '目前尚無文章。'}</p> : <div className="article-list">{read.data.list.map(
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
function CommentComposer({ pathname, article, backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange, onCreated, onArticleRefresh, commentsRead }) {
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
  useEffect(() => {
    // 僅清理這個元件擁有的草稿來源，其他操作仍保留自己的保護。
    return () => onDraftChange(false, false)
  }, [onDraftChange])
  // 保留純文字原值，僅清理這次輸入的錯誤顯示。
  function edit(event) { setBody(event.target.value); setNotice(null); setErrors({}); onDraftChange(true, false) }
  // 新留言沒有 ID；只讀文章留言第一頁供人工查證，不猜最新留言就是本次新增。
  function checkCreated() { setCheckingUnknown(true); onCreated() }
  // 人工看過重讀結果後才解鎖，原留言文字仍由使用者自行決定是否重送。
  function acceptCreatedCheck() { write.checked(); setCheckingUnknown(false); setNotice(null) }
  // 新留言沒有回傳 ID；成功留在文章頁並從第一頁重讀，不猜新資源。
  async function submit(event) {
    event.preventDefault()
    // 公開文章與目前帳號都確認後才驗證文字，避免在失權期間送出。
    if (!allowed || write.pending || write.uncertain) return
    const found = validateContent({ body }, 'comment')
    setErrors(found)
    if (Object.keys(found).length) { setNotice({ message: '請修正標示欄位後再送出。', errors: found }); return }
    // 送出後獨立追蹤這筆新增，其他可見性操作不能替它解除未知結果。
    onDraftChange(true, true)
    const result = await write.send(`/api/articles/${article.id}/comments`, 'POST', { body }, 201)
    // 舊回應不得清掉重新登入後的草稿或新請求；只有完整成功清稿並重讀。
    if (result.obsolete) return
    if (result.ok) { setBody(''); onDraftChange(false, false); setNotice({ message: '留言已送出，正在重讀第一頁。' }); onCreated(); return }
    // 拒絕與未知結果都保留文字；權限改變時由文章 GET 再決定可讀範圍。
    onDraftChange(true, false)
    if (result.problem) { setNotice(result.problem); setErrors(result.problem.errors || {}) }
    if (result.problem?.code === 'E0003' || result.problem?.code === 'E0004') onArticleRefresh()
  }
  return (
    <section className="comment-composer" aria-labelledby="comment-compose-title">
      <h3 id="comment-compose-title">發表留言</h3>
      {article.visibility === 'hidden' ? <p>文章已下架，暫時無法新增留言。</p> : !account ? (
        <p>登入後可以留言。<a href={`/react/login?return_to=${encodeURIComponent(pathname)}`}>前往登入</a></p>
      ) : <>
        {notice && <ErrorNotice error={notice} focusRef={summary} />}
        {write.uncertain && (
          <div className="notice">
            結果尚未確認，請先查看留言第一頁，確認後才決定是否重送。
            <button type="button" className="secondary" onClick={checkCreated}>重讀留言第一頁</button>
            {checkingUnknown && commentsRead.data && !commentsRead.loading && !commentsRead.error && (
              <button type="button" className="secondary" onClick={acceptCreatedCheck}>已查證，允許手動再送</button>
            )}
          </div>
        )}
        <form noValidate onSubmit={submit}>
          <label htmlFor="body">留言內容</label>
          <textarea id="body" rows={5} value={body} onChange={edit} disabled={!allowed || write.pending} aria-invalid={Boolean(errors.body)} aria-describedby={errors.body ? 'body-error' : undefined} />
          {errors.body && <p id="body-error" className="field-error">{errors.body.join('；')}</p>}
          <button className="primary" type="submit" disabled={!allowed || write.pending || write.uncertain}>{write.pending ? '送出中…' : '發表留言'}</button>
        </form>
      </>}
    </section>
  )
}

// 文章本體與留言各自有載入／錯誤狀態，作者動作仍以目前後端授權為準。
export function Article({ articleId, listPage, pathname, backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange }) {
  const article = useRead(backend, credential, `/api/articles/${articleId}`, null, enabled, onAuthFailure, switchEpoch, 'article', articleId)
  const [snapshot, setSnapshot] = useState(null)
  const [commentPage, setCommentPage] = useState(listPage)
  const comments = useRead(backend, credential, `/api/articles/${articleId}/comments?page=${commentPage}&per_page=20`, commentPage, enabled && Boolean(article.data), onAuthFailure, switchEpoch, 'comment')
  // 留言草稿與可見性寫入分別追蹤，互不清除對方的離頁保護。
  const changeCommentDraft = useCallback((dirty, pending) => onDraftChange(dirty, pending, 'comment'), [onDraftChange])
  // 短暫的下架請求不會把尚未送出的留言當作已儲存。
  const changeVisibilityDraft = useCallback((dirty, pending) => onDraftChange(dirty, pending, 'visibility'), [onDraftChange])
  // 快照維持留言草稿與可見性寫入的生命週期；重讀或失權時不顯示舊內容。
  useEffect(() => { if (article.data) setSnapshot(article.data) }, [article.data])
  // 留言分頁只影響留言讀取，不搬移文章或其他列表狀態。
  function previousComments() { setCommentPage(commentPage - 1) }
  // 下一頁由後端 total_pages 決定，不猜測更多資料。
  function nextComments() { setCommentPage(commentPage + 1) }
  // 合法超頁返回第一頁，保留 API 實際總筆數的判斷。
  function firstComments() { setCommentPage(1) }
  // 新留言後先回第一頁再重讀，避免以當前超頁錯認建立失敗。
  function reloadFirstComments() { setCommentPage(1); comments.retry() }
  return (
    <section className="content reading">
      <a className="back" href={account?.role === 'admin' ? '/react/admin/articles' : '/react/'}>← 返回{account?.role === 'admin' ? '文章管理' : '文章列表'}</a>
      {article.loading && <p role="status">正在載入文章…</p>}
      <ErrorNotice error={article.error} retry={article.retry} />
      {article.data && <>
        <article>
          <p className="eyebrow">ARTICLE / {article.data.id}</p>
          <h1>{article.data.title}</h1>
          <Metadata item={article.data} />
          <p>{article.data.visibility === 'hidden' ? '已下架' : '公開'}</p>
          <div className="prose">{article.data.body}</div>
        </article>
        {canEditContent(article.data, account) && <a className="back" href={`/react/articles/${article.data.id}/edit`}>編輯文章</a>}
      </>}
      {/* 操作留在留言列表之前，重讀只隱藏，不卸載各自的寫入狀態。 */}
      {snapshot && <div hidden={!article.data || !account}>
        <VisibilityActions item={article.data || snapshot} read={article} kind="article" account={account} backend={backend} credential={credential} onAuthFailure={onAuthFailure} switchEpoch={switchEpoch} onRefresh={article.retry} enabled={enabled && Boolean(article.data) && Boolean(account)} onDraftChange={changeVisibilityDraft} />
      </div>}
      {article.data && <>
        <section className="comments" aria-labelledby="comments-title">
          <h2 id="comments-title">留言</h2>
          {comments.loading && <p role="status">正在載入留言…</p>}
          <ErrorNotice error={comments.error} retry={comments.retry} />
          {comments.data && <>
            {/* 指定超頁先顯示此頁無資料，避免把第 99 頁誤說成首頁尚無留言。 */}
            {comments.data.list.length === 0
              ? <p className="notice">{emptyCommentMessage(comments.data.pagination)}</p>
              : comments.data.list.map(
                // 使用留言 ID 保留穩定身分，全文依純文字呈現。
                (item) => (
                  <article className="comment" key={item.id}>
                    <Metadata item={item} />
                    <p className="prose">{item.body}</p>
                    {account && (account.id === item.author.id || account.role === 'admin') && <a href={`/react/comments/${item.id}`}>留言詳情</a>}
                  </article>
                ),
              )}
            {/* 留言分頁直接呈現 API 原頁碼與總筆數，超頁也不誤報為零筆。 */}
            {showCommentPagination(comments.data.pagination) && (
              <nav className="pagination" aria-label="留言分頁">
                <span>第 {comments.data.pagination.page} 頁／共 {comments.data.pagination.total_pages} 頁，{comments.data.pagination.total} 筆</span>
                {commentPage > 1 && <button className="text-button" onClick={previousComments}>上一頁</button>}
                {commentPage < comments.data.pagination.total_pages && <button className="text-button" onClick={nextComments}>下一頁</button>}
                {commentPage > comments.data.pagination.total_pages && <button className="text-button" onClick={firstComments}>返回第一頁</button>}
              </nav>
            )}
          </>}
        </section>
      </>}
      {/* 保留各寫入元件的生命週期；重讀或失權時隱藏快照，成功重驗後才恢復操作。 */}
      {snapshot && (
        <div hidden={!article.data}>
          <CommentComposer pathname={pathname} article={article.data || snapshot} backend={backend} credential={credential} account={account} enabled={enabled && Boolean(article.data)} onAuthFailure={onAuthFailure} switchEpoch={switchEpoch} onDraftChange={changeCommentDraft} onCreated={reloadFirstComments} onArticleRefresh={article.retry} commentsRead={comments} />
        </div>
      )}
    </section>
  )
}

// 留言詳情先依留言權限讀取；父文章獨立讀取，404 不推測其隱藏原因。
export function CommentDetail({ commentId, backend, credential, account, enabled, onAuthFailure, switchEpoch, onDraftChange }) {
  const comment = useRead(backend, credential, `/api/comments/${commentId}`, null, enabled, onAuthFailure, switchEpoch, 'comment', commentId)
  const [snapshot, setSnapshot] = useState(null)
  // 快照只讓狀態寫入元件跨重讀存活；未驗證資料仍由 hidden 隔離。
  useEffect(() => { if (comment.data) setSnapshot(comment.data) }, [comment.data])
  const parent = useRead(backend, credential, `/api/articles/${comment.data?.article_id || 0}`, null, enabled && Boolean(comment.data), onAuthFailure, switchEpoch, 'article', comment.data?.article_id)
  return (
    <section className="content reading">
      <a className="back" href={account?.role === 'admin' ? '/react/admin/comments' : '/react/me/comments'}>← 返回{account?.role === 'admin' ? '留言管理' : '我的留言'}</a>
      <p className="eyebrow">COMMENT / REACT</p>
      <h1>留言詳情</h1>
      {comment.loading && enabled && <p role="status">正在讀取留言…</p>}
      <ErrorNotice error={comment.error} retry={comment.retry} />
      {comment.data && <>
        <article>
          <Metadata item={comment.data} />
          <p>{comment.data.visibility === 'hidden' ? '已下架' : '公開'}</p>
          <div className="prose">{comment.data.body}</div>
        </article>
        <section className="related">
          <h2>所屬文章</h2>
          {parent.loading && <p role="status">正在讀取所屬文章…</p>}
          {parent.data && <p><a href={`/react/articles/${parent.data.id}`}>{parent.data.title}</a> · {parent.data.visibility === 'hidden' ? '已下架' : '公開'}</p>}
          {parent.error && <>
            <ErrorNotice error={{ ...parent.error, message: parent.error.code === 'E0004' ? '所屬文章目前無法讀取。' : parent.error.message }} />
            <button type="button" className="secondary" onClick={parent.retry}>單獨重試文章讀取</button>
          </>}
        </section>
        {canEditContent(comment.data, account, parent.data?.visibility === 'visible') && <a className="back" href={`/react/comments/${comment.data.id}/edit`}>編輯留言</a>}
      </>}
      {snapshot && <div hidden={!comment.data || !account}>
        <VisibilityActions item={comment.data || snapshot} read={comment} kind="comment" account={account} backend={backend} credential={credential} onAuthFailure={onAuthFailure} switchEpoch={switchEpoch} onRefresh={comment.retry} enabled={enabled && Boolean(comment.data) && Boolean(account)} onDraftChange={onDraftChange} />
      </div>}
    </section>
  )
}
