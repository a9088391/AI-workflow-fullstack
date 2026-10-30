import { ref } from 'vue'
import { articleFailure, contentUpdated, readArticleList } from './articles.js'
import { readCommentList } from './comments.js'
import { readMember, readMemberList } from './admin.js'

// 管理列表與會員狀態共用當頁資源；設定另有 owner，不與公開 reader 或作者寫入共用請求。
export function useManagement({ page, path, requestedId, listPage, session, canRead, isActive, showError, feedback }) {
  const { backend, credential, account, authChecking, logoutPending, forgetCredential, reLogin, disabledAccount } = session
  const { summary } = feedback
  const data = ref(null)
  const loading = ref(false)
  const ready = ref(false)
  const statusPending = ref(false)
  const statusUnknown = ref(false)
  const statusNeedsReadback = ref(false)
  const statusNotice = ref('')
  const initialKeyword = page === 'members' ? new URLSearchParams(window.location.search).get('keyword') || '' : ''
  const keyword = ref(initialKeyword)
  const searchedKeyword = initialKeyword.replace(/^ *| *$/g, '')
  let statusOwner = null
  // 已確認 PUT 的讀回尚未完成；人工 GET 成功才可結束「尚未讀到」回饋。
  let confirmedReadback = false
  let request = null
  let epoch = 0

  // 身分、後端與角色都須相符；相同 token 也不能使已失去管理權的回應重新出現。
  function sameActor(operation) {
    return isActive() && backend.value === operation.backend && credential.value?.accessToken === operation.token
      && account.value?.id === operation.actor && account.value?.role === 'admin'
  }

  // 請求只屬於本頁固定資源，epoch 擋住不遵守 abort 的傳輸或過期讀回。
  function current(operation) {
    return request === operation && epoch === operation.epoch && sameActor(operation)
  }

  // 已有明確結果時先結束等待，之後撤權不得把已拒絕的 PUT 誤標為結果不明。
  function finish(operation) {
    if (request !== operation) return
    clearTimeout(operation.timer)
    request = null
    loading.value = false
    statusPending.value = false
  }

  // 無回應的 PUT 可能已提交；取消只回收前端等待，保留人工查證要求。
  function stop() {
    epoch += 1
    if (!request) return
    const operation = request
    if (operation.write) {
      statusUnknown.value = true
      statusNeedsReadback.value = true
    }
    finish(operation)
    operation.controller.abort()
  }

  // 每個操作持有自己的時限；逾時立即解除 UI 等待，不依賴 fetch 是否遵守 abort。
  function start(write, readback = false) {
    const operation = {
      controller: new AbortController(), epoch: ++epoch, write,
      backend: backend.value, token: credential.value.accessToken, actor: account.value.id, timer: null,
    }
    request = operation
    // 逾時只可更新原身分的狀態，晚到回應不能補寫資料或觸發讀回。
    operation.timer = setTimeout(() => {
      if (request !== operation) return
      if (!current(operation)) {
        stop()
        return
      }
      stop()
      if (readback) statusNotice.value = '更新已送達，但尚未讀到最新會員狀態；請手動重新讀取。'
      showError(write ? '更新結果尚未確認；請先重讀同一會員查證，再決定是否手動重試。' : '讀取失敗或伺服器回應不完整，請手動重試。')
    }, 20000)
    return operation
  }

  // 管理資料撤下時立即結束讀取與等待；狀態未知結果仍由原管理員保留。
  function clear() {
    stop()
    data.value = null
    ready.value = false
    loading.value = false
    statusNotice.value = ''
  }

  // 新帳號、後端或角色不可接收舊操作的控制；同管理員重登仍需手動查證。
  function acceptAccount(nextAccount) {
    if (!statusOwner || nextAccount.role === 'admin' && statusOwner.id === nextAccount.id && statusOwner.backend === backend.value) return
    statusUnknown.value = false
    statusNeedsReadback.value = false
    statusNotice.value = ''
    confirmedReadback = false
    statusOwner = null
  }

  // 所有管理 GET 都在 /me 與角色確認後才送出，文章管理明確帶 management scope。
  // readback 只讓已確認 PUT 的逾時提示正確結束，不像設定 owner 的 applyDraft 會套用草稿。
  async function load(readback = false) {
    if (!['members', 'member', 'managed-articles', 'managed-comments'].includes(page) || loading.value || statusPending.value || !canRead()) return
    data.value = null
    ready.value = false
    loading.value = true
    summary.value = ''
    // 普通重讀不能沿用上次成功；尚未完成的 confirmed 讀回仍保留原操作的查證提示。
    if (!readback) statusNotice.value = confirmedReadback ? '更新已送達，但尚未讀到最新會員狀態；請手動重新讀取。' : ''
    const operation = start(false, readback)
    const endpoint = page === 'member' ? `/api/members/${requestedId}` : page === 'members' ? '/api/members'
      : page === 'managed-comments' ? '/api/comments' : '/api/articles'
    const query = page === 'member' ? '' : `?page=${listPage}&per_page=20${page === 'managed-articles' ? '&filters%5Bscope%5D=management'
      : page === 'members' && searchedKeyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(searchedKeyword)}` : ''}`
    try {
      const response = await fetch(`${operation.backend}${endpoint}${query}`, {
        headers: { Accept: 'application/json', Authorization: `Bearer ${operation.token}` }, signal: operation.controller.signal,
      })
      const body = await response.json()
      if (!current(operation)) return
      const value = page === 'member' ? readMember(response.status, body, requestedId)
        : page === 'members' ? readMemberList(response.status, body, listPage)
        : page === 'managed-comments' ? readCommentList(response.status, body, listPage) : readArticleList(response.status, body, listPage)
      if (value) {
        data.value = value
        ready.value = true
        if (page === 'member' && statusUnknown.value) {
          statusNeedsReadback.value = false
          statusNotice.value = `已重讀，目前伺服器狀態為${value.status === 'active' ? '啟用' : '停用'}。`
        } else if (confirmedReadback) {
          statusNotice.value = `已重讀會員，目前狀態為${value.status === 'active' ? '啟用' : '停用'}。`
        }
        // 初讀不能虛構寫入成功；完成讀回後消耗已確認事件，未知結果仍獨立保留。
        confirmedReadback = false
        return value
      }
      if (response.status === 200 && body?.success === true) {
        await showError('伺服器回應不完整，請手動重新讀取。')
        return
      }
      const result = articleFailure(body)
      // 共用 parser 保留未對應明細；巢狀搜尋欄位另連回會員搜尋輸入框。
      if (page === 'members' && response.status === 422 && result.code === 'E0001' && Array.isArray(body?.errors?.['filters.keyword'])) {
        const messages = []
        for (const message of body.errors['filters.keyword']) if (typeof message === 'string') messages.push(message)
        result.errors['filters.keyword'] = messages
      }
      finish(operation)
      rejectAccess(response.status, result.code)
      await showError(result.message, result.errors, result.logId, result.code)
    } catch {
      if (current(operation)) await showError('讀取失敗或伺服器回應不完整，請手動重試。')
    } finally {
      finish(operation)
    }
  }

  // 明確認證拒絕交回唯一 session；權限或目標失效只撤下本頁資源。
  function rejectAccess(status, code) {
    if (status === 401 && code === 'E0002') {
      forgetCredential()
      reLogin.value = true
    } else if (status === 403 && code === 'E0102') {
      forgetCredential()
      disabledAccount.value = true
    } else if (status === 403 && code === 'E0003' || status === 404 && code === 'E0004') clear()
  }

  // 搜尋只去除契約指定的 ASCII 空格，字面條件導覽至第一頁，不自行決定 NUL 政策。
  function submitMemberSearch() {
    const value = keyword.value.replace(/^ *| *$/g, '')
    if ([...value].length > 254) {
      showError('欄位資料不正確，請確認後再送出', { 'filters.keyword': ['搜尋關鍵字最多 254 個字。'] })
      return
    }
    window.location.assign(`/vue/admin/members${value ? `?keyword=${encodeURIComponent(value)}` : ''}`)
  }

  // 分頁保留原字面搜尋條件，管理文章／留言只帶頁碼，不污染公開範圍。
  function adminPageHref(nextPage) {
    const query = new URLSearchParams()
    if (page === 'members' && searchedKeyword) query.set('keyword', searchedKeyword)
    query.set('page', String(nextPage))
    return `${path}?${query.toString()}`
  }

  // 會員狀態只送契約欄位；完整成功後讀同一 ID，不以空 envelope 推測最新值。
  async function submitMemberStatus() {
    if (page !== 'member' || !data.value || !ready.value || loading.value || statusPending.value || statusNeedsReadback.value
      || authChecking.value || logoutPending.value || !credential.value || account.value?.role !== 'admin' || !isActive()) return
    const nextStatus = data.value.status === 'active' ? 'disabled' : 'active'
    statusOwner = { id: account.value.id, backend: backend.value }
    statusPending.value = true
    statusNotice.value = ''
    const operation = start(true)
    let confirmed = false
    try {
      const response = await fetch(`${operation.backend}/api/members/${requestedId}/status`, {
        method: 'PUT', headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${operation.token}` },
        body: JSON.stringify({ status: nextStatus }), signal: operation.controller.signal,
      })
      const body = await response.json()
      if (!current(operation)) return
      if (contentUpdated(response.status, body)) {
        confirmed = true
        confirmedReadback = true
        statusNotice.value = '更新已送達；正在重新讀取會員狀態。'
      } else {
        const result = articleFailure(body)
        const unknown = response.status >= 500 || response.status === 200
        if (unknown) {
          statusUnknown.value = true
          statusNeedsReadback.value = true
        }
        finish(operation)
        rejectAccess(response.status, result.code)
        await showError(unknown ? `更新結果尚未確認；請先重讀同一會員查證。${result.message}` : result.message, {}, result.logId, result.code)
      }
    } catch {
      if (current(operation)) {
        statusUnknown.value = true
        statusNeedsReadback.value = true
        await showError('更新結果尚未確認；請先重讀同一會員查證，再決定是否手動重試。')
      }
    } finally {
      finish(operation)
    }
    if (!confirmed || !sameActor(operation)) return
    // 讀回後再次確認同一身分與讀取世代，避免舊續段把新頁面顯示成先前成功。
    const readback = load(true)
    const readbackEpoch = epoch
    await readback
    if (!sameActor(operation) || epoch !== readbackEpoch) return
    if (ready.value && !statusUnknown.value) statusNotice.value = `已重讀會員，目前狀態為${data.value.status === 'active' ? '啟用' : '停用'}。`
    else if (!ready.value) statusNotice.value = '更新已送達，但尚未讀到最新會員狀態；請手動重新讀取。'
  }

  return { data, loading, ready, keyword, searchedKeyword, statusPending, statusUnknown, statusNeedsReadback, statusNotice,
    load, clear, acceptAccount, submitMemberSearch, adminPageHref, submitMemberStatus }
}
