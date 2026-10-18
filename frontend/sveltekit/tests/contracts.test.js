import test from 'node:test';
import assert from 'node:assert/strict';
import {
  BACKEND_KEY, BACKENDS, SESSION_KEY, clearSessionIfOwned, countdown, describeFailure, expired,
  frontendTarget, isArticle, isComment, isPage, readBackend, readSession,
  retryAfterSeconds, saveSession, validReturn, validateAccount
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
  assert.equal(validReturn('/sveltekit/admin/settings/login-lockout'), false);
  assert.equal(validReturn('/sveltekit/articles/9007199254740992'), false);
  assert.equal(validReturn('//evil.example/'), false);
  assert.equal(validReturn('/sveltekit/%2F%2Fevil.example'), false);
  assert.equal(frontendTarget('/sveltekit/articles/101', 'vue'), '/vue/articles/101');
  assert.equal(frontendTarget('/sveltekit/login', 'next', '/sveltekit/articles/101'), '/next/login?return_to=%2Fnext%2Farticles%2F101');
  assert.equal(frontendTarget('/sveltekit/', 'constructor'), '/sveltekit/');
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
    created_at: '2026-10-18 10:00:00', created_backend: 'laravel', version: 1, visibility: 'visible', can_restore: false };
  const comment = { id: 201, article_id: 101, body: '留言', author: { id: 2, nickname: '讀者' },
    created_at: '2026-10-18 10:01:00', created_backend: 'gin', version: 1, visibility: 'visible', can_restore: false };
  assert.equal(isArticle(article), true);
  assert.equal(isComment(comment, 101), true);
  assert.equal(isComment(comment, 102), false);
  assert.equal(isPage({ list: [article], pagination: { page: 2, per_page: 20, total: 1, total_pages: 1 } }, 1), false);
});
