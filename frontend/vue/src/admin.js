// 管理員只讀取一般會員完整欄位，避免把不完整的權限資料顯示為可操作帳號。
export function isMember(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.email === 'string' && value.email.length > 0
    && typeof value.nickname === 'string' && [...value.nickname].length > 0
    && value.role === 'member' && (value.status === 'active' || value.status === 'disabled')
    && typeof value.created_at === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value.created_at)
}

// 分頁須保留後端回傳的原頁碼與總數；超頁仍可分辨為有資料但此頁為空。
export function readMemberList(status, body, requestedPage) {
  const data = body?.data
  const p = data?.pagination
  return status === 200 && body?.success === true && body.message === 'OK'
    && Array.isArray(data?.list) && data.list.every(isMember)
    && p?.page === requestedPage && p.per_page === 20
    && Number.isSafeInteger(p.total) && p.total >= 0 && p.total_pages === Math.ceil(p.total / 20) ? data : null
}

// 詳情必須對應網址中的 ID；成功寫入的空物件本身不是最新狀態。
export function readMember(status, body, requestedId) {
  return status === 200 && body?.success === true && body.message === 'OK'
    && isMember(body.data) && body.data.id === requestedId ? body.data : null
}

const settingLimits = { window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 }

// 設定只接受四個已核准欄位與整數範圍，避免把不完整回應顯示成可儲存值。
export function validLockoutSettings(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== 4 || typeof value.enabled !== 'boolean') return false
  for (const [name, maximum] of Object.entries(settingLimits)) {
    if (!Number.isInteger(value[name]) || value[name] < 1 || value[name] > maximum) return false
  }
  return true
}

// 讀取成功仍須檢查完整 envelope；畫面只根據伺服器確認的值呈現設定。
export function readLockoutSettings(status, body) {
  return status === 200 && body?.success === true && body.message === 'OK' && validLockoutSettings(body.data) ? body.data : null
}

// 表單的 number input 可能暫時是空字串；先驗證再建立完整 PUT payload。
export function validateLockoutSettings(value) {
  const errors = {}
  if (typeof value.enabled !== 'boolean') errors.enabled = ['請選擇是否啟用。']
  for (const [name, maximum] of Object.entries(settingLimits)) {
    if (!Number.isInteger(value[name]) || value[name] < 1 || value[name] > maximum) errors[name] = [`請輸入 1 到 ${maximum} 的整數。`]
  }
  return errors
}
