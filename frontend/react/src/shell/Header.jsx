import { useEffect, useRef, useState } from 'react'
import { BACKENDS, FRONTENDS, failure, frontendTarget, ownsSession, request, supportsAngularRoute, supportsNextRoute } from '../contracts.js'
import { ErrorNotice } from '../shared/ui.jsx'

// 工具列保持目前版本與後端可見，切換只導覽同源已知路徑。
export function Header({ page, pathname, rawReturn, returnTo, backend, onBackend, account, authStatus, credential, onLogout, draft, switchEpoch, approvedNavigation }) {
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
  // 使用原始登入返回值，避免不安全目標被回退值掩蓋而誤啟用 SvelteKit。
  const svelteTarget = frontendTarget(pathname, 'sveltekit', rawReturn ?? '/react/')
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
    if (next === 'sveltekit' && !svelteTarget) { event.target.value = 'react'; return }
    if (!mayLeave()) { event.target.value = 'react'; return }
    const target = next === 'sveltekit' ? svelteTarget : frontendTarget(pathname, next, returnTo)
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
    // 無憑證或已在等待時不重送；有草稿先由使用者決定是否放棄。
    if (!credential || busy) return
    if (draft.dirty && !window.confirm(draft.pending ? '請求已送出，登出後仍可能完成。要捨棄目前表單並登出嗎？' : '目前表單有未儲存文字。要捨棄並登出嗎？')) return
    // 這次登出持有自己的取消控制與頁面世代，供切換後隔離延遲回應。
    setBusy(true)
    setLogoutError(null)
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    logoutController.current = controller
    try {
      const { status, body } = await request(backend, '/api/auth/logout', { method: 'POST', signal: controller.signal, headers: { Authorization: `Bearer ${credential.accessToken}` } })
      // 舊登出縱使伺服器已完成，也不可清除另一個前端剛保存的 JWT。
      if (controller.signal.aborted || startedEpoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential)) return
      if (status === 200 && body?.success === true && body.message === 'OK' && body.data && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0) onLogout(null, backend, credential)
      else {
        // 只有共同失敗 envelope 的失效代碼才能清除 JWT，矛盾回應不能冒充登出。
        const problem = failure(body)
        if (problem.code === 'E0002' || problem.code === 'E0102') onLogout(problem, backend, credential)
        else setLogoutError({ ...problem, message: `登出未完成；請再次按「登出」手動重試。${problem.message}` })
      }
    } catch { if (!controller.signal.aborted && startedEpoch === switchEpoch.current && ownsSession(sessionStorage, backend, credential)) setLogoutError({ message: '登出尚未確認；請稍後手動重試。' }) }
    finally { if (!controller.signal.aborted && startedEpoch === switchEpoch.current) setBusy(false) }
  }

  return <>
    <header className="site-header">
      <a className="brand" href="/react/">文章集<span> / REACT</span></a>
      <div className="current">目前前端　<strong>React＋Vite</strong><br />目前後端　<strong>{name}</strong></div>
      <details className="switches">
        <summary>切換版本</summary>
        <div className="switch-panel">
          <label htmlFor="frontend">目前前端</label>
          <select id="frontend" value={front} onChange={switchFrontend}>
            {Object.entries(FRONTENDS).map(
              // 六版入口保持可見；只啟用目前頁面在目標版已完成且可用的選項。
              ([id, label]) => {
                const unavailable = id === 'sveltekit' && !svelteTarget || id === 'angular' && !angularReady || id === 'next' && !nextReady
                return <option key={id} value={id} disabled={unavailable}>{label}{unavailable ? '（此頁尚未提供）' : ''}</option>
              },
            )}
          </select>
          <label htmlFor="backend">目前後端</label>
          <select id="backend" value={backend} onChange={switchBackend}>
            {Object.entries(BACKENDS).map(
              // 十二個後端共享 API 路徑，只需更換固定基底。
              ([id, [label, url]]) => <option key={id} value={url}>{label}</option>,
            )}
          </select>
          <p className="switch-help">切換後端會清除登入，請重新登入。</p>
        </div>
      </details>
    </header>
    <button className="mobile-nav-toggle" ref={navToggle} type="button" aria-controls="react-main-nav" aria-expanded={mobileNavOpen} onClick={toggleMobileNav}>{mobileNavOpen ? '關閉導覽' : '開啟導覽'}</button>
    <nav id="react-main-nav" className={`site-nav${mobileNavOpen ? ' open' : ''}`} aria-label="主導覽" onKeyDown={mobileNavKeyDown}>
      <a href="/react/" aria-current={page === 'articles' || page === 'article' ? 'page' : undefined}>所有文章</a>
      {account ? <>
        <a href="/react/articles/new" aria-current={page === 'article-new' ? 'page' : undefined}>發表文章</a>
        <a href="/react/me/articles" aria-current={page === 'my-articles' ? 'page' : undefined}>我的文章</a>
        <a href="/react/me/comments" aria-current={page === 'my-comments' ? 'page' : undefined}>我的留言</a>
        {account.role === 'admin' && (
          <details className="admin-nav" open={page.startsWith('managed-') || ['members', 'member', 'lockout-settings'].includes(page)}>
            <summary>管理介面</summary>
            <a href="/react/admin/members" aria-current={page === 'members' || page === 'member' ? 'page' : undefined}>會員管理</a>
            <a href="/react/admin/articles" aria-current={page === 'managed-articles' ? 'page' : undefined}>文章管理</a>
            <a href="/react/admin/comments" aria-current={page === 'managed-comments' ? 'page' : undefined}>留言管理</a>
            <a href="/react/admin/settings/login-lockout" aria-current={page === 'lockout-settings' ? 'page' : undefined}>鎖定設定</a>
          </details>
        )}
        <span className="account-name">{account.nickname}</span>
        <button className="nav-button" type="button" disabled={busy} onClick={logout}>{busy ? '登出中…' : '登出'}</button>
      </> : authStatus === 'checking' ? <span>驗證登入中…</span> : <>
        <a href="/react/login" aria-current={page === 'login' ? 'page' : undefined}>登入</a>
        <a href="/react/register" aria-current={page === 'register' ? 'page' : undefined}>註冊</a>
      </>}
      <button className="nav-close" type="button" onClick={closeMobileNav}>關閉導覽</button>
    </nav>
    {logoutError && <div className="shell-error"><ErrorNotice error={logoutError} /></div>}
  </>
}
