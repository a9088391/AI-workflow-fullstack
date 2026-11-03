'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { randomBytes, createHmac } = require('node:crypto');
const { hashPassword, verifyPassword, issueToken, verifyToken, login, recheckSession } = require('./auth');

// signed 專供測試建立同一金鑰下的壞 claims，確保拒絕不是僅因 HMAC 不符。
function signed(key, header, claims) {
  const unsigned = `${Buffer.from(JSON.stringify(header)).toString('base64url')}.${Buffer.from(JSON.stringify(claims)).toString('base64url')}`;
  return `${unsigned}.${createHmac('sha256', key).update(unsigned).digest('base64url')}`;
}

// SEC03 固定 PHC 參數且能驗密碼；毀損格式不得視為一般錯密碼。
test('Argon2id PHC matches the shared cost and rejects corruption', async () => {
  const hash = await hashPassword('Ab12');
  assert.match(hash, /^\$argon2id\$v=19\$m=19456,t=2,p=1\$[A-Za-z0-9+/]{22}\$[A-Za-z0-9+/]{43}$/);
  assert.equal(await verifyPassword('Ab12', hash), true);
  assert.equal(await verifyPassword('bad1', hash), false);
  await assert.rejects(verifyPassword('Ab12', hash.replace('m=19456', 'm=1')), { code: 'E0104' });
});

// SEC04/05 驗固定 issuer/audience、整數時間、演算法及到期關係。
test('JWT only accepts the Express fixed claims and HS256', () => {
  const key = randomBytes(32);
  const now = Math.floor(Date.now() / 1000);
  const jti = randomBytes(16).toString('base64url');
  const token = issueToken(key, 42, jti, now);
  assert.equal(verifyToken(key, token).sub, '42');
  // 更換驗證金鑰必須拒絕，證明成功不是跳過 HMAC。
  assert.throws(() => verifyToken(randomBytes(32), token), { code: 'E0002' });
  const header = { alg: 'HS256', typ: 'JWT' };
  const claims = { iss: 'ai-workflow-fullstack:express', aud: 'ai-workflow-fullstack-api:express', sub: '42', jti, iat: now, nbf: now, exp: now + 28800 };
  // 即使重新用正確金鑰簽章，none header 仍不符合固定演算法。
  assert.throws(() => verifyToken(key, signed(key, { alg: 'none', typ: 'JWT' }, claims)), { code: 'E0002' });
  // 正確簽章不能跨越後端 audience 界線。
  assert.throws(() => verifyToken(key, signed(key, header, { ...claims, aud: 'ai-workflow-fullstack-api:laravel' })), { code: 'E0002' });
  // 多一秒也是不合法的八小時期限，不提供寬限。
  assert.throws(() => verifyToken(key, signed(key, header, { ...claims, exp: now + 28801 })), { code: 'E0002' });
  // NumericDate 必須是 JSON 整數，不能自動轉換字串。
  assert.throws(() => verifyToken(key, signed(key, header, { ...claims, iat: String(now) })), { code: 'E0002' });
  // sub 使用無前導零字串，以便與登入紀錄精確比對。
  assert.throws(() => verifyToken(key, signed(key, header, { ...claims, sub: '042' })), { code: 'E0002' });
});

// 共用鎖後查核維持到期先於停用、停用先於撤銷的結果，兩類受保護寫入沿用相同判定。
test('locked session validation preserves expiry, disabled and revoked precedence', async () => {
  const issued = 1767225600;
  const subject = { claims: { sub: '7', jti: 'test-jti', iat: issued, exp: issued + 28800 } };
  const saved = { account_id: 7, backend_id: 'express', issued_at: new Date(issued * 1000),
    expires_at: new Date((issued + 28800) * 1000), expired: false, revoked_at: new Date() };
  const calls = [];
  const client = {
    // 模擬已取得帳號鎖後的 session 列；確認查詢仍使用 FOR UPDATE。
    async query(statement, args) {
      calls.push([statement, args]);
      return { rows: [saved] };
    },
  };
  saved.expired = true;
  await assert.rejects(recheckSession(client, subject, { status: 'disabled' }), { code: 'E0002' });
  saved.expired = false;
  await assert.rejects(recheckSession(client, subject, { status: 'disabled' }), { code: 'E0102' });
  await assert.rejects(recheckSession(client, subject, { status: 'active' }), { code: 'E0002' });
  saved.revoked_at = null;
  await recheckSession(client, subject, { status: 'active' });
  assert.equal(calls.length, 4);
  for (const [statement, args] of calls) {
    assert.match(statement, /FROM login_sessions WHERE jti=\$1 FOR UPDATE/);
    assert.deepEqual(args, ['test-jti']);
  }
});

// LOCK01 的精確鎖定判斷沿用 SQL 微秒布林值，不受 pg 的 Date 毫秒截斷影響。
test('login uses database microsecond lock result', async () => {
  const calls = [];
  const until = '2026-01-01 00:00:00.0005+00';
  const client = {
    // 模擬已取得設定鎖及帳號鎖的資料庫回覆，查核鎖定分支不再驗密碼。
    async query(statement, args) {
      calls.push([statement, args]);
      if (statement.includes('FROM login_lockout_settings')) return { rows: [{ enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 }] };
      if (statement.includes('FROM accounts WHERE email=')) return { rows: [{ id: 7, role: 'member', status: 'active', failure_epoch: '1', locked_until: until }] };
      if (statement.includes('FROM (SELECT clock_timestamp()')) return { rows: [{ checked_at: '2026-01-01 00:00:00.0004+00', issued: '1767225600', locked: true }] };
      if (statement.includes('GREATEST(1,CEIL')) return { rows: [{ retry: 1 }] };
      return { rows: [] };
    },
    // 假連線沒有外部資源；保留 release 讓登入走完整清理路徑。
    release() {},
  };
  // connect callback 只借出同一假連線，不會接觸真資料庫。
  const db = { connect: async () => client };
  await assert.rejects(login(db, randomBytes(32), { email: 'a@example.test', password: 'Abcd1234' }), { code: 'E0105', retry: 1 });
  // callback 尋找所有寫入，既有有效鎖定不得增加事件或延長期限。
  assert.equal(calls.some(([statement]) => statement.startsWith('UPDATE') || statement.startsWith('INSERT')), false);
  // callback 取出鎖後時間查詢，確認傳入保留微秒的期限字串。
  assert.equal(calls.find(([statement]) => statement.includes('FROM (SELECT clock_timestamp()'))[1][0], until);
});

// 到期後的世代取資料庫 RETURNING，失敗時間和視窗界線均保留 SQL 微秒精度。
test('expired lock uses exact database epoch and timestamp', async () => {
  const hash = await hashPassword('Abcd1234');
  const checkedAt = '2026-01-01 00:00:00.0004+00';
  const newEpoch = '9007199254740993';
  const calls = [];
  const client = {
    // 設定與帳號列已鎖；錯密碼仍執行真實 Argon2id 驗證。
    async query(statement, args) {
      calls.push([statement, args]);
      if (statement.includes('FROM login_lockout_settings')) return { rows: [{ enabled: true, window_minutes: 5, failure_threshold: 2, lock_seconds: 30 }] };
      if (statement.includes('FROM accounts WHERE email=')) return { rows: [{ id: 7, role: 'member', status: 'active', password_hash: hash, failure_epoch: '9007199254740992', locked_until: '2026-01-01 00:00:00.0003+00' }] };
      if (statement.includes('FROM (SELECT clock_timestamp()')) return { rows: [{ checked_at: checkedAt, issued: '1767225600', locked: false }] };
      if (statement.includes('RETURNING failure_epoch')) return { rows: [{ failure_epoch: newEpoch }] };
      if (statement.includes('count(*)::integer')) return { rows: [{ n: 1 }] };
      return { rows: [] };
    },
    // 無外部資源可釋放；登入仍須呼叫與 pg 連線相同的清理介面。
    release() {},
  };
  // connect callback 回傳固定交易資料，隔離資料庫依賴。
  const db = { connect: async () => client };
  await assert.rejects(login(db, randomBytes(32), { email: 'a@example.test', password: 'Wrong1234' }), { code: 'E0101' });
  // callback 取得事件寫入，確認使用新世代與同一個精確資料庫時間。
  const inserted = calls.find(([statement]) => statement.startsWith('INSERT INTO login_failures'));
  // callback 取得視窗查詢，避免事件和計數採不同基準。
  const counted = calls.find(([statement]) => statement.includes('count(*)::integer'));
  assert.deepEqual(inserted[1], [7, newEpoch, checkedAt]);
  assert.deepEqual(counted[1], [7, newEpoch, checkedAt, 5]);
  assert.match(counted[0], /occurred_at>\$3::timestamptz-\$4::integer\*interval '1 minute'/);
});

// fullPoolLogin 讓二十個登入先各占一條假連線，驗證滿池時 Retry-After 不再向池借連線。
async function fullPoolLogin(alreadyLocked) {
  const hash = alreadyLocked ? undefined : await hashPassword('Abcd1234');
  const until = '2026-01-01 00:00:30+00';
  let active = 0;
  let arrived = 0;
  let start;
  // executor 保存解鎖函式，所有請求到齊後才放行，無須不穩定的 sleep。
  const barrier = new Promise(resolve => { start = resolve; });
  const db = {
    // 取得連線後等待所有二十個請求，模擬已達 max=20 的池。
    async connect() {
      active++;
      if (++arrived === 20) start();
      await barrier;
      return {
        // 僅模擬交易固定回覆；Retry-After 查詢必須走此既有連線。
        async query(statement) {
          if (statement.includes('FROM login_lockout_settings')) return { rows: [{ enabled: true, window_minutes: 5, failure_threshold: 1, lock_seconds: 30 }] };
          if (statement.includes('FROM accounts WHERE email=')) return { rows: [{ id: 7, role: 'member', status: 'active', failure_epoch: '1', locked_until: alreadyLocked ? until : null, password_hash: hash }] };
          if (statement.includes('FROM (SELECT clock_timestamp()')) return { rows: [{ checked_at: '2026-01-01 00:00:00+00', issued: '1767225600', locked: alreadyLocked }] };
          if (statement.includes('count(*)::integer')) return { rows: [{ n: 1 }] };
          if (statement.includes('RETURNING locked_until::text')) return { rows: [{ until }] };
          if (statement.includes('GREATEST(1,CEIL')) return { rows: [{ retry: 30 }] };
          return { rows: [] };
        },
        // 登入結束歸還容量，讓最終斷言偵測連線遺漏。
        release() { active--; },
      };
    },
    // 舊實作於二十條連線都借出時，再向池借第廿一條查 Retry-After 會失敗。
    async query() {
      if (active >= 20) throw Error('pool exhausted');
      return { rows: [{ retry: 30 }] };
    },
  };
  // callback 為每個登入收集業務拒絕，若意外成功則讓測試失敗。
  const results = await Promise.all(Array.from({ length: 20 }, async (_, index) => {
    try { await login(db, randomBytes(32), { email: `member${index}@example.test`, password: 'Wrong1234' }); }
    catch (error) { return error; }
    throw Error('錯誤密碼或現有鎖定竟然登入成功');
  }));
  assert.equal(arrived, 20);
  assert.equal(active, 0);
  for (const result of results) assert.equal(result.code, 'E0105');
  for (const result of results) assert.equal(result.retry, 30);
}

// 已鎖定分支不驗密碼，也須在滿池時計算 Retry-After 並歸還原連線。
test('full pool returns Retry-After for an existing lock', async () => fullPoolLogin(true));
// 門檻當次會保存失敗與期限，仍須沿原連線取得 Retry-After。
test('full pool returns Retry-After when this failure reaches the threshold', async () => fullPoolLogin(false));
