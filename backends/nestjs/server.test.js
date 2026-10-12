'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { randomBytes, createHmac } = require('node:crypto');
const { createApp } = require('./server');
const { hashPassword, verifyPassword, issueToken, verifyToken } = require('./auth');

// fakeDb 只接受本測試真正會觸發的查詢，意外 SQL 必須使案例失敗。
function fakeDb() {
  return {
    // query 僅回空列表的必要結果，使額外資料存取立即暴露。
    async query(sql) {
      if (sql.startsWith('SELECT count(*)::bigint')) return { rows: [{ total: '0' }] };
      if (sql.startsWith('SELECT 1 FROM')) return { rows: [] };
      throw Error(`unexpected query: ${sql}`);
    },
  };
}

// request 對真實 Nest HTTP socket 傳原始本文，避免測試客戶端改寫待驗證位元組。
async function request(base, method, path, body, headers = {}) {
  const response = await fetch(base + path, { method, headers, ...(body === undefined ? {} : { body }) });
  const raw = await response.text();
  return { status: response.status, body: raw ? JSON.parse(raw) : null, headers: response.headers };
}

// expectError 核對共同 envelope 與逐字欄位文案，不接受 Nest 原生錯誤格式。
function expectError(result, status, code, errors) {
  assert.equal(result.status, status);
  assert.equal(result.body.success, false);
  assert.equal(result.body.error_code, code);
  assert.match(result.body.log_id, /^[0-9a-f-]{36}$/);
  if (errors) assert.deepEqual(result.body.errors, errors);
}

// 框架路由須保留公開可讀、JWT 優先、Query/JSON 驗證與共同錯誤結果。
test('Nest controller routes preserve shared validation and envelopes', async () => {
  const app = await createApp(fakeDb(), randomBytes(32));
  try {
    await app.listen(0, '127.0.0.1');
    const base = `http://127.0.0.1:${app.getHttpServer().address().port}`;
    const page = await request(base, 'GET', '/api/articles?page=1&per_page=20');
    assert.equal(page.status, 200);
    assert.deepEqual(page.body.data, { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } });
    expectError(await request(base, 'GET', '/api/articles?page=bad&page=1&per_page=20'), 422, 'E0001', { page: ['此參數不可重複'] });
    expectError(await request(base, 'GET', '/api/articles?page=1&per_page=20', undefined, { Authorization: 'Bearer bad' }), 401, 'E0002');
    expectError(await request(base, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'), 401, 'E0002');
    expectError(await request(base, 'POST', '/api/auth/login', '{', { 'Content-Type': 'application/json' }), 400, 'E0005');
    expectError(await request(base, 'POST', '/api/auth/login'), 422, 'E0001', { _body: ['Request Body 必須為 JSON object'] });
    expectError(await request(base, 'POST', '/api/auth/login', '{}', { 'Content-Type': 'application/json' }), 422, 'E0001', { email: ['此欄位為必填'], password: ['此欄位為必填'] });
    expectError(await request(base, 'POST', '/api/auth/login', '[]', { 'Content-Type': 'application/json' }), 422, 'E0001', { _body: ['Request Body 必須為 JSON object'] });
    expectError(await request(base, 'GET', '/api/me'), 401, 'E0002');
    expectError(await request(base, 'GET', '/api/no-such-route'), 404, 'E0004');
    expectError(await request(base, 'PUT', '/api/auth/login'), 405, 'E0005');
    const preflight = await fetch(base + '/api/auth/login', { method: 'OPTIONS', headers: { Origin: 'http://localhost:8080' } });
    assert.equal(preflight.status, 204);
    assert.equal(preflight.headers.get('access-control-allow-origin'), 'http://localhost:8080');
  } finally { await app.close(); }
});

// 跨後端可讀的 Argon2id 格式及 Nest 專屬 JWT claims 不應受框架路由影響。
test('Argon2id and Nest JWT reject incompatible values', async () => {
  const hash = await hashPassword('Ab12');
  assert.match(hash, /^\$argon2id\$v=19\$m=19456,t=2,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}$/);
  assert.equal(await verifyPassword('Ab12', hash), true);
  assert.equal(await verifyPassword('wrong', hash), false);
  await assert.rejects(verifyPassword('Ab12', hash.replace('m=19456', 'm=1')), { code: 'E0104' });
  const key = randomBytes(32);
  const now = Math.floor(Date.now() / 1000);
  const token = issueToken(key, 7, randomBytes(16).toString('base64url'), now);
  assert.equal(verifyToken(key, token).sub, '7');
  assert.throws(() => verifyToken(randomBytes(32), token), { code: 'E0002' });
  // 重新簽章驗證後端識別，即使共享金鑰也不可冒用他後端 claims。
  const [head, body] = token.split('.');
  const claims = { ...JSON.parse(Buffer.from(body, 'base64url')), iss: 'ai-workflow-fullstack:laravel' };
  const signed = `${head}.${Buffer.from(JSON.stringify(claims)).toString('base64url')}`;
  const forged = `${signed}.${createHmac('sha256', key).update(signed).digest('base64url')}`;
  assert.throws(() => verifyToken(key, forged), { code: 'E0002' });
});
