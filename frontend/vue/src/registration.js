// 單字元折小寫，供 Email 正規化使用且不碰其他 Unicode 字元。
function lowerAscii(letter) {
  return letter.toLowerCase()
}

// Email 只裁掉前後 U+0020 並折小寫 ASCII，避免瀏覽器或語言內建 trim 改變帳號。
export function normalizeEmail(value) {
  return value.replace(/^ +| +$/g, '').replace(/[A-Z]/g, lowerAscii)
}

// 網域每段都須遵守長度及連字號位置規則，避免寬鬆 regex 放過錯誤地址。
function validDomainLabel(label) {
  return label.length >= 1 && label.length <= 63 && /^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$/.test(label)
}

// 依核准格式逐段檢查 Email，禁止寬鬆的瀏覽器原生格式悄悄放行。
export function validEmail(value) {
  // 正規化後必須完全是 ASCII，且整個地址不得超過共用上限。
  if (value.length > 254 || !/^[\x00-\x7F]+$/.test(value)) return false
  const parts = value.split('@')
  if (parts.length !== 2) return false
  const [local, domain] = parts
  // 前段保留指定符號，但排除首尾或連續句點。
  if (local.length < 1 || local.length > 64 || !/^[a-z0-9._%+-]+$/.test(local) || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return false
  // 網域逐段限制長度與連字號；末段必須是純英文字母。
  const labels = domain.split('.')
  return labels.length >= 2 && labels.every(validDomainLabel) && /^[a-z]{2,63}$/.test(labels.at(-1))
}

// 只保留 API 契約的文字訊息，不把意外型態插進畫面。
function isText(value) {
  return typeof value === 'string'
}

// 客戶端先回饋同一契約的三欄錯誤；伺服器仍決定最終驗證與唯一性。
export function validateRegistration(input) {
  const email = normalizeEmail(input.email)
  const errors = {}
  if (!validEmail(email)) errors.email = ['請輸入符合格式的 Email']
  if (!/^[A-Za-z0-9]{4,16}$/.test(input.password)) errors.password = ['密碼須為 4–16 碼英文字母或數字']
  const nicknameLength = [...input.nickname].length
  if (nicknameLength < 1 || nicknameLength > 30 || /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028-\u2029\u202f\u205f\u3000]*$/u.test(input.nickname)) {
    errors.nickname = ['暱稱須為 1–30 個字，且不可全為空白']
  } else if (input.nickname.includes('\0')) {
    // PostgreSQL text 無法保存實際 U+0000；拒絕該字元而保留其他原始文字。
    errors.nickname = ['暱稱含有不支援的字元，請移除後再試']
  }
  return { email, errors }
}

// 將 API envelope 化成可顯示的結果；5xx 和不完整回應均標明寫入結果未知。
export function interpretRegistrationResponse(status, body) {
  // 寫入只接受 201 與空 data 的成功 envelope，其他格式不能假定已建立帳號。
  if (status === 201 && body?.success === true && body.message === 'OK' && body.data && typeof body.data === 'object' && !Array.isArray(body.data) && Object.keys(body.data).length === 0) return { ok: true }
  if (body?.success !== false) return { ok: false, message: '無法確認註冊結果；請先確認帳號狀態，再決定是否重試。', errors: {}, logId: '' }

  // 對應欄位的訊息可直接修正；未知欄位仍保留在總摘要，不吞掉伺服器提示。
  const errors = {}
  const extra = []
  if (body.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)) {
    for (const [key, values] of Object.entries(body.errors)) {
      if (!Array.isArray(values)) continue
      const messages = values.filter(isText)
      if (['email', 'password', 'nickname'].includes(key)) errors[key] = messages
      else extra.push(...messages)
    }
  }
  // 優先採共同錯誤文案；5xx 可能已完成寫入，所以明示結果未知與手動查證。
  const known = { E0001: '欄位資料不正確，請確認後再送出', E0005: '系統暫時無法處理，請稍後再試', E9999: '系統暫時無法處理，請稍後再試' }
  const message = known[body.error_code] || (typeof body.message === 'string' ? body.message : '系統暫時無法處理，請稍後再試')
  const detail = extra.length ? `${message}：${extra.join('；')}` : message
  return {
    ok: false,
    message: status >= 500 ? `${detail}。無法確認註冊結果；請先確認帳號狀態，再決定是否重試。` : detail,
    errors,
    logId: typeof body.log_id === 'string' ? body.log_id : '',
  }
}
