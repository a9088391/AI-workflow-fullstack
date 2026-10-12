'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { createApp } = require('./server');
const { issueToken } = require('./auth');

const now = new Date('2026-10-12T04:00:00Z');

// fixture 只模擬本票觸及的 SQL 與列狀態，未預期查詢必須使測試失敗。
function fixture() {
  const key = randomBytes(32);
  // 三個角色共用同一結構，只讓 ID 2 擔任管理員以測恢復控制權。
  const accounts = new Map([1, 2, 3].map(id => [id, {
    id, email: `person${id}@example.test`, nickname: `作者${id}`, role: id === 2 ? 'admin' : 'member',
    status: 'active', created_at: now,
  }]));
  const sessions = new Map();
  const article = { id: 10, author_id: 1, title: '原標題', body: '原內文', visibility: 'visible',
    restore_control: null, version: 1, created_backend: 'laravel', created_at: now, updated_at: now };
  const comment = { id: 20, author_id: 1, article_id: 10, body: '原留言', visibility: 'visible',
    restore_control: null, version: 1, created_backend: 'laravel', created_at: now, updated_at: now };
  const calls = [];
  let inserted = null;
  let released = 0;

  // token 為每個角色提供有效的 Fastify session，不經測試外部帳號或金鑰。
  function token(id) {
    const jti = randomBytes(16).toString('base64url');
    const issued = Math.floor(Date.now() / 1000);
    sessions.set(jti, { account_id: id, backend_id: 'fastify', issued_at: new Date(issued * 1000),
      expires_at: new Date((issued + 28800) * 1000), revoked_at: null });
    return issueToken(key, id, jti, issued);
  }

  // query 依 SQL 意圖返回目前 fixture；UPDATE 真正改狀態才能核對版本與來源不變。
  async function query(sql, args = []) {
    calls.push([sql, args]);
    if (sql === 'BEGIN' || sql === 'COMMIT' || sql === 'ROLLBACK') return { rows: [] };
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts')) return { rows: [accounts.get(Number(args[0]))].filter(Boolean) };
    if (sql.startsWith('SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions')) return { rows: [sessions.get(args[0])].filter(Boolean) };
    if (sql.startsWith('SELECT floor(extract(epoch FROM clock_timestamp()))')) return { rows: [{ now: String(Math.floor(Date.now() / 1000)) }] };
    if (sql.startsWith('SELECT clock_timestamp() AS now')) return { rows: [{ now }] };
    if (sql.startsWith('SELECT a.id,a.email,a.nickname')) {
      const session = sessions.get(args[0]);
      const account = session && accounts.get(session.account_id);
      return { rows: account ? [{ ...account, ...session }] : [] };
    }
    if (sql.startsWith('SELECT id,author_id,visibility FROM articles') || sql.startsWith('SELECT id,visibility FROM articles')) return { rows: Number(args[0]) === article.id ? [{ ...article }] : [] };
    if (sql.startsWith('SELECT article_id FROM comments')) return { rows: Number(args[0]) === comment.id ? [{ article_id: comment.article_id }] : [] };
    if (sql.startsWith('SELECT * FROM articles')) return { rows: Number(args[0]) === article.id ? [{ ...article }] : [] };
    if (sql.startsWith('SELECT * FROM comments')) return { rows: Number(args[0]) === comment.id ? [{ ...comment }] : [] };
    if (sql.startsWith('SELECT comments.id,') && sql.includes('AS article_visibility')) return { rows: Number(args[0]) === comment.id ? [{ ...comment, nickname: accounts.get(comment.author_id).nickname, article_visibility: article.visibility }] : [] };
    if (sql.startsWith('SELECT count(*)::bigint')) return { rows: [{ total: '1' }] };
    if (sql.startsWith('SELECT articles.id,') || sql.startsWith('SELECT comments.id,')) {
      const row = sql.startsWith('SELECT articles.id,') ? article : comment;
      return { rows: [{ ...row, nickname: accounts.get(row.author_id).nickname }] };
    }
    if (sql.startsWith('INSERT INTO articles') || sql.startsWith('INSERT INTO comments')) {
      inserted = { sql, args };
      return { rows: [], rowCount: 1 };
    }
    if (sql.startsWith('UPDATE articles') || sql.startsWith('UPDATE comments')) {
      const row = sql.startsWith('UPDATE articles') ? article : comment;
      // 只套用本次 SET 欄位，讓測試可核對版本與建立來源未被改寫。
      const columns = [...sql.matchAll(/([a-z_]+)=\$(\d+)/g)].filter(match => match[1] !== 'id' && match[1] !== 'version');
      for (const [, column, index] of columns) row[column] = args[Number(index) - 1];
      row.version++;
      row.updated_at = new Date(now.getTime() + 1000);
      return { rows: [], rowCount: 1 };
    }
    throw Error(`unexpected query: ${sql}`);
  }

  const db = { query,
    // connect 提供與 pg Pool 相同的交易接縫，以驗證每條出口都釋放連線。
    async connect() {
      return { query,
        // release 記錄資源釋放次數，避免失敗分支漏還連線。
        release() { released++; },
      };
    },
  };
  return { key, db, token, accounts, sessions, article, comment, calls,
    // getter 僅暴露觀測值，不讓測試改動 fixture 內部參照。
    get inserted() { return inserted; },
    get released() { return released; },
  };
}

// send 保留 JSON 原始本文，使錯誤案例能測真正的 HTTP 驗證順序。
async function send(app, method, path, body, token) {
  const response = await app.inject({ method, url: path, payload: body === undefined ? undefined :
    typeof body === 'string' ? body : JSON.stringify(body),
    headers: { ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      ...(token ? { authorization: `Bearer ${token}` } : {}) } });
  return { status: response.statusCode, data: response.json() };
}

// error 核對契約狀態、固定文案與欄位，不只看框架 HTTP 狀態。
function error(result, status, code, fields) {
  assert.equal(result.status, status);
  assert.equal(result.data.error_code, code);
  assert.equal(result.data.success, false);
  if (fields) assert.deepEqual(result.data.errors, fields);
}

// 文章案例涵蓋建立、本人列表、同值、過期版本、下架與管理員接管。
test('author article writes enforce validation, versions and restore ownership', async () => {
  const f = fixture();
  const app = createApp(f.db, f.key);
  const owner = f.token(1), admin = f.token(2), other = f.token(3);
  try {
    error(await send(app, 'POST', '/api/articles', {}), 401, 'E0002');
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: 'x' }, 'bad'), 401, 'E0002');
    error(await send(app, 'POST', '/api/articles', { title: '\u0000', body: 'x' }, owner), 422, 'E0001', { title: ['欄位格式或範圍不正確'] });
    error(await send(app, 'POST', '/api/articles', { title: '　', body: 'x' }, owner), 422, 'E0001', { title: ['欄位格式或範圍不正確'] });
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: 'x', created_backend: 'evil' }, owner), 422, 'E0001', { created_backend: ['不支援此欄位'] });
    assert.equal((await send(app, 'POST', '/api/articles', { title: '新標題', body: '換\r\n行' }, owner)).status, 201);
    assert.match(f.inserted.sql, /'fastify'/);
    assert.deepEqual(f.inserted.args.slice(0, 3), [1, '新標題', '換\r\n行']);
    const mine = await send(app, 'GET', '/api/me/articles?page=1&per_page=20', undefined, owner);
    assert.equal(mine.status, 200);
    assert.equal(mine.data.data.list[0].created_backend, 'laravel');
    assert.equal(mine.data.data.list[0].author.id, 1);
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: 1 }, other), 403, 'E0003');
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: 1 }, admin), 403, 'E0003');
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: '1' }, owner), 422, 'E0001', { version: ['欄位型態不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', '{"title":"改","body":"文","version":1e0}', owner), 422, 'E0001', { version: ['欄位型態不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: -1 }, owner), 422, 'E0001', { version: ['欄位格式或範圍不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', '{"title":"改","body":"文","version":9007199254740992}', owner), 422, 'E0001', { version: ['欄位格式或範圍不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', { title: '原標題', body: '原內文', version: 2 }, owner), 409, 'E0201');
    const before = f.calls.filter(([sql]) => sql.startsWith('UPDATE articles')).length;
    assert.equal((await send(app, 'PUT', '/api/articles/10', { title: '原標題', body: '原內文', version: 1 }, owner)).status, 200);
    assert.equal(f.calls.filter(([sql]) => sql.startsWith('UPDATE articles')).length, before);
    assert.equal((await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: 1 }, owner)).status, 200);
    assert.equal(f.article.version, 2);
    assert.equal(f.article.created_backend, 'laravel');
    assert.equal((await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 2 }, owner)).status, 200);
    error(await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 3 }, other), 404, 'E0004');
    assert.equal((await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 3 }, admin)).status, 200);
    assert.equal(f.article.restore_control, 'admin');
    error(await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'visible', version: 4 }, owner), 403, 'E0003');
    error(await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'visible', version: 3 }, admin), 409, 'E0201');
  } finally { await app.close(); }
});

// 留言案例涵蓋父文章先後、作者獨立詳情、編修限制與下架期間恢復。
test('author comment writes respect parent visibility and independent detail', async () => {
  const f = fixture();
  const app = createApp(f.db, f.key);
  const owner = f.token(1), admin = f.token(2), other = f.token(3);
  try {
    assert.equal((await send(app, 'POST', '/api/articles/10/comments', { body: '新留言' }, owner)).status, 201);
    assert.match(f.inserted.sql, /'fastify'/);
    assert.equal((await send(app, 'GET', '/api/comments/20', undefined, owner)).data.data.body, '原留言');
    error(await send(app, 'GET', '/api/comments/20', undefined, other), 403, 'E0003');
    assert.equal((await send(app, 'PUT', '/api/comments/20', { body: '編修', version: 1 }, owner)).status, 200);
    error(await send(app, 'PUT', '/api/comments/20', { body: '編修', version: 1 }, owner), 409, 'E0201');
    f.article.visibility = 'hidden';
    error(await send(app, 'POST', '/api/articles/10/comments', { body: '新增' }, owner), 403, 'E0003');
    error(await send(app, 'POST', '/api/articles/10/comments', { body: '新增' }, other), 404, 'E0004');
    error(await send(app, 'PUT', '/api/comments/20', { body: '再編修', version: 2 }, owner), 403, 'E0003');
    assert.equal((await send(app, 'GET', '/api/comments/20', undefined, owner)).data.data.article_id, 10);
    error(await send(app, 'GET', '/api/comments/20', undefined, other), 404, 'E0004');
    const mine = await send(app, 'GET', '/api/me/comments?page=1&per_page=20', undefined, owner);
    assert.equal(mine.data.data.list[0].article_id, 10);
    error(await send(app, 'GET', '/api/me/comments?page=1&per_page=20&unknown=1', undefined, owner), 422, 'E0001', { unknown: ['不支援此欄位'] });
    assert.equal((await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'hidden', version: 2 }, owner)).status, 200);
    assert.equal((await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'hidden', version: 3 }, admin)).status, 200);
    error(await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'visible', version: 4 }, owner), 403, 'E0003');
    assert.equal((await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'visible', version: 4 }, admin)).status, 200);
  } finally { await app.close(); }
});

// 交易重驗案例使外層無法沿用撤銷前的身分，且失敗不得提交內容寫入。
test('author write rejects revoked session within transaction', async () => {
  const f = fixture();
  const app = createApp(f.db, f.key);
  const token = f.token(1);
  const jti = [...f.sessions.keys()][0];
  f.sessions.get(jti).revoked_at = now;
  try {
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: 'x' }, token), 401, 'E0002');
    assert.equal(f.inserted, null);
    assert.equal(f.calls.some(([sql]) => sql === 'COMMIT'), false);
    assert.equal(f.calls.some(([sql]) => sql === 'ROLLBACK'), true);
    assert.equal(f.released, 1);
  } finally { await app.close(); }
});

// 四個版本寫入都必須拒絕可偽造的 JSON object，且不能進入 UPDATE。
test('versioned article and comment writes reject forged parser objects', async () => {
  const f = fixture();
  const app = createApp(f.db, f.key);
  const owner = f.token(1);
  const fakeVersion = { value: 1, source: '1' };
  try {
    for (const [path, body] of [
      ['/api/articles/10', { title: '改', body: '文', version: fakeVersion }],
      ['/api/articles/10/visibility', { visibility: 'hidden', version: fakeVersion }],
      ['/api/comments/20', { body: '改', version: fakeVersion }],
      ['/api/comments/20/visibility', { visibility: 'hidden', version: fakeVersion }],
    ]) {
      error(await send(app, 'PUT', path, body, owner), 422, 'E0001', { version: ['欄位型態不正確'] });
    }
    assert.equal(f.calls.some(([sql]) => sql.startsWith('UPDATE articles') || sql.startsWith('UPDATE comments')), false);
    assert.equal(f.article.version, 1);
    assert.equal(f.comment.version, 1);
    // 合法 JSON 整數仍需走到持久更新，避免把所有版本都擋掉。
    assert.equal((await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: 1 }, owner)).status, 200);
    assert.equal(f.article.version, 2);
  } finally { await app.close(); }
});

// 資料庫寫入失敗要走 500 共同錯誤、回滾及釋放連線，不能回傳假成功。
test('author write rolls back and releases on rejected database I/O', async () => {
  const f = fixture();
  const originalConnect = f.db.connect;
  // 故障只放在 INSERT，讓認證和驗證仍走真實交易接縫。
  f.db.connect = async function failingConnect() {
    const client = await originalConnect();
    const originalQuery = client.query;
    // 查詢失敗交給 Fastify 錯誤處理器，不吞掉 rejected Promise。
    client.query = async function failingQuery(sql, args) {
      if (sql.startsWith('INSERT INTO articles')) throw Error('isolated database failure');
      return originalQuery(sql, args);
    };
    return client;
  };
  const app = createApp(f.db, f.key);
  try {
    error(await send(app, 'POST', '/api/articles', { title: '測試', body: '內文' }, f.token(1)), 500, 'E9999');
    assert.equal(f.calls.some(([sql]) => sql === 'COMMIT'), false);
    assert.equal(f.calls.some(([sql]) => sql === 'ROLLBACK'), true);
    assert.equal(f.released, 1);
  } finally { await app.close(); }
});
