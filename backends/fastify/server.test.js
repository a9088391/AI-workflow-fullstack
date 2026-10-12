'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { randomBytes, createHmac } = require('node:crypto');
const { createApp } = require('./server');
const { hashPassword, verifyPassword, issueToken, verifyToken } = require('./auth');

// fakeDb 僅回應路由測試真正用到的唯讀查詢，意外 SQL 必須使案例失敗。
function fakeDb() {
  // 意外 SQL 一律失敗，避免空 fixture 假裝覆蓋登入或內容查詢。
  return { async query(sql) {
    if (sql.startsWith('SELECT count(*)::bigint')) return { rows: [{ total: '0' }] };
    if (sql.startsWith('SELECT 1 FROM')) return { rows: [] };
    throw Error(`unexpected query: ${sql}`);
  } };
}

// request 用 Fastify inject 保留原始 Body 與 Query，驗證框架錯誤映射。
async function request(app, method, url, payload, headers = {}) {
  const response = await app.inject({ method, url, payload, headers });
  return { status: response.statusCode, body: response.json(), headers: response.headers };
}

// expectError 核對共同 envelope 及逐字文案，不接受 Fastify 預設錯誤格式。
function expectError(result, status, code, errors) {
  assert.equal(result.status, status);
  assert.equal(result.body.success, false);
  assert.equal(result.body.error_code, code);
  assert.match(result.body.log_id, /^[0-9a-f-]{36}$/);
  if (errors) assert.deepEqual(result.body.errors, errors);
}

// 原始 JSON、GET Body、Query 與管理門禁須按共同順序回應。
test('Fastify routes preserve validation and safe envelopes', async () => {
  const app = createApp(fakeDb(), randomBytes(32));
  try {
    const page = await request(app, 'GET', '/api/articles?page=1&per_page=20');
    assert.equal(page.status, 200);
    assert.deepEqual(page.body.data, { list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } });
    expectError(await request(app, 'GET', '/api/articles?page=bad&page=1&per_page=20'), 422, 'E0001', { page: ['此參數不可重複'] });
    expectError(await request(app, 'GET', '/api/articles?page=bad&per_page=20', '{}', { 'content-type': 'application/json' }), 422, 'E0001', { _body: ['此 API 不接受 Request Body'] });
    expectError(await request(app, 'GET', '/api/articles?page=1&per_page=20', undefined, { authorization: 'Bearer bad' }), 401, 'E0002');
    expectError(await request(app, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'), 401, 'E0002');
    expectError(await request(app, 'POST', '/api/auth/login', '{', { 'content-type': 'application/json' }), 400, 'E0005');
    expectError(await request(app, 'POST', '/api/auth/login', '{}', { 'content-type': 'application/json', 'content-encoding': 'not-supported' }), 415, 'E0005');
    expectError(await request(app, 'POST', '/api/auth/login'), 422, 'E0001', { _body: ['Request Body 必須為 JSON object'] });
    expectError(await request(app, 'POST', '/api/auth/login', '{}', { 'content-type': 'application/json' }), 422, 'E0001', { email: ['此欄位為必填'], password: ['此欄位為必填'] });
    expectError(await request(app, 'POST', '/api/auth/login', '[]', { 'content-type': 'application/json' }), 422, 'E0001', { _body: ['Request Body 必須為 JSON object'] });
    expectError(await request(app, 'POST', '/api/auth/login', '{"email":"a@example.test","password":"abc"}', { 'content-type': 'text/plain' }), 422, 'E0001', { _body: ['Request Body 必須為 JSON object'] });
    expectError(await request(app, 'GET', '/api/me'), 401, 'E0002');
    expectError(await request(app, 'GET', '/api/no-such-route'), 404, 'E0004');
    expectError(await request(app, 'GET', '/api/articles/' + '9'.repeat(101)), 422, 'E0001', { article_id: ['欄位格式或範圍不正確'] });
    expectError(await request(app, 'GET', '/api/articles/%ZZ'), 400, 'E0005');
    expectError(await request(app, 'PUT', '/api/auth/login'), 405, 'E0005');
    const preflight = await app.inject({ method: 'OPTIONS', url: '/api/auth/login', headers: { origin: 'http://localhost:8080' } });
    assert.equal(preflight.statusCode, 204);
    assert.equal(preflight.headers['access-control-allow-origin'], 'http://localhost:8080');
  } finally { await app.close(); }
});

// Argon2id 與 JWT 的固定格式必須獨立於 HTTP 框架，且不能接受他後端 claims。
test('Argon2id and Fastify JWT reject incompatible values', async () => {
  const hash = await hashPassword('Ab12');
  assert.match(hash, /^\$argon2id\$v=19\$m=19456,t=2,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}$/);
  assert.equal(await verifyPassword('Ab12', hash), true);
  assert.equal(await verifyPassword('wrong', hash), false);
  await assert.rejects(verifyPassword('Ab12', hash.replace('m=19456', 'm=1')), { code: 'E0104' });
  const key = randomBytes(32);
  const now = Math.floor(Date.now() / 1000);
  const token = issueToken(key, 7, randomBytes(16).toString('base64url'), now);
  assert.equal(verifyToken(key, token).sub, '7');
  // 獨立金鑰不能驗本後端簽發的 token。
  assert.throws(() => verifyToken(randomBytes(32), token), { code: 'E0002' });
  // 重新簽章確保錯後端 claims 的拒絕來自內容，而不只是簽章錯誤。
  const [head, body] = token.split('.');
  const claims = { ...JSON.parse(Buffer.from(body, 'base64url')), iss: 'ai-workflow-fullstack:express' };
  const changed = Buffer.from(JSON.stringify(claims)).toString('base64url');
  const unsigned = `${head}.${changed}`;
  const forged = `${unsigned}.${createHmac('sha256', key).update(unsigned).digest('base64url')}`;
  // 即使重新簽章，其他後端的 issuer 仍不得被接受。
  assert.throws(() => verifyToken(key, forged), { code: 'E0002' });
});
