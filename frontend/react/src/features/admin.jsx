import { useEffect, useRef, useState } from 'react'
import { BACKENDS, validateLockout } from '../contracts.js'
import { useRead, useWrite } from '../shared/useApi.js'
import { ErrorNotice, Pagination } from '../shared/ui.jsx'

// 管理頁在 /api/me 確認角色前不送私有請求，也不呈現上次登入的資料。
function AdminGate({ status, account }) {
  return (
    <section className="content">
      <h1>管理介面</h1>
      {status === 'ready' && account?.role !== 'admin' ? <p className="notice error" role="alert">沒有權限使用此功能（403）。</p>
        : status === 'checking' ? <p role="status">正在驗證管理員身分…</p>
        : status === 'unverified' ? <p>目前無法驗證登入，請使用上方重新驗證。</p>
        : status === 'disabled' ? <p>此帳號已停用，請聯絡系統管理員。</p>
        : <p>請在原頁重新登入管理員帳號。</p>}
    </section>
  )
}

// 會員列表只送已核准的字面 keyword 與分頁參數；422 明細同時連回搜尋框。
export function Members({ listPage, search, backend, credential, enabled, status, account, onAuthFailure, switchEpoch }) {
  const [keyword, setKeyword] = useState(search)
  const [keywordError, setKeywordError] = useState(null)
  const ownerAccount = useRef(null)
  const errorSummaryRef = useRef(null)
  const accountChanged = enabled && ownerAccount.current && ownerAccount.current !== account?.id
  const query = search ? `&filters%5Bkeyword%5D=${encodeURIComponent(search)}` : ''
  const read = useRead(backend, credential, `/api/members?page=${listPage}&per_page=20${query}`, listPage, enabled && !accountChanged, onAuthFailure, switchEpoch, 'member')
  // 搜尋讀取失敗時先聚焦摘要，讓鍵盤使用者取得欄位明細與追蹤資訊。
  useEffect(() => { if (read.error) errorSummaryRef.current?.focus() }, [read.error])
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
  // 輸入保留原字並清除本地長度錯誤；舊伺服器錯誤只標記原查詢。
  function editKeyword(event) { setKeyword(event.target.value); setKeywordError(null) }
  const serverKeywordError = keyword === search ? read.error?.errors?.['filters.keyword']?.[0] : null
  const fieldError = keywordError || serverKeywordError
  const pagination = read.data?.pagination
  return (
    <section className="content admin-page">
      <p className="eyebrow">ADMIN / MEMBERS</p>
      <h1>會員管理</h1>
      <p className="intro">查詢一般會員並管理帳號狀態。</p>
      <form className="admin-toolbar" onSubmit={submit}>
        <label htmlFor="member-keyword">暱稱或 Email 關鍵字</label>
        <input id="member-keyword" value={keyword} onChange={editKeyword} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? 'member-keyword-error' : undefined} />
        <button type="submit" className="secondary">搜尋</button>
        {fieldError && <p className="field-error" id="member-keyword-error">{fieldError}</p>}
      </form>
      {read.loading && <p role="status">正在載入會員…</p>}
      <ErrorNotice error={read.error} retry={read.retry} focusRef={errorSummaryRef} />
      {read.data && <>
        {read.data.list.length === 0 ? (
          <p className="notice">{listPage > 1 || pagination.total > 0 ? '此頁沒有會員。' : search ? '找不到符合關鍵字的會員。' : '目前尚無會員。'}</p>
        ) : (
          <div className="admin-list">{read.data.list.map(
            // 每筆會員使用後端 ID 作穩定鍵，狀態以當次讀取為準。
            (item) => (
              <article className="admin-row" key={item.id}>
                <div><span className="admin-label">會員</span><h2><a href={`/react/admin/members/${item.id}`}>{item.nickname}</a></h2></div>
                <p><span className="admin-label">Email</span>{item.email}</p>
                <p><span className="admin-label">狀態</span>{item.status === 'active' ? '啟用' : '停用'}</p>
                <p><span className="admin-label">建立時間</span>{item.created_at}</p>
              </article>
            ),
          )}</div>
        )}
        {(pagination.total_pages > 1 || listPage > 1) && (
          <nav className="pagination" aria-label="會員分頁">
            <span>第 {listPage} 頁／共 {pagination.total_pages} 頁，{pagination.total} 筆</span>
            {listPage > 1 && <a href={`/react/admin/members?page=${listPage - 1}${query}`}>上一頁</a>}
            {listPage < pagination.total_pages && <a href={`/react/admin/members?page=${listPage + 1}${query}`}>下一頁</a>}
            {listPage > pagination.total_pages && <a href={`/react/admin/members?page=1${query}`}>返回第一頁</a>}
          </nav>
        )}
      </>}
    </section>
  )
}

// 會員狀態更新後只採信重新 GET 的結果；未知結果須由管理員手動查證。
export function Member({ memberId, backend, credential, enabled, status, account, onAuthFailure, switchEpoch, onDraftChange }) {
  const read = useRead(backend, credential, `/api/members/${memberId}`, null, enabled, onAuthFailure, switchEpoch, 'member', memberId)
  const write = useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange)
  const [checking, setChecking] = useState(false)
  const [saved, setSaved] = useState(false)
  // 已完成的伺服器重讀才解除成功後的等待；未知結果仍由人決定。
  useEffect(() => { if (saved && read.data) { setSaved(false); onDraftChange(false, false) } }, [saved, read.data, onDraftChange])
  // 離頁時清掉本管理操作的保護標記。
  useEffect(() => {
    // 僅清理這個元件擁有的草稿來源，其他操作仍保留自己的保護。
    return () => onDraftChange(false, false)
  }, [onDraftChange])
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
  function finishCheck() {
    if (!read.data || read.loading || read.error) return
    write.checked()
    setChecking(false)
    onDraftChange(false, false)
  }
  if (!enabled) return <AdminGate status={status} account={account} />
  return (
    <section className="content admin-page">
      <a className="back" href="/react/admin/members">← 返回會員管理</a>
      <p className="eyebrow">ADMIN / MEMBER</p>
      <h1>會員詳情</h1>
      {read.loading && <p role="status">正在讀取會員…</p>}
      <ErrorNotice error={read.error} retry={read.retry} />
      {read.data && (
        <div className="admin-detail">
          <p><strong>暱稱</strong>{read.data.nickname}</p>
          <p><strong>Email</strong>{read.data.email}</p>
          <p><strong>狀態</strong>{read.data.status === 'active' ? '啟用' : '停用'}</p>
          <p><strong>建立時間</strong>{read.data.created_at}</p>
          <button type="button" className="secondary" onClick={change} disabled={write.pending || write.uncertain || checking || saved}>{read.data.status === 'active' ? '停用會員' : '恢復會員'}</button>
        </div>
      )}
      {write.pending && <p role="status">正在更新狀態…</p>}
      {saved && <p role="status">已提交，正在重讀會員狀態…</p>}
      <ErrorNotice error={write.error} />
      {write.uncertain && (
        <div className="notice">
          <p>寫入結果尚未確認。請重讀目前狀態，再決定是否手動操作。</p>
          <button type="button" className="secondary" onClick={checkLatest}>讀取最新狀態</button>
          {checking && read.data && !read.loading && !read.error && <button type="button" className="secondary" onClick={finishCheck}>已查證狀態</button>}
        </div>
      )}
    </section>
  )
}

// 管理內容清單沿用完整資源和既有詳情／可見性操作，不提供他人內文編修。
export function ManagedContent({ listPage, kind, backend, credential, enabled, status, account, onAuthFailure, switchEpoch }) {
  const isArticle = kind === 'article'
  const path = isArticle ? `/api/articles?page=${listPage}&per_page=20&filters%5Bscope%5D=management` : `/api/comments?page=${listPage}&per_page=20`
  const read = useRead(backend, credential, path, listPage, enabled, onAuthFailure, switchEpoch, kind)
  if (!enabled) return <AdminGate status={status} account={account} />
  return (
    <section className="content admin-page">
      <p className="eyebrow">ADMIN / {isArticle ? 'ARTICLES' : 'COMMENTS'}</p>
      <h1>{isArticle ? '文章管理' : '留言管理'}</h1>
      {read.loading && <p role="status">正在載入內容…</p>}
      <ErrorNotice error={read.error} retry={read.retry} />
      {read.data && <>
        {read.data.list.length === 0 ? (
          <p className="notice">{listPage > 1 || read.data.pagination.total > 0 ? '此頁沒有資料。' : '目前尚無內容。'}</p>
        ) : (
          <div className="admin-list">{read.data.list.map(
            // 詳情會再次驗權並讀取最新版；清單只提供入口與當次狀態。
            (item) => (
              <article className="admin-row" key={item.id}>
                <div><span className="admin-label">{isArticle ? '文章' : '留言'}</span><h2><a href={`/react/${isArticle ? 'articles' : 'comments'}/${item.id}`}>{isArticle ? item.title : `留言 #${item.id}`}</a></h2></div>
                <p className="excerpt">{item.body}</p>
                <p><span className="admin-label">作者</span>{item.author.nickname}</p>
                <p><span className="admin-label">狀態</span>{item.visibility === 'visible' ? '公開' : '已下架'}</p>
                <p><span className="admin-label">建立來源</span>{BACKENDS[item.created_backend]?.[0]}</p>
              </article>
            ),
          )}</div>
        )}
        <Pagination pagination={read.data.pagination} base={`/react/admin/${isArticle ? 'articles' : 'comments'}`} />
      </>}
    </section>
  )
}

// 設定保留本頁草稿；成功後重讀四欄，未知寫入只允許人工讀回確認。
export function LockoutSettings({ backend, credential, enabled, status, account, onAuthFailure, switchEpoch, onDraftChange }) {
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
  useEffect(() => {
    // 僅清理這個元件擁有的草稿來源，其他操作仍保留自己的保護。
    return () => onDraftChange(false, false)
  }, [onDraftChange])
  // 布林開關與數值欄位只更新本地草稿，失效登入時仍不外送。
  function edit(event) {
    const { name, type, checked, value } = event.target
    setDraft({ ...draft, [name]: type === 'checkbox' ? checked : value })
    setErrors({})
    setNotice(null)
    onDraftChange(true, false)
  }
  // 本地邊界先提示；後端仍完整驗證並原子更新四欄。
  async function submit(event) {
    event.preventDefault()
    // 先確認管理資格與恢復狀態，再檢查四欄完整值，避免只送局部設定。
    if (!enabled || !draft || write.pending || write.uncertain || saved || checking) return
    const found = validateLockout(draft)
    setErrors(found)
    if (Object.keys(found).length) { setNotice({ message: '請修正標示欄位後再送出。', errors: found }); return }
    // 表單保留字串方便修正，只有這次合法 payload 轉成共同 API 的數值型態。
    onDraftChange(true, true)
    /** @type {import('../contracts.js').LockoutSettingsDto} */
    const payload = { enabled: draft.enabled, window_minutes: Number(draft.window_minutes), failure_threshold: Number(draft.failure_threshold), lock_seconds: Number(draft.lock_seconds) }
    const result = await write.send('/api/settings/login-lockout', 'PUT', payload, 200)
    // 舊身分的回應不能清草稿；成功仍等待 GET 四欄，失敗則保留輸入與明細。
    if (result.obsolete) return
    onDraftChange(true, false)
    if (result.ok) { setSaved(true); read.retry() }
    else if (result.problem) { setErrors(result.problem.errors || {}); setNotice(result.problem) }
  }
  // 寫入結果不明時只讀伺服器目前完整設定，不自動再次提交。
  function checkLatest() { setChecking(true); read.retry() }
  // 顯示伺服器四欄後才讓管理員自行選擇是否重新送出保留草稿。
  function finishCheck() {
    if (!read.data || read.loading || read.error) return
    write.checked()
    setChecking(false)
    setNotice(null)
  }
  if (!enabled) return <AdminGate status={status} account={account} />
  if (accountChanged) return <section className="content"><p role="status">正在載入管理設定…</p></section>
  return (
    <section className="content admin-page settings">
      <p className="eyebrow">ADMIN / SECURITY</p>
      <h1>登入鎖定設定</h1>
      <p className="intro">四項設定一併儲存，請以重讀結果確認生效值。</p>
      {read.loading && <p role="status">正在讀取設定…</p>}
      <ErrorNotice error={read.error} retry={read.retry} />
      {draft && (
        <form noValidate onSubmit={submit}>
          {notice && <ErrorNotice error={notice} focusRef={summary} />}
          <label className="check-row" htmlFor="enabled">
            <input id="enabled" name="enabled" type="checkbox" checked={draft.enabled} onChange={edit} disabled={write.pending || saved} />
            啟用會員登入錯誤鎖定
          </label>
          {[["window_minutes", "累計失敗時間（分鐘）", 60], ["failure_threshold", "觸發鎖定失敗次數", 100], ["lock_seconds", "鎖定時間（秒）", 86400]].map(
            // 各欄保留可見標籤與錯誤，手機也能逐欄操作。
            ([name, label, max]) => (
              <div className="setting-field" key={name}>
                <label htmlFor={name}>{label}</label>
                <input id={name} name={name} type="number" min="1" max={max} step="1" inputMode="numeric" value={draft[name]} onChange={edit} disabled={write.pending || saved} aria-invalid={Boolean(errors[name])} aria-describedby={errors[name] ? `${name}-error` : undefined} />
                {errors[name] && <p className="field-error" id={`${name}-error`}>{errors[name].join('；')}</p>}
              </div>
            ),
          )}
          <button className="primary" type="submit" disabled={write.pending || write.uncertain || saved || checking}>{write.pending ? '儲存中…' : '儲存設定'}</button>
        </form>
      )}
      {saved && <p role="status">已提交，正在重讀設定…</p>}
      {write.uncertain && write.error && !notice && <ErrorNotice error={write.error} />}
      {write.uncertain && (
        <div className="notice">
          <p>寫入結果尚未確認。草稿已保留；請先讀取目前伺服器設定，人工比較後再決定是否送出。</p>
          <button type="button" className="secondary" onClick={checkLatest}>讀取目前設定</button>
          {checking && read.data && !read.loading && !read.error && <>
            <div className="admin-detail"><p>伺服器目前值：{read.data.enabled ? '啟用' : '停用'}；{read.data.window_minutes} 分鐘；{read.data.failure_threshold} 次；{read.data.lock_seconds} 秒。</p></div>
            <button type="button" className="secondary" onClick={finishCheck}>已比較，允許手動再送</button>
          </>}
        </div>
      )}
    </section>
  )
}
