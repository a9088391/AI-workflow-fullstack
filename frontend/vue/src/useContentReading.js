import { nextTick, ref } from 'vue'
import { articleFailure, readArticle, readArticleList } from './articles.js'
import { readComment, readCommentList } from './comments.js'

// 每個讀取 owner 各自持有資料與取消控制；寫入不共用此請求，避免重試中斷尚在等待的提交。
function useReadRequest(context, reportFailure) {
  const data = ref(null)
  const loading = ref(false)
  const ready = ref(false)
  let controller = null
  let timeout = null
  let epoch = 0

  // 取消與離頁都立即結束等待；epoch 也能拒絕不遵守 abort 的舊回應。
  function stop() {
    epoch += 1
    if (timeout) clearTimeout(timeout)
    timeout = null
    controller?.abort()
    controller = null
    loading.value = false
  }

  // 撤銷讀取資格時清除資料，不把舊的授權內容交給下一次登入或頁面。
  function clear() {
    stop()
    data.value = null
    ready.value = false
  }

  // 只接受仍屬目前頁面、後端與登入身分的結果；JWT 相同也不能跨帳號採用資料。
  function current(request) {
    return context.isActive() && controller === request.controller && epoch === request.epoch
      && context.backend.value === request.backend
      && context.credential.value?.accessToken === request.token
      && context.account.value?.id === request.accountId
  }

  // 取得一筆完整的契約資料；失敗保留明確錯誤，不能假裝讀到空列表。
  async function load(endpoint, parseData) {
    if (loading.value || !context.canRead()) return null
    clear()
    loading.value = true
    controller = new AbortController()
    const request = {
      controller, epoch, backend: context.backend.value,
      token: context.credential.value?.accessToken, accountId: context.account.value?.id,
    }
    // 逾時結束本次 owner；即使 fetch 忽略 abort，後續回應仍無法還原資料。
    timeout = setTimeout(() => {
      if (!current(request)) return
      stop()
      reportFailure(0, { message: '讀取失敗或伺服器回應不完整，請手動重試。', errors: {}, logId: '', code: '' })
    }, 20000)
    try {
      const response = await fetch(`${request.backend}${endpoint}`, {
        headers: { Accept: 'application/json', ...(request.token ? { Authorization: `Bearer ${request.token}` } : {}) },
        signal: request.controller.signal,
      })
      const body = await response.json()
      if (!current(request)) return null
      const result = parseData(response.status, body)
      if (result) {
        data.value = result
        ready.value = true
        return result
      }
      const failure = articleFailure(body)
      if (response.status === 200 && body?.success === true) failure.message = '伺服器回應不完整，請手動重新讀取。'
      await reportFailure(response.status, failure)
    } catch {
      if (current(request)) await reportFailure(0, { message: '讀取失敗或伺服器回應不完整，請手動重試。', errors: {}, logId: '', code: '' })
    } finally {
      // 即使身分已改變，也回收本次控制；舊請求不能清掉較新的 loading 或計時器。
      if (controller === request.controller && epoch === request.epoch) stop()
    }
    return null
  }

  return { data, loading, ready, load, stop, clear }
}

// 公開、自己的文章與自己的留言各建立一個列表 owner；端點決定範圍，不自行過濾伺服器資料。
function useContentList(context, page, pageNumber) {
  const request = useReadRequest(context, context.onFailure)
  const endpoint = page === 'my-articles' ? '/api/me/articles' : page === 'my-comments' ? '/api/me/comments' : '/api/articles'

  // 依原請求頁碼驗證分頁，合法超頁仍保留後端精確總數及原頁碼。
  function parseList(status, body) {
    return page === 'my-comments' ? readCommentList(status, body, pageNumber) : readArticleList(status, body, pageNumber)
  }

  // 每次只重讀本頁的固定 scope，公開列表即使管理員登入也不加入 management。
  function load() {
    if (request.loading.value || !context.canRead()) return null
    context.onStart()
    return request.load(`${endpoint}?page=${pageNumber}&per_page=20`, parseList)
  }

  return { ...request, load }
}

// 留言列表及父文章有獨立錯誤與焦點；一般失敗不會撤下仍有權讀取的主要資源。
function useRelatedReading(context, onMissing) {
  const error = ref('')
  const logId = ref('')
  const summary = ref(null)
  const request = useReadRequest(context, reportFailure)

  // 認證失效交回唯一 session 撤權，其餘錯誤只影響這個獨立讀取區塊。
  async function reportFailure(status, failure) {
    if (status === 401 && failure.code === 'E0002' || status === 403 && failure.code === 'E0102') {
      await context.onFailure(status, failure)
      return
    }
    if (status === 404 && failure.code === 'E0004' && onMissing(failure)) return
    if (status === 200) failure.message = '伺服器回應不完整，請手動重新讀取。'
    error.value = failure.message
    logId.value = failure.logId
    await nextTick()
    if (context.isActive() && error.value === failure.message) summary.value?.focus()
  }

  // 主資源重讀或授權撤銷時一併移除舊結果及錯誤，避免殘留錯誤屬於另一筆資源。
  function clear() {
    request.clear()
    error.value = ''
    logId.value = ''
  }

  // 重試只更新本區塊，不清除主要資源與寫入結果不明警告。
  function load(endpoint, parseData) {
    if (request.loading.value || !context.canRead()) return
    error.value = ''
    logId.value = ''
    return request.load(endpoint, parseData)
  }

  return { ...request, error, logId, summary, clear, load }
}

// 文章 owner 同時協調自己的留言列表；編輯頁沿用主文讀取，但不額外載入留言。
function useArticleReading(context, id, pageNumber, editing) {
  const article = useReadRequest(context, context.onFailure)
  const commentPage = ref(pageNumber)
  const comments = useRelatedReading(context, missingArticle)

  // 留言端點 404 已否定父文章可讀性，撤下舊文章及操作資格並保留記憶體草稿。
  function missingArticle(failure) {
    clear()
    context.onFailure(404, failure)
    return true
  }

  // 文章回應必須是目前 ID，不能用其他文章的完整資料替代。
  function parseArticle(status, body) {
    return readArticle(status, body, id)
  }

  // 留言回應必須屬於目前文章及請求頁碼，維持伺服器排序與可見範圍。
  function parseComments(status, body) {
    return readCommentList(status, body, commentPage.value, id)
  }

  // 重讀主文會撤銷舊留言，成功後才依新的主文可讀性讀取留言。
  async function load() {
    if (article.loading.value || !context.canRead()) return null
    comments.clear()
    context.onStart()
    const data = await article.load(`/api/articles/${id}`, parseArticle)
    if (data && !editing) loadRelated()
    return data
  }

  // 留言可獨立重試；寫入成功將 commentPage 設回 1 後走相同讀取入口。
  function loadRelated() {
    if (editing || !article.ready.value) return
    return comments.load(`/api/articles/${id}/comments?page=${commentPage.value}&per_page=20`, parseComments)
  }

  // 撤權或離頁時主文與留言一起回收，各自的 epoch 阻擋晚到結果。
  function clear() {
    article.clear()
    comments.clear()
  }

  return { ...article, load, clear, related: comments, loadRelated, commentPage }
}

// 作者留言擁有自己的讀取；父文章只供導覽與編修資格判斷，不能決定留言是否顯示。
function useCommentReading(context, id) {
  const comment = useReadRequest(context, context.onFailure)
  const parent = useRelatedReading(context, missingParent)

  // 404 不代表一定下架，也不能展示錯誤回應夾帶的文章標題。
  function missingParent(failure) {
    failure.message = '所屬文章目前無法讀取'
    return false
  }

  // 留言必須符合完整資源與目前 ID 契約，才可作為父文章讀取的來源。
  function parseComment(status, body) {
    return readComment(status, body, id)
  }

  // 父文章必須符合留言已確認的 article_id，不從錯誤或舊回應猜測關聯。
  function parseParent(status, body) {
    return readArticle(status, body, comment.data.value.article_id)
  }

  // 主留言重新讀取時撤銷父文章資格，成功後才另行確認父文章。
  async function load() {
    if (comment.loading.value || !context.canRead()) return null
    parent.clear()
    context.onStart()
    const data = await comment.load(`/api/comments/${id}`, parseComment)
    if (data) loadRelated()
    return data
  }

  // 父文章失敗可獨立重試，不重新取得或清除自己的留言。
  function loadRelated() {
    if (!comment.ready.value) return
    return parent.load(`/api/articles/${comment.data.value.article_id}`, parseParent)
  }

  // 清掉目前留言與父文章資格，避免換帳號或離頁後還能採用舊內容。
  function clear() {
    comment.clear()
    parent.clear()
  }

  return { ...comment, load, clear, related: parent, loadRelated }
}

// App 只選擇目前語意頁的讀取 owner；管理資料與寫入表單仍由既有流程負責。
export function useContentReading(context, page, id, pageNumber) {
  if (page === 'article' || page === 'article-edit') return useArticleReading(context, id, pageNumber, page === 'article-edit')
  if (page === 'comment' || page === 'comment-edit') return useCommentReading(context, id)
  if (['articles', 'my-articles', 'my-comments'].includes(page)) return useContentList(context, page, pageNumber)
  return null
}
