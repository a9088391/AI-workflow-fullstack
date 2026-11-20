import { useEffect, useRef, useState } from 'react'
import { apiFailure, apiRequest, emptySuccess, ownsSession, successData } from '../../lib/contracts.js'

// 每次路徑、頁碼或登入變動都取消舊讀取，並只呈現目前來源的結果。
export function useApiRead(backend, credential, path, kind, expected, articleId, enabled, onAuthFailure) {
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState({ key: '', loading: true, data: null, error: null })
  const failureRef = useRef(onAuthFailure)
  failureRef.current = onAuthFailure
  const token = credential?.accessToken || ''
  const key = `${backend}|${token}|${path}|${kind}|${expected}|${articleId}|${enabled}|${revision}`
  // 讀取生命週期只擁有自己的 AbortController；清理後的回應不得改畫面。
  useEffect(() => {
    if (!enabled || !backend) return undefined
    const controller = new AbortController()
    setResult({ key, loading: true, data: null, error: null })
    // 只有完整 envelope 與預期 ID／頁碼才作為成功資料。
    async function load() {
      try {
        const response = await apiRequest(backend, path, { token, signal: controller.signal })
        if (controller.signal.aborted) return
        const data = successData(response.status, response.body, kind, expected, articleId)
        if (data) { setResult({ key, loading: false, data, error: null }); return }
        // 會員列表保留 Query 錯誤鍵以連回欄位，其他讀取仍沿用一般摘要。
        const failure = apiFailure(response.status, response.body, kind === 'members' ? ['filters.keyword'] : [])
        if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') failureRef.current(backend, credential, failure)
        setResult({ key, loading: false, data: null, error: failure })
      } catch {
        if (!controller.signal.aborted) setResult({ key, loading: false, data: null, error: { message: '連線失敗，請確認目前後端後手動重試。' } })
      }
    }
    load()
    // 離開此讀取條件即中止請求，避免舊結果覆蓋目前資料。
    return () => controller.abort()
  }, [backend, token, path, kind, expected, articleId, enabled, revision, key])
  // 依當前請求識別隔離切換瞬間的舊資料，不等待 effect 才清空。
  const current = result.key === key ? result : { loading: true, data: null, error: null }
  // 手動重試只遞增讀取版次，讓原條件重新請求且舊回應仍被隔離。
  function reload() {
    // 用先前值遞增，避免快速連續重試遺失更新。
    setRevision((value) => value + 1)
  }
  return { ...current, reload }
}

// 瀏覽器可能在 await 期間拒絕 storage getter；寫入與查證須安全保留未知結果。
export function ownsWriteSession(backend, credential) {
  try { return ownsSession(sessionStorage, backend, credential) }
  catch { return false }
}

// 每個作者寫入各自追蹤請求與草稿，切換時作廢回應但不聲稱取消伺服器寫入。
export function useAuthorWrite(backend, credential, account, onAuthFailure, formNavigation, formKey) {
  const [state, setState] = useState({ pending: false, error: null, uncertain: false, conflict: false })
  const request = useRef(null)
  const readbackRequest = useRef(null)
  const sequence = useRef(0)
  const failedAuth = useRef(null)
  const stateRef = useRef(state)
  const ownerId = useRef(null)
  stateRef.current = state
  // 同一操作記住首次登入者；換帳號不能繼承前人的待查證結果。
  useEffect(() => { if (account && ownerId.current === null) ownerId.current = account.id }, [account])
  const sameAccount = Boolean(account && (ownerId.current === null || ownerId.current === account.id))
  // 登記每份表單的生命週期，文章頁的留言與可見性操作可同時受離頁保護。
  useEffect(() => {
    const entry = {
      dirty: false,
      pending: false,
      unresolved: false,
      // 接受離頁後同時作廢寫入與人工讀回；中止不代表伺服器回滾。
      cancel() { sequence.current += 1; request.current?.abort(); readbackRequest.current?.abort() },
    }
    formNavigation.current.authorForms.set(formKey, entry)
    // 只有真正離頁才清除操作狀態，父頁重讀及暫時失權仍保留此元件。
    return () => { entry.cancel(); formNavigation.current.authorForms.delete(formKey) }
  }, [formNavigation, formKey])
  // 新憑證或後端不能沿用舊查證回應；寫入仍等原請求決定已知／未知結果。
  useEffect(() => {
    // 讀回清理與寫入各自獨立，避免取消讀取就遺失已送出的寫入。
    return () => { readbackRequest.current?.abort(); readbackRequest.current = null }
  }, [backend, credential?.accessToken])
  // 只在原帳號取得新憑證後移除舊認證提示；草稿、版本及其他恢復鎖維持原狀。
  useEffect(() => {
    const failed = failedAuth.current
    if (!failed || !account || account.id !== failed.accountId || backend !== failed.backend || !credential
      || credential.accessToken === failed.accessToken || !ownsWriteSession(backend, credential)) return
    failedAuth.current = null
    // 只清舊認證錯誤，不用重登事件解除未知結果或版本衝突。
    setState((current) => ['E0002', 'E0102'].includes(current.error?.code) ? { ...current, error: null } : current)
  }, [account, backend, credential])
  // 同步標記目前草稿，讓選單在 React 下一次 render 前仍能阻止誤切換。
  function markDirty(dirty) {
    const entry = formNavigation.current.authorForms.get(formKey)
    if (entry) entry.dirty = dirty
  }
  // 未知結果鎖住自動重送；明確 409 留待人工讀取最新版後才可解鎖。
  /**
   * @param {import('../../lib/contracts.js').ArticleInputDto | import('../../lib/contracts.js').CommentInputDto | import('../../lib/contracts.js').ContentUpdateInputDto | import('../../lib/contracts.js').VisibilityInputDto | import('../../lib/contracts.js').MemberStatusInputDto | import('../../lib/contracts.js').LockoutSettingsDto} body
   */
  async function send(path, method, body, expectedStatus, fields) {
    if (!backend || !credential || !sameAccount || request.current || stateRef.current.uncertain || stateRef.current.conflict) return { obsolete: true }
    const controller = new AbortController()
    const turn = ++sequence.current
    request.current = controller
    const entry = formNavigation.current.authorForms.get(formKey)
    // 同步佔有請求與離頁摘要，避免快速點擊送出兩次。
    if (entry) entry.pending = true
    setState({ pending: true, error: null, uncertain: false, conflict: false })
    try {
      const response = await apiRequest(backend, path, { method, body, token: credential.accessToken, signal: controller.signal })
      if (controller.signal.aborted || turn !== sequence.current) return { obsolete: true }
      if (!ownsWriteSession(backend, credential)) {
        // 另一個 401 可能已清除原 JWT；已送出的寫入仍可能完成，必須人工讀回確認。
        const error = { message: '寫入結果尚未確認；請重新登入並讀回查證後，才決定是否手動再送。' }
        if (entry) entry.unresolved = true
        setState({ pending: false, error, uncertain: true, conflict: false })
        return { error, uncertain: true }
      }
      // 空 data 的完整封套才可觸發成功導頁或清稿。
      if (emptySuccess(response.status, response.body, expectedStatus)) {
        setState({ pending: false, error: null, uncertain: false, conflict: false })
        return { ok: true }
      }
      const error = apiFailure(response.status, response.body, fields)
      if (response.status === 401 && error.code === 'E0002' || response.status === 403 && error.code === 'E0102') {
        failedAuth.current = { backend, accessToken: credential.accessToken, accountId: account?.id }
        onAuthFailure(backend, credential, error)
      }
      const conflict = response.status === 409 && error.code === 'E0201'
      // 所有無法確認的 2xx 都可能已寫入，錯誤成功狀態也必須查證。
      const uncertain = response.status >= 500 || response.status >= 200 && response.status < 300
      if (entry) entry.unresolved = uncertain || conflict
      setState({ pending: false, error, uncertain, conflict })
      return { error, conflict, uncertain }
    } catch {
      if (controller.signal.aborted || turn !== sequence.current) return { obsolete: true }
      const error = { message: !ownsWriteSession(backend, credential)
        ? '寫入結果尚未確認；請重新登入並讀回查證後，才決定是否手動再送。'
        : '寫入結果尚未確認；請先重讀查證，不會自動重送。' }
      if (entry) entry.unresolved = true
      setState({ pending: false, error, uncertain: true, conflict: false })
      return { error, uncertain: true }
    } finally {
      // 只釋放本請求的 pending，未知結果留在 unresolved，仍需離頁確認。
      if (request.current === controller) {
        request.current = null
        if (entry) entry.pending = false
      }
    }
  }
  // 只能在使用者明確比較或查證後解除送出鎖，不自動再次呼叫 API。
  function checked() {
    const entry = formNavigation.current.authorForms.get(formKey)
    if (entry) entry.unresolved = false
    setState({ pending: false, error: null, uncertain: false, conflict: false })
  }
  // 使用者修改欄位時移除已過期的 422 提示，保留衝突或未知結果鎖。
  function clearError() {
    // 欄位修正不代表已查證寫入，保留其他狀態。
    setState((current) => ({ ...current, error: null }))
  }
  return { ...state, sameAccount, markDirty, send, checked, clearError, readbackRequest }
}
