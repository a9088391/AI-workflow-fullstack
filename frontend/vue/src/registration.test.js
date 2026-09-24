import test from 'node:test'
import assert from 'node:assert/strict'
import { interpretRegistrationResponse, normalizeEmail, validateRegistration } from './registration.js'

// 證明 Email 只處理指定空格、密碼不裁切，以及暱稱按碼點與固定空白集合驗證。
test('registration boundary rules', () => {
  assert.equal(normalizeEmail('  A.B+Tag@EXAMPLE.COM  '), 'a.b+tag@example.com')
  assert.equal(normalizeEmail('\tA@EXAMPLE.COM\t'), '\ta@example.com\t')
  assert.deepEqual(validateRegistration({ email: ' A@EXAMPLE.COM ', password: 'abcd', nickname: ' 林 ' }).errors, {})
  assert.ok(validateRegistration({ email: 'a..b@example.com', password: 'abcd', nickname: '林' }).errors.email)
  assert.ok(validateRegistration({ email: 'a@example.com', password: ' abcd', nickname: '林' }).errors.password)
  assert.ok(validateRegistration({ email: 'a@example.com', password: 'abcd', nickname: '\u00a0\u3000' }).errors.nickname)
  assert.deepEqual(validateRegistration({ email: 'a@example.com', password: 'abcd', nickname: '😀'.repeat(30) }).errors, {})
  assert.ok(validateRegistration({ email: 'a@example.com', password: 'abcd', nickname: '😀'.repeat(31) }).errors.nickname)
})

// 實際 U+0000 會讓 PostgreSQL text 寫入失敗；字面反斜線文字則須原樣允許。
test('nickname rejects actual NUL without rejecting literal escape text', () => {
  const input = { email: 'a@example.com', password: 'abcd', nickname: '' }
  assert.match(validateRegistration({ ...input, nickname: '\0' }).errors.nickname[0], /不支援的字元/)
  assert.match(validateRegistration({ ...input, nickname: '林\0' }).errors.nickname[0], /不支援的字元/)
  assert.deepEqual(validateRegistration({ ...input, nickname: String.raw`\u0000` }).errors, {})
})

// 驗證已知 422 可回到欄位，而 5xx 必須提示結果不明且保留追蹤編號。
test('registration API failure states', () => {
  assert.deepEqual(interpretRegistrationResponse(201, { success: true, message: 'OK', data: {} }), { ok: true })
  assert.deepEqual(interpretRegistrationResponse(422, { success: false, error_code: 'E0001', errors: { email: ['此 Email 已註冊'] }, log_id: 'trace-1' }), {
    ok: false,
    message: '欄位資料不正確，請確認後再送出',
    errors: { email: ['此 Email 已註冊'] },
    logId: 'trace-1',
  })
  assert.match(interpretRegistrationResponse(500, { success: false, error_code: 'E9999', log_id: 'trace-2' }).message, /無法確認註冊結果/)
  assert.equal(interpretRegistrationResponse(500, { success: false, error_code: 'E9999', log_id: 'trace-2' }).logId, 'trace-2')
})
