import { useEffect, useRef, useState } from 'react'
import { BACKENDS, memberKeyword, parseLockoutDraft, positiveId } from '../../lib/contracts.js'
import { useApiRead, useAuthorWrite } from '../_shared/api-hooks.js'
import { ErrorNotice, Pagination } from '../_shared/ui.jsx'

// 管理入口沿用同一組語意頁，避免在窄螢幕塞入過長的單列導覽。
export function AdminMenu() {
  return <details className="admin-menu">
    <summary>管理</summary>
    <nav aria-label="管理導覽">
      <a href="/next/admin/members">會員</a>
      <a href="/next/admin/articles">文章</a>
      <a href="/next/admin/comments">留言</a>
      <a href="/next/admin/settings/login-lockout">鎖定設定</a>
    </nav>
  </details>
}

// 瑞士式側欄與清楚的操作區延伸到管理頁，資料仍由各頁獨立讀取。
function AdminFrame({ title, children }) {
  return <div className="editorial-layout admin-layout">
    <aside className="article-rail" aria-label="管理介面">
      <p className="rail-kicker">THE ARTICLE COLLECTION</p>
      <p className="rail-title">管理<br />中心</p>
      <p className="rail-foot">會員・文章・留言</p>
    </aside>
    <div className="article-column admin-page">
      <div className="section-heading"><h1>{title}</h1><p className="eyebrow">ADMINISTRATION / 管理</p></div>
      <a className="back-link" href="/next/">← 返回文章列表</a>
      {children}
    </div>
  </div>
}

// 管理員身分只控制前端顯示與請求時機，實際授權仍由各 API 重驗。
function AdminAccess({ ready, authStatus, account }) {
  if (ready) return null
  return <p className="notice" role="status">
    {account && account.role !== 'admin'
      ? '沒有權限使用管理功能。'
      : authStatus === 'checking'
        ? '正在驗證登入…'
        : authStatus === 'error'
          ? '目前無法確認登入，請先重新驗證。'
          : authStatus === 'disabled'
            ? '此帳號已停用，不能使用管理功能。'
            : authStatus === 'expired'
              ? '請重新登入以繼續管理操作。'
              : '請先登入以使用管理功能。'}
  </p>
}

// 會員搜尋只保留核准的 keyword；伺服器錯誤聚焦摘要並連回欄位。
export function AdminMembers({ backend, credential, account, authStatus, ready, page, keyword: rawKeyword, onAuthFailure }) {
  const keyword = memberKeyword(rawKeyword)
  const [input, setInput] = useState(rawKeyword)
  const [searchError, setSearchError] = useState(null)
  const searchRef = useRef(null)
  const readErrorRef = useRef(null)
  const filter = keyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(keyword)}` : ''
  const read = useApiRead(backend, credential, `/api/members?page=${page}&per_page=20${filter}`, 'members', page, null, ready && keyword !== null, onAuthFailure)
  // 搜尋驗證失敗時聚焦摘要，可由其中的欄位按鈕回到輸入。
  useEffect(() => { if (searchError) searchRef.current?.focus() }, [searchError])
  // API 讀取失敗時聚焦獨立摘要，避免與本地搜尋錯誤互相覆蓋焦點。
  useEffect(() => { if (read.error) readErrorRef.current?.focus() }, [read.error])
  // 只將修剪後的字面值放進網址，未知字元由 URL 編碼而非自行解讀。
  function search(event) {
    event.preventDefault()
    const next = memberKeyword(input)
    if (next === null) { setSearchError({ message: '搜尋關鍵字不得超過 254 個字。', errors: { keyword: ['最多 254 個 Unicode 字元'] } }); return }
    window.location.assign(`/next/admin/members${next ? `?keyword=${encodeURIComponent(next)}` : ''}`)
  }
  // 搜尋字串變動後移除舊的長度錯誤，保留輸入直到送出。
  function changeSearch(event) { setInput(event.target.value); setSearchError(null) }
  // 輸入仍是送出中的查詢時才把伺服器欄位錯誤連到欄位。
  const serverKeywordError = input === rawKeyword ? read.error?.errors?.['filters.keyword']?.[0] : null
  const fieldError = searchError?.errors?.keyword?.[0] || serverKeywordError
  // 指定超頁優先顯示此頁無資料，首頁才區分搜尋無結果與尚無會員。
  return <AdminFrame title="會員管理">
    <AdminAccess ready={ready} authStatus={authStatus} account={account} />
    {ready && <>
      <form className="admin-toolbar" onSubmit={search}>
        <label htmlFor="keyword">搜尋暱稱或 Email</label>
        <input id="keyword" value={input} onChange={changeSearch} aria-invalid={Boolean(fieldError)} aria-describedby={fieldError ? 'keyword-error' : undefined} />
        <button className="outline-button" type="submit">搜尋會員</button>
        {fieldError && <p id="keyword-error" className="field-error">{fieldError}</p>}
      </form>
      <ErrorNotice error={searchError || (keyword === null ? { message: '網址中的搜尋關鍵字過長，請修正後再搜尋。' } : null)} focusRef={searchRef} />
      {keyword !== null && <>
        {read.loading && <p className="notice" role="status">正在載入會員…</p>}
        <ErrorNotice error={read.error} retry={read.reload} focusRef={readErrorRef} />
        {read.data && <>
          {read.data.list.length ? <ol className="admin-list">{read.data.list.map(
            // 列表只連到一般會員詳情，不在前端推測狀態寫入結果。
            (member) => <li className="admin-row" key={member.id}>
              <div><small>會員 #{member.id}</small><h2><a href={`/next/admin/members/${member.id}`}>{member.nickname}</a></h2></div>
              <p className="break"><span className="row-label">Email </span>{member.email}</p>
              <p><span className="row-label">狀態 </span>{member.status === 'active' ? '啟用' : '停用'}</p>
              <a href={`/next/admin/members/${member.id}`}>查看詳情</a>
            </li>,
          )}</ol> : <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有會員資料。' : keyword ? '沒有符合搜尋條件的會員。' : '目前尚無會員。'}</p>}
          <Pagination pagination={read.data.pagination} base="/next/admin/members" query={keyword ? `keyword=${encodeURIComponent(keyword)}` : ''} />
        </>}
      </>}
    </>}
  </AdminFrame>
}

// 停用／恢復後只相信重新讀取的伺服器狀態；不把空成功回應當成新狀態。
export function AdminMember({ id, backend, credential, account, authStatus, ready, onAuthFailure, formNavigation }) {
  const numericId = positiveId(id)
  const read = useApiRead(backend, credential, `/api/members/${id}`, 'member', numericId, null, ready, onAuthFailure)
  const write = useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, `member-status-${id}`)
  const [checking, setChecking] = useState(false)
  // 寫入未確認時須由人重新讀回，不能自動重送同一狀態。
  async function change() {
    if (!write.sameAccount || !read.data || write.pending || write.uncertain || write.conflict || !window.confirm(`確定${read.data.status === 'active' ? '停用' : '恢復'}這位會員嗎？`)) return
    const status = read.data.status === 'active' ? 'disabled' : 'active'
    const result = await write.send(`/api/members/${id}/status`, 'PUT', { status }, 200, ['status'])
    if (result.ok) { setChecking(false); read.reload() }
  }
  // 查證啟動時清除先前資料，讀取完成後才可解鎖下一次手動操作。
  function check() { setChecking(true); read.reload() }
  // 使用者親自比對最新狀態後解鎖，下一次點擊仍須再次確認。
  function accept() {
    if (!read.data || read.loading || read.error || !write.sameAccount) return
    write.checked()
    setChecking(false)
  }
  return <AdminFrame title="會員詳情">
    <a className="back-link" href="/next/admin/members">← 返回會員列表</a>
    <AdminAccess ready={ready} authStatus={authStatus} account={account} />
    {ready && <>
      {read.loading && <p className="notice" role="status">正在讀取會員…</p>}
      <ErrorNotice error={read.error} retry={read.reload} />
      {read.data && <div className="admin-detail">
        <p><strong>會員 ID</strong><span>{read.data.id}</span></p>
        <p><strong>暱稱</strong><span>{read.data.nickname}</span></p>
        <p><strong>Email</strong><span>{read.data.email}</span></p>
        <p><strong>角色</strong><span>一般會員</span></p>
        <p><strong>狀態</strong><span>{read.data.status === 'active' ? '啟用' : '停用'}</span></p>
        <p><strong>加入時間</strong><span>{read.data.created_at}</span></p>
      </div>}
      {!write.sameAccount ? <p className="notice">登入帳號已變更，請重新開啟此頁再管理會員。</p> : <>
        <ErrorNotice error={write.error} />
        {(write.uncertain || write.conflict) && <div className="notice">
          <p>結果尚未確認。請重新讀取並核對會員狀態，不會自動重送。</p>
          <button type="button" className="outline-button" onClick={check} disabled={read.loading}>重讀會員狀態</button>
          {checking && read.data && <button type="button" className="outline-button" onClick={accept}>已核對最新狀態</button>}
        </div>}
        {read.data && <button type="button" className="red-button" onClick={change} disabled={write.pending || write.uncertain || write.conflict}>
          {write.pending ? '處理中…' : read.data.status === 'active' ? '停用會員' : '恢復會員'}
        </button>}
      </>}
    </>}
  </AdminFrame>
}

// 管理列表使用既有內容詳情與可見性控制，避免開放修改其他作者內文。
export function AdminContent({ kind, backend, credential, account, authStatus, ready, page, onAuthFailure }) {
  const article = kind === 'article'
  const route = article ? 'articles' : 'comments'
  const path = article ? `/api/articles?page=${page}&per_page=20&filters%5Bscope%5D=management` : `/api/comments?page=${page}&per_page=20`
  const read = useApiRead(backend, credential, path, article ? 'articles' : 'managed-comments', page, null, ready, onAuthFailure)
  // 零筆超頁仍保留 API 原頁碼與返回第一頁入口，不宣稱整個管理列表為空。
  return <AdminFrame title={article ? '文章管理' : '留言管理'}>
    <AdminAccess ready={ready} authStatus={authStatus} account={account} />
    {ready && <>
      {read.loading && <p className="notice" role="status">正在載入{article ? '文章' : '留言'}…</p>}
      <ErrorNotice error={read.error} retry={read.reload} />
      {read.data && <>
        {read.data.list.length ? <ol className="admin-list">{read.data.list.map(
          // 完整內文和版本操作留在已知 ID 詳情，列表保留作者、來源及狀態供辨識。
          (item) => <li className="admin-row" key={item.id}>
            <div><small>{article ? '文章' : '留言'} #{item.id}</small><h2><a href={`/next/${route}/${item.id}`}>{article ? item.title : `留言 #${item.id}`}</a></h2></div>
            <p>作者 {item.author.nickname}<br />建立於 {BACKENDS[item.created_backend][0]}</p>
            <p><span className="row-label">狀態 </span>{item.visibility === 'hidden' ? '已下架' : '公開'}</p>
            <a href={`/next/${route}/${item.id}`}>查看詳情</a>
            <p className="excerpt">{item.body}</p>
          </li>,
        )}</ol> : <p className="notice">{page > 1 || read.data.pagination.total > 0 ? '此頁沒有資料。' : `目前尚無${article ? '文章' : '留言'}。`}</p>}
        <Pagination pagination={read.data.pagination} base={`/next/admin/${route}`} />
      </>}
    </>}
  </AdminFrame>
}

// 設定表單保留未送出的值；PUT 期間停用輸入，成功後才以讀回四欄取代草稿。
export function AdminSettings({ backend, credential, account, authStatus, ready, onAuthFailure, formNavigation }) {
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
  function accept() {
    if (!read.data || read.loading || read.error || !sameAccount) return
    write.checked()
    setChecking(false)
  }
  return <AdminFrame title="登入鎖定設定">
    <AdminAccess ready={ready} authStatus={authStatus} account={account} />
    {ready && <>
      {read.loading && <p className="notice" role="status">正在讀取設定…</p>}
      <ErrorNotice error={read.error} retry={read.reload} />
      {saved && !read.loading && read.data && <p className="notice success" role="status">已儲存；以下數值已重新向伺服器讀取。</p>}
      {!sameAccount && <p className="notice error">登入帳號已變更，舊帳號的設定草稿不可由目前帳號送出。請重新開啟此頁。</p>}
      {draft && sameAccount && <form className="account-form settings-form" onSubmit={save}>
        <ErrorNotice error={localError || write.error} focusRef={errorRef} />
        <label className="setting-toggle">
          <input id="enabled" type="checkbox" checked={draft.enabled} onChange={changeEnabled} disabled={write.pending}
            aria-invalid={Boolean((localError || write.error)?.errors?.enabled)} aria-describedby={(localError || write.error)?.errors?.enabled ? 'enabled-error' : undefined} />
          啟用會員登入錯誤鎖定
        </label>
        {(localError || write.error)?.errors?.enabled && <p id="enabled-error" className="field-error">{(localError || write.error).errors.enabled.join('；')}</p>}
        <p className="hint">停用仍保留以下三個數值；重新啟用時沿用。</p>
        {[['window_minutes', '累計失敗時間（分鐘）', 60], ['failure_threshold', '觸發鎖定次數', 100], ['lock_seconds', '鎖定秒數', 86400]].map(
          // 各欄保留可見標籤、上限與 422 明細的焦點目標。
          ([name, label, max]) => <div className="setting-field" key={name}>
            <label htmlFor={name}>{label}</label>
            <input id={name} inputMode="numeric" value={draft[name]} onChange={changeNumber} disabled={write.pending}
              aria-invalid={Boolean((localError || write.error)?.errors?.[name])} aria-describedby={(localError || write.error)?.errors?.[name] ? `${name}-error` : undefined} />
            <p className="hint">1–{max}</p>
            {(localError || write.error)?.errors?.[name]?.map(
              // 同欄多個明細仍保留，第一個錯誤供 aria-describedby 定位。
              (message, index) => <p id={index === 0 ? `${name}-error` : undefined} className="field-error" key={`${index}-${message}`}>{message}</p>,
            )}
          </div>,
        )}
        {(write.uncertain || write.conflict) && <div className="notice">
          <p>結果尚未確認。請重讀並核對目前設定，不會自動重送。</p>
          <button className="outline-button" type="button" onClick={check} disabled={read.loading}>重讀目前設定</button>
          {checking && read.data && <>
            <p>伺服器目前：{read.data.enabled ? '啟用' : '停用'}，{read.data.window_minutes} 分鐘／{read.data.failure_threshold} 次／{read.data.lock_seconds} 秒。</p>
            <button className="outline-button" type="button" onClick={accept}>已核對目前設定</button>
          </>}
        </div>}
        <button className="red-button" type="submit" disabled={write.pending || write.uncertain || write.conflict}>{write.pending ? '儲存中…' : '儲存設定'}</button>
      </form>}
    </>}
  </AdminFrame>
}
