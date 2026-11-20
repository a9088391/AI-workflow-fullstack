import { useCallback, useEffect, useRef, useState } from 'react'
import { failure, ownsRead, ownsSession, request, validContent, validList, validLockout, validMember, validWriteSuccess } from '../contracts.js'

// 每次後端、憑證或分頁變動都取消舊讀取，避免過時資料覆寫新選擇。
export function useRead(backend, credential, path, currentPage, enabled, onAuthFailure, switchEpoch, kind = 'article', expectedId = null) {
  const [result, setResult] = useState({ loading: true, data: null, error: null })
  const [revision, setRevision] = useState(0)
  // 結果附原請求身分，渲染時立即隔離切換前的私有資料，不等 effect 清理。
  const owner = { backend, credential, path, currentPage, enabled, kind, expectedId, revision }
  const current = ownsRead(result, owner)
  // 每個讀取生命週期只擁有自己的一次請求，依賴變動時清理。
  useEffect(() => {
    // 尚未取得讀取資格時清掉資料與等待，避免失權畫面繼續顯示舊結果。
    if (!enabled) {
      setResult({ ...owner, loading: false, data: null, error: null })
      return undefined
    }
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    setResult({ ...owner, loading: true, data: null, error: null })
    // 公開 API 若帶 JWT 就必須驗證；此處不在錯誤後靜默改以訪客重試。
    request(backend, path, { signal: controller.signal, headers: credential ? { Authorization: `Bearer ${credential.accessToken}` } : {} })
      .then(
        // 只接受本次路徑預期的成功 envelope；不完整資料不顯示為空列表。
        ({ status, body }) => {
          if (controller.signal.aborted || startedEpoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential)) return
          const validData = currentPage
            ? validList(body?.data, currentPage, kind)
            : kind === 'settings'
              ? validLockout(body?.data)
              : body?.data?.id === Number(expectedId) && (kind === 'member' ? validMember(body.data) : validContent(body.data, kind))
          if (status === 200 && body?.success === true && body.message === 'OK' && validData) {
            setResult({ ...owner, loading: false, data: body.data, error: null })
          } else {
            const problem = failure(body)
            if (credential && (problem.code === 'E0002' || problem.code === 'E0102')) onAuthFailure(problem, backend, credential)
            setResult({ ...owner, loading: false, data: null, error: problem })
          }
        },
        // 網路中斷保留登入狀態並提供手動重讀。
        () => {
          if (!controller.signal.aborted && startedEpoch === switchEpoch.current && ownsSession(sessionStorage, backend, credential)) {
            setResult({ ...owner, loading: false, data: null, error: { message: '暫時無法連線，請稍後手動重試。' } })
          }
        },
      )
    // 來源變動或元件離開時中止舊請求，避免舊回應落到新畫面。
    return () => controller.abort()
  }, [backend, credential, path, currentPage, enabled, revision, onAuthFailure, switchEpoch, kind, expectedId])
  // 手動重讀保留相同路徑，只增加請求世代；連線錯誤不自動重送。
  function retry() {
    // 同步清空舊資料，衝突比較不會把上一版誤當成剛讀回的新版本。
    setResult({ ...owner, loading: true, data: null, error: null })
    setRevision(
      // 使用最新世代，連續點擊也不會因過時閉包漏掉重讀。
      (value) => value + 1,
    )
  }
  return { loading: current ? result.loading : enabled, data: current ? result.data : null, error: current ? result.error : null, retry }
}

// 每次寫入只接受原 JWT 與原頁面世代的回應；未知結果須先查證再由人決定。
export function useWrite(backend, credential, onAuthFailure, switchEpoch, onDraftChange) {
  const [state, setState] = useState({ pending: false, error: null, uncertain: false })
  const active = useRef(null)
  const pendingRef = useRef(false)
  // 卸載時撤銷 UI 更新資格；伺服器可能已收到寫入，不能當作未送出。
  useEffect(() => {
    // 卸載的舊元件只中止自己的請求，不更新即將重建的畫面。
    return () => {
      active.current?.controller.abort()
      active.current = null
      pendingRef.current = false
    }
  }, [])
  // 只替目前這筆寫入收尾；舊 POST 不得清除新 POST 的等待狀態。
  const markUnknown = useCallback((operation) => {
    if (active.current !== operation) return false
    active.current = null
    pendingRef.current = false
    setState({ pending: false, error: { message: '結果尚未確認；請先查證資料，再決定是否手動重試。' }, uncertain: true })
    onDraftChange(true, false)
    return true
  }, [onDraftChange])
  // GET 先回 401 或重新登入會換掉原憑證；立即結束舊寫入等待並保留查證入口。
  useEffect(() => {
    const operation = active.current
    if (operation && (operation.backend !== backend || operation.credential !== credential || operation.epoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential))) {
      operation.controller.abort()
      markUnknown(operation)
    }
  }, [backend, credential, switchEpoch, markUnknown])
  // 呼叫者只有收到完整成功 envelope 才能導覽或重讀，其他結果保留表單。
  /**
   * @param {import('../contracts.js').ArticleInputDto | import('../contracts.js').CommentInputDto | import('../contracts.js').ContentUpdateInputDto | import('../contracts.js').VisibilityInputDto | import('../contracts.js').MemberStatusInputDto | import('../contracts.js').LockoutSettingsDto} payload
   */
  async function send(path, method, payload, successStatus) {
    if (!credential || pendingRef.current || state.uncertain) return { ok: false }
    // 同步鎖住同一操作，避免 React 下一次渲染前重複送出。
    pendingRef.current = true
    const controller = new AbortController()
    const startedEpoch = switchEpoch.current
    const operation = { controller, backend, credential, epoch: startedEpoch }
    active.current = operation
    setState({ pending: true, error: null, uncertain: false })
    try {
      const { status, body } = await request(backend, path, { method, signal: controller.signal, headers: { Authorization: `Bearer ${credential.accessToken}`, 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      // 已撤銷的原請求即使晚到 2xx，也不能當作新登入後的寫入成功。
      if (active.current !== operation) return { ok: false, uncertain: true, obsolete: true }
      if (controller.signal.aborted || startedEpoch !== switchEpoch.current || !ownsSession(sessionStorage, backend, credential)) {
        markUnknown(operation)
        return { ok: false, uncertain: true, obsolete: true }
      }
      if (validWriteSuccess(status, body, successStatus)) {
        setState({ pending: false, error: null, uncertain: false })
        return { ok: true }
      }
      const problem = failure(body)
      if (problem.code === 'E0002' || problem.code === 'E0102') onAuthFailure(problem, backend, credential)
      // 已發送的 5xx 或損壞成功回應不證明未寫入；鎖住重送直到手動查證。
      const uncertain = status >= 500 || status === successStatus || !body || body.success !== false
      setState({ pending: false, error: problem, uncertain })
      return { ok: false, problem, uncertain }
    } catch {
      const settled = markUnknown(operation)
      return { ok: false, uncertain: true, obsolete: !settled }
    } finally {
      // 新請求可能已接替舊請求，只有相同操作才能解除同步送出鎖。
      if (active.current === operation) {
        active.current = null
        pendingRef.current = false
      }
    }
  }
  // 只有使用者查證後才能解鎖未知結果；重讀本身不會重送寫入。
  const checked = useCallback(() => { setState({ pending: false, error: null, uncertain: false }) }, [])
  return { ...state, send, checked }
}
