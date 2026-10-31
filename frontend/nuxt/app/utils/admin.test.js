import assert from 'node:assert/strict'
import { test } from 'node:test'
import { isMember, readLockoutSettings, readMember, readMemberList, validateLockoutSettings } from './admin.js'

const member = { id: 2, email: 'member@example.test', nickname: '阿明', role: 'member', status: 'active', created_at: '2026-09-24 09:00:00' }
const settings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 }

// 管理清單只接受一般會員、原頁碼與完整分頁；超頁仍保留總數。
test('member responses preserve authorized list and over-page semantics', () => {
  const body = { success: true, message: 'OK', data: { list: [], pagination: { page: 9, per_page: 20, total: 1, total_pages: 1 } } }
  assert.deepEqual(readMemberList(200, body, 9), body.data)
  assert.equal(readMemberList(200, body, 1), null)
  assert.equal(isMember({ ...member, role: 'admin' }), false)
  assert.equal(readMember(200, { success: true, message: 'OK', data: member }, 2), member)
  assert.equal(readMember(200, { success: true, message: 'OK', data: member }, 3), null)
})

// 會員暱稱依 API10 固定碼點集合判斷空白，保留允許字元與伺服器原值。
test('member nickname uses the fixed Unicode whitespace contract', () => {
  const blankCodePoints = [...'\u0009\u000A\u000B\u000C\u000D\u0020\u0085\u00A0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200A\u2028\u2029\u202F\u205F\u3000']
  const oneMemberPagination = { page: 1, per_page: 20, total: 1, total_pages: 1 }
  const acceptedNicknames = [
    ['FEFF only', '\uFEFF'],
    ['U+200B only', '\u200B'],
    ['FEFF and U+200B only', '\uFEFF\u200B'],
    ['normal text', '阿明'],
    ['normal text with fixed whitespace and permitted format characters', '\uFEFF\u00A0阿明\u3000\u200B'],
  ]

  // JavaScript trim 會移除 FEFF、保留 U+200B；兩者都不在 API10 空白集合，必須原樣接受。
  for (const [caseName, nickname] of acceptedNicknames) {
    const acceptedMember = { ...member, nickname }
    const acceptedList = {
      success: true,
      message: 'OK',
      data: { list: [acceptedMember], pagination: oneMemberPagination },
    }
    assert.equal(isMember(acceptedMember), true, caseName)
    assert.equal(readMember(200, { success: true, message: 'OK', data: acceptedMember }, member.id), acceptedMember, caseName)
    assert.deepEqual(readMemberList(200, acceptedList, 1), acceptedList.data, caseName)
  }

  // 三個匯入入口都須拒絕只含固定空白碼點的會員，不能只驗直接呼叫。
  for (const nickname of blankCodePoints) {
    const invalidMember = { ...member, nickname }
    const invalidList = { success: true, message: 'OK', data: { list: [invalidMember], pagination: oneMemberPagination } }
    const codePoint = 'U+' + nickname.codePointAt(0).toString(16).toUpperCase()
    assert.equal(isMember(invalidMember), false, codePoint)
    assert.equal(readMember(200, { success: true, message: 'OK', data: invalidMember }, member.id), null, codePoint)
    assert.equal(readMemberList(200, invalidList, 1), null, codePoint)
  }

  // 長度依 Unicode 碼點計算，並拒絕 null 與不可儲存的 U+0000。
  for (const nickname of [null, '\0', '', '阿'.repeat(31)]) {
    const invalidMember = { ...member, nickname }
    const invalidList = { success: true, message: 'OK', data: { list: [invalidMember], pagination: oneMemberPagination } }
    assert.equal(isMember(invalidMember), false)
    assert.equal(readMember(200, { success: true, message: 'OK', data: invalidMember }, member.id), null)
    assert.equal(readMemberList(200, invalidList, 1), null)
  }
  assert.equal(isMember({ ...member, nickname: '阿'.repeat(30) }), true)
})

// 停用設定仍須完整驗證三個數值；布林與非整數不可被 number input 容錯送出。
test('lockout settings require exact fields and bounded integers', () => {
  assert.deepEqual(readLockoutSettings(200, { success: true, message: 'OK', data: settings }), settings)
  assert.equal(readLockoutSettings(200, { success: true, message: 'OK', data: { ...settings, secret: 1 } }), null)
  assert.deepEqual(validateLockoutSettings({ ...settings, enabled: false }), {})
  assert.deepEqual(Object.keys(validateLockoutSettings({ ...settings, window_minutes: 0, failure_threshold: 1.5, lock_seconds: '' })), ['window_minutes', 'failure_threshold', 'lock_seconds'])
})
