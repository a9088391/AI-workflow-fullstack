import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BACKEND_KEY, BACKENDS, SESSION_KEY, apiRequest, clearSessionIfOwned, countdown, describeFailure, expired,
  frontendTarget, isAccount, isArticle, isComment, isLockoutSettings, isMember, isPage, readBackend, readSession,
  retryAfterSeconds, saveSession, uncertainWrite, validReturn, validateAccount, validateContent, validateLockoutSettings
} from '../src/lib/contracts.js';

// 模擬同源分頁儲存，確定六版共用欄位時不依賴框架私有狀態。
function memoryStorage() {
  const values = new Map();
  return {
    // 以瀏覽器 Storage 的 null 語意讀取不存在的鍵。
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    // 模擬儲存字串，供另一版前端讀取。
    setItem(key, value) { values.set(key, String(value)); },
    // 刪除憑證時不影響目前後端選擇。
    removeItem(key) { values.delete(key); }
  };
}

// 切換前端保留原 JWT、後端與到期時間；舊失效回應不能清掉新登入。
test('shared session survives frontend switch without extending expiry', () => {
  const storage = memoryStorage();
  const first = { backend: BACKENDS.laravel[1], accessToken: 'first', expiresAt: '2099-10-18 21:00:00' };
  assert.equal(saveSession(storage, first), true);
  assert.deepEqual(readSession(storage), first);
  assert.equal(readBackend(storage), BACKENDS.laravel[1]);
  assert.equal(expired(first, Date.parse('2099-10-18T13:00:01Z')), true);
  const second = { ...first, accessToken: 'second' };
  assert.equal(saveSession(storage, second), true);
  assert.equal(clearSessionIfOwned(storage, first), false);
  assert.deepEqual(readSession(storage), second);
  storage.setItem(BACKEND_KEY, BACKENDS.symfony[1]);
  assert.equal(readSession(storage), null);
  assert.equal(storage.getItem(SESSION_KEY), null);
});

// 未知後端不可成為 JWT 目的地，錯誤資料不能借用 Laravel 的預設值。
test('untrusted backend and malformed session are rejected', () => {
  const storage = memoryStorage();
  storage.setItem(BACKEND_KEY, 'https://elsewhere.test');
  storage.setItem(SESSION_KEY, JSON.stringify({ backend: 'https://elsewhere.test', accessToken: 'secret', expiresAt: '2026-10-18 21:00:00' }));
  assert.equal(readBackend(storage), BACKENDS.laravel[1]);
  assert.equal(readSession(storage), null);
  assert.equal(storage.getItem(SESSION_KEY), null);
});

// 返回路徑只接受共同語意頁與安全 ID，不允許網域、編碼繞過或超範圍 ID。
test('frontend and login targets stay on approved paths', () => {
  assert.equal(validReturn('/sveltekit/articles/101'), true);
  assert.equal(validReturn('/sveltekit/admin/settings/login-lockout'), true);
  assert.equal(validReturn('/sveltekit/admin/members/8'), true);
  assert.equal(validReturn('/sveltekit/admin/members/0'), false);
  assert.equal(validReturn('/sveltekit/articles/9007199254740992'), false);
  assert.equal(validReturn('//evil.example/'), false);
  assert.equal(validReturn('/sveltekit/%2F%2Fevil.example'), false);
  assert.equal(frontendTarget('/sveltekit/articles/101', 'vue'), '/vue/articles/101');
  assert.equal(frontendTarget('/sveltekit/login', 'next', '/sveltekit/articles/101'), '/next/login?return_to=%2Fnext%2Farticles%2F101');
  assert.equal(frontendTarget('/sveltekit/', 'constructor'), '/sveltekit/');
  for (const path of ['/sveltekit/articles/new', '/sveltekit/me/articles', '/sveltekit/me/comments',
    '/sveltekit/articles/101/edit', '/sveltekit/comments/201', '/sveltekit/comments/201/edit',
    '/sveltekit/admin/members', '/sveltekit/admin/members/8', '/sveltekit/admin/articles',
    '/sveltekit/admin/comments', '/sveltekit/admin/settings/login-lockout']) {
    assert.equal(validReturn(path), true);
    assert.equal(frontendTarget(path, 'vue'), path.replace('/sveltekit/', '/vue/'));
  }
  assert.equal(validReturn('/sveltekit/comments/9007199254740992/edit'), false);
});

// 管理資料先核對角色、形狀與四欄設定範圍，避免錯誤回應成為可操作狀態。
test('admin read models and settings validation reject malformed values', () => {
  const member = { id: 8, email: 'reader@example.test', nickname: '讀者', role: 'member', status: 'active', created_at: '2026-10-19 09:00:00' };
  const settings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  assert.equal(isMember(member), true);
  assert.equal(isAccount({ ...member, role: 'admin' }), true);
  assert.equal(isAccount({ ...member, id: '8' }), false);
  assert.equal(isAccount({ ...member, nickname: '\0' }), false);
  assert.equal(isAccount({ ...member, created_at: [member.created_at] }), false);
  assert.equal(isMember({ ...member, role: 'admin' }), false);
  assert.equal(isLockoutSettings(settings), true);
  assert.equal(isLockoutSettings({ ...settings, lock_seconds: 86401 }), false);
  assert.equal(isLockoutSettings({ ...settings, version: 1 }), false);
  assert.deepEqual(validateLockoutSettings(settings), {});
  assert.deepEqual(validateLockoutSettings({ ...settings, failure_threshold: 0 }).failure_threshold, ['請輸入 1 到 100 的整數。']);
});

// 本地文字與結果不明判斷須保留原文，且不把已知拒絕誤當可能寫入。
test('content validation and uncertain writes follow author recovery rules', () => {
  assert.deepEqual(validateContent('標題', '換行\n文字'), {});
  assert.deepEqual(validateContent(' ', '內容').title, ['欄位格式或範圍不正確']);
  assert.deepEqual(validateContent('', '😀'.repeat(2000), true), {});
  assert.equal(Boolean(validateContent('', '😀'.repeat(2001), true).body), true);
  assert.equal(Boolean(validateContent('標題', '\0').body), true);
  assert.equal(uncertainWrite({ status: 409, code: 'E0201' }), false);
  assert.equal(uncertainWrite({ status: 401, code: 'E0002' }), false);
  assert.equal(uncertainWrite({ status: 500, code: 'E0001' }), true);
  assert.equal(uncertainWrite({ status: 503, code: 'E9999' }), true);
  assert.equal(uncertainWrite(new TypeError('Failed to fetch')), true);
});

// 寫入只接受完整空物件成功；破損或狀態不符的回應不能觸發清稿與成功導覽。
test('API writes reject incomplete success and untrusted rejection codes', async () => {
  const previousFetch = globalThis.fetch;
  try {
    for (const [status, body] of [
      [201, { success: true }],
      [201, { success: true, message: 'OK', data: null }],
      [201, { success: true, message: 'OK', data: [] }],
      [201, { success: true, message: 'OK', data: { id: 1 } }],
      [200, { success: true, message: 'OK', data: {} }],
      [500, { success: false, message: '錯誤', error_code: 'E0001', errors: {} }]
    ]) {
      // 每次只替換傳輸回應，保留正式 API 判讀與結果不明分類。
      globalThis.fetch = async () => Response.json(body, { status });
      await assert.rejects(apiRequest(BACKENDS.laravel[1], '/api/articles', { method: 'POST', data: {} }),
        // 不能因回應帶有貌似已知代碼而重新開放重送。
        (failure) => uncertainWrite(failure) && failure.message === '回應資料格式不正確，請確認目前後端。');
    }
    // 合法新增成功才可交回頁面完成動作。
    globalThis.fetch = async () => Response.json({ success: true, message: 'OK', data: {} }, { status: 201 });
    assert.deepEqual(await apiRequest(BACKENDS.laravel[1], '/api/articles', { method: 'POST', data: {} }), {});
  } finally {
    globalThis.fetch = previousFetch;
  }
});

// E0005 的共同文案適用整個 4xx 範圍；合法訊息與可否重送仍須分開判定。
test('HTTP errors retain E0005 messages only for valid 4xx envelopes', async () => {
  const previousFetch = globalThis.fetch;
  try {
    for (const status of [399, 400, 405, 413, 499, 500]) {
      // 只替換 HTTP 邊界，正式解析與結果不明邏輯都保持原實作。
      globalThis.fetch = async () => Response.json({ success: false, message: '系統暫時無法處理，請稍後再試', error_code: 'E0005' }, { status });
      await assert.rejects(apiRequest(BACKENDS.laravel[1], '/api/articles', { method: 'POST', data: {} }),
        // 範圍內保留共同文案；範圍外仍是破損回應，兩者都不自動開放重送。
        (failure) => failure.message === (status >= 400 && status < 500 ? '系統暫時無法處理，請稍後再試' : '回應資料格式不正確，請確認目前後端。') && uncertainWrite(failure));
    }
  } finally { globalThis.fetch = previousFetch; }
});

// 倒數不猜測無效標頭，歸零不保留假的剩餘時間。
test('lock countdown uses valid remaining seconds only', () => {
  assert.equal(retryAfterSeconds('300'), 300);
  assert.equal(retryAfterSeconds('0'), null);
  assert.equal(retryAfterSeconds('Sun, 18 Oct 2026 00:00:00 GMT'), null);
  assert.equal(countdown(130_000, 0), '02:10');
  assert.equal(countdown(3_600_000, 0), '01:00:00');
  assert.equal(countdown(1_000, 1_000), '');
  assert.equal(describeFailure({ status: 503, message: '暫時失敗', logId: 'abc' }, '其他錯誤'), '暫時失敗　追查碼：abc');
});

// 帳號欄位遵守 ASCII Email／密碼與 Unicode 暱稱，不修剪密碼。
test('registration input validation preserves approved boundaries', () => {
  assert.deepEqual(validateAccount(' Demo@Example.Test ', 'abcd', '陳以澄').errors, {});
  assert.equal(validateAccount('demo@example.test', ' abc ', '陳以澄').errors.password.length, 1);
  assert.equal(validateAccount('demo@example.test', 'abcd', '\0').errors.nickname.length, 1);
});

// 列表與留言的形狀檢查確保錯誤頁碼和別篇留言不當作完整閱讀資料。
test('read models reject mismatched pagination and parent article', () => {
  const article = { id: 101, title: '標題', body: '內文', author: { id: 1, nickname: '作者' },
    created_at: '2026-10-18 10:00:00', updated_at: '2026-10-18 10:00:00', created_backend: 'laravel', version: 1, visibility: 'visible', can_restore: false };
  const comment = { id: 201, article_id: 101, body: '留言', author: { id: 2, nickname: '讀者' },
    created_at: '2026-10-18 10:01:00', updated_at: '2026-10-18 10:01:00', created_backend: 'gin', version: 1, visibility: 'visible', can_restore: false };
  assert.equal(isArticle(article), true);
  assert.equal(isArticle({ ...article, id: '101' }), false);
  assert.equal(isArticle({ ...article, updated_at: undefined }), false);
  assert.equal(isArticle({ ...article, created_backend: 'unknown' }), false);
  assert.equal(isComment(comment, 101), true);
  assert.equal(isComment(comment, 102), false);
  assert.equal(isComment({ ...comment, article_id: '101' }, 101), false);
  assert.equal(isPage({ list: [], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } }, 1), false);
  assert.equal(isPage({ list: [article], pagination: { page: 2, per_page: 20, total: 1, total_pages: 1 } }, 1), false);
});
