'use strict';

const { ApiError } = require('./input');
const { accountData, lockedIdentity } = require('./auth');
const { contentData, pagination } = require('./content');

const SETTINGS = 'enabled,window_minutes,failure_threshold,lock_seconds';
const COMMENT_COLUMNS = 'comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname';

// asciiFold 只折疊 ASCII 大寫，讓查詢詞與 SQL 的字面比較規則相同。
function asciiFold(value) {
  // callback 不使用 Unicode 小寫轉換，以免改變非 ASCII 的搜尋語意。
  return value.replace(/[A-Z]/g, letter => letter.toLowerCase());
}

// members 在 PostgreSQL 做篩選、精確計數與分頁，作者資料不逐筆查詢。
async function members(db, page, perPage, keyword = '') {
  // PostgreSQL text 不接受 NUL；包含 NUL 的合法關鍵字不可能命中任何資料。
  if (keyword.includes('\0')) return { list: [], pagination: pagination(page, perPage, 0) };
  const base = `FROM accounts WHERE role='member'` + (keyword ? ` AND (
    strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0 OR
    strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0)` : '');
  const args = keyword ? [asciiFold(keyword)] : [];
  const counted = await db.query(`SELECT count(*)::bigint AS total ${base}`, args);
  const total = Number(counted.rows[0].total);
  const offset = (BigInt(page) - 1n) * BigInt(perPage);
  let list = [];
  if (offset < BigInt(total)) {
    const rows = await db.query(`SELECT id,email,nickname,role,status,created_at ${base}
      ORDER BY created_at DESC,id DESC LIMIT $${args.length + 1} OFFSET $${args.length + 2}`,
    [...args, perPage, offset.toString()]);
    // callback 僅輸出帳號公開欄位，不夾帶鎖定或密碼資料。
    list = rows.rows.map(row => accountData(row));
  }
  return { list, pagination: pagination(page, perPage, total) };
}

// memberDetail 只讀一般會員，使管理員 ID 與不存在 ID 都回相同的 404。
async function memberDetail(db, id) {
  const found = await db.query("SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1 AND role='member'", [id]);
  if (!found.rows.length) throw new ApiError('E0004');
  return accountData(found.rows[0]);
}

// memberStatus 依帳號 ID 鎖序重驗管理員與目標，使停用及舊登入撤銷一起提交。
async function memberStatus(db, key, req, subject, id, status) {
  const client = await db.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    // 先依 ID 鎖住兩個帳號，再鎖管理員 session；與登入、內容寫入鎖序一致。
    const rows = await client.query('SELECT id,role,status FROM accounts WHERE id IN ($1,$2) ORDER BY id FOR UPDATE', [subject.account.id, id]);
    const current = await lockedIdentity(client, key, req);
    if (current.account.role !== 'admin') throw new ApiError('E0003');
    // callback 以鎖定後狀態判定一般會員，避免管理員目標洩露。
    const target = rows.rows.find(row => String(row.id) === String(id));
    if (!target || target.role !== 'member') throw new ApiError('E0004');
    if (target.status !== status) {
      await client.query('UPDATE accounts SET status=$1 WHERE id=$2', [status, id]);
      // 只有實際停用才撤銷所有後端的舊登入；恢復不會使其復活。
      if (status === 'disabled') await client.query('UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$1 AND revoked_at IS NULL', [id]);
    }
    await client.query('COMMIT'); committed = true;
  } finally {
    if (!committed) { try { await client.query('ROLLBACK'); } catch {} }
    client.release();
  }
}

// managementComments 包含個別及父文章下架的留言，仍只以兩筆查詢完成分頁。
async function managementComments(db, viewer, page, perPage) {
  const base = 'FROM comments JOIN accounts ON accounts.id=comments.author_id';
  const counted = await db.query(`SELECT count(*)::bigint AS total ${base}`);
  const total = Number(counted.rows[0].total);
  const offset = (BigInt(page) - 1n) * BigInt(perPage);
  let list = [];
  if (offset < BigInt(total)) {
    const rows = await db.query(`SELECT ${COMMENT_COLUMNS} ${base} ORDER BY comments.created_at DESC,comments.id DESC LIMIT $1 OFFSET $2`, [perPage, offset.toString()]);
    // callback 重用內容欄位與 can_restore 計算，保留父文章下架時的留言狀態。
    list = rows.rows.map(row => contentData(row, viewer, false));
  }
  return { list, pagination: pagination(page, perPage, total) };
}

// lockoutSettings 只回共用設定四欄，避免洩露會員的失敗事件。
async function lockoutSettings(db) {
  const found = await db.query(`SELECT ${SETTINGS} FROM login_lockout_settings WHERE id=1`);
  if (!found.rows.length) throw new ApiError('E0104');
  return found.rows[0];
}

// updateLockoutSettings 完整覆寫四欄，啟用轉關閉時原子清除會員鎖定與計數。
async function updateLockoutSettings(db, key, req, body) {
  const client = await db.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    // 設定列先於帳號鎖，讓登入與管理修改有確定的提交先後。
    const found = await client.query(`SELECT ${SETTINGS} FROM login_lockout_settings WHERE id=1 FOR UPDATE`);
    if (!found.rows.length) throw new ApiError('E0104');
    const previous = found.rows[0];
    const disabling = previous.enabled && !body.enabled;
    if (disabling) await client.query('SELECT id FROM accounts ORDER BY id FOR UPDATE');
    const current = await lockedIdentity(client, key, req);
    if (current.account.role !== 'admin') throw new ApiError('E0003');
    // 同值不重做副作用；單純修改數值不改既有 locked_until。
    if (Object.keys(body).some(name => body[name] !== previous[name])) {
      await client.query(`UPDATE login_lockout_settings SET enabled=$1,window_minutes=$2,failure_threshold=$3,lock_seconds=$4 WHERE id=1`,
        [body.enabled, body.window_minutes, body.failure_threshold, body.lock_seconds]);
      if (disabling) await client.query("UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL WHERE role='member'");
    }
    await client.query('COMMIT'); committed = true;
  } finally {
    if (!committed) { try { await client.query('ROLLBACK'); } catch {} }
    client.release();
  }
}

module.exports = { members, memberDetail, memberStatus, managementComments, lockoutSettings, updateLockoutSettings };
