import { useCallback, useEffect, useRef, useState } from 'react'
import { clearSessionIfOwned, failure, ownsSession, positiveId, readBackend, readSession, request, selectBackend, validAccount, validReturn } from './contracts.js'
import { AccountForm } from './features/account.jsx'
import { Article, Articles, AuthorEditor, CommentDetail, MyContent } from './features/content.jsx'
import { LockoutSettings, ManagedContent, Member, Members } from './features/admin.jsx'
import { ErrorNotice } from './shared/ui.jsx'
import { Header } from './shell/Header.jsx'

// 路由只在入口解析一次，各功能以 props 接收 ID 與查詢，避免跨模組依賴網址全域值。
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

// 入口只信任本分頁 schema，重新載入時請目前後端 /api/me 確認帳號。
export default function App() {
  // 初始值只讀同分頁儲存，避免渲染過程反覆解析已損壞的 session。
  const [backend, setBackend] = useState(
    // 延遲讀取只在掛載時發生，固定目前分頁的後端來源。
    () => readBackend(sessionStorage),
  )
  // 跨前端僅傳 JWT、後端與原期限；帳號資料必須重新驗證。
  const [credential, setCredential] = useState(
    // 只還原共享三欄憑證；帳號與授權另外向後端確認。
    () => readSession(sessionStorage),
  )
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
    function confirmUnload(event) {
      if (draftRef.current.dirty && !approvedNavigation.current) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', confirmUnload)
    // 卸載時移除離頁保護，避免後續頁面繼承舊表單監聽。
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
    return () => {
      window.removeEventListener('pagehide', onPageHide)
      window.removeEventListener('pageshow', onPageShow)
    }
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
          if (ownerId.current && ownerId.current !== body.data.id) {
            clearSessionIfOwned(sessionStorage, backend, credential)
            setCredential(null)
            setAuthStatus('expired')
            setAuthError({ message: '登入身分與原草稿作者不同；請使用原帳號重新登入。' })
            return
          }
          ownerId.current = body.data.id
          setAccount(body.data)
          setAuthError(null)
          setAuthStatus('ready')
          return
        }
        const problem = failure(body)
        if (problem.code === 'E0002' || problem.code === 'E0102') {
          if (!clearSessionIfOwned(sessionStorage, backend, credential)) return
          setCredential(null)
          setAccount(null)
          setAuthStatus(problem.code === 'E0102' ? 'disabled' : 'expired')
        } else setAuthStatus('unverified')
        setAuthError(problem)
      },
      // 暫時故障不清除憑證，亦不先顯示受保護帳號資料。
      () => {
        if (ownsCurrent()) {
          setAuthStatus('unverified')
          setAuthError({ message: '目前無法驗證登入；請稍後重新載入。' })
        }
      },
    )
    // 憑證或後端變更時舊 me 結果沒有權力更新新身份。
    return () => controller.abort()
  }, [backend, credential])

  // 後端變更立即移除舊憑證與帳號，並讓依賴它的請求取消。
  function changeBackend(next) {
    if (next === backend) return false
    const hadCredential = Boolean(credential)
    try {
      if (!selectBackend(sessionStorage, next)) return false
    } catch {
      setAuthError({ message: '無法儲存後端選擇，請檢查瀏覽器設定。' })
      return false
    }
    // 儲存後端選擇成功才同步撤銷舊表單回應的擁有權；取消切換則保留。
    switchEpoch.current++
    ownerId.current = null
    setBackend(next)
    setCredential(null)
    setAccount(null)
    setAuthStatus('guest')
    setAuthError({ message: '已切換後端，請重新登入。' })
    if (hadCredential && page !== 'login' && page !== 'register') window.location.assign(`/react/login?return_to=${encodeURIComponent(validReturn(pathname) ? pathname : '/react/')}`)
    else approvedNavigation.current = false
    return true
  }

  // 新 JWT 一保存就替換本頁請求身份，舊 me 不可再覆寫，並重新驗證帳號。
  function acceptLogin() {
    // 即使後端重簽得到相同字串，較早請求也不得繼承這次登入的寫入資格。
    switchEpoch.current++
    const current = readSession(sessionStorage)
    setCredential(current)
    setAccount(null)
    setAuthStatus(current ? 'checking' : 'guest')
    setAuthError(null)
  }

  // 登出成功或已知失效才清除憑證，保留目前後端選擇。
  function completeLogout(problem, sourceBackend, sourceCredential) {
    if (!clearSessionIfOwned(sessionStorage, sourceBackend, sourceCredential)) return
    // 已確認登出後結束這個帳號的草稿生命週期，不讓新帳號接收舊內容。
    if (draftRef.current.dirty) {
      changeDraft(false, false)
      approvedNavigation.current = true
      window.location.assign('/react/')
      return
    }
    ownerId.current = null
    setCredential(null)
    setAccount(null)
    setAuthStatus(problem?.code === 'E0102' ? 'disabled' : 'guest')
    setAuthError(problem || { message: '已登出。' })
  }

  // 公開讀取若遇憑證失效也立即停止授權狀態，不降級為訪客後暗中重試。
  const invalidateSession = useCallback((problem, sourceBackend, sourceCredential) => {
    if (!clearSessionIfOwned(sessionStorage, sourceBackend, sourceCredential)) return
    setCredential(null)
    setAccount(null)
    setAuthStatus(problem.code === 'E0102' ? 'disabled' : 'expired')
    setAuthError(problem)
  }, [])

  // 只追蹤目前帳號表單是否有草稿或在途寫入，供前後端切換確認。
  const changeDraft = useCallback((dirty, pending, source = 'page') => {
    // 文章頁可同時有留言草稿與下架請求；聚合後再更新全頁確認狀態。
    if (dirty || pending) draftSources.current.set(source, { dirty, pending })
    else draftSources.current.delete(source)
    const values = [...draftSources.current.values()]
    draftRef.current = {
      dirty: values.some(
        // 任一表單仍有草稿，就保留全頁離頁提示。
        (item) => item.dirty,
      ),
      pending: values.some(
        // 在途操作會改用已送出仍可能完成的提示。
        (item) => item.pending,
      ),
    }
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
  return <div className="app-shell">
    <Header page={page} pathname={pathname} rawReturn={rawReturn} returnTo={returnTo} key={backend} backend={backend} onBackend={changeBackend} account={account} authStatus={authStatus} credential={credential} onLogout={completeLogout} draft={draft} switchEpoch={switchEpoch} approvedNavigation={approvedNavigation} />
    <main>
      {authError && (
        <div className="auth-notice">
          <ErrorNotice error={authError} />
          {authStatus === 'expired' && !inlineLogin && <a href={`/react/login?return_to=${encodeURIComponent(validReturn(pathname) ? pathname : '/react/')}`}>重新登入</a>}
          {authStatus === 'unverified' && <button className="secondary" type="button" onClick={reloadPage}>重新驗證</button>}
        </div>
      )}
      {inlineLogin && <AccountForm returnTo={returnTo} registered={params.get('registered') === '1'} key={`inline-${backend}`} mode="login" backend={backend} onLogin={acceptLogin} onDraftChange={ignoreInlineDraft} switchEpoch={switchEpoch} inline expectedOwnerId={ownerId.current} />}
      {page === 'articles' && (authStatus === 'checking' ? <p role="status">正在驗證登入…</p> : <Articles listPage={listPage} key={backend} backend={backend} credential={credential} enabled={enabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />)}
      {page === 'article' && <Article articleId={articleId} listPage={listPage} pathname={pathname} key={backend} backend={backend} credential={credential} account={account} enabled={enabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
      {page === 'article-new' && <AuthorEditor key={backend} kind="article" backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
      {page === 'article-edit' && <AuthorEditor key={backend} kind="article" resourceId={editingArticleId} backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
      {page === 'my-articles' && <MyContent listPage={listPage} key={backend} kind="article" backend={backend} credential={credential} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
      {page === 'my-comments' && <MyContent listPage={listPage} key={backend} kind="comment" backend={backend} credential={credential} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
      {page === 'comment' && <CommentDetail commentId={commentId} key={backend} backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
      {page === 'comment-edit' && <AuthorEditor key={backend} kind="comment" resourceId={editingCommentId} backend={backend} credential={credential} account={account} enabled={authorEnabled} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
      {page === 'members' && <Members listPage={listPage} search={params.get('filters[keyword]') || ''} key={backend} backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
      {page === 'member' && <Member memberId={memberId} key={backend} backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
      {page === 'managed-articles' && <ManagedContent listPage={listPage} key={backend} kind="article" backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
      {page === 'managed-comments' && <ManagedContent listPage={listPage} key={backend} kind="comment" backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} />}
      {page === 'lockout-settings' && <LockoutSettings key={backend} backend={backend} credential={credential} enabled={adminEnabled} status={authStatus} account={account} onAuthFailure={invalidateSession} switchEpoch={switchEpoch} onDraftChange={changeDraft} />}
      {(page === 'login' || page === 'register') && <AccountForm returnTo={returnTo} registered={params.get('registered') === '1'} key={`${page}-${backend}`} mode={page} backend={backend} onLogin={acceptLogin} onDraftChange={changeDraft} switchEpoch={switchEpoch} />}
      {page === 'missing' && (
        <section className="content">
          <h1>找不到頁面</h1>
          <p>這個 React 頁面目前不存在。</p>
          <a className="back" href="/react/">返回文章列表</a>
        </section>
      )}
    </main>
    <footer>文章集　·　React＋Vite　·　公開閱讀、作者與管理頁</footer>
  </div>
}
