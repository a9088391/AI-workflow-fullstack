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

// 停用設定仍須完整驗證三個數值；布林與非整數不可被 number input 容錯送出。
test('lockout settings require exact fields and bounded integers', () => {
  assert.deepEqual(readLockoutSettings(200, { success: true, message: 'OK', data: settings }), settings)
  assert.equal(readLockoutSettings(200, { success: true, message: 'OK', data: { ...settings, secret: 1 } }), null)
  assert.deepEqual(validateLockoutSettings({ ...settings, enabled: false }), {})
  assert.deepEqual(Object.keys(validateLockoutSettings({ ...settings, window_minutes: 0, failure_threshold: 1.5, lock_seconds: '' })), ['window_minutes', 'failure_threshold', 'lock_seconds'])
})
