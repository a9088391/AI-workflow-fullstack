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
  // map callback 建立三個角色共用結構，只讓 ID 2 擔任管理員以測恢復控制權。
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

  // token 為每個角色提供有效的 NestJS session，不經測試外部帳號或金鑰。
  function token(id) {
    const jti = randomBytes(16).toString('base64url');
    const issued = Math.floor(Date.now() / 1000);
    sessions.set(jti, { account_id: id, backend_id: 'nestjs', issued_at: new Date(issued * 1000),
      expires_at: new Date((issued + 28800) * 1000), revoked_at: null });
    return issueToken(key, id, jti, issued);
  }

  // query 依 SQL 意圖返回目前 fixture；UPDATE 真正改狀態才能核對版本與來源不變。
  async function query(sql, args = []) {
    calls.push([sql, args]);
    if (sql === 'BEGIN' || sql === 'COMMIT' || sql === 'ROLLBACK') return { rows: [] };
    // filter callback 移除不存在的帳號，讓交易身分驗證收到空 rows。
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts')) return { rows: [accounts.get(Number(args[0]))].filter(Boolean) };
    // filter callback 同樣讓已失效或不存在的 session 呈現空查詢結果。
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
      // filter callback 只套用本次 SET 欄位，讓測試核對來源欄位未被改寫。
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
    // inserted getter 僅暴露最近新增的觀測值，不讓測試改動內部參照。
    get inserted() { return inserted; },
    // released getter 回報連線歸還次數，供每條交易出口檢查。
    get released() { return released; },
  };
}

// send 使用真實 Nest HTTP socket 並保留原始 JSON，測框架路由與驗證順序。
async function send(app, method, path, body, token) {
  const base = `http://127.0.0.1:${app.getHttpServer().address().port}`;
  const response = await fetch(base + path, { method,
    headers: { ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      ...(token ? { authorization: `Bearer ${token}` } : {}) },
    ...(body === undefined ? {} : { body: typeof body === 'string' ? body : JSON.stringify(body) }) });
  return { status: response.status, data: await response.json() };
}

// error 核對契約狀態、固定文案與欄位，不只看框架 HTTP 狀態。
function error(result, status, code, fields) {
  assert.equal(result.status, status);
  assert.equal(result.data.error_code, code);
  assert.equal(result.data.success, false);
  assert.equal(Object.hasOwn(result.data, 'data'), false);
  assert.match(result.data.log_id, /^[0-9a-f-]{36}$/);
  assert.equal(result.data.message, {
    E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入',
    E0003: '沒有權限使用此功能', E0004: '找不到指定資料或 API',
    E0005: '系統暫時無法處理，請稍後再試',
    E0102: '此帳號已停用，請聯絡系統管理員',
    E0201: '內容已更新，請確認最新內容後再送出', E9999: '系統暫時無法處理，請稍後再試',
  }[code]);
  if (fields) assert.deepEqual(result.data.errors, fields);
  else assert.equal(Object.hasOwn(result.data, 'errors'), false);
}

// 文章案例涵蓋建立、本人列表、同值、過期版本、下架與管理員接管。
test('author article writes enforce validation, versions and restore ownership', async () => {
  const f = fixture();
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  const owner = f.token(1), admin = f.token(2), other = f.token(3);
  try {
    error(await send(app, 'POST', '/api/articles', {}), 401, 'E0002');
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: 'x' }, 'bad'), 401, 'E0002');
    error(await send(app, 'POST', '/api/articles?extra=1', { title: 'x', body: 'x' }, owner), 422, 'E0001', { extra: ['不支援此欄位'] });
    error(await send(app, 'POST', '/api/articles?extra=1&extra=2', { title: 'x', body: 'x' }, owner), 422, 'E0001', { extra: ['此參數不可重複'] });
    error(await send(app, 'POST', '/api/articles', null, owner), 422, 'E0001', { _body: ['Request Body 必須為 JSON object'] });
    error(await send(app, 'POST', '/api/articles', { title: '\u0000', body: 'x' }, owner), 422, 'E0001', { title: ['欄位格式或範圍不正確'] });
    error(await send(app, 'POST', '/api/articles', { title: '　', body: 'x' }, owner), 422, 'E0001', { title: ['欄位格式或範圍不正確'] });
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: '\u0000' }, owner), 422, 'E0001', { body: ['欄位格式或範圍不正確'] });
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: 'x', author_id: 3 }, owner), 422, 'E0001', { author_id: ['不支援此欄位'] });
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: 'x', created_backend: 'evil' }, owner), 422, 'E0001', { created_backend: ['不支援此欄位'] });
    assert.equal((await send(app, 'POST', '/api/articles', { title: '新標題', body: '換\r\n行' }, owner)).status, 201);
    assert.match(f.inserted.sql, /'nestjs'/);
    assert.deepEqual(f.inserted.args.slice(0, 3), [1, '新標題', '換\r\n行']);
    const mine = await send(app, 'GET', '/api/me/articles?page=1&per_page=20', undefined, owner);
    assert.equal(mine.status, 200);
    assert.equal(mine.data.data.list[0].created_backend, 'laravel');
    assert.equal(mine.data.data.list[0].author.id, 1);
    // some callback 確認「我的文章」在 SQL 而非回傳後才限制作者與排序。
    assert.equal(f.calls.some(([sql, args]) => sql.includes('FROM articles JOIN accounts') &&
      sql.includes('WHERE articles.author_id=$1') && sql.includes('ORDER BY articles.created_at DESC,articles.id DESC') &&
      args[0] === 1 && args[1] === 20), true);
    error(await send(app, 'GET', '/api/me/articles?page=1&page=2&per_page=20', undefined, owner), 422, 'E0001', { page: ['此參數不可重複'] });
    error(await send(app, 'PUT', '/api/articles/no', { title: '改', body: '文', version: 1 }, owner), 422, 'E0001', { article_id: ['欄位格式或範圍不正確'] });
    error(await send(app, 'PUT', '/api/articles/999', { title: '改', body: '文', version: 1 }, owner), 404, 'E0004');
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: 1 }, other), 403, 'E0003');
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: 1 }, admin), 403, 'E0003');
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: '1' }, owner), 422, 'E0001', { version: ['欄位型態不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', '{"title":"改","body":"文","version":1e0}', owner), 422, 'E0001', { version: ['欄位型態不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', '{"title":"改","body":"文","version":1.0}', owner), 422, 'E0001', { version: ['欄位型態不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: -1 }, owner), 422, 'E0001', { version: ['欄位格式或範圍不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', '{"title":"改","body":"文","version":9007199254740992}', owner), 422, 'E0001', { version: ['欄位格式或範圍不正確'] });
    error(await send(app, 'PUT', '/api/articles/10', { title: '原標題', body: '原內文', version: 2 }, owner), 409, 'E0201');
    // filter callback 計數實際 UPDATE，辨認同值請求有沒有改動持久資料。
    const before = f.calls.filter(([sql]) => sql.startsWith('UPDATE articles')).length;
    const beforeTime = f.article.updated_at;
    assert.equal((await send(app, 'PUT', '/api/articles/10', { title: '原標題', body: '原內文', version: 1 }, owner)).status, 200);
    // filter callback 重算寫入次數，確保同值沒有新增 UPDATE。
    assert.equal(f.calls.filter(([sql]) => sql.startsWith('UPDATE articles')).length, before);
    assert.equal(f.article.updated_at, beforeTime);
    assert.equal((await send(app, 'PUT', '/api/articles/10', { title: '改', body: '文', version: 1 }, owner)).status, 200);
    assert.equal(f.article.version, 2);
    assert.equal(f.article.created_backend, 'laravel');
    assert.equal((await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 2 }, owner)).status, 200);
    error(await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 3 }, other), 404, 'E0004');
    assert.equal((await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 3 }, admin)).status, 200);
    assert.equal(f.article.restore_control, 'admin');
    assert.equal(f.article.version, 4);
    error(await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'visible', version: 4 }, owner), 403, 'E0003');
    error(await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'visible', version: 3 }, admin), 409, 'E0201');
    assert.equal((await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'visible', version: 4 }, admin)).status, 200);
    assert.equal(f.article.visibility, 'visible');
    assert.equal(f.article.restore_control, null);
    error(await send(app, 'DELETE', '/api/articles/10'), 405, 'E0005');
  } finally { await app.close(); }
});

// 管理員操作自己的內容仍保留作者恢復權，同值下架不應新增版本。
test('admin author keeps author restore control on same-value visibility', async () => {
  const f = fixture();
  f.article.author_id = 2;
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  try {
    const admin = f.token(2);
    assert.equal((await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 1 }, admin)).status, 200);
    assert.equal(f.article.restore_control, 'author');
    assert.equal(f.article.version, 2);
    const time = f.article.updated_at;
    assert.equal((await send(app, 'PUT', '/api/articles/10/visibility', { visibility: 'hidden', version: 2 }, admin)).status, 200);
    assert.equal(f.article.version, 2);
    assert.equal(f.article.updated_at, time);
  } finally { await app.close(); }
});

// 下架父文章時，他人留言的獨立詳情與寫入均隱藏存在；留言本人仍可讀自己的詳情。
test('hidden parent hides another author comment detail and write target', async () => {
  const f = fixture();
  f.comment.author_id = 3;
  f.article.visibility = 'hidden';
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  try {
    const articleAuthor = f.token(1);
    const denied = await send(app, 'GET', '/api/comments/20', undefined, articleAuthor);
    error(denied, 404, 'E0004');
    assert.equal(JSON.stringify(denied.data).includes(f.article.title), false);
    assert.equal(JSON.stringify(denied.data).includes(f.comment.body), false);
    error(await send(app, 'PUT', '/api/comments/20', { body: '修改', version: 1 }, articleAuthor), 404, 'E0004');
    error(await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'hidden', version: 1 }, articleAuthor), 404, 'E0004');
    const commentAuthor = f.token(3);
    assert.equal((await send(app, 'GET', '/api/comments/20', undefined, commentAuthor)).status, 200);
    error(await send(app, 'PUT', '/api/comments/20', { body: '修改', version: 1 }, commentAuthor), 403, 'E0003');
  } finally { await app.close(); }
});

// 留言案例涵蓋父文章先後、作者獨立詳情、編修限制與下架期間恢復。
test('author comment writes respect parent visibility and independent detail', async () => {
  const f = fixture();
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  const owner = f.token(1), admin = f.token(2), other = f.token(3);
  try {
    assert.equal((await send(app, 'POST', '/api/articles/10/comments', { body: '新留言' }, owner)).status, 201);
    assert.match(f.inserted.sql, /'nestjs'/);
    assert.deepEqual(f.inserted.args.slice(0, 3), [10, 1, '新留言']);
    // some callback 確認新增留言有鎖父文章，與下架作業序列化。
    assert.equal(f.calls.some(([sql]) => sql.includes('FROM articles WHERE id=$1 FOR UPDATE')), true);
    assert.equal((await send(app, 'GET', '/api/comments/20', undefined, owner)).data.data.body, '原留言');
    assert.equal((await send(app, 'GET', '/api/comments/20', undefined, admin)).status, 200);
    error(await send(app, 'GET', '/api/comments/20', undefined, other), 403, 'E0003');
    error(await send(app, 'PUT', '/api/comments/20', { body: '改', version: 1 }, admin), 403, 'E0003');
    const before = f.comment.updated_at;
    assert.equal((await send(app, 'PUT', '/api/comments/20', { body: '原留言', version: 1 }, owner)).status, 200);
    assert.equal(f.comment.updated_at, before);
    assert.equal(f.comment.version, 1);
    assert.equal((await send(app, 'PUT', '/api/comments/20', { body: '編修', version: 1 }, owner)).status, 200);
    error(await send(app, 'PUT', '/api/comments/20', { body: '編修', version: 1 }, owner), 409, 'E0201');
    f.article.visibility = 'hidden';
    error(await send(app, 'POST', '/api/articles/10/comments?extra=1', { body: '新增' }, other), 422, 'E0001', { extra: ['不支援此欄位'] });
    error(await send(app, 'POST', '/api/articles/10/comments', { body: '新增' }, owner), 403, 'E0003');
    error(await send(app, 'POST', '/api/articles/10/comments', { body: '新增' }, other), 404, 'E0004');
    error(await send(app, 'PUT', '/api/comments/20', { body: '再編修', version: 2 }, owner), 403, 'E0003');
    assert.equal((await send(app, 'GET', '/api/comments/20', undefined, owner)).data.data.article_id, 10);
    error(await send(app, 'GET', '/api/comments/20', undefined, other), 404, 'E0004');
    const mine = await send(app, 'GET', '/api/me/comments?page=1&per_page=20', undefined, owner);
    assert.equal(mine.data.data.list[0].article_id, 10);
    assert.equal(Object.hasOwn(mine.data.data.list[0], 'title'), false);
    // some callback 確認「我的留言」於 SQL 篩選本人並以時間與 ID 排序。
    assert.equal(f.calls.some(([sql, args]) => sql.includes('FROM comments JOIN accounts') &&
      sql.includes('WHERE comments.author_id=$1') && sql.includes('ORDER BY comments.created_at DESC,comments.id DESC') &&
      args[0] === 1 && args[1] === 20), true);
    error(await send(app, 'GET', '/api/me/comments?page=1&per_page=20&unknown=1', undefined, owner), 422, 'E0001', { unknown: ['不支援此欄位'] });
    assert.equal((await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'hidden', version: 2 }, owner)).status, 200);
    assert.equal((await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'hidden', version: 3 }, admin)).status, 200);
    assert.equal(f.comment.restore_control, 'admin');
    error(await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'visible', version: 4 }, owner), 403, 'E0003');
    assert.equal((await send(app, 'PUT', '/api/comments/20/visibility', { visibility: 'visible', version: 4 }, admin)).status, 200);
    assert.equal(f.comment.visibility, 'visible');
    assert.equal(f.article.visibility, 'hidden');
    assert.equal(f.comment.created_backend, 'laravel');
  } finally { await app.close(); }
});

// 交易重驗案例使外層無法沿用撤銷前的身分，且失敗不得提交內容寫入。
test('author write rejects revoked session within transaction', async () => {
  const f = fixture();
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  const token = f.token(1);
  const jti = [...f.sessions.keys()][0];
  f.sessions.get(jti).revoked_at = now;
  try {
    error(await send(app, 'POST', '/api/articles', { title: 'x', body: 'x' }, token), 401, 'E0002');
    assert.equal(f.inserted, null);
    // 第一個 some callback 防止撤銷 session 後提交；第二個確認有回滾。
    assert.equal(f.calls.some(([sql]) => sql === 'COMMIT'), false);
    assert.equal(f.calls.some(([sql]) => sql === 'ROLLBACK'), true);
    assert.equal(f.released, 1);
  } finally { await app.close(); }
});

// 停用帳號在交易鎖後仍須失去寫入權，不能讓舊 JWT 建立內容。
test('author write rejects disabled account before content locks', async () => {
  const f = fixture();
  const token = f.token(1);
  f.accounts.get(1).status = 'disabled';
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  try {
    error(await send(app, 'POST', '/api/articles', { title: '新文', body: '內容' }, token), 403, 'E0102');
    assert.equal(f.inserted, null);
    // some callback 確認停用時尚未讀寫文章列，只鎖帳號與 session。
    assert.equal(f.calls.some(([sql]) => sql.includes('FROM articles WHERE id=$1 FOR UPDATE') || sql.startsWith('INSERT INTO articles')), false);
    assert.equal(f.released, 1);
  } finally { await app.close(); }
});

// 四個版本寫入都必須拒絕可偽造的 JSON object，且不能進入 UPDATE。
test('versioned article and comment writes reject forged parser objects', async () => {
  const f = fixture();
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
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
    // some callback 確認偽造 version 沒有進入任何持久更新。
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
    // 查詢失敗交給 Nest 共同錯誤處理器，不吞掉 rejected Promise。
    client.query = async function failingQuery(sql, args) {
      if (sql.startsWith('INSERT INTO articles')) throw Error('isolated database failure');
      return originalQuery(sql, args);
    };
    return client;
  };
  const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  try {
    error(await send(app, 'POST', '/api/articles', { title: '測試', body: '內文' }, f.token(1)), 500, 'E9999');
    // 第一個 some callback 防止 SQL 失敗後提交；第二個確認有回滾。
    assert.equal(f.calls.some(([sql]) => sql === 'COMMIT'), false);
    assert.equal(f.calls.some(([sql]) => sql === 'ROLLBACK'), true);
    assert.equal(f.released, 1);
  } finally { await app.close(); }
});
