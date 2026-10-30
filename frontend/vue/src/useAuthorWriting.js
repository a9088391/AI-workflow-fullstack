import { computed, ref } from 'vue'
import { articleCreated, contentUpdated, articleFailure, validateArticle } from './articles.js'
import { validateComment } from './comments.js'

// 作者表單持有文字、初讀版本及寫入生命週期；閱讀與認證仍呼叫各自唯一 owner。
export function useAuthorWriting({ page, path, requestedId, session, reader, feedback, showError, isActive, loadContent, submitted }) {
  const { backend, credential, account, authChecking, logoutPending, reLogin, disabledAccount, forgetCredential } = session
  const { summary, summaryCode, errors, logId } = feedback
  const editing = page === 'article-edit' || page === 'comment-edit'
  const articleResource = page === 'article' || page === 'article-edit'
  const commentResource = page === 'comment' || page === 'comment-edit'
  const articleData = articleResource ? reader.data : ref(null)
  const commentData = commentResource ? reader.data : ref(null)
  const parentArticle = commentResource ? reader.related.data : ref(null)
  const contentReady = reader?.ready || ref(false)
  const contentLoading = reader?.loading || ref(false)
  const relatedLoading = reader?.related?.loading || ref(false)
  const commentPage = reader?.commentPage

  const articleTitle = ref('')
  const articleBody = ref('')
  const commentBody = ref('')
  const commentPosted = ref(false)
  const contentPending = ref(false)
  const writeKind = ref('')
  const writeUnknown = ref(false)
  const lifecycleUnknown = ref(false)
  const editVersion = ref(null)
  const editLoaded = ref(false)
  const editOriginal = ref(null)
  const conflict = ref(false)
  const comparison = ref(null)
  const updateNotice = ref('')
  let draftOwner = null
  let applyReadback = false
  let writeRequest = null
  let writeTimeout = null
  const publishedOwner = ref(null)
  const publishNoticeKey = 'article-collection-vue-published'

  // 等待狀態依操作區分，狀態 PUT 不能讓留言表單宣稱正在發表。
  const createPending = computed(() => contentPending.value && writeKind.value === 'create')
  // 編修與 visibility 共用已知 ID 操作資格；建立請求不顯示為狀態更新。
  const updatePending = computed(() => contentPending.value && writeKind.value === 'update')

  // 草稿比對使用初讀基準；重讀中或失敗時都不能將原文當成空值。
  const hasDraft = computed(() => {
    if (page === 'article-new') return Boolean(articleTitle.value || articleBody.value)
    if (page === 'article') return Boolean(commentBody.value)
    if (!editing || !editLoaded.value) return false
    return articleResource
      ? articleTitle.value !== editOriginal.value?.title || articleBody.value !== editOriginal.value?.body
      : commentBody.value !== editOriginal.value?.body
  })

  // 成功提示只對已驗權的原帳號與原後端顯示，暫時無法驗權只隱藏而不否定事件。
  const published = computed(() => page === 'my-articles' && publishedOwner.value?.accountId === account.value?.id
    && publishedOwner.value?.backend === backend.value && Boolean(credential.value)
    && !authChecking.value && !reLogin.value && !disabledAccount.value && !logoutPending.value)

  // 201 已確認後只保存非敏感事件；儲存失敗不能將成功誤判為未知或容許自動重送。
  function savePublishedNotice() {
    try {
      sessionStorage.setItem(publishNoticeKey, JSON.stringify({ accountId: account.value.id, backend: backend.value }))
    } catch { /* 瀏覽器儲存不可用時仍前往我的文章，已確認寫入不重新送出。 */ }
  }

  // 目的頁完成驗權後才讀取並消費事件；明確身分不符會清除，暫時失敗不會呼叫此入口。
  function acceptPublishedNotice() {
    if (page !== 'my-articles' || !account.value || !credential.value || authChecking.value) return
    try {
      const notice = JSON.parse(sessionStorage.getItem(publishNoticeKey) || 'null')
      if (!notice) return
      sessionStorage.removeItem(publishNoticeKey)
      if (notice.accountId === account.value.id && notice.backend === backend.value) publishedOwner.value = notice
    } catch { /* 無可讀事件時不憑網址旗標顯示成功。 */ }
  }

  // 取消等待並回收 timer；已送出的操作仍可能完成，保留該操作自己的未知結果。
  function stopWriting() {
    if (writeRequest) {
      if (writeRequest.kind === 'create') writeUnknown.value = true
      else lifecycleUnknown.value = true
      writeRequest.controller.abort()
    }
    writeRequest = null
    if (writeTimeout) clearTimeout(writeTimeout)
    writeTimeout = null
    contentPending.value = false
    writeKind.value = ''
  }

  // 回應必須仍屬目前請求、帳號與後端；取消或切換後的晚到成功／拒絕都不能回寫。
  function currentWriteRequest(request) {
    return isActive() && writeRequest === request && backend.value === request.backend
      && credential.value?.accessToken === request.token && account.value?.id === request.accountId
  }

  // 只結束原請求的等待；舊回應不得清掉新操作的 pending 或 timer。
  function finishWriteRequest(request) {
    if (writeRequest !== request) return
    writeRequest = null
    if (writeTimeout) clearTimeout(writeTimeout)
    writeTimeout = null
    contentPending.value = false
    writeKind.value = ''
  }

  // 作者寫入使用獨立取消控制，逾時即結束等待，不依賴 fetch 是否遵守 abort。
  function startWriteRequest(kind) {
    const request = {
      kind, controller: new AbortController(), backend: backend.value,
      token: credential.value.accessToken, accountId: account.value.id,
    }
    writeRequest = request
    writeKind.value = kind
    // 先作廢回應再顯示未知結果，忽略 abort 的網路層也不能在逾時後宣稱成功。
    writeTimeout = setTimeout(() => {
      if (!currentWriteRequest(request)) return
      stopWriting()
      let message = '更新結果尚未確認；請先重新讀取同一筆內容查證，再決定是否手動重試。'
      if (kind === 'create') {
        message = page === 'article-new'
          ? '無法確認發文結果；請先到我的文章查證，再決定是否手動重試。'
          : '無法確認留言結果；請先重新讀取留言查證，再決定是否手動重試。'
      }
      showError(message)
    }, 20000)
    return request
  }

  // reader 已確認目前身分及完整資料後，初讀／成功回讀才建立基準；一般重讀僅供人工比較。
  function acceptRead(data) {
    if (editing && data.author.id === account.value?.id) {
      if (!editLoaded.value || applyReadback) {
        articleTitle.value = articleResource ? data.title : ''
        articleBody.value = articleResource ? data.body : ''
        commentBody.value = commentResource ? data.body : ''
        editOriginal.value = data
        editVersion.value = data.version
        editLoaded.value = true
        applyReadback = false
        draftOwner = account.value.id
      } else comparison.value = data
    } else if (conflict.value) comparison.value = data
  }

  // 撤權前記住草稿所屬帳號；同帳重登保留版本與未知結果，不把草稿寫入 storage。
  function rememberOwner(previousAccount) {
    if (previousAccount && (hasDraft.value || writeUnknown.value || lifecycleUnknown.value)) draftOwner = previousAccount.id
  }

  // 直接驗權與原頁重登共用隔離規則；新帳號不能取得前一人的文字、版本或操作提示。
  function acceptAccount(nextAccountId) {
    const changed = draftOwner !== null && draftOwner !== nextAccountId
    if (changed) {
      articleTitle.value = ''
      articleBody.value = ''
      commentBody.value = ''
      commentPosted.value = false
      resetEditor()
    }
    if (publishedOwner.value && publishedOwner.value.accountId !== nextAccountId) publishedOwner.value = null
    draftOwner = nextAccountId
    return changed
  }

  // 留言寫入須確認父文章公開及目前登入有效，後端再做最終權限判斷。
  const canComment = computed(() => page === 'article' && contentReady.value && articleData.value?.visibility === 'visible' && Boolean(account.value && credential.value) && !authChecking.value && !reLogin.value && !disabledAccount.value && !logoutPending.value)
  // 詳情與編輯以後端最新讀取值判斷資格；管理員不能修改他人內文。
  const currentResource = computed(() => articleResource ? articleData.value : commentResource ? commentData.value : null)
  // 確認目前登入與資源後才顯示操作，避免重驗期間洩漏受保護內容。
  const authorizedResource = computed(() => Boolean(contentReady.value && currentResource.value && account.value && credential.value && !authChecking.value && !reLogin.value && !disabledAccount.value && !logoutPending.value))
  // 作者編修仍需確認資格及父文章狀態，不能只憑列表連結開放內文寫入。
  const canEdit = computed(() => authorizedResource.value && currentResource.value.author.id === account.value.id && (!commentResource || parentArticle.value?.visibility === 'visible' && !relatedLoading.value))
  // 按目前資源資格顯示下架操作，實際權限及版本仍由後端重驗。
  const canManageVisibility = computed(() => authorizedResource.value && (currentResource.value.author.id === account.value.id || account.value.role === 'admin'))
  // 管理員可再次下架他人內容以接管恢復權；自己的內容仍按作者規則。
  const canHide = computed(() => canManageVisibility.value && (currentResource.value.visibility === 'visible' || account.value.role === 'admin' && currentResource.value.author.id !== account.value.id))

  // 編輯欄位只接收真實 input 值，不改動初讀版本或比較結果。
  function updateEditorBody(value) {
    if (articleResource) articleBody.value = value
    else commentBody.value = value
  }

  // 作者只送 title/body；確認 201 空 data 後才帶提示回我的文章第一頁，失敗保留草稿。
  async function submitArticle() {
    if (page !== 'article-new' || contentPending.value || authChecking.value || logoutPending.value || !account.value || !credential.value || reLogin.value || disabledAccount.value || submitted.value) return
    const checked = validateArticle({ title: articleTitle.value, body: articleBody.value })
    if (Object.keys(checked).length) { await showError('欄位資料不正確，請確認後再送出', checked); return }
    draftOwner = account.value.id
    contentPending.value = true
    summary.value = ''
    errors.value = {}
    logId.value = ''
    const request = startWriteRequest('create')
    try {
      const response = await fetch(`${request.backend}/api/articles`, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${request.token}` },
        body: JSON.stringify({ title: articleTitle.value, body: articleBody.value }), signal: request.controller.signal,
      })
      const body = await response.json()
      if (!currentWriteRequest(request)) return
      if (articleCreated(response.status, body)) {
        finishWriteRequest(request)
        submitted.value = true
        savePublishedNotice()
        window.location.assign('/vue/me/articles?published=1')
        return
      }
      const result = articleFailure(body)
      if (response.status >= 500 || response.status === 201) writeUnknown.value = true
      if (response.status === 401 && result.code === 'E0002') {
        draftOwner = account.value.id
        // 401 已確認拒絕；先結束寫入再撤權，避免 stopWriting 把已知拒絕誤標為 unknown。
        finishWriteRequest(request)
        forgetCredential()
        reLogin.value = true
      } else if (response.status === 403 && result.code === 'E0102') {
        // 停用亦是已確認拒絕；先結束寫入，避免撤權清理的 stopWriting 誤標為 unknown。
        finishWriteRequest(request)
        forgetCredential()
        disabledAccount.value = true
      }
      await showError(result.message, result.errors, result.logId, result.code)
    } catch {
      if (currentWriteRequest(request)) {
        writeUnknown.value = true
        await showError('無法確認發文結果；請先到我的文章查證，再決定是否手動重試。')
      }
    } finally {
      // 無論成功、拒絕或取消都回收原請求；較新操作不受舊 finally 影響。
      finishWriteRequest(request)
    }
  }

  // 留言只送原始 body；確認建立後留在原文章重讀第一頁，不推測新 ID 或自動重送。
  async function submitComment() {
    if (!canComment.value || contentPending.value) return
    const checked = validateComment(commentBody.value)
    if (Object.keys(checked).length) { await showError('欄位資料不正確，請確認後再送出', checked); return }
    draftOwner = account.value.id
    contentPending.value = true
    commentPosted.value = false
    // 留言與資源更新的未知結果都保留，其他操作不能替先前請求作出結論。
    summary.value = ''
    summaryCode.value = ''
    errors.value = {}
    logId.value = ''
    // 送出時撤銷舊留言讀取，避免舊列表或授權回應覆蓋本次操作。
    reader?.related?.stop()
    const request = startWriteRequest('create')
    try {
      const response = await fetch(`${request.backend}/api/articles/${requestedId}/comments`, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${request.token}` },
        body: JSON.stringify({ body: commentBody.value }), signal: request.controller.signal,
      })
      const body = await response.json()
      if (!currentWriteRequest(request)) return
      if (articleCreated(response.status, body)) {
        commentBody.value = ''
        commentPosted.value = true
        commentPage.value = 1
        // 同步網址與畫面分頁；重整與上一頁都沿用原文章，不新增未知留言路徑。
        window.history.replaceState(null, '', `${path}#comments`)
        finishWriteRequest(request)
        reader.loadRelated()
        return
      }
      const result = articleFailure(body)
      if (response.status >= 500 || response.status === 201) writeUnknown.value = true
      if (response.status === 401 && result.code === 'E0002') {
        // 401 已確認拒絕；先結束寫入再撤權，避免 stopWriting 把已知拒絕誤標為 unknown。
        finishWriteRequest(request)
        forgetCredential()
        reLogin.value = true
      } else if (response.status === 403 && result.code === 'E0102') {
        // 停用亦是已確認拒絕；先結束寫入，避免撤權清理的 stopWriting 誤標為 unknown。
        finishWriteRequest(request)
        forgetCredential()
        disabledAccount.value = true
      } else if (response.status === 403 && result.code === 'E0003' || response.status === 404 && result.code === 'E0004') {
        // 可讀性已改變，先隱藏舊文章與表單，須手動重讀確認現況。
        reader.clear()
      }
      await showError(result.message, result.errors, result.logId, result.code)
    } catch {
      if (currentWriteRequest(request)) {
        writeUnknown.value = true
        await showError('無法確認留言結果；請先重新讀取留言查證，再決定是否手動重試。')
      }
    } finally {
      // 無論成功、拒絕或取消都回收原請求；較新操作不受舊 finally 影響。
      finishWriteRequest(request)
    }
  }

  // 換帳號時清掉版本、比較及未知結果，原帳號文字不能交給新身分。
  function resetEditor() {
    editVersion.value = null
    editLoaded.value = false
    editOriginal.value = null
    conflict.value = false
    comparison.value = null
    updateNotice.value = ''
    writeUnknown.value = false
    lifecycleUnknown.value = false
    applyReadback = false
  }

  // 人工比較後明確選擇整合或捨棄；只更新版本，不自動 PUT。
  function acceptComparison(discard) {
    if (!editing || !comparison.value || !canEdit.value || contentPending.value || contentLoading.value) return
    if (!window.confirm(discard ? '確定捨棄自己的輸入，採用最新內容？' : '已比較並整合自己的輸入，確定採用最新版本供手動送出？')) return
    if (discard) {
      if (articleResource) { articleTitle.value = comparison.value.title; articleBody.value = comparison.value.body }
      else commentBody.value = comparison.value.body
    }
    editOriginal.value = comparison.value
    editVersion.value = comparison.value.version
    conflict.value = false
    comparison.value = null
    updateNotice.value = '已確認比較結果；如要儲存，請手動送出。'
  }

  // 作者編修保留原文與原讀取版本；衝突未人工確認前不能重送。
  async function submitEdit() {
    if (!editing || !canEdit.value || contentPending.value || contentLoading.value || conflict.value || !editVersion.value) return
    const checked = articleResource ? validateArticle({ title: articleTitle.value, body: articleBody.value }) : validateComment(commentBody.value)
    if (Object.keys(checked).length) { await showError('欄位資料不正確，請確認後再送出', checked); return }
    const payload = articleResource ? { title: articleTitle.value, body: articleBody.value, version: editVersion.value } : { body: commentBody.value, version: editVersion.value }
    await updateContent(payload)
  }

  // 每次下架／恢復都重新確認操作；409 後必須先重讀，不能只替換版本再重送。
  async function submitVisibility(visibility) {
    if (editing || !canManageVisibility.value || contentPending.value || contentLoading.value || conflict.value && !comparison.value) return
    if (visibility !== 'hidden' && visibility !== 'visible' || visibility === 'hidden' && !canHide.value || visibility === 'visible' && !currentResource.value.can_restore) return
    const action = visibility === 'hidden' ? currentResource.value.visibility === 'hidden' ? '再次下架並由管理員控制恢復' : '下架' : '恢復'
    if (!window.confirm(`確定${action}這筆內容？`)) return
    await updateContent({ visibility, version: currentResource.value.version }, true)
  }

  // 已知 ID 寫入沿用作者請求身分；成功與同值成功都重讀同一資源，未知結果不自動重送。
  async function updateContent(payload, visibility = false) {
    // 記錄送出身分並停止舊父文章讀取；後續重登不能接收他人的草稿。
    draftOwner = account.value.id
    contentPending.value = true
    // 新 PUT 僅清理本次摘要；前次未知結果無法由另一操作的結果判定。
    updateNotice.value = ''
    summary.value = ''
    summaryCode.value = ''
    errors.value = {}
    logId.value = ''
    reader?.related?.stop()
    const request = startWriteRequest('update')
    const endpoint = `/api/${articleResource ? 'articles' : 'comments'}/${requestedId}${visibility ? '/visibility' : ''}`
    try {
      const response = await fetch(`${request.backend}${endpoint}`, {
        method: 'PUT',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${request.token}` },
        body: JSON.stringify(payload), signal: request.controller.signal,
      })
      const body = await response.json()
      if (!currentWriteRequest(request)) return
      // 成功不樂觀改寫資源；重新讀取同 ID，才把編輯基準切到伺服器確認值。
      if (contentUpdated(response.status, body)) {
        conflict.value = false
        comparison.value = null
        applyReadback = editing
        updateNotice.value = '更新已確認；內容以重新讀取的結果為準。'
        finishWriteRequest(request)
        loadContent()
        return
      }
      const result = articleFailure(body)
      if (response.status >= 500 || response.status === 200) lifecycleUnknown.value = true
      // 409 只撤銷比較結果，不改使用者文字與舊版本，等待人工重讀及確認。
      if (response.status === 409 && result.code === 'E0201') {
        conflict.value = true
        comparison.value = null
      } else if (response.status === 401 && result.code === 'E0002') {
        // 401 已確認拒絕；先結束寫入再撤權，避免 stopWriting 把已知拒絕誤標為 unknown。
        finishWriteRequest(request)
        forgetCredential()
        reLogin.value = true
      } else if (response.status === 403 && result.code === 'E0102') {
        // 停用亦是已確認拒絕；先結束寫入，避免撤權清理的 stopWriting 誤標為 unknown。
        finishWriteRequest(request)
        forgetCredential()
        disabledAccount.value = true
      } else if (response.status === 403 && result.code === 'E0003' || response.status === 404 && result.code === 'E0004') {
        // 權限／可讀性已改變，撤下舊資源和父文章但保留草稿，重讀後才重建資格。
        reader.clear()
      }
      await showError(result.message, result.errors, result.logId, result.code)
    } catch {
      if (currentWriteRequest(request)) {
        lifecycleUnknown.value = true
        await showError('更新結果尚未確認；請先重新讀取同一筆內容查證，再決定是否手動重試。')
      }
    } finally {
      // 無論成功、拒絕或取消都回收原請求；較新操作不受舊 finally 影響。
      finishWriteRequest(request)
    }
  }

  return {
    articleTitle, articleBody, commentBody, commentPosted, contentPending, createPending, updatePending,
    writeUnknown, lifecycleUnknown, editVersion, editLoaded,
    conflict, comparison, updateNotice, hasDraft, published,
    canComment, currentResource, authorizedResource, canEdit, canManageVisibility, canHide,
    updateEditorBody, submitArticle, submitComment, submitEdit, submitVisibility, acceptComparison,
    acceptRead, rememberOwner, acceptAccount, acceptPublishedNotice, stopWriting,
  }
}
