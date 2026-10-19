import assert from 'node:assert/strict';
import test from 'node:test';
import { BACKENDS, BACKEND_KEY, clearSessionIfOwned, countdown, frontendTarget, normalizeEmail, positiveNumber, readSession, retryAfterSeconds, saveSession, SESSION_KEY, supportsSvelteRoute, validEmail, validReturn, validateAccount, validateContent } from './contracts.ts';

// 模擬同一分頁儲存，核對跨版登入不改寫原到期時間與舊回應隔離。
function storage(): Storage {
  const values = new Map<string, string>();
  return {
    // 只回傳同分頁中已保存的鍵值。
    getItem: (key: string) => values.get(key) ?? null,
    // 更新值以模擬登入或後端選擇。
    setItem: (key: string, value: string) => { values.set(key, value); },
    // 登出或失效時只刪指定鍵。
    removeItem: (key: string) => { values.delete(key); },
  } as Storage;
}

// 固定三欄與目前後端必須匹配，舊請求不能清掉新的登入。
test('同分頁登入與後端切換', function sessionContract() {
  const tab = storage();
  const original = { backend: BACKENDS.laravel[1], accessToken: 'first', expiresAt: '2026-10-18 20:00:00' };
  assert.equal(saveSession(tab, original), true);
  assert.deepEqual(readSession(tab), original);
  const newer = { ...original, accessToken: 'newer' };
  assert.equal(saveSession(tab, newer), true);
  assert.equal(clearSessionIfOwned(tab, original), false);
  assert.deepEqual(readSession(tab), newer);
  tab.setItem(BACKEND_KEY, BACKENDS.symfony[1]);
  assert.equal(readSession(tab), null);
  assert.equal(tab.getItem(SESSION_KEY), null);
});

// 帳號格式與返回 URL 的壞輸入須被拒絕，避免錯誤登入或外站轉址。
test('輸入與合法返回目標', function inputContract() {
  assert.equal(validEmail(normalizeEmail(' A+B@Example.Test ')), true);
  assert.deepEqual(Object.keys(validateAccount('bad', 'a$', '　').errors), ['email', 'password', 'nickname']);
  assert.deepEqual(validateAccount('ok@example.test', '1234', '小草').errors, {});
  assert.equal(positiveNumber('9007199254740992'), null);
  assert.equal(validReturn('/angular/articles/42'), true);
  assert.equal(validReturn('https://other.example/'), false);
  assert.equal(validReturn('/angular/articles/0'), false);
  assert.equal(frontendTarget('/angular/articles/42', 'next'), '/next/articles/42');
  assert.equal(frontendTarget('/angular/login', 'vue', '/angular/articles/42'), '/vue/login?return_to=%2Fvue%2Farticles%2F42');
});

// 鎖定秒數只接受安全正整數；顯示依真實經過時間遞減。
test('Retry-After 倒數', function countdownContract() {
  assert.equal(retryAfterSeconds('300'), 300);
  assert.equal(retryAfterSeconds('0'), null);
  assert.equal(retryAfterSeconds('Tue, 20 Oct 2026 00:00:00 GMT'), null);
  assert.equal(countdown(300_000, 0), '05:00');
  assert.equal(countdown(300_000, 1_000), '04:59');
  assert.equal(countdown(300_000, 300_000), '');
});

// 作者路徑僅搬合法 ID；文字邊界先提示，但伺服器仍作最後驗證。
test('作者頁切換與純文字輸入', function authorContract() {
  assert.equal(validReturn('/angular/articles/new'), true);
  assert.equal(validReturn('/angular/me/articles'), true);
  assert.equal(validReturn('/angular/me/comments'), true);
  assert.equal(validReturn('/angular/articles/15/edit'), true);
  assert.equal(validReturn('/angular/comments/9/edit'), true);
  assert.equal(validReturn('/angular/comments/0/edit'), false);
  assert.equal(validReturn('/angular/articles/1/edit?version=2'), false);
  assert.equal(frontendTarget('/angular/comments/9/edit', 'react'), '/react/comments/9/edit');
  assert.equal(frontendTarget('/angular/login', 'nuxt', '/angular/articles/15/edit'), '/nuxt/login?return_to=%2Fnuxt%2Farticles%2F15%2Fedit');
  assert.deepEqual(validateContent('留\n言'), {});
  assert.deepEqual(Object.keys(validateContent('　\n')), ['body']);
  assert.deepEqual(Object.keys(validateContent('a'.repeat(2001))), ['body']);
  assert.deepEqual(validateContent('內文', '標題'), {});
  assert.deepEqual(Object.keys(validateContent('內文', 'a'.repeat(201))), ['title']);
});

// SvelteKit 已完成作者路由，但管理頁、非法 ID 與外站不可作登入返回目標。
test('SvelteKit 作者路由白名單', function svelteAuthorRoutes() {
  for (const path of ['/angular/', '/angular/login', '/angular/register', '/angular/articles/new',
    '/angular/me/articles', '/angular/me/comments', '/angular/articles/001', '/angular/articles/001/edit',
    '/angular/comments/009', '/angular/comments/009/edit']) {
    assert.equal(supportsSvelteRoute(path), true, path);
    if (path !== '/angular/login') assert.equal(frontendTarget(path, 'sveltekit'), path.replace('/angular/', '/sveltekit/'));
  }
  for (const path of ['/angular/admin/members', '/angular/admin/articles', '/angular/articles/0/edit',
    '/angular/comments/9007199254740992', '/angular/comments/009?preview=1', 'https://evil.test']) {
    assert.equal(supportsSvelteRoute(path), false, path);
  }
  assert.equal(frontendTarget('/angular/login', 'sveltekit', '/angular/comments/009/edit'), '/sveltekit/login?return_to=%2Fsveltekit%2Fcomments%2F009%2Fedit');
});
