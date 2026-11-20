// API 失敗保持欄位錯誤與追蹤編號，避免使用者只能看到泛用失敗訊息。
export function ErrorNotice({ error, retry, focusRef }) {
  if (!error) return null
  // 可聚焦摘要讓鍵盤使用者取得錯誤，欄位連結可直接回到需修正的輸入。
  function focusField(event) { document.getElementById(event.currentTarget.dataset.field)?.focus() }
  // 複製失敗仍保留可選取原文，讓使用者能回報同次請求的追蹤編號。
  async function copyLogId() {
    try { await navigator.clipboard?.writeText(error.logId) }
    catch { /* 保留可選取的原文供手動複製。 */ }
  }
  return (
    <div className="notice error" role="alert" tabIndex={-1} ref={focusRef}>
      <strong>{error.message}</strong>
      {error.errors && Object.entries(error.errors).length > 0 && (
        <ul className="error-fields">
          {Object.entries(error.errors).map(
            // 已知欄位連回對應輸入；filters.keyword 對應會員搜尋框，其餘明細保留文字。
            ([name, messages]) => (
              <li key={name}>
                {['email', 'password', 'nickname', 'title', 'body', 'enabled', 'window_minutes', 'failure_threshold', 'lock_seconds', 'filters.keyword'].includes(name)
                  ? <button type="button" className="text-button" data-field={name === 'filters.keyword' ? 'member-keyword' : name} onClick={focusField}>{name}：{messages.join('；')}</button>
                  : `${name}：${messages.join('；')}`}
              </li>
            ),
          )}
        </ul>
      )}
      {error.logId && (
        <p>追蹤編號：<span className="break">{error.logId}</span> <button type="button" className="text-button" onClick={copyLogId}>複製</button></p>
      )}
      {retry && <button className="secondary" type="button" onClick={retry}>重新讀取</button>}
    </div>
  )
}

// 分頁使用 API 回傳的總頁數；超頁仍保留原頁碼及返回第一頁入口。
export function Pagination({ pagination, base }) {
  if (!pagination || pagination.total_pages <= 1 && pagination.page === 1) return null
  const { page: number, total_pages: totalPages, total } = pagination
  return <nav className="pagination" aria-label="分頁">
    <span>第 {number} 頁／共 {totalPages} 頁，{total} 筆</span>
    {number > 1 && <a href={`${base}?page=${number - 1}`}>上一頁</a>}
    {number < totalPages && <a href={`${base}?page=${number + 1}`}>下一頁</a>}
    {number > totalPages && <a href={base}>返回第一頁</a>}
  </nav>
}
