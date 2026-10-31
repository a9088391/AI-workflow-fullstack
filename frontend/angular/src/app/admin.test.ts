import assert from 'node:assert/strict';
import test from 'node:test';
import { isLockoutSettings, isMember, memberKeyword, settingErrors } from './admin.ts';
import { frontendTarget, isReadingPage, validReturn } from './contracts.ts';

// 管理頁只允許已核准的深連結，會員資料不能把管理員或缺欄當成可操作目標。
test('管理路徑及會員資料', function memberContract() {
  assert.equal(validReturn('/angular/admin/members'), true);
  assert.equal(validReturn('/angular/admin/members/9'), true);
  assert.equal(validReturn('/angular/admin/members/0'), false);
  assert.equal(frontendTarget('/angular/admin/members/9', 'vue'), '/vue/admin/members/9');
  assert.equal(frontendTarget('/angular/admin/settings/login-lockout', 'react'), '/react/admin/settings/login-lockout');
  const member = { id: 9, email: 'a@example.test', nickname: '小草', role: 'member', status: 'disabled', created_at: '2026-10-18 10:00:00' };
  assert.equal(isMember(member), true);
  assert.equal(isMember({ ...member, role: 'admin' }), false);
  assert.equal(isMember({ ...member, email: '' }), false);
});

// 四欄設定缺值、非整數與越界都須擋在送出前；搜尋只修剪 ASCII 空格。
test('鎖定設定與搜尋契約', function settingsContract() {
  const current = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  assert.equal(isLockoutSettings(current), true);
  assert.deepEqual(settingErrors(current), {});
  assert.equal(isLockoutSettings({ ...current, lock_seconds: 86401 }), false);
  assert.equal(isLockoutSettings({ ...current, failure_threshold: 1.5 }), false);
  assert.equal(isLockoutSettings({ ...current, unknown: 1 }), false);
  assert.equal(isLockoutSettings({ ...current, enabled: 'true' }), false);
  assert.equal(memberKeyword('  陳%_  '), '陳%_');
  assert.equal(memberKeyword('\u3000陳\u3000'), '\u3000陳\u3000');
});

// 管理列表沿用完整分頁契約；錯頁、重複目標及不可能筆數不能變成可停用會員。
test('會員管理分頁保留完整性與超頁語意', function memberPageContract() {
  const member = { id: 9, email: 'a@example.test', nickname: '小草', role: 'member', status: 'active', created_at: '2026-10-18 10:00:00' };
  const page = { list: [member], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } };
  assert.equal(isReadingPage(page, 1, isMember), true);
  assert.equal(isReadingPage(page, 2, isMember), false);
  assert.equal(isReadingPage({ ...page, list: [member, member] }, 1, isMember), false);
  assert.equal(isReadingPage({ ...page, list: [{ ...member, role: 'admin' }] }, 1, isMember), false);
  assert.equal(isReadingPage({ ...page, pagination: { ...page.pagination, total: 0, total_pages: 0 } }, 1, isMember), false);
  assert.equal(isReadingPage({ list: [], pagination: { ...page.pagination, page: 2 } }, 2, isMember), true);
});
