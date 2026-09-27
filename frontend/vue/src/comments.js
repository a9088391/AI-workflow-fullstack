import { isContent, readArticleList } from './articles.js'

// 留言必須附合法父文章 ID；此識別資料不代表使用者可讀取父文章。
export function isComment(value) {
  return isContent(value, 2000) && Number.isSafeInteger(value.article_id) && value.article_id > 0
}

// 沿用共用分頁檢查，維持伺服器排序、總數及合法超頁的原頁碼。
export function readCommentList(status, body, requestedPage, articleId = null) {
  const data = readArticleList(status, body, requestedPage, isComment)
  // 文章下的留言須屬於目前文章；我的留言不限制父文章。
  if (data && articleId !== null && data.list.some(
    // 拒絕混入其他父文章，避免錯誤資源出現在目前閱讀頁。
    (item) => item.article_id !== articleId,
  )) return null
  return data
}

// 單則作者詳情須對上網址 ID，不能從成功回應猜測資源。
export function readComment(status, body, requestedId) {
  return status === 200 && body?.success === true && body.message === 'OK' && isComment(body.data) && body.data.id === requestedId ? body.data : null
}

const blank = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u

// 留言以 Unicode 碼點驗證，保留原文與換行；U+0000 無法安全儲存，必須拒絕而非改寫。
export function validateComment(body) {
  return typeof body !== 'string' || [...body].length < 1 || [...body].length > 2000 || blank.test(body) || body.includes('\u0000') ? { body: ['欄位格式或範圍不正確'] } : {}
}
