import { computed, ref } from 'vue'
import { articleFailure, contentUpdated } from './articles.js'
import { readLockoutSettings, validateLockoutSettings } from './admin.js'

// 設定 owner 單獨持有四欄草稿、伺服器值與讀寫生命週期，不與會員狀態互清結果。
export function useLockoutSettings({ session, canRead, isActive, showError, feedback }) {
  const { backend, credential, account, authChecking, logoutPending, forgetCredential, reLogin, disabledAccount } = session
  const { summary, summaryCode, errors, logId } = feedback
  const data = ref(null)
  const draft = ref(null)
  const loading = ref(false)
  const ready = ref(false)
  const pending = ref(false)
  const unknown = ref(false)
  const needsReadback = ref(false)
  const notice = ref('')
  let owner = null
  // 保留已確認 PUT 尚待讀回的事件，讓人工重讀可結束過期失敗提示，不推測未知寫入。
  let confirmedReadback = false
  let request = null
  let epoch = 0

  // 驗權或讀回期間沒有可信伺服器值，仍須保護已輸入的記憶體草稿。
  const hasDraft = computed(() => {
    if (!draft.value) return false
    if (!data.value) return true
    for (const name of Object.keys(data.value)) if (draft.value[name] !== data.value[name]) return true
    return false
  })

  // 同一 ID 也須維持原後端與管理員角色，才能接收先前請求的資料或回饋。
  function sameActor(operation) {
    return isActive() && backend.value === operation.backend && credential.value?.accessToken === operation.token
      && account.value?.id === operation.actor && account.value?.role === 'admin'
  }

  // 只有當前請求與原身分能更新設定；過期傳輸即使忽略 abort 也不能覆寫新狀態。
  function current(operation) {
    return request === operation && epoch === operation.epoch && sameActor(operation)
  }

  // 確定結果先終止計時與 pending，再撤銷身分，避免將明確拒絕誤標未知。
  function finish(operation) {
    if (request !== operation) return
    clearTimeout(operation.timer)
    request = null
    loading.value = false
    pending.value = false
  }

  // 取消前端等待不代表回滾；尚無回應的 PUT 保留原輸入與人工查證要求。
  function stop() {
    epoch += 1
    if (!request) return
    const operation = request
    if (operation.write) {
      unknown.value = true
      needsReadback.value = true
    }
    finish(operation)
    operation.controller.abort()
  }

  // 每次 GET 或 PUT 自己持有 controller／timer，逾時直接讓 UI 回到可查證狀態。
  function start(write, readback = false) {
    const operation = {
      controller: new AbortController(), epoch: ++epoch, write,
      backend: backend.value, token: credential.value.accessToken, actor: account.value.id, timer: null,
    }
    request = operation
    // 不等待 abort rejection 才解除控制，避免傳輸晚回應使 pending 永遠卡住。
    operation.timer = setTimeout(() => {
      if (request !== operation) return
      if (!current(operation)) {
        stop()
        return
      }
      stop()
      if (readback) notice.value = '更新已送達，但尚未讀到伺服器設定；請手動重新讀取。'
      showError(write ? '更新結果尚未確認；請手動重讀設定查證，再決定是否重新送出。' : '讀取失敗或伺服器回應不完整，請手動重試。')
    }, 20000)
    return operation
  }

  // 撤下授權資料與舊讀取提示但保留原帳號草稿／confirmed 事件，重驗後才判斷能否恢復。
  function clear() {
    stop()
    data.value = null
    ready.value = false
    loading.value = false
    notice.value = ''
  }

  // 角色、帳號或後端改變就清掉舊草稿與控制；同管理員重登只讀目前值供比較。
  function acceptAccount(nextAccount) {
    if (!owner || nextAccount.role === 'admin' && owner.id === nextAccount.id && owner.backend === backend.value) return
    draft.value = null
    unknown.value = false
    needsReadback.value = false
    notice.value = ''
    confirmedReadback = false
    owner = null
  }

  // 明確認證失效交給唯一 session；管理權限失效即撤下伺服器資料與表單。
  function rejectAccess(status, code) {
    if (status === 401 && code === 'E0002') {
      forgetCredential()
      reLogin.value = true
    } else if (status === 403 && code === 'E0102') {
      forgetCredential()
      disabledAccount.value = true
    } else if (status === 403 && code === 'E0003') clear()
  }

  // 初讀或本次已確認 PUT 的讀回才套用草稿；人工查證只提供比較，不能推斷先前結果。
  async function load(applyDraft = false) {
    if (loading.value || pending.value || !canRead()) return
    data.value = null
    ready.value = false
    loading.value = true
    summary.value = ''
    // 人工重讀先撤下舊成功提示；只有仍待讀回的 confirmed 事件保留尚未讀到的回饋。
    if (!applyDraft) notice.value = confirmedReadback ? '更新已送達，但尚未讀到伺服器設定；請手動重新讀取。' : ''
    const operation = start(false, applyDraft)
    try {
      const response = await fetch(`${operation.backend}/api/settings/login-lockout`, {
        headers: { Accept: 'application/json', Authorization: `Bearer ${operation.token}` }, signal: operation.controller.signal,
      })
      const body = await response.json()
      if (!current(operation)) return
      const value = readLockoutSettings(response.status, body)
      if (value) {
        data.value = value
        owner = { id: operation.actor, backend: operation.backend }
        if (!draft.value || applyDraft) draft.value = { ...value }
        ready.value = true
        if (unknown.value) {
          needsReadback.value = false
          notice.value = '已重讀目前伺服器設定；先前結果仍無法單憑目前值判定，請確認後再操作。'
        } else if (confirmedReadback) {
          notice.value = '已重新讀取並顯示目前伺服器設定。'
        }
        // 只有已確認 PUT 的後續 GET 才產生完成提示；不變更人工讀取的草稿採用規則。
        confirmedReadback = false
        return value
      }
      if (response.status === 200 && body?.success === true) {
        await showError('伺服器回應不完整，請手動重新讀取。')
        return
      }
      const result = articleFailure(body)
      finish(operation)
      rejectAccess(response.status, result.code)
      await showError(result.message, result.errors, result.logId, result.code)
    } catch {
      if (current(operation)) await showError('讀取失敗或伺服器回應不完整，請手動重試。')
    } finally {
      finish(operation)
    }
  }

  // 由元件送回完整新物件，草稿只有此 owner 能替換，不讓欄位元件修改 props。
  function updateDraft(value) {
    draft.value = value
  }

  // 四欄一同驗證與送出；即使關閉仍保留三個數值，未知結果只能手動重讀後再決定。
  async function submitLockoutSettings() {
    if (!data.value || !draft.value || !ready.value || loading.value || pending.value || needsReadback.value
      || authChecking.value || logoutPending.value || !credential.value || account.value?.role !== 'admin' || !isActive()) return
    const checked = validateLockoutSettings(draft.value)
    if (Object.keys(checked).length) {
      await showError('欄位資料不正確，請確認後再送出', checked)
      return
    }
    const payload = { enabled: draft.value.enabled, window_minutes: draft.value.window_minutes,
      failure_threshold: draft.value.failure_threshold, lock_seconds: draft.value.lock_seconds }
    owner = { id: account.value.id, backend: backend.value }
    pending.value = true
    notice.value = ''
    summary.value = ''
    const operation = start(true)
    let confirmed = false
    try {
      const response = await fetch(`${operation.backend}/api/settings/login-lockout`, {
        method: 'PUT', headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${operation.token}` },
        body: JSON.stringify(payload), signal: operation.controller.signal,
      })
      const body = await response.json()
      if (!current(operation)) return
      if (contentUpdated(response.status, body)) {
        // 完整成功才清除前次 422；PUT 不帶新值，畫面仍需 GET 才可呈現。
        summaryCode.value = ''
        errors.value = {}
        logId.value = ''
        confirmed = true
        confirmedReadback = true
        notice.value = '更新已送達；正在重新讀取伺服器設定。'
      } else {
        const result = articleFailure(body)
        const uncertain = response.status >= 500 || response.status === 200
        if (uncertain) {
          unknown.value = true
          needsReadback.value = true
        }
        const fieldErrors = {}
        // 只把已知四欄連到欄位；其他驗證明細仍由共用錯誤摘要呈現。
        if (response.status === 422 && result.code === 'E0001' && body?.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
          for (const name of ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds']) {
            if (!Array.isArray(body.errors[name])) continue
            fieldErrors[name] = []
            for (const message of body.errors[name]) if (typeof message === 'string') fieldErrors[name].push(message)
          }
        }
        finish(operation)
        rejectAccess(response.status, result.code)
        await showError(uncertain ? `更新結果尚未確認；請手動重讀設定查證。${result.message}` : result.message, fieldErrors, result.logId, result.code)
      }
    } catch {
      if (current(operation)) {
        unknown.value = true
        needsReadback.value = true
        await showError('更新結果尚未確認；請手動重讀設定查證，再決定是否重新送出。')
      }
    } finally {
      finish(operation)
    }
    if (!confirmed || !sameActor(operation)) return
    // 套用草稿只屬於這次 GET；失敗、切換或撤權不能把旗標遺留給之後另一人的讀取。
    const readback = load(true)
    const readbackEpoch = epoch
    await readback
    if (!sameActor(operation) || epoch !== readbackEpoch) return
    if (data.value && ready.value && !unknown.value) notice.value = '已重新讀取並顯示目前伺服器設定。'
    else if (!data.value) notice.value = '更新已送達，但尚未讀到伺服器設定；請手動重新讀取。'
  }

  return { data, draft, loading, ready, pending, unknown, needsReadback, notice, hasDraft,
    load, clear, acceptAccount, updateDraft, submitLockoutSettings }
}
