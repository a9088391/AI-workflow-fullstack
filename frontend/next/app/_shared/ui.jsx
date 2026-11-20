// 由 site.jsx 的 client 入口引用，保留錯誤摘要與分頁原有的 DOM 與焦點行為。

// 保留錯誤摘要、欄位導覽與 log_id，讓鍵盤使用者能修正或回報問題。
export function ErrorNotice({ error, retry, focusRef }) {
  if (!error) return null
  // 點選欄位明細會移至輸入，不以顏色單獨傳達錯誤。
  function focusField(event) { (document.getElementById(event.currentTarget.dataset.field) || document.getElementById('comment-body'))?.focus() }
  // 複製失敗仍可手動選取原文，不阻斷錯誤處理。
  async function copyLogId() { try { await navigator.clipboard?.writeText(error.logId) } catch { /* 原文仍可選取。 */ } }
  return <div className="notice error" role="alert" tabIndex={-1} ref={focusRef}>
    <strong>{error.message}</strong>
    {error.errors && Object.keys(error.errors).length > 0 && <ul>{Object.entries(error.errors).map(
      // 已知欄位可聚焦；filters.keyword 對應會員搜尋框，其餘明細仍完整顯示。
      ([name, messages]) => <li key={name}>{['email', 'password', 'nickname', 'title', 'body', 'keyword', 'enabled', 'window_minutes', 'failure_threshold', 'lock_seconds', 'filters.keyword'].includes(name)
        ? <button type="button" className="text-button" data-field={name === 'filters.keyword' ? 'keyword' : name} onClick={focusField}>{name}：{messages.join('；')}</button>
        : `${name}：${messages.join('；')}`}</li>,
    )}</ul>}
    {error.logId && <p>追蹤編號：<span className="break">{error.logId}</span> <button type="button" className="text-button" onClick={copyLogId}>複製</button></p>}
    {retry && <button type="button" className="outline-button" onClick={retry}>重新讀取</button>}
  </div>
}

// 以 API 原頁碼及總數導覽，超頁仍提供第一頁入口。
export function Pagination({ pagination, base, fragment = '', query = '' }) {
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
