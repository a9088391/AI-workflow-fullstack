import test from 'node:test';
import assert from 'node:assert/strict';
import { get } from 'svelte/store';
import { BACKENDS, readSession, saveSession } from '../src/lib/contracts.js';
import { createSite } from '../src/lib/site.js';

// 全部固定後端都可在同一分頁切至 Nuxt；未知前端值仍不得導覽。
test('frontend switch accepts Nuxt for every backend', () => {
  const site = createSite();
  for (const [, base] of Object.values(BACKENDS)) {
    site.backend.set(base);
    assert.equal(site.canSwitchFrontend('nuxt'), true, base);
  }
  assert.equal(site.canSwitchFrontend('unknown'), false);
});

// 模擬同分頁儲存，檢查失效與其他帳號登入不會誤清目前憑證。
function storage() {
  const values = new Map();
  return {
    // 不存在的鍵須符合瀏覽器 Storage 的 null 回傳。
    getItem(key) { return values.get(key) ?? null; },
    // 共用登入欄位應以字串保存。
    setItem(key, value) { values.set(key, String(value)); },
    // 撤銷只移除指定鍵，不清掉後端選擇。
    removeItem(key) { values.delete(key); }
  };
}

// 登入／註冊只處理本次帳號；停用 B 不可撤銷仍有效的 A。
test('public account requests do not attach or invalidate the current session', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const local = storage();
  const saved = { backend: BACKENDS.laravel[1], accessToken: 'account-a', expiresAt: '2099-10-18 21:00:00' };
  saveSession(local, saved);
  globalThis.window = { sessionStorage: local };
  const seen = [];
  // 用兩個帳號的 API 回應確認 Authorization 僅用於舊帳號身分重驗。
  globalThis.fetch = async (url, options) => {
    seen.push({ url, authorization: options.headers.Authorization });
    if (url.endsWith('/api/me')) return Response.json({ success: true, data: { id: 1, role: 'member', status: 'active', nickname: 'A' } });
    return Response.json({ success: false, error_code: 'E0102', message: '此帳號已停用，請聯絡系統管理員', errors: {} }, { status: 403 });
  };
  try {
    const site = createSite();
    await site.initialize();
    await assert.rejects(site.request('/api/auth/login', { method: 'POST', data: { email: 'b@example.test', password: 'abcd' } }),
      // 停用結果屬於 B，仍應回給表單顯示。
      (failure) => failure.code === 'E0102');
    assert.deepEqual(seen.map(
      // 只比較實際送出的憑證，不把其他請求細節當成契約。
      (entry) => entry.authorization
    ), ['Bearer account-a', undefined]);
    assert.deepEqual(readSession(local), saved);
    assert.equal(get(site.authState), 'active');
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});

// 登出遇到後端已撤銷的 JWT 時，失效流程也須解除共用等待狀態。
test('logout invalidation clears pending after an expired token response', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const local = storage();
  saveSession(local, { backend: BACKENDS.laravel[1], accessToken: 'expired-a', expiresAt: '2099-10-18 21:00:00' });
  globalThis.window = { sessionStorage: local };
  // /api/me 先確認原登入；登出 401 模擬後端已撤銷同一憑證。
  globalThis.fetch = async (url) => url.endsWith('/api/me')
    ? Response.json({ success: true, data: { id: 1, role: 'member', status: 'active', nickname: 'A' } })
    : Response.json({ success: false, error_code: 'E0002', message: '登入已失效，請重新登入', errors: {} }, { status: 401 });
  try {
    const site = createSite();
    await site.initialize();
    await site.logout();
    assert.equal(get(site.pending), false);
    assert.equal(get(site.authState), 'expired');
    assert.equal(readSession(local), null);
  } finally {
    globalThis.window = previousWindow;
    globalThis.fetch = previousFetch;
  }
});
