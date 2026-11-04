'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { createApp } = require('./server');
const { issueToken } = require('./auth');

const created = new Date('2026-10-12T04:00:00Z');

// fixture 模擬本票 SQL 與狀態副作用，未知查詢必須失敗以免測試誤通過。
function fixture() {
  const key = randomBytes(32);
  const accounts = new Map([
    [1, { id: 1, email: 'admin@example.test', nickname: '管理員', role: 'admin', status: 'active', created_at: created }],
    [2, { id: 2, email: 'ALICE@example.test', nickname: 'Älice%_\\', role: 'member', status: 'active', created_at: created, failure_epoch: 1, locked_until: created }],
    [3, { id: 3, email: 'chen@example.test', nickname: '陳A', role: 'member', status: 'disabled', created_at: created, failure_epoch: 4, locked_until: created }],
  ]);
  const sessions = new Map();
  const settings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  const calls = [];
  let released = 0;
  let failUpdate = false;
  let beginAction = null;

  // token 為每個帳號建立有效本後端 session，讓路由實際經過 JWT 與帳號查核。
  function token(id) {
    const issued = Math.floor(Date.now() / 1000);
    const jti = randomBytes(16).toString('base64url');
    sessions.set(jti, { account_id: id, backend_id: 'fastify', issued_at: new Date(issued * 1000),
      expires_at: new Date((issued + 28800) * 1000), revoked_at: null });
    return issueToken(key, id, jti, issued);
  }

  // query 只模擬契約涉及的資料列；同時保留 SQL 和參數供測試檢查計數與鎖序。
  async function query(sql, args = []) {
    calls.push([sql, args]);
    if (sql === 'BEGIN') { if (beginAction) beginAction(); return { rows: [] }; }
    if (sql === 'COMMIT' || sql === 'ROLLBACK') return { rows: [] };
    if (sql.startsWith('SELECT a.id,a.email,a.nickname')) {
      const session = sessions.get(args[0]);
      const account = session && accounts.get(session.account_id);
      return { rows: account ? [{ ...account, ...session }] : [] };
    }
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1 FOR UPDATE')) return { rows: [accounts.get(Number(args[0]))].filter(Boolean) };
    if (sql.startsWith('SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions')) return { rows: [sessions.get(args[0])].filter(Boolean) };
    if (sql.startsWith('SELECT floor(extract(epoch FROM clock_timestamp()))')) return { rows: [{ now: String(Math.floor(Date.now() / 1000)) }] };
    if (sql.startsWith('SELECT id,role,status FROM accounts WHERE id IN')) return { rows: [...new Set(args)]
      // 數值排序模擬帳號 ID 鎖序，避免字串排序產生錯誤先後。
      .sort((a, b) => a - b)
      // 只回查詢要求的帳號，不用其他 fixture 掩蓋不存在情境。
      .map(id => accounts.get(Number(id))).filter(Boolean) };
    // callback 只投影 ID，模擬設定關閉前鎖住全部帳號的查詢。
    if (sql.startsWith('SELECT id FROM accounts ORDER BY id FOR UPDATE')) return { rows: [...accounts.values()].map(row => ({ id: row.id })) };
    if (sql.startsWith('SELECT count(*)::bigint AS total FROM accounts')) {
      // callback 僅計一般會員，依 nickname 或 email 任一字面命中模擬 count。
      const matched = [...accounts.values()].filter(row => row.role === 'member' && (!args.length ||
        // callback 保留任一欄位匹配；內層只折疊 ASCII，避免 fixture 掩蓋 Unicode 差異。
        [row.email, row.nickname].some(value => value.replace(/[A-Z]/g,
          // callback 不使用 Unicode 大小寫轉換，與核准的搜尋語意一致。
          letter => letter.toLowerCase()).includes(args[0]))));
      return { rows: [{ total: String(matched.length) }] };
    }
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts WHERE role=')) {
      const keyword = args.length === 3 ? args[0] : '';
      // callback 沿用 count 的一般會員與字面條件，讓總數可與列表比較。
      const matched = [...accounts.values()].filter(row => row.role === 'member' && (!keyword ||
        // callback 檢查任一公開搜尋欄位，不把符號解讀成萬用字元。
        [row.email, row.nickname].some(value => value.replace(/[A-Z]/g,
          // callback 只折疊 ASCII，保留中文與非 ASCII 字元的差異。
          letter => letter.toLowerCase()).includes(keyword))));
      // callback 依 ID 降冪打破相同建立時間的平手，再切出要求頁面。
      return { rows: matched.sort((a, b) => b.id - a.id).slice(Number(args.at(-1)), Number(args.at(-1)) + Number(args.at(-2))) };
    }
    if (sql.startsWith('SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1 AND role=')) {
      const row = accounts.get(Number(args[0]));
      return { rows: row?.role === 'member' ? [row] : [] };
    }
    if (sql.startsWith('UPDATE accounts SET status=')) {
      if (failUpdate) throw Error('isolated database failure');
      accounts.get(Number(args[1])).status = args[0];
      return { rows: [], rowCount: 1 };
    }
    if (sql.startsWith('UPDATE login_sessions SET revoked_at=')) {
      for (const row of sessions.values()) if (row.account_id === Number(args[0]) && !row.revoked_at) row.revoked_at = created;
      return { rows: [] };
    }
    if (sql.startsWith('SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings')) return { rows: [{ ...settings }] };
    if (sql.startsWith('UPDATE login_lockout_settings SET')) {
      if (failUpdate) throw Error('isolated database failure');
      Object.assign(settings, { enabled: args[0], window_minutes: args[1], failure_threshold: args[2], lock_seconds: args[3] });
      return { rows: [] };
    }
    if (sql.startsWith('UPDATE accounts SET failure_epoch=')) {
      for (const row of accounts.values()) if (row.role === 'member') { row.failure_epoch++; row.locked_until = null; }
      return { rows: [] };
    }
    if (sql.startsWith('SELECT count(*)::bigint AS total FROM comments')) return { rows: [{ total: '1' }] };
    if (sql.startsWith('SELECT comments.id,') && sql.includes('FROM comments JOIN accounts')) return { rows: [{
      id: 7, author_id: 2, article_id: 9, body: '隱藏留言', visibility: 'hidden', restore_control: 'admin', version: 2,
      created_backend: 'laravel', created_at: created, updated_at: created, nickname: 'Älice%_\\',
    }] };
    if (sql.startsWith('SELECT count(*)::bigint AS total FROM articles')) return { rows: [{ total: '0' }] };
    throw Error(`unexpected query: ${sql}`);
  }

  return { key, accounts, sessions, settings, calls, token,
    db: { query,
      // connect 提供交易接縫，讓測試查到每條路徑是否釋放連線。
      async connect() { return { query,
        // release 記錄歸還的連線，避免錯誤分支留下佔用資源。
        release() { released++; },
      }; },
    },
    // getter 供失敗案例觀測，不允許測試直接改內部計數。
    get released() { return released; },
    // setter 只在指定案例注入資料庫拒絕，檢查共同錯誤與回滾。
    set failUpdate(value) { failUpdate = value; },
    // setter 在外層驗權後、交易開始時切換狀態，驗證鎖後重新查核。
    set beginAction(value) { beginAction = value; },
  };
}

// send 保留原始 JSON 與 URL，讓 HTTP 契約驗證不被測試工具改寫。
async function send(app, method, path, body, token) {
  const response = await app.inject({ method, url: path,
    payload: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
    headers: { ...(body === undefined ? {} : { 'content-type': 'application/json' }),
      ...(token ? { authorization: `Bearer ${token}` } : {}) } });
  return { status: response.statusCode, body: response.json() };
}

// error 檢查共同狀態、錯誤碼與精確欄位訊息，避免框架預設格式誤通過。
function error(result, status, code, fields) {
  assert.equal(result.status, status);
  assert.equal(result.body.error_code, code);
  if (fields) assert.deepEqual(result.body.errors, fields);
}

// 管理端點先驗角色；管理員目標與不存在目標都必須隱藏為 404。
test('admin routes enforce role, validation order and member visibility', async () => {
  const f = fixture(), app = createApp(f.db, f.key);
  const admin = f.token(1), member = f.token(2);
  try {
    // 認證與管理角色優先，只有管理員才可收到精確的格式錯誤。
    error(await send(app, 'GET', '/api/members?page=bad&per_page=20'), 401, 'E0002');
    error(await send(app, 'GET', '/api/members?page=bad&per_page=20', undefined, member), 403, 'E0003');
    error(await send(app, 'GET', '/api/members?page=bad&per_page=20', undefined, admin), 422, 'E0001', { page: ['欄位格式或範圍不正確'] });
    error(await send(app, 'GET', '/api/members?page=1&per_page=20&filters[keyword]=a&filters[keyword]=b', undefined, admin), 422, 'E0001', { 'filters.keyword': ['此參數不可重複'] });
    error(await send(app, 'GET', '/api/members?page=1&per_page=20&filters[keyword][]=a', undefined, admin), 422, 'E0001', { 'filters.keyword': ['欄位型態不正確'] });
    error(await send(app, 'GET', `/api/members?page=1&per_page=20&filters[keyword]=${'x'.repeat(255)}`, undefined, admin), 422, 'E0001', { 'filters.keyword': ['欄位格式或範圍不正確'] });
    // 管理員 ID 與缺少的會員都回 404，避免跨越一般會員管理範圍。
    error(await send(app, 'GET', '/api/members/1', undefined, admin), 404, 'E0004');
    error(await send(app, 'GET', '/api/members/999', undefined, admin), 404, 'E0004');
    assert.equal((await send(app, 'GET', '/api/members/2', undefined, admin)).body.data.email, 'ALICE@example.test');
    error(await send(app, 'PUT', '/api/members/1/status', { status: 'disabled' }, member), 403, 'E0003');
    error(await send(app, 'PUT', '/api/members/1/status', { status: 'disabled' }, admin), 404, 'E0004');
    error(await send(app, 'PUT', '/api/members/2/status', { status: 'paused' }, admin), 422, 'E0001', { status: ['欄位格式或範圍不正確'] });
    // 相同管理角色門禁也適用文章管理範圍及設定，不能先回格式明細。
    error(await send(app, 'GET', '/api/articles?page=1&per_page=20&filters[scope]=management', undefined, member), 403, 'E0003');
    assert.equal((await send(app, 'GET', '/api/articles?page=1&per_page=20&filters[scope]=management', undefined, admin)).status, 200);
    error(await send(app, 'GET', '/api/settings/login-lockout?unknown=1', undefined, member), 403, 'E0003');
    error(await send(app, 'GET', '/api/settings/login-lockout?unknown=1', undefined, admin), 422, 'E0001');
    error(await send(app, 'PUT', '/api/comments', {}, admin), 405, 'E0005');
  } finally { await app.close(); }
});

// 會員列表檢查字面符號、ASCII 折疊、Unicode 精確、計數及超頁 SQL 次數。
test('member search keeps literal contains and exact pagination without per-row queries', async () => {
  const f = fixture(), app = createApp(f.db, f.key), admin = f.token(1);
  try {
    const match = await send(app, 'GET', '/api/members?page=1&per_page=20&filters[keyword]=%20ICE%25_%5C%20', undefined, admin);
    assert.equal(match.status, 200);
    // callback 只取 ID，以公開列表結果核對字面搜尋命中對象。
    assert.deepEqual(match.body.data.list.map(row => row.id), [2]);
    assert.equal(match.body.data.pagination.total, 1);
    // callback 只計會員列表 SQL，排除認證查詢而核對沒有 N+1。
    const search = f.calls.filter(([sql]) => sql.includes("FROM accounts WHERE role='member'"));
    assert.equal(search.length, 2);
    // callback 確認 count 與列表都採字面搜尋，不能套用 LIKE 萬用字元。
    assert.ok(search.every(([sql]) => sql.includes('strpos(translate(') && !/\bILIKE\b|\bLIKE\b/.test(sql)));
    assert.deepEqual(search[0][1], ['ice%_\\']);
    assert.equal((await send(app, 'GET', '/api/members?page=1&per_page=20&filters[keyword]=ä', undefined, admin)).body.data.pagination.total, 0);
    assert.equal((await send(app, 'GET', '/api/members?page=1&per_page=20&filters[keyword]=陳', undefined, admin)).body.data.pagination.total, 1);
    const deep = await send(app, 'GET', '/api/members?page=9007199254740991&per_page=20', undefined, admin);
    assert.deepEqual(deep.body.data, { list: [], pagination: { page: 9007199254740991, per_page: 20, total: 2, total_pages: 1 } });
  } finally { await app.close(); }
});

// 狀態轉移只在實際停用撤銷舊登入，恢復不改鎖定、內容或 session。
test('member status revokes old sessions once and preserves locks on restore', async () => {
  const f = fixture(), app = createApp(f.db, f.key), admin = f.token(1), old = f.token(2);
  try {
    assert.equal((await send(app, 'PUT', '/api/members/2/status', { status: 'disabled' }, admin)).status, 200);
    assert.equal(f.accounts.get(2).status, 'disabled');
    // callback 找出被停用會員的舊登入，核對實際撤銷而非只看 200。
    assert.equal([...f.sessions.values()].find(row => row.account_id === 2).revoked_at?.getTime(), created.getTime());
    error(await send(app, 'GET', '/api/me', undefined, old), 403, 'E0102');
    // callback 記錄首次撤銷次數，供同值操作比較副作用。
    const revokes = f.calls.filter(([sql]) => sql.startsWith('UPDATE login_sessions SET revoked_at=')).length;
    assert.equal((await send(app, 'PUT', '/api/members/2/status', { status: 'disabled' }, admin)).status, 200);
    // callback 只計撤銷 SQL，確保重複停用不重做副作用。
    assert.equal(f.calls.filter(([sql]) => sql.startsWith('UPDATE login_sessions SET revoked_at=')).length, revokes);
    assert.equal((await send(app, 'PUT', '/api/members/2/status', { status: 'active' }, admin)).status, 200);
    error(await send(app, 'GET', '/api/me', undefined, old), 401, 'E0002');
    assert.equal(f.accounts.get(2).locked_until, created);
    assert.equal(f.accounts.get(2).failure_epoch, 1);
  } finally { await app.close(); }
});

// 設定必須四欄驗證、完整提交，且關閉只在 true→false 時清鎖換世代。
test('lockout settings validate types and apply atomic disable effects once', async () => {
  const f = fixture(), app = createApp(f.db, f.key), admin = f.token(1);
  try {
    const valid = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
    error(await send(app, 'PUT', '/api/settings/login-lockout', '{"enabled":"false","window_minutes":0,"failure_threshold":101,"lock_seconds":1e5}', admin), 422, 'E0001', {
      enabled: ['欄位型態不正確'], window_minutes: ['欄位格式或範圍不正確'],
      failure_threshold: ['欄位格式或範圍不正確'], lock_seconds: ['欄位型態不正確'],
    });
    error(await send(app, 'PUT', '/api/settings/login-lockout', '{"enabled":true,"window_minutes":1e0,"failure_threshold":5,"lock_seconds":300}', admin), 422, 'E0001', {
      window_minutes: ['欄位型態不正確'],
    });
    error(await send(app, 'PUT', '/api/settings/login-lockout', '{"enabled":true,"window_minutes":5,"failure_threshold":5,"lock_seconds":9007199254740992}', admin), 422, 'E0001', {
      lock_seconds: ['欄位格式或範圍不正確'],
    });
    error(await send(app, 'PUT', '/api/settings/login-lockout', { ...valid, extra: true }, admin), 422, 'E0001', { extra: ['不支援此欄位'] });
    // 數值變更只覆寫設定，不影響既有鎖定到期時間。
    assert.equal((await send(app, 'PUT', '/api/settings/login-lockout', { ...valid, window_minutes: 10 }, admin)).status, 200);
    assert.equal(f.accounts.get(2).locked_until, created);
    assert.equal(f.settings.window_minutes, 10);
    // 真正關閉才清鎖換世代；重複同值由下方 SQL 次數確認沒有再次副作用。
    assert.equal((await send(app, 'PUT', '/api/settings/login-lockout', { ...valid, enabled: false }, admin)).status, 200);
    assert.deepEqual(f.settings, { ...valid, enabled: false });
    assert.equal(f.accounts.get(2).failure_epoch, 2);
    assert.equal(f.accounts.get(2).locked_until, null);
    // callback 記下首次清鎖換世代的 SQL 次數。
    const clearCount = f.calls.filter(([sql]) => sql.startsWith('UPDATE accounts SET failure_epoch=')).length;
    assert.equal((await send(app, 'PUT', '/api/settings/login-lockout', { ...valid, enabled: false }, admin)).status, 200);
    // callback 只計清鎖 SQL，確保再次關閉不增加世代。
    assert.equal(f.calls.filter(([sql]) => sql.startsWith('UPDATE accounts SET failure_epoch=')).length, clearCount);
    assert.deepEqual((await send(app, 'GET', '/api/settings/login-lockout', undefined, admin)).body.data, { ...valid, enabled: false });
  } finally { await app.close(); }
});

// 管理留言清單保留個別 hidden 與父文章可見性獨立，且不逐筆查作者。
test('management comments include hidden rows with content contract fields', async () => {
  const f = fixture(), app = createApp(f.db, f.key), admin = f.token(1);
  try {
    const found = await send(app, 'GET', '/api/comments?page=1&per_page=20', undefined, admin);
    assert.equal(found.status, 200);
    // callback 僅取可觀察的狀態與恢復權，驗證管理列表包含下架留言。
    assert.deepEqual(found.body.data.list.map(row => [row.id, row.visibility, row.can_restore]), [[7, 'hidden', true]]);
    // callback 只計留言讀取 SQL，核對列表與 count 各一次。
    assert.equal(f.calls.filter(([sql]) => sql.includes('FROM comments JOIN accounts')).length, 2);
    // callback 核對管理列表不以父文章 JOIN 過濾留言。
    assert.ok(f.calls.every(([sql]) => !sql.includes('JOIN articles ON articles.id=comments.article_id')));
  } finally { await app.close(); }
});

// 交易 SQL 失敗須回安全錯誤、回滾並釋放連線，不可假稱已提交。
test('admin write rolls back and releases connection on database failure', async () => {
  const f = fixture(), app = createApp(f.db, f.key), admin = f.token(1);
  f.failUpdate = true;
  try {
    error(await send(app, 'PUT', '/api/settings/login-lockout', { enabled: false, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 }, admin), 500, 'E9999');
    // callback 確認失敗沒有提交，避免只驗錯誤碼卻漏掉持久化副作用。
    assert.equal(f.calls.some(([sql]) => sql === 'COMMIT'), false);
    // callback 確認交易走回滾出口，再另外核對連線釋放。
    assert.equal(f.calls.some(([sql]) => sql === 'ROLLBACK'), true);
    assert.equal(f.released, 1);
  } finally { await app.close(); }
});

// 外層驗權後若 session 已撤銷，設定與會員寫入都須在交易中拒絕舊憑證。
test('admin writes recheck session after a concurrent revocation', async () => {
  const f = fixture(), app = createApp(f.db, f.key), admin = f.token(1);
  // callback 模擬在外層 identity 查詢與交易開始之間完成的撤銷。
  f.beginAction = () => { [...f.sessions.values()][0].revoked_at = created; };
  try {
    error(await send(app, 'PUT', '/api/members/2/status', { status: 'disabled' }, admin), 401, 'E0002');
    assert.equal(f.accounts.get(2).status, 'active');
    // callback 核對撤銷後沒有執行會員狀態更新。
    assert.equal(f.calls.some(([sql]) => sql.startsWith('UPDATE accounts SET status=')), false);
    // callback 確認交易走回滾出口，再另外核對連線釋放。
    assert.equal(f.calls.some(([sql]) => sql === 'ROLLBACK'), true);
    assert.equal(f.released, 1);
  } finally { await app.close(); }
  const g = fixture(), settingsApp = createApp(g.db, g.key), settingsAdmin = g.token(1);
  // callback 在 BEGIN 時撤銷登入，讓後續設定列鎖與 session 重查讀到已撤銷狀態。
  g.beginAction = () => { [...g.sessions.values()][0].revoked_at = created; };
  try {
    error(await send(settingsApp, 'PUT', '/api/settings/login-lockout', {
      enabled: false, window_minutes: 5, failure_threshold: 5, lock_seconds: 300,
    }, settingsAdmin), 401, 'E0002');
    assert.equal(g.settings.enabled, true);
    assert.equal(g.accounts.get(2).locked_until, created);
    // callback 核對撤銷後沒有進入設定更新，原值與會員鎖都必須保留。
    assert.equal(g.calls.some(([sql]) => sql.startsWith('UPDATE login_lockout_settings SET')), false);
    assert.equal(g.released, 1);
  } finally { await settingsApp.close(); }
});

// 管理員會員搜尋拒絕解碼後 NUL，並保留欄位優先序、聚合及合法字面搜尋。
test('member keyword rejects decoded NUL without changing query precedence', async () => {
  const f = fixture(), app = createApp(f.db, f.key);
  const admin = f.token(1), member = f.token(2);
  const invalid = ['欄位格式或範圍不正確'];
  const base = '/api/members?page=1&per_page=20&';
  try {
    // 身分與角色先於本文，本文先於 Query；同時無效時不得洩漏後續驗證明細。
    const bad = '/api/members?page=bad&per_page=20&filters[keyword]=%00';
    error(await send(app, 'GET', bad, '{}'), 401, 'E0002');
    error(await send(app, 'GET', bad, '{}', member), 403, 'E0003');
    error(await send(app, 'GET', bad, '{}', admin), 422, 'E0001', { _body: ['此 API 不接受 Request Body'] });
    const before = f.calls.length;
    // 明確先確認編碼承載真正 U+0000，再分別驗證開頭、中間、結尾及 U+0020 包覆。
    for (const keyword of ['\0', '\0abc', 'ab\0cd', 'abc\0', ' \0 ']) {
      const encoded = encodeURIComponent(keyword);
      assert.ok(decodeURIComponent(encoded).includes('\0'));
      const result = await send(app, 'GET', `${base}filters[keyword]=${encoded}`, undefined, admin);
      error(result, 422, 'E0001', { 'filters.keyword': invalid });
      assert.equal(result.body.success, false);
      assert.equal(result.body.message, '欄位資料不正確，請確認後再送出');
    }
    // 其他欄位仍獨立彙整；同欄位重複與陣列型態優先於 NUL 值檢查。
    for (const [query, errors] of [
      ['page=bad&per_page=20&filters[keyword]=%00', { page: invalid, 'filters.keyword': invalid }],
      ['per_page=20&filters[keyword]=%00', { page: ['此欄位為必填'], 'filters.keyword': invalid }],
      ['page=1&per_page=20&filters[keyword]=%00&filters[keyword]=x', { 'filters.keyword': ['此參數不可重複'] }],
      ['page=1&per_page=20&filters[keyword]=x&filters[keyword]=%00', { 'filters.keyword': ['此參數不可重複'] }],
      ['page=1&per_page=20&filters[keyword][]=%00', { 'filters.keyword': ['欄位型態不正確'] }],
      ['page=1&per_page=20&filters[keyword][x]=%00', { 'filters.keyword': ['欄位型態不正確'] }],
    ]) {
      error(await send(app, 'GET', `/api/members?${query}`, undefined, admin), 422, 'E0001', errors);
    }
    const long = encodeURIComponent(` ${'😀'.repeat(255)} `);
    error(await send(app, 'GET', `${base}filters[keyword]=${long}`, undefined, admin), 422, 'E0001', { 'filters.keyword': invalid });
    // some callback 區分認證讀取與搜尋計數，拒絕案例不得送出搜尋 SQL 或任何寫入。
    assert.equal(f.calls.slice(before).some(([sql]) => sql.includes("FROM accounts WHERE role=") || sql.startsWith('UPDATE')), false);
    // 文字形式的反斜線 u0000 及 254 個非 BMP 碼點合法；只裁兩端 U+0020。
    for (const keyword of ['\\u0000', ` ${'😀'.repeat(254)} `]) {
      const start = f.calls.length;
      const encoded = encodeURIComponent(keyword);
      assert.equal(keyword.includes('\0'), false);
      const result = await send(app, 'GET', `${base}filters[keyword]=${encoded}`, undefined, admin);
      assert.equal(result.status, 200);
      assert.equal(result.body.success, true);
      // find callback 核對真正交給搜尋 SQL 的參數，不讓成功空列表掩蓋錯誤正規化。
      const counted = f.calls.slice(start).find(([sql]) => sql.startsWith('SELECT count(*)::bigint AS total FROM accounts'));
      assert.equal(counted[1][0], keyword.replace(/^ +| +$/g, ''));
    }
  } finally {
    await app.close();
  }
});
