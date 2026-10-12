'use strict';

const { createHmac, randomBytes, timingSafeEqual } = require('node:crypto');
const argon2 = require('argon2');
const { ApiError, urlInteger } = require('./input');

const ISSUER = 'ai-workflow-fullstack:fastify';
const AUDIENCE = 'ai-workflow-fullstack-api:fastify';
const DURATION = 28800;
const PHC = /^\$argon2id\$v=19\$m=19456,t=2,p=1\$([A-Za-z0-9+/]{22})\$([A-Za-z0-9+/]{43})$/;

// businessTime 以固定 UTC+8 輸出台北秒級業務時間，不依賴容器時區。
function businessTime(date) {
  return new Date(date.getTime() + 8 * 3600000).toISOString().slice(0, 19).replace('T', ' ');
}

// accountData 只回傳公開契約允許的帳號欄位，不帶密碼及鎖定資料。
function accountData(row) {
  return { id: Number(row.id), email: row.email, nickname: row.nickname, role: row.role, status: row.status, created_at: businessTime(row.created_at) };
}

// hashPassword 使用安全亂數鹽與固定 Argon2id 成本，產生跨後端 PHC 字串。
async function hashPassword(password) {
  const phc = await argon2.hash(password, { type: argon2.argon2id, version: 0x13, memoryCost: 19456, timeCost: 2, parallelism: 1, salt: randomBytes(16), hashLength: 32 });
  // node-argon2 輸出 m,p,t；共用資料要求 m,t,p，參數語意相同但表示須一致。
  return phc.replace('m=19456,p=1,t=2', 'm=19456,t=2,p=1');
}

// verifyPassword 先驗 PHC 形狀與成本，避免毀損值或惡意成本觸發未界定工作量。
async function verifyPassword(password, hash) {
  const match = PHC.exec(hash);
  if (!match || Buffer.from(match[1], 'base64').length !== 16 || Buffer.from(match[2], 'base64').length !== 32 ||
      Buffer.from(match[1], 'base64').toString('base64').replace(/=+$/, '') !== match[1] ||
      Buffer.from(match[2], 'base64').toString('base64').replace(/=+$/, '') !== match[2]) throw new ApiError('E0104');
  try { return await argon2.verify(hash, password); }
  catch { throw new ApiError('E0104'); }
}

// issueToken 固定 HS256 header 與八小時 claims，JWT 只承載身分不承載角色。
function issueToken(key, id, jti, issued) {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({ iss: ISSUER, aud: AUDIENCE, sub: String(id), jti, iat: issued, nbf: issued, exp: issued + DURATION })).toString('base64url');
  const signed = `${header}.${payload}`;
  return `${signed}.${createHmac('sha256', key).update(signed).digest('base64url')}`;
}

// verifyToken 先驗本後端簽章，再嚴格檢查固定 header、claims 與時間邊界。
function verifyToken(key, token) {
  if (token.length > 4096 || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(token)) throw new ApiError('E0002');
  const parts = token.split('.');
  const signature = Buffer.from(parts[2], 'base64url');
  const expected = createHmac('sha256', key).update(`${parts[0]}.${parts[1]}`).digest();
  if (signature.length !== expected.length || signature.toString('base64url') !== parts[2] || !timingSafeEqual(signature, expected)) throw new ApiError('E0002');
  let header, claims;
  try {
    const head = Buffer.from(parts[0], 'base64url');
    const body = Buffer.from(parts[1], 'base64url');
    if (head.toString('base64url') !== parts[0] || body.toString('base64url') !== parts[1]) throw Error();
    header = JSON.parse(head.toString('utf8'));
    claims = JSON.parse(body.toString('utf8'));
  } catch { throw new ApiError('E0002'); }
  if (!header || Array.isArray(header) || Object.keys(header).length !== 2 || header.alg !== 'HS256' || header.typ !== 'JWT' ||
      !claims || Array.isArray(claims) || Object.keys(claims).length !== 7 ||
      claims.iss !== ISSUER || claims.aud !== AUDIENCE || typeof claims.sub !== 'string' ||
      typeof claims.jti !== 'string' || !/^[A-Za-z0-9_-]{22}$/.test(claims.jti)) throw new ApiError('E0002');
  let id;
  try { id = urlInteger(claims.sub, 'sub'); } catch { throw new ApiError('E0002'); }
  const jti = Buffer.from(claims.jti, 'base64url');
  const now = Math.floor(Date.now() / 1000);
  if (String(id) !== claims.sub || jti.length !== 16 || jti.toString('base64url') !== claims.jti ||
      !Number.isSafeInteger(claims.iat) || !Number.isSafeInteger(claims.nbf) || !Number.isSafeInteger(claims.exp) ||
      claims.iat < 0 || claims.nbf !== claims.iat || claims.exp !== claims.iat + DURATION || now < claims.nbf || now >= claims.exp) throw new ApiError('E0002');
  return claims;
}

// identity 對保護與帶憑證的公開端點查簽章、登入紀錄及目前帳號狀態。
async function identity(db, key, req, required = false) {
  const supplied = Object.hasOwn(req.headers, 'authorization');
  if (!required && !supplied) return null;
  const authorization = req.headers.authorization || '';
  const parts = authorization.split(' ');
  if (parts.length !== 2 || !/^Bearer$/i.test(parts[0]) || !parts[1]) throw new ApiError('E0002');
  const claims = verifyToken(key, parts[1]);
  const result = await db.query(`SELECT a.id,a.email,a.nickname,a.role,a.status,a.created_at,
    s.account_id,s.backend_id,s.issued_at,s.expires_at,s.revoked_at
    FROM login_sessions s JOIN accounts a ON a.id=s.account_id WHERE s.jti=$1`, [claims.jti]);
  if (!result.rows.length) throw new ApiError('E0002');
  const row = result.rows[0];
  if (String(row.id) !== claims.sub || row.backend_id !== 'fastify' ||
      row.issued_at.getTime() !== claims.iat * 1000 || row.expires_at.getTime() !== claims.exp * 1000) throw new ApiError('E0002');
  if (row.status === 'disabled') throw new ApiError('E0102');
  if (row.revoked_at) throw new ApiError('E0002');
  return { account: row, claims };
}

// lockedIdentity 在寫入交易中依帳號→session 取得鎖並重驗，讓撤銷與內容寫入有確定先後。
async function lockedIdentity(client, key, req) {
  const authorization = req.headers.authorization || '';
  const parts = authorization.split(' ');
  if (parts.length !== 2 || !/^Bearer$/i.test(parts[0]) || !parts[1]) throw new ApiError('E0002');
  const claims = verifyToken(key, parts[1]);
  // 等鎖後才讀 session 與資料庫時間，避免外層已通過的身分在等待期間被撤銷。
  const found = await client.query('SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1 FOR UPDATE', [claims.sub]);
  const session = await client.query('SELECT account_id,backend_id,issued_at,expires_at,revoked_at FROM login_sessions WHERE jti=$1 FOR UPDATE', [claims.jti]);
  const now = await client.query('SELECT floor(extract(epoch FROM clock_timestamp()))::bigint AS now');
  if (!found.rows.length || !session.rows.length) throw new ApiError('E0002');
  const account = found.rows[0];
  const row = session.rows[0];
  if (String(row.account_id) !== claims.sub || row.backend_id !== 'fastify' ||
      row.issued_at.getTime() !== claims.iat * 1000 || row.expires_at.getTime() !== claims.exp * 1000 ||
      Number(now.rows[0].now) < claims.nbf || Number(now.rows[0].now) >= claims.exp) throw new ApiError('E0002');
  if (account.status === 'disabled') throw new ApiError('E0102');
  if (row.revoked_at) throw new ApiError('E0002');
  return { account, claims };
}

// lockRetry 在交易完成後沿用登入連線取資料庫時間，避免滿池時再借連線。
async function lockRetry(client, until) {
  const result = await client.query('SELECT GREATEST(1,CEIL(EXTRACT(EPOCH FROM ($1::timestamptz-clock_timestamp()))))::integer AS retry', [until]);
  return result.rows[0].retry;
}

// login 先鎖共用設定再鎖帳號，序列化各後端的失敗計數與設定變更。
async function login(db, key, body) {
  const client = await db.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    const settings = await client.query('SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1 FOR UPDATE');
    if (!settings.rows.length) throw new ApiError('E0104');
    const { enabled, window_minutes: window, failure_threshold: threshold, lock_seconds: seconds } = settings.rows[0];
    const found = await client.query('SELECT id,email,password_hash,nickname,role,status,failure_epoch,locked_until::text AS locked_until,created_at FROM accounts WHERE email=$1 FOR UPDATE', [body.email]);
    if (!found.rows.length) throw new ApiError('E0101');
    const account = found.rows[0];
    // 同一個資料庫時間點同時判鎖與取 JWT 整數秒；文字參數保留 timestamptz 微秒。
    const checked = (await client.query(`SELECT t::text AS checked_at,floor(extract(epoch FROM t))::bigint AS issued,
      $1::timestamptz > t AS locked FROM (SELECT clock_timestamp() AS t) clock`, [account.locked_until])).rows[0];
    const now = checked.checked_at;
    let epoch = account.failure_epoch;
    if (account.status === 'disabled') throw new ApiError('E0102');
    // 有效鎖不驗密碼、不增加事件；期限已到或設定停用時才清鎖換世代。
    if (account.role === 'member' && account.locked_until) {
      if (enabled && checked.locked) {
        await client.query('COMMIT'); committed = true;
        throw new ApiError('E0105', undefined, await lockRetry(client, account.locked_until));
      }
      const reset = await client.query('UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL WHERE id=$1 RETURNING failure_epoch', [account.id]);
      epoch = reset.rows[0].failure_epoch;
    }
    let correct;
    correct = await verifyPassword(body.password, account.password_hash);
    if (!correct) {
      if (enabled && account.role === 'member') {
        await client.query('INSERT INTO login_failures (account_id,failure_epoch,occurred_at) VALUES ($1,$2,$3::timestamptz)', [account.id, epoch, now]);
        const count = await client.query(`SELECT count(*)::integer AS n FROM login_failures
          WHERE account_id=$1 AND failure_epoch=$2 AND occurred_at>$3::timestamptz-$4::integer*interval '1 minute'
          AND occurred_at<=$3::timestamptz`, [account.id, epoch, now, window]);
        if (count.rows[0].n >= threshold) {
          const locked = await client.query("UPDATE accounts SET locked_until=$1::timestamptz+$2::integer*interval '1 second' WHERE id=$3 RETURNING locked_until::text AS until", [now, seconds, account.id]);
          await client.query('COMMIT'); committed = true;
          throw new ApiError('E0105', undefined, await lockRetry(client, locked.rows[0].until));
        }
      }
      await client.query('COMMIT'); committed = true;
      throw new ApiError('E0101');
    }
    // 成功登入在同一交易重設世代並保存 jti；回應的 exp 由同一秒數推導。
    const issued = Number(checked.issued);
    const jti = randomBytes(16).toString('base64url');
    const token = issueToken(key, account.id, jti, issued);
    await client.query('UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL WHERE id=$1', [account.id]);
    await client.query("INSERT INTO login_sessions (jti,account_id,backend_id,issued_at,expires_at) VALUES ($1,$2,'fastify',to_timestamp($3),to_timestamp($4))", [jti, account.id, issued, issued + DURATION]);
    await client.query('COMMIT'); committed = true;
    return { access_token: token, token_type: 'Bearer', expires_at: businessTime(new Date((issued + DURATION) * 1000)), user: accountData(account) };
  } finally {
    if (!committed) { try { await client.query('ROLLBACK'); } catch {} }
    client.release();
  }
}

// logout 用帳號後登入紀錄的鎖序重驗，僅撤銷目前 jti。
async function logout(db, subject) {
  const client = await db.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    const account = await client.query('SELECT status FROM accounts WHERE id=$1 FOR UPDATE', [subject.account.id]);
    const session = await client.query('SELECT revoked_at FROM login_sessions WHERE jti=$1 FOR UPDATE', [subject.claims.jti]);
    if (!account.rows.length || !session.rows.length) throw new ApiError('E0002');
    if (account.rows[0].status === 'disabled') throw new ApiError('E0102');
    if (session.rows[0].revoked_at || Math.floor(Date.now() / 1000) >= subject.claims.exp) throw new ApiError('E0002');
    await client.query('UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE jti=$1', [subject.claims.jti]);
    await client.query('COMMIT'); committed = true;
  } finally {
    if (!committed) { try { await client.query('ROLLBACK'); } catch {} }
    client.release();
  }
}

module.exports = { businessTime, accountData, hashPassword, verifyPassword, issueToken, verifyToken, identity, lockedIdentity, login, logout };
