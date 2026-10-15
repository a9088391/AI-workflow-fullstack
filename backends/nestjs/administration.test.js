'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { randomBytes } = require('node:crypto');
const { createApp } = require('./server');
const { issueToken } = require('./auth');

const created = new Date('2026-10-12T04:00:00Z');

// fixture 模擬受保護身分與交易寫入，未列出的 SQL 一律失敗以暴露遺漏的資料流程。
function fixture() {
  const key = randomBytes(32);
  const accounts = new Map([
    [1, { id: 1, email: 'admin@example.test', nickname: '管理員', role: 'admin', status: 'active', created_at: created }],
    [2, { id: 2, email: 'first@example.test', nickname: 'A%_\\陳', role: 'member', status: 'active', created_at: created, failure_epoch: 4, locked_until: created }],
    [3, { id: 3, email: 'second@example.test', nickname: 'É字', role: 'member', status: 'disabled', created_at: created, failure_epoch: 7, locked_until: created }],
  ]);
  const settings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  const sessions = new Map();
  const calls = [];
  let released = 0;
  let failClear = false;
  let before = null;

  // token 產生與資料庫紀錄一致的簽章，測試真正的 HTTP 身分驗證順序。
  function token(id) {
    const jti = randomBytes(16).toString('base64url');
    const issued = Math.floor(Date.now() / 1000);
    sessions.set(jti, { account_id: id, backend_id: 'nestjs', issued_at: new Date(issued * 1000),
      expires_at: new Date((issued + 28800) * 1000), revoked_at: null });
    return issueToken(key, id, jti, issued);
  }

  // query 僅模擬管理路徑查詢與交易效果；搜尋結果獨立預先定義供 SQL 斷言驗證。
  async function query(sql, args = []) {
    calls.push([sql, args]);
    if (sql === 'BEGIN') {
      before = { accounts: structuredClone([...accounts]), sessions: structuredClone([...sessions]), settings: { ...settings } };
      return { rows: [] };
    }
    if (sql === 'ROLLBACK') {
      if (before) {
        accounts.clear(); for (const [id, row] of before.accounts) accounts.set(id, row);
        sessions.clear(); for (const [id, row] of before.sessions) sessions.set(id, row);
        Object.assign(settings, before.settings);
      }
      return { rows: [] };
    }
    if (sql === 'COMMIT') { before = null; return { rows: [] }; }
    if (sql.startsWith('SELECT a.id,a.email,a.nickname')) {
      const session = sessions.get(args[0]);
      const account = session && accounts.get(session.account_id);
      return { rows: account ? [{ ...account, ...session }] : [] };
    }
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts') && sql.includes('FOR UPDATE')) {
      // sort/map callback 模擬 SQL 依 ID 鎖帳號；缺少的目標不會被虛構出來。
      if (sql.includes('id=ANY')) return { rows: args[0].map(Number).sort((a, b) => a - b).map(id => accounts.get(id)).filter(Boolean) };
      // sort callback 讓關閉設定案例觀察全部帳號的既定鎖序。
      if (sql.includes('ORDER BY id')) return { rows: [...accounts.values()].sort((a, b) => a.id - b.id) };
      return { rows: [accounts.get(Number(args[0]))].filter(Boolean) };
    }
    if (sql.startsWith('SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions')) return { rows: [sessions.get(args[0])].filter(Boolean) };
    if (sql.startsWith('SELECT floor(extract(epoch FROM clock_timestamp()))')) return { rows: [{ now: String(Math.floor(Date.now() / 1000)) }] };
    if (sql.startsWith('SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings')) return { rows: [{ ...settings }] };
    if (sql.startsWith('UPDATE login_lockout_settings')) { Object.assign(settings, { enabled: args[0], window_minutes: args[1], failure_threshold: args[2], lock_seconds: args[3] }); return { rows: [] }; }
    if (sql.startsWith('UPDATE accounts SET failure_epoch')) {
      if (failClear) throw Error('fixture failure');
      for (const row of accounts.values()) if (row.role === 'member') { row.failure_epoch++; row.locked_until = null; }
      return { rows: [] };
    }
    if (sql.startsWith('UPDATE accounts SET status=')) { accounts.get(args[1]).status = args[0]; return { rows: [] }; }
    if (sql.startsWith('UPDATE login_sessions SET revoked_at=')) {
      for (const row of sessions.values()) if (row.account_id === args[0] && !row.revoked_at) row.revoked_at = new Date();
      return { rows: [] };
    }
    // filter callback 使管理員 ID 與不存在 ID 都落到空結果。
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=')) return { rows: [accounts.get(Number(args[0]))].filter(row => row?.role === 'member') };
    if (sql.startsWith('SELECT count(*)::bigint AS total FROM accounts')) {
      const ids = args.length ? ({ 'a%_\\': [2], a: [2], '%': [2], _: [2], '\\': [2], '陳': [2], 'É': [3], 'é': [] })[args[0]] : [3, 2];
      if (!ids) throw Error(`unknown search fixture: ${args[0]}`);
      return { rows: [{ total: String(ids.length) }] };
    }
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts WHERE role=')) {
      const ids = args.length > 2 ? ({ 'a%_\\': [2], a: [2], '%': [2], _: [2], '\\': [2], '陳': [2], 'É': [3], 'é': [] })[args[0]] : [3, 2];
      if (!ids) throw Error(`unknown search fixture: ${args[0]}`);
      // map callback 以測資指定的命中 ID 輸出列，和實際 SQL 字串斷言分開。
      return { rows: ids.map(id => accounts.get(id)) };
    }
    if (sql.startsWith('SELECT count(*)::bigint AS total FROM comments')) return { rows: [{ total: '2' }] };
    if (sql.startsWith('SELECT comments.id,') && sql.includes('FROM comments JOIN accounts')) return { rows: [
      { id: 10, author_id: 2, article_id: 20, body: '下架留言', visibility: 'hidden', restore_control: 'admin', version: 2, created_backend: 'laravel', created_at: created, updated_at: created, nickname: 'A%_\\陳' },
      { id: 9, author_id: 3, article_id: 21, body: '父文章下架', visibility: 'visible', restore_control: null, version: 1, created_backend: 'nestjs', created_at: created, updated_at: created, nickname: 'É字' },
    ] };
    if (sql.startsWith('SELECT count(*)::bigint AS total FROM articles')) return { rows: [{ total: '1' }] };
    if (sql.startsWith('SELECT articles.id,') && sql.includes('FROM articles JOIN accounts')) return { rows: [
      { id: 20, author_id: 2, title: '隱藏', body: '內容', visibility: 'hidden', restore_control: 'admin', version: 2, created_backend: 'laravel', created_at: created, updated_at: created, nickname: 'A%_\\陳' },
    ] };
    throw Error(`unexpected query: ${sql}`);
  }
  const db = { query,
    // connect 提供 pg Pool 的交易接縫，量測失敗與成功都歸還連線。
    async connect() { return { query,
      // release 計數資源歸還，避免管理交易遺漏釋放。
      release() { released++; },
    }; },
  };
  return { db, key, token, accounts, sessions, settings, calls,
    // failClear 模擬清鎖失敗以驗證四欄與副作用同時 rollback。
    set failClear(value) { failClear = value; },
    // released 回報交易結束後已歸還連線數。
    get released() { return released; },
  };
}

// send 直接送 HTTP 位元組，包含 fetch 不允許的 GET Body 驗證案例。
async function send(app, method, path, body, token) {
  const payload = body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body);
  const port = app.getHttpServer().address().port;
  // Promise callback 等待完整 HTTP 結果，讓每項斷言觀察已送出的公開回應。
  return new Promise((resolve, reject) => {
    // HTTP callback 收完整回應，保留原始狀態與共同錯誤欄位供斷言。
    const req = http.request({ host: '127.0.0.1', port, method, path, headers: {
      ...(payload === undefined ? {} : { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) }),
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    } }, function response(res) {
      let raw = '';
      // data callback 收集單次小型 fixture 回應，避免把測試結果與請求混淆。
      res.on('data', function chunk(part) { raw += part; });
      // end callback 等完整 JSON 才比較公開契約。
      res.on('end', function finished() { resolve({ status: res.statusCode, body: JSON.parse(raw) }); });
    });
    req.on('error', reject);
    req.end(payload);
  });
}

// failure 逐字核對共同錯誤文案與欄位，避免只檢查 HTTP 狀態造成假陽性。
function failure(result, status, code, errors) {
  assert.equal(result.status, status);
  assert.equal(result.body.success, false);
  assert.equal(result.body.error_code, code);
  assert.equal(result.body.message, { E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入',
    E0003: '沒有權限使用此功能', E0004: '找不到指定資料或 API',
    E0102: '此帳號已停用，請聯絡系統管理員', E9999: '系統暫時無法處理，請稍後再試' }[code]);
  if (errors) assert.deepEqual(result.body.errors, errors);
  else assert.equal(Object.hasOwn(result.body, 'errors'), false);
}

// 測試 callback 區分角色、驗證順序、搜尋及隱藏內容列表的公開回應。
test('administration reads enforce role, validation and list contracts', async () => {
  const f = fixture(); const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  const admin = f.token(1), member = f.token(2);
  try {
    failure(await send(app, 'GET', '/api/members?page=x&per_page=20'), 401, 'E0002');
    failure(await send(app, 'GET', '/api/members?page=x&per_page=20', undefined, member), 403, 'E0003');
    failure(await send(app, 'GET', '/api/members/1?bad=1', undefined, member), 403, 'E0003');
    failure(await send(app, 'GET', '/api/comments?page=x&per_page=20', undefined, member), 403, 'E0003');
    failure(await send(app, 'GET', '/api/settings/login-lockout?bad=1', undefined, member), 403, 'E0003');
    failure(await send(app, 'GET', '/api/members?page=1&per_page=20', '{}', admin), 422, 'E0001', { _body: ['此 API 不接受 Request Body'] });
    failure(await send(app, 'GET', '/api/members?page=1&page=2&per_page=20', undefined, admin), 422, 'E0001', { page: ['此參數不可重複'] });
    failure(await send(app, 'GET', '/api/members?page=1&per_page=20&extra=1', undefined, admin), 422, 'E0001', { extra: ['不支援此欄位'] });
    failure(await send(app, 'GET', '/api/members?page=1&per_page=20&filters%5Bkeyword%5D=x&filters%5Bkeyword%5D=y', undefined, admin), 422, 'E0001', { 'filters.keyword': ['此參數不可重複'] });
    failure(await send(app, 'GET', '/api/members?page=1&per_page=20&filters%5Bkeyword%5D%5B%5D=x', undefined, admin), 422, 'E0001', { 'filters.keyword': ['欄位型態不正確'] });
    failure(await send(app, 'GET', `/api/members?page=1&per_page=20&filters%5Bkeyword%5D=${'a'.repeat(255)}`, undefined, admin), 422, 'E0001', { 'filters.keyword': ['欄位格式或範圍不正確'] });
    failure(await send(app, 'GET', '/api/members/+2', undefined, admin), 422, 'E0001', { member_id: ['欄位格式或範圍不正確'] });
    failure(await send(app, 'GET', '/api/members/1', undefined, admin), 404, 'E0004');
    failure(await send(app, 'GET', '/api/members/99', undefined, admin), 404, 'E0004');
    const detail = await send(app, 'GET', '/api/members/2', undefined, admin);
    assert.equal(detail.status, 200);
    assert.deepEqual(detail.body.data, { id: 2, email: 'first@example.test', nickname: 'A%_\\陳', role: 'member', status: 'active', created_at: '2026-10-12 12:00:00' });
    const page = await send(app, 'GET', '/api/members?page=1&per_page=20&filters%5Bkeyword%5D=a%25_%5C', undefined, admin);
    assert.equal(page.status, 200);
    assert.deepEqual(page.body.data.pagination, { page: 1, per_page: 20, total: 1, total_pages: 1 });
    // map callback 核對實際回應 ID 與排序，不只核對總數。
    assert.deepEqual(page.body.data.list.map(row => row.id), [2]);
    // some callback 核對篩選在 SQL 且採參數綁定，避免 fixture 自行實作成唯一依據。
    assert.equal(f.calls.some(([sql, args]) => sql.includes('strpos(translate(nickname') && sql.includes("role='member'") && args[0] === 'a%_\\'), true);
    // 短字、符號與非 ASCII 大小寫各需獨立命中／不命中，不能依 LIKE 或 Unicode fold 推測。
    for (const [keyword, ids] of [['a', [2]], ['%', [2]], ['_', [2]], ['\\', [2]], ['陳', [2]], ['É', [3]], ['é', []]]) {
      const found = await send(app, 'GET', `/api/members?page=1&per_page=20&filters%5Bkeyword%5D=${encodeURIComponent(keyword)}`, undefined, admin);
      // map callback 保留短字與 Unicode 各案例的完整命中集合。
      assert.deepEqual(found.body.data.list.map(row => row.id), ids);
      assert.equal(found.body.data.pagination.total, ids.length);
    }
    const deep = await send(app, 'GET', '/api/members?page=9007199254740991&per_page=100&filters%5Bkeyword%5D=a', undefined, admin);
    assert.deepEqual(deep.body.data.list, []);
    assert.deepEqual(deep.body.data.pagination, { page: 9007199254740991, per_page: 100, total: 1, total_pages: 1 });
    const nul = await send(app, 'GET', '/api/members?page=1&per_page=20&filters%5Bkeyword%5D=%00', undefined, admin);
    assert.deepEqual(nul.body.data.pagination, { page: 1, per_page: 20, total: 0, total_pages: 0 });
    const all = await send(app, 'GET', '/api/members?page=1&per_page=20&filters%5Bkeyword%5D=%20%20', undefined, admin);
    // map callback 驗證空字搜尋同未篩選且固定排序。
    assert.deepEqual(all.body.data.list.map(row => row.id), [3, 2]);
    assert.equal(all.body.data.pagination.total, 2);
    const hidden = await send(app, 'GET', '/api/comments?page=1&per_page=20', undefined, admin);
    assert.equal(hidden.status, 200);
    // map callback 核對兩種隱藏來源仍各自映射正確狀態與恢復權。
    assert.deepEqual(hidden.body.data.list.map(row => [row.id, row.visibility, row.can_restore]), [[10, 'hidden', true], [9, 'visible', false]]);
    // some/filter callback 確認留言列表只需 count 與 JOIN 頁查詢，未逐筆補查作者。
    assert.equal(f.calls.some(([sql]) => sql.includes('FROM comments JOIN accounts') && sql.includes('ORDER BY comments.created_at DESC,comments.id DESC') && !sql.includes('visibility=')), true);
    assert.equal(f.calls.filter(([sql]) => sql.includes('FROM comments JOIN accounts')).length, 2);
    failure(await send(app, 'GET', '/api/comments?page=1&per_page=20&article_id=20', undefined, admin), 422, 'E0001', { article_id: ['不支援此欄位'] });
    failure(await send(app, 'GET', '/api/settings/login-lockout?bad=1', undefined, admin), 422, 'E0001', { bad: ['不支援此欄位'] });
    const articles = await send(app, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', undefined, admin);
    assert.equal(articles.body.data.list[0].visibility, 'hidden');
    failure(await send(app, 'GET', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', undefined, member), 403, 'E0003');
    assert.equal(f.released > 0, true);
  } finally { await app.close(); }
});

// 測試 callback 區分同值、停用撤銷、恢復與設定關閉的原子副作用。
test('administration writes preserve sessions and atomic lockout settings', async () => {
  const f = fixture(); const app = await createApp(f.db, f.key);
  await app.listen(0, '127.0.0.1');
  const admin = f.token(1), member = f.token(2), second = f.token(2);
  // put callback 共用 HTTP 呼叫型態，避免每個設定案例自行改寫 Body。
  const put = (path, body, token = admin) => send(app, 'PUT', path, body, token);
  try {
    failure(await put('/api/members/2/status', { status: 'disabled' }, member), 403, 'E0003');
    failure(await put('/api/settings/login-lockout', { enabled: 'bad' }, member), 403, 'E0003');
    failure(await put('/api/members/1/status', { status: 'disabled' }), 404, 'E0004');
    failure(await put('/api/members/2/status?bad=1', { status: 'disabled' }), 422, 'E0001', { bad: ['不支援此欄位'] });
    failure(await put('/api/members/2/status', { status: 'no' }), 422, 'E0001', { status: ['欄位格式或範圍不正確'] });
    failure(await put('/api/members/2/status', { status: null }), 422, 'E0001', { status: ['此欄位不可為 null'] });
    const initial = f.calls.length;
    assert.equal((await put('/api/members/2/status', { status: 'active' })).status, 200);
    // some callback 保證狀態同值沒有資料庫寫入或撤銷。
    assert.equal(f.calls.slice(initial).some(([sql]) => sql.startsWith('UPDATE')), false);
    assert.equal((await put('/api/members/2/status', { status: 'disabled' })).status, 200);
    assert.equal(f.accounts.get(2).status, 'disabled');
    // filter/every callback 檢查兩筆既有登入都已撤銷。
    assert.equal([...f.sessions.values()].filter(row => row.account_id === 2).every(row => row.revoked_at), true);
    failure(await send(app, 'GET', '/api/me', undefined, member), 403, 'E0102');
    assert.equal((await put('/api/members/2/status', { status: 'active' })).status, 200);
    assert.equal(f.accounts.get(2).locked_until?.getTime(), created.getTime());
    failure(await send(app, 'GET', '/api/me', undefined, second), 401, 'E0002');
    const values = { enabled: true, window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 };
    failure(await put('/api/settings/login-lockout', { ...values, failure_threshold: 101 }), 422, 'E0001', { failure_threshold: ['欄位格式或範圍不正確'] });
    failure(await put('/api/settings/login-lockout', { ...values, window_minutes: 0 }), 422, 'E0001', { window_minutes: ['欄位格式或範圍不正確'] });
    failure(await put('/api/settings/login-lockout', { ...values, lock_seconds: 86401 }), 422, 'E0001', { lock_seconds: ['欄位格式或範圍不正確'] });
    failure(await put('/api/settings/login-lockout', '{"enabled":true,"window_minutes":1e1,"failure_threshold":5,"lock_seconds":300}'), 422, 'E0001', { window_minutes: ['欄位型態不正確'] });
    failure(await put('/api/settings/login-lockout', { ...values, enabled: 'true' }), 422, 'E0001', { enabled: ['欄位型態不正確'] });
    failure(await put('/api/settings/login-lockout', { ...values, extra: 1 }), 422, 'E0001', { extra: ['不支援此欄位'] });
    failure(await put('/api/settings/login-lockout', { enabled: true }), 422, 'E0001', { window_minutes: ['此欄位為必填'], failure_threshold: ['此欄位為必填'], lock_seconds: ['此欄位為必填'] });
    assert.equal((await put('/api/settings/login-lockout', values)).status, 200);
    assert.equal(f.accounts.get(2).locked_until?.getTime(), created.getTime());
    assert.deepEqual((await send(app, 'GET', '/api/settings/login-lockout', undefined, admin)).body.data, values);
    const unchanged = f.calls.length;
    assert.equal((await put('/api/settings/login-lockout', values)).status, 200);
    // some callback 驗證完整設定同值無資料庫副作用。
    assert.equal(f.calls.slice(unchanged).some(([sql]) => sql.startsWith('UPDATE')), false);
    const disabled = { ...values, enabled: false };
    f.failClear = true;
    failure(await put('/api/settings/login-lockout', disabled), 500, 'E9999');
    assert.deepEqual(f.settings, values);
    f.failClear = false;
    assert.equal((await put('/api/settings/login-lockout', disabled)).status, 200);
    assert.deepEqual(f.settings, disabled);
    assert.equal(f.accounts.get(2).locked_until, null);
    assert.equal(f.accounts.get(3).locked_until, null);
    assert.equal(f.accounts.get(2).failure_epoch, 5);
    assert.equal(f.accounts.get(3).failure_epoch, 8);
    assert.equal((await put('/api/settings/login-lockout', disabled)).status, 200);
    assert.equal(f.accounts.get(2).failure_epoch, 5);
    // some callback 證明關閉設定觸發全帳號依 ID 鎖定路徑。
    assert.equal(f.calls.some(([sql]) => sql.includes('FROM accounts ORDER BY id FOR UPDATE')), true);
    assert.equal(f.released > 0, true);
  } finally { await app.close(); }
});
