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

// 未知畸形 filters key 必須保留原名，只有完整巢狀欄位可改為點記法。
test('query validation keeps malformed filter names exact', () => {
  // 尾端多字元不是合法巢狀欄位，錯誤鍵必須與原 Query 名稱一致。
  assert.throws(() => queryInput(request('/api/articles?page=1&per_page=20&filters[scope]x=1'), ['page', 'per_page', 'filters[scope]']), {
    code: 'E0001', errors: { 'filters[scope]x': ['不支援此欄位'] },
  });
  // 完整 filters[scope] 的格式錯誤仍使用共同的點記法鍵名。
  assert.throws(() => queryInput(request('/api/articles?page=1&per_page=20&filters[scope]=bad'), ['page', 'per_page', 'filters[scope]']), {
    code: 'E0001', errors: { 'filters.scope': ['欄位格式或範圍不正確'] },
  });
  // 合法欄位的陣列鍵仍屬型態錯誤，維持既有判斷順序。
  assert.throws(() => queryInput(request('/api/articles?page=1&per_page=20&filters[scope][]=management'), ['page', 'per_page', 'filters[scope]']), {
    code: 'E0001', errors: { 'filters.scope': ['欄位型態不正確'] },
  });
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

// 內容欄位依碼點計長，version 必須是 JSON 整數且文字原樣保留。
test('author content accepts text boundaries and rejects invalid version syntax', () => {
  const valid = request('/api/comments/1', JSON.stringify({ body: '甲\r\n😀', version: 1 }));
  assert.deepEqual(bodyInput(valid, ['body', 'version']), { body: '甲\r\n😀', version: 1 });
  for (const raw of ['1.0', '1e0', '"1"', 'true']) {
    const req = request('/api/comments/1', `{"body":"甲","version":${raw}}`);
    // JSON version 詞形不為整數時，應先報型態錯誤。
    assert.throws(() => bodyInput(req, ['body', 'version']), { errors: { version: ['欄位型態不正確'] } });
  }
  const negative = request('/api/comments/1', '{"body":"甲","version":-1}');
  // 整數詞形有效但版本小於一時，應報範圍錯誤。
  assert.throws(() => bodyInput(negative, ['body', 'version']), { errors: { version: ['欄位格式或範圍不正確'] } });
  const article = request('/api/articles', JSON.stringify({ title: '標題', body: '😀'.repeat(20000) }));
  assert.equal([...bodyInput(article, ['title', 'body']).body].length, 20000);
  for (const value of ['\u3000\r\n', '甲\0', '😀'.repeat(2001)]) {
    const req = request('/api/comments/1', JSON.stringify({ body: value }));
    // 全空白、NUL 或超長留言均須在寫入前拒絕。
    assert.throws(() => bodyInput(req, ['body']), { errors: { body: ['欄位格式或範圍不正確'] } });
  }
});

// 巢狀未知欄位不可覆蓋頂層 version 詞形；錯誤明細仍須同時列出兩欄。
test('nested version cannot change top-level version validation', () => {
  for (const syntax of ['1.0', '1e0']) {
    for (const raw of [
      `{"body":"甲","version":${syntax},"x":{"version":1}}`,
      `{"body":"甲","x":{"version":1},"version":${syntax}}`,
    ]) {
      // 巢狀欄位不可覆蓋頂層原始詞形；兩種錯誤需一併回報。
      assert.throws(() => bodyInput(request('/api/comments/1', raw), ['body', 'version']), {
        errors: { version: ['欄位型態不正確'], x: ['不支援此欄位'] },
      });
    }
  }
  // 頂層詞形正確時，不可把巢狀無效數字誤判成頂層錯誤。
  assert.throws(() => bodyInput(request('/api/comments/1', '{"body":"甲","version":1,"x":{"version":1.0}}'), ['body', 'version']), {
    errors: { x: ['不支援此欄位'] },
  });
});

// 管理輸入驗證涵蓋字面搜尋、狀態與四欄設定，避免框架型態轉換放寬契約。
test('admin inputs preserve keyword and strict setting types', () => {
  const allowed = ['page', 'per_page', 'filters[keyword]'];
  assert.deepEqual(queryInput(request('/api/members?page=1&per_page=20&filters%5Bkeyword%5D=%20A%25_%5C%C3%84%20'), allowed),
    { page: 1, per_page: 20, 'filters[keyword]': 'A%_\\Ä' });
  assert.throws(() => queryInput(request('/api/members?page=1&per_page=20&filters[keyword][]=x'), allowed),
    { errors: { 'filters.keyword': ['欄位型態不正確'] } });
  assert.throws(() => queryInput(request('/api/members?page=1&per_page=20&filters[keyword]=x&filters[keyword]=y'), allowed),
    { errors: { 'filters.keyword': ['此參數不可重複'] } });
  assert.throws(() => bodyInput(request('/', '{"status":"admin"}'), ['status']),
    { errors: { status: ['欄位格式或範圍不正確'] } });
  const fields = ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'];
  assert.deepEqual(bodyInput(request('/', '{"enabled":false,"window_minutes":1,"failure_threshold":100,"lock_seconds":86400}'), fields),
    { enabled: false, window_minutes: 1, failure_threshold: 100, lock_seconds: 86400 });
  for (const raw of ['1.0', '1e0', '"1"', 'true']) {
    assert.throws(() => bodyInput(request('/', `{"enabled":true,"window_minutes":${raw},"failure_threshold":5,"lock_seconds":300}`), fields),
      { errors: { window_minutes: ['欄位型態不正確'] } });
  }
  assert.throws(() => bodyInput(request('/', '{"enabled":"false","window_minutes":0,"failure_threshold":101,"lock_seconds":86401}'), fields),
    { errors: { enabled: ['欄位型態不正確'], window_minutes: ['欄位格式或範圍不正確'], failure_threshold: ['欄位格式或範圍不正確'], lock_seconds: ['欄位格式或範圍不正確'] } });
});
