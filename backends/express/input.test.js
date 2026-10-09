'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { bodyInput, queryInput, getBody, urlInteger } = require('./input');

// request 只提供驗證器需要的原始 URL/Body，確保測到原始 Query 位元組。
function request(url, body) { return { originalUrl: url, body: body === undefined ? undefined : Buffer.from(body) }; }

// API15 的重複參數優先於格式錯誤，陣列鍵也不得當單值使用。
test('query validation preserves duplicate and array semantics', () => {
  assert.deepEqual(queryInput(request('/api/articles?page=01&per_page=20'), ['page', 'per_page']), { page: 1, per_page: 20 });
  assert.throws(() => queryInput(request('/api/articles?page=bad&page=1&per_page=20'), ['page', 'per_page']), { code: 'E0001', errors: { page: ['此參數不可重複'] } });
  assert.throws(() => queryInput(request('/api/articles?page[]=1&per_page=20'), ['page', 'per_page']), { code: 'E0001', errors: { page: ['欄位型態不正確'] } });
  assert.throws(() => queryInput(request('/api/articles?__proto__=x'), []), error =>
    Object.hasOwn(error.errors, '__proto__') && error.errors['__proto__'][0] === '不支援此欄位');
  for (const value of ['+1', '-1', ' 1', '1 ', '1e0', '1.0', '0', '9007199254740992']) {
    assert.throws(() => urlInteger(value, 'article_id'), { code: 'E0001', errors: { article_id: ['欄位格式或範圍不正確'] } });
  }
});

// API14 先拒絕 GET 非空本文；POST 無本文與非 object 要回 _body。
test('body boundary distinguishes GET, JSON syntax and object shape', () => {
  assert.throws(() => getBody(request('/api/me', '{}')), { errors: { _body: ['此 API 不接受 Request Body'] } });
  assert.throws(() => bodyInput(request('/api/auth/login'), ['email', 'password']), { errors: { _body: ['Request Body 必須為 JSON object'] } });
  assert.throws(() => bodyInput(request('/api/auth/login', '[]'), ['email', 'password']), { errors: { _body: ['Request Body 必須為 JSON object'] } });
  assert.throws(() => bodyInput(request('/api/auth/login', '{'), ['email', 'password']), { code: 'E0005' });
  assert.throws(() => bodyInput(request('/api/auth/login', '"\\ud800"'), ['email', 'password']), { code: 'E0005' });
  assert.deepEqual(bodyInput(request('/api/auth/logout', '{}'), [], true), {});
});

// SEC01/02 同時驗 Email 正規化與密碼原樣保留，格式錯誤不進入登入交易。
test('registration field rules normalize only ASCII email', () => {
  const body = request('/api/auth/register', JSON.stringify({ email: ' USER+tag@EXAMPLE.TEST ', password: 'Ab12', nickname: '陳同學' }));
  assert.deepEqual(bodyInput(body, ['email', 'password', 'nickname']), { email: 'user+tag@example.test', password: 'Ab12', nickname: '陳同學' });
  assert.throws(() => bodyInput(request('/', JSON.stringify({ email: '\tuser@example.test', password: 'abc', nickname: '\u0000' })), ['email', 'password', 'nickname']), {
    errors: { email: ['欄位格式或範圍不正確'], password: ['欄位格式或範圍不正確'], nickname: ['欄位格式或範圍不正確'] },
  });
});
