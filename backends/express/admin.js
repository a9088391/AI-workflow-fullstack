'use strict';

const { ApiError } = require('./input');
const { accountData, recheckSession } = require('./auth');
const { pagination } = require('./content');

const SETTINGS = 'enabled,window_minutes,failure_threshold,lock_seconds';

// asciiFold 只折疊 ASCII 大寫，讓搜尋詞與 SQL translate 使用相同規則。
function asciiFold(value) {
  // callback 只轉換 ASCII，避免通用 Unicode lower-case 改變非 ASCII 比對。
  return value.replace(/[A-Z]/g, letter => letter.toLowerCase());
}

// members 在資料庫完成字面搜尋、精確計數與分頁，避免逐筆查詢與讀取敏感欄位。
async function members(db, page, perPage, keyword = '') {
  const base = `FROM accounts WHERE role='member'` + (keyword ? ` AND (
    strpos(translate(nickname,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0 OR
    strpos(translate(email,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),$1)>0)` : '');
  const args = keyword ? [asciiFold(keyword)] : [];
  const count = await db.query(`SELECT count(*)::bigint AS total ${base}`, args);
  const total = Number(count.rows[0].total);
  const offset = (BigInt(page) - 1n) * BigInt(perPage);
  let list = [];
  if (offset < BigInt(total)) {
    const rows = await db.query(`SELECT id,email,nickname,role,status,created_at ${base}
      ORDER BY created_at DESC,id DESC LIMIT $${args.length + 1} OFFSET $${args.length + 2}`,
    [...args, perPage, offset.toString()]);
    // callback 將查詢列縮成契約允許的帳號欄位，不帶私有鎖定資料。
    list = rows.rows.map(row => accountData(row));
  }
  return { list, pagination: pagination(page, perPage, total) };
}

// memberDetail 僅讀一般會員，管理員 ID 與不存在 ID 都不揭露。
async function memberDetail(db, id) {
  const found = await db.query("SELECT id,email,nickname,role,status,created_at FROM accounts WHERE id=$1 AND role='member'", [id]);
  if (!found.rows.length) throw new ApiError('E0004');
  return accountData(found.rows[0]);
}

// recheckAdmin 在帳號鎖後重驗 session 與目前角色，避免外層認證快照授權已撤銷寫入。
async function recheckAdmin(client, subject, current) {
  await recheckSession(client, subject, current);
  if (current.role !== 'admin') throw new ApiError('E0003');
}

// memberStatus 鎖住管理員與目標會員，將狀態變更與舊 session 撤銷原子提交。
async function memberStatus(db, subject, id, status) {
  const client = await db.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    // 多筆帳號依 ID 鎖定，與設定關閉及其他受保護寫入維持共同鎖序。
    const locked = await client.query('SELECT id,role,status FROM accounts WHERE id IN ($1,$2) ORDER BY id FOR UPDATE', [subject.account.id, id]);
    // callback 從依 ID 加鎖的結果定位目前管理員，供交易內重驗權限。
    const current = locked.rows.find(row => String(row.id) === String(subject.account.id));
    // callback 定位目標；不存在或不是一般會員均隱藏為 404。
    const target = locked.rows.find(row => String(row.id) === String(id));
    if (!current) throw new ApiError('E0002');
    await recheckAdmin(client, subject, current);
    if (!target || target.role !== 'member') throw new ApiError('E0004');
    if (target.status !== status) {
      await client.query('UPDATE accounts SET status=$1 WHERE id=$2', [status, id]);
      // 只在實際停用時撤銷所有後端的登入；恢復不會讓舊憑證復活。
      if (status === 'disabled') await client.query('UPDATE login_sessions SET revoked_at=clock_timestamp() WHERE account_id=$1 AND revoked_at IS NULL', [id]);
    }
    await client.query('COMMIT'); committed = true;
  } finally {
    // 尚未提交的狀態與撤銷一起回滾，保留原始錯誤並歸還連線。
    if (!committed) { try { await client.query('ROLLBACK'); } catch {} }
    client.release();
  }
}

// lockoutSettings 只回共用設定四欄，不暴露會員失敗事件或鎖定狀態。
async function lockoutSettings(db) {
  const found = await db.query(`SELECT ${SETTINGS} FROM login_lockout_settings WHERE id=1`);
  if (!found.rows.length) throw new ApiError('E0104');
  return found.rows[0];
}

// updateLockoutSettings 原子覆寫四欄；只有啟用轉關閉才清除會員計數世代與期限。
async function updateLockoutSettings(db, subject, body) {
  const client = await db.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    const found = await client.query(`SELECT ${SETTINGS} FROM login_lockout_settings WHERE id=1 FOR UPDATE`);
    if (!found.rows.length) throw new ApiError('E0104');
    const previous = found.rows[0];
    const disabling = previous.enabled && !body.enabled;
    // 需要清除全體會員鎖定時，先按 ID 鎖帳號再重驗管理員，避免反向取鎖。
    if (disabling) await client.query('SELECT id FROM accounts ORDER BY id FOR UPDATE');
    const current = await client.query('SELECT id,role,status FROM accounts WHERE id=$1 FOR UPDATE', [subject.account.id]);
    if (!current.rows.length) throw new ApiError('E0002');
    await recheckAdmin(client, subject, current.rows[0]);
    // 完全同值時不碰設定列，數值變更亦不改既有鎖定期限。
    // callback 比對完整四欄；全同值不更新設定或重做關閉副作用。
    if (Object.keys(body).some(name => body[name] !== previous[name])) {
      await client.query(`UPDATE login_lockout_settings SET enabled=$1,window_minutes=$2,failure_threshold=$3,lock_seconds=$4 WHERE id=1`,
        [body.enabled, body.window_minutes, body.failure_threshold, body.lock_seconds]);
      if (disabling) await client.query("UPDATE accounts SET failure_epoch=failure_epoch+1,locked_until=NULL WHERE role='member'");
    }
    await client.query('COMMIT'); committed = true;
  } finally {
    // 四欄設定與關閉副作用不可部分提交；清理失敗不蓋掉原始操作錯誤。
    if (!committed) { try { await client.query('ROLLBACK'); } catch {} }
    client.release();
  }
}

module.exports = { members, memberDetail, memberStatus, lockoutSettings, updateLockoutSettings };
