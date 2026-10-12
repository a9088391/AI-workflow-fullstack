'use strict';

const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const { Pool } = require('pg');
const { hashPassword, login } = require('./auth');

// runCase 只替換 login 取得 t 的單一 SELECT，其餘帳號鎖、事件寫入與 count 均使用真實 PostgreSQL。
async function runCase(pool, key, offsetMicros, expectedCode, expectedCount) {
  const email = `ticket37-boundary-${randomBytes(8).toString('hex')}@example.test`;
  const hash = await hashPassword('Abcd1234');
  const fixed = (await pool.query('SELECT clock_timestamp()::text AS checked_at')).rows[0].checked_at;
  const account = (await pool.query("INSERT INTO accounts(email,password_hash,nickname,role,status) VALUES ($1,$2,'邊界測試','member','active') RETURNING id,failure_epoch", [email, hash])).rows[0];
  await pool.query(`INSERT INTO login_failures(account_id,failure_epoch,occurred_at)
    VALUES ($1,$2,$3::timestamptz-interval '5 minutes'+$4::integer*interval '1 microsecond')`,
  [account.id, account.failure_epoch, fixed, offsetMicros]);

  let checkedCalls = 0;
  let counted;
  const db = {
    // 保持每個 login 交易的同一真實連線，只截取取得 clock_timestamp 的查詢。
    async connect() {
      const client = await pool.connect();
      return {
        // 此替身把固定 t 與原本 locked_until 比較，其餘 SQL 原封不動交由原連線執行。
        async query(statement, args) {
          if (statement.includes('FROM (SELECT clock_timestamp() AS t) clock')) {
            checkedCalls++;
            return client.query(`SELECT $1::timestamptz::text AS checked_at,
              floor(extract(epoch FROM $1::timestamptz))::bigint AS issued,
              $2::timestamptz > $1::timestamptz AS locked`, [fixed, args[0]]);
          }
          const result = await client.query(statement, args);
          if (statement.includes('SELECT count(*)::integer AS n FROM login_failures')) counted = result.rows[0].n;
          return result;
        },
        release() { client.release(); },
      };
    },
    // lockRetry 在交易提交後仍使用真實資料庫時鐘，不受測試時間替身影響。
    query: (...args) => pool.query(...args),
  };
  await assert.rejects(login(db, randomBytes(32), { email, password: 'Wrong1234' }), { code: expectedCode });
  assert.equal(checkedCalls, 1);
  assert.equal(counted, expectedCount);

  // 交易結束後另查已提交資料，避免僅依例外碼推測事件或鎖定是否保存。
  const state = (await pool.query(`SELECT failure_epoch,locked_until IS NULL AS unlocked,
    locked_until=$2::timestamptz+interval '30 seconds' AS exact_lock FROM accounts WHERE id=$1`, [account.id, fixed])).rows[0];
  assert.equal(state.failure_epoch, account.failure_epoch);
  assert.equal(state.unlocked, expectedCode === 'E0101');
  if (expectedCode === 'E0105') assert.equal(state.exact_lock, true);
  const events = (await pool.query(`SELECT count(*)::integer AS total,
    count(*) FILTER (WHERE occurred_at=$2::timestamptz)::integer AS current,
    count(*) FILTER (WHERE occurred_at=$2::timestamptz-interval '5 minutes'+$3::integer*interval '1 microsecond')::integer AS seeded
    FROM login_failures WHERE account_id=$1 AND failure_epoch=$4`, [account.id, fixed, offsetMicros, account.failure_epoch])).rows[0];
  assert.deepEqual(events, { total: 2, current: 1, seeded: 1 });
}

// main 只連已由 Python guard 核對的隔離資料庫，分別驗左邊界排除與晚一微秒納入。
async function main() {
  const pool = new Pool({ host: process.env.DB_HOST, port: Number(process.env.DB_PORT), database: process.env.DB_DATABASE,
    user: process.env.DB_USERNAME, password: process.env.DB_PASSWORD, max: 2, connectionTimeoutMillis: 10000 });
  try {
    const settings = (await pool.query('SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1')).rows[0];
    assert.deepEqual(settings, { enabled: true, window_minutes: 5, failure_threshold: 2, lock_seconds: 30 });
    const key = randomBytes(32);
    await runCase(pool, key, 0, 'E0101', 1);
    await runCase(pool, key, 1, 'E0105', 2);
    console.log('PASS ticket37 real login SQL boundary: left=excluded, +1us=included, committed state verified');
  } finally {
    await pool.end();
  }
}

// 主測試失敗時明確回傳非零，交由 Python finally 復原共用設定。
main().catch(error => { console.error(error); process.exitCode = 1; });
