'use client'

import { useEffect, useRef, useState } from 'react'
import { apiFailure, apiRequest, BACKENDS, canSwitchFrontend, clearSessionIfOwned, emptySuccess, FRONTENDS, frontendTarget, ownsSession, pageNumber, positiveId, readBackend, readSession, selectBackend, SESSION_KEY, successData, validReturn } from '../lib/contracts.js'
import { AccountForm } from './_features/account.jsx'
import { ArticleList, ArticlePage, CommentDetail, ContentEditor, MyContent } from './_features/content.jsx'
import { AdminContent, AdminMember, AdminMembers, AdminMenu, AdminSettings } from './_features/admin.jsx'
import { ErrorNotice } from './_shared/ui.jsx'

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
        (entry) => entry.dirty || entry.pending || entry.unresolved,
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
      (entry) => entry.pending || entry.unresolved,
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
      entry.unresolved = false
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
    <div className="brand-row"><a className="brand" href="/next/">文章集</a><nav className="desktop-nav" aria-label="主要導覽"><a aria-current={path === '/next/' ? 'page' : undefined} href="/next/">所有文章</a>{account ? <><a href="/next/articles/new">發表文章</a><a href="/next/me/articles">我的文章</a><a href="/next/me/comments">我的留言</a>{account.role === 'admin' && <AdminMenu />}</> : <><a href="/next/login">登入</a><a href="/next/register">註冊</a></>}</nav></div>
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
  {/* 主要內容地標供跳過導覽；公開帳號路由標記只讓各自的視覺延伸生效。 */}
  <main id="main-content" tabIndex={-1} className={path === '/next/login' ? 'route-login' : path === '/next/register' ? 'route-register' : undefined}>{path === '/next/' || path === '/next' ? <ArticleList backend={backend} credential={account ? credential : null} ready={ready} page={page} onAuthFailure={handleAuthFailure} />
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
        : <div className="missing"><p className="eyebrow">404 / PAGE NOT FOUND</p><h1>找不到頁面</h1><p>此路徑尚未提供可閱讀的頁面。</p><a className="red-button" href="/next/">返回文章列表 <span aria-hidden="true">↗</span></a></div>}</main>
  <footer className="site-footer"><span>文章集 / NEXT.JS EDITION</span><span>閱讀是另一種記錄。</span></footer></div>
}
