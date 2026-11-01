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
    if (url.endsWith('/api/me')) return Response.json({ success: true, message: 'OK', data: { id: 1, email: 'a@example.test', created_at: '2026-10-31 10:00:00', role: 'member', status: 'active', nickname: 'A' } });
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
    ? Response.json({ success: true, message: 'OK', data: { id: 1, email: 'a@example.test', created_at: '2026-10-31 10:00:00', role: 'member', status: 'active', nickname: 'A' } })
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

// 身分重驗屬共用外殼；站內換頁後仍須進入成功、失效或可重試錯誤，而不是永久 checking。
test('initial session verification survives page navigation in every terminal state', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const actor = { id: 1, email: 'a@example.test', created_at: '2026-10-31 10:00:00', role: 'member', status: 'active', nickname: 'A' };
  try {
    for (const terminal of ['active', 'expired', 'error']) {
      const local = storage();
      saveSession(local, { backend: BACKENDS.laravel[1], accessToken: 'a', expiresAt: '2099-10-18 21:00:00' });
      globalThis.window = { sessionStorage: local };
      let release;
      // 延後正式傳輸，讓離頁先於原憑證驗證完成。
      globalThis.fetch = () => new Promise(
        // 保存回應控制權，只在已接受離頁後釋放。
        (resolve, reject) => { release = terminal === 'error' ? reject : resolve; }
      );
      const site = createSite();
      const initialization = site.initialize();
      site.leavePage();
      release(terminal === 'active'
        ? Response.json({ success: true, message: 'OK', data: actor })
        : terminal === 'expired'
          ? Response.json({ success: false, error_code: 'E0002', message: '登入已逾期，請重新登入' }, { status: 401 })
          : new TypeError('controlled network failure'));
      await initialization;
      await site.initialize();
      assert.equal(get(site.authState), terminal);
      assert.equal(get(site.account)?.id ?? null, terminal === 'active' ? actor.id : null);
    }
  } finally { globalThis.window = previousWindow; globalThis.fetch = previousFetch; }
});

// 登出收尾依憑證而非頁面世代；成功、失敗都須解除等待，且不可撤銷較新的登入。
test('logout survives page navigation but preserves a newer credential', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const actor = { id: 1, email: 'a@example.test', created_at: '2026-10-31 10:00:00', role: 'member', status: 'active', nickname: 'A' };
  try {
    for (const terminal of ['success', 'failure', 'new-login']) {
      const local = storage();
      const saved = { backend: BACKENDS.laravel[1], accessToken: 'a', expiresAt: '2099-10-18 21:00:00' };
      saveSession(local, saved);
      globalThis.window = { sessionStorage: local, location: { pathname: '/sveltekit/' } };
      let release;
      // 重驗先完成；只延後撤銷原憑證的回應。
      globalThis.fetch = (url) => url.endsWith('/api/me')
        ? Promise.resolve(Response.json({ success: true, message: 'OK', data: actor }))
        : new Promise(
          // 讓下一頁或新登入先取得控制，再完成原登出。
          (resolve, reject) => { release = terminal === 'failure' ? reject : resolve; }
        );
      const site = createSite();
      await site.initialize();
      const logout = site.logout();
      site.leavePage();
      site.dirty.set(true);
      if (terminal === 'new-login') assert.equal(site.acceptLogin({ access_token: 'new-a', token_type: 'Bearer', expires_at: saved.expiresAt, user: actor }, actor.email), true);
      release(terminal === 'failure' ? new TypeError('controlled network failure') : Response.json({ success: true, message: 'OK', data: {} }));
      await logout;
      assert.equal(get(site.pending), false);
      assert.equal(get(site.dirty), true);
      assert.equal(get(site.authState), terminal === 'success' ? 'guest' : 'active');
      assert.equal(readSession(local)?.accessToken ?? null, terminal === 'success' ? null : terminal === 'new-login' ? 'new-a' : 'a');
      if (terminal === 'failure') assert.equal(get(site.notice), '登出未完成，請確認目前後端後重試。');
    }
  } finally { globalThis.window = previousWindow; globalThis.fetch = previousFetch; }
});

// 舊登出只撤銷原憑證；新頁已送出的註冊仍須交代結果，且等待與草稿不能被提早清除。
test('logout preserves a new page public write and its pending ownership', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const local = storage();
  saveSession(local, { backend: BACKENDS.laravel[1], accessToken: 'a', expiresAt: '2099-10-18 21:00:00' });
  globalThis.window = { sessionStorage: local, location: { pathname: '/sveltekit/register',
    // 新頁從未同意被原登出強制導離，任何 assign 都應讓測試失敗。
    assign() { assert.fail('舊登出不可導離新頁'); }
  } };
  let releaseLogout;
  let releaseRegistration;
  let registrationStarted;
  // 明確等新頁的 HTTP 已送出，不以固定延遲猜測競態順序。
  const started = new Promise((resolve) => { registrationStarted = resolve; });
  // 模擬同一後端的獨立操作，註冊不能附帶原帳號憑證。
  globalThis.fetch = (url, options) => {
    if (url.endsWith('/api/me')) return Promise.resolve(Response.json({ success: true, message: 'OK', data: { id: 1, email: 'a@example.test', created_at: '2026-10-31 10:00:00', role: 'member', status: 'active', nickname: 'A' } }));
    if (url.endsWith('/api/auth/logout')) return new Promise(
      // 原登出在新頁寫入已送出後才完成。
      (resolve) => { releaseLogout = resolve; }
    );
    assert.equal(url.endsWith('/api/auth/register'), true);
    assert.equal(options.headers.Authorization, undefined);
    return new Promise(
      // 第二個操作保留自己的回應責任與等待狀態。
      (resolve) => { releaseRegistration = resolve; registrationStarted(); }
    );
  };
  try {
    const site = createSite();
    await site.initialize();
    const logout = site.logout();
    site.leavePage(); site.dirty.set(true);
    const pageVersion = site.version();
    const writeVersion = site.writeVersion();
    const registration = site.request('/api/auth/register', { method: 'POST', data: { email: 'new@example.test', nickname: '新頁', password: 'abcd' } });
    await started;
    releaseLogout(Response.json({ success: true, message: 'OK', data: {} }));
    await logout;
    assert.equal(get(site.authState), 'guest');
    assert.equal(get(site.pending), true);
    assert.equal(get(site.dirty), true);
    assert.equal(site.version(), pageVersion);
    assert.equal(site.writeVersion(), writeVersion);
    releaseRegistration(Response.json({ success: false, error_code: 'E9999', message: '系統暫時無法處理，請稍後再試' }, { status: 500 }));
    await assert.rejects(registration,
      // 原寫入結果仍回到新頁，不可被取消訊號或舊登出世代吞掉。
      (failure) => failure.code === 'E9999');
    assert.equal(get(site.pending), false);
    assert.equal(get(site.dirty), true);
  } finally { globalThis.window = previousWindow; globalThis.fetch = previousFetch; }
});

// 站內換頁可沿用初始化，但後端切換、外殼銷毀或較新登入都不能被舊 /api/me 覆蓋。
test('initial verification cannot overwrite a superseding shell or credential', async () => {
  const previousWindow = globalThis.window;
  const previousFetch = globalThis.fetch;
  const actor = { id: 1, email: 'a@example.test', created_at: '2026-10-31 10:00:00', role: 'member', status: 'active', nickname: 'A' };
  try {
    for (const action of ['backend', 'dispose', 'login']) {
      const local = storage();
      saveSession(local, { backend: BACKENDS.laravel[1], accessToken: 'a', expiresAt: '2099-10-18 21:00:00' });
      globalThis.window = { sessionStorage: local, location: {
        // 本測試只觀察共用身分隔離，不啟動真正文件導覽。
        assign() {}
      } };
      let release;
      // 舊重驗故意不理會取消，以驗證應用本身的持有者門禁。
      globalThis.fetch = () => new Promise(
        // 原回應晚於外殼失效或新憑證出現。
        (resolve) => { release = resolve; }
      );
      const site = createSite();
      const initialization = site.initialize();
      if (action === 'backend') site.switchBackend(BACKENDS.symfony[1], '/sveltekit/');
      else if (action === 'dispose') site.dispose();
      else assert.equal(site.acceptLogin({ access_token: 'new-b', token_type: 'Bearer', expires_at: '2099-10-18 21:00:00', user: { ...actor, id: 2, email: 'b@example.test' } }, 'b@example.test'), true);
      release(Response.json({ success: true, message: 'OK', data: actor }));
      await initialization;
      assert.equal(get(site.account)?.id ?? null, action === 'login' ? 2 : null);
      assert.equal(get(site.authState), action === 'backend' ? 'guest' : action === 'login' ? 'active' : 'checking');
    }
  } finally { globalThis.window = previousWindow; globalThis.fetch = previousFetch; }
});
