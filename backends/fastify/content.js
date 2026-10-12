'use strict';

const { ApiError, urlInteger, queryInput, bodyInput } = require('./input');
const { businessTime, lockedIdentity } = require('./auth');

const ARTICLE_COLUMNS = 'articles.id,articles.author_id,articles.title,articles.body,articles.visibility,articles.restore_control,articles.version,articles.created_backend,articles.created_at,articles.updated_at,accounts.nickname';
const COMMENT_COLUMNS = 'comments.id,comments.author_id,comments.article_id,comments.body,comments.visibility,comments.restore_control,comments.version,comments.created_backend,comments.created_at,comments.updated_at,accounts.nickname';

// contentData 僅輸出公開作者欄位，依目前身分重算 hidden 內容的恢復權。
function contentData(row, viewer, article) {
  const data = {
    id: Number(row.id), body: row.body,
    author: { id: Number(row.author_id), nickname: row.nickname },
    version: Number(row.version), created_backend: row.created_backend,
    created_at: businessTime(row.created_at), updated_at: businessTime(row.updated_at),
    visibility: row.visibility,
    can_restore: row.visibility === 'hidden' && !!viewer &&
      (viewer.role === 'admin' || Number(viewer.id) === Number(row.author_id) && row.restore_control === 'author'),
  };
  if (article) data.title = row.title;
  else data.article_id = Number(row.article_id);
  return data;
}

// pagination 保留合法超頁的原始頁碼與實際總數，不改寫成最後一頁。
function pagination(page, perPage, total) {
  return { page, per_page: perPage, total, total_pages: Math.ceil(total / perPage) };
}

// pageRows 以同一可見性條件計數和查詢，SQL 完成排序分頁而不逐筆取作者。
async function pageRows(db, columns, base, args, page, perPage, viewer, article) {
  const counted = await db.query(`SELECT count(*)::bigint AS total ${base}`, args);
  const total = Number(counted.rows[0].total);
  const offset = (BigInt(page) - 1n) * BigInt(perPage);
  let list = [];
  if (offset < BigInt(total)) {
    const rows = await db.query(`SELECT ${columns} ${base} ORDER BY ${article ? 'articles' : 'comments'}.created_at DESC,${article ? 'articles' : 'comments'}.id DESC LIMIT $${args.length + 1} OFFSET $${args.length + 2}`, [...args, perPage, offset.toString()]);
    // 每筆資料只在輸出時轉成共同欄位，作者已由 JOIN 一次取得。
    list = rows.rows.map(row => contentData(row, viewer, article));
  }
  return { list, pagination: pagination(page, perPage, total) };
}

// articles 預設只讀公開文章；明確管理範圍才納入 hidden。
async function articles(db, viewer, page, perPage, management) {
  const base = `FROM articles JOIN accounts ON accounts.id=articles.author_id ${management ? '' : "WHERE articles.visibility='visible'"}`;
  return pageRows(db, ARTICLE_COLUMNS, base, [], page, perPage, viewer, true);
}

// article 對訪客與非作者隱藏下架文章的存在。
async function article(db, viewer, id) {
  const result = await db.query(`SELECT ${ARTICLE_COLUMNS} FROM articles JOIN accounts ON accounts.id=articles.author_id WHERE articles.id=$1`, [id]);
  if (!result.rows.length) throw new ApiError('E0004');
  const row = result.rows[0];
  if (row.visibility === 'hidden' && (!viewer || viewer.role !== 'admin' && Number(viewer.id) !== Number(row.author_id))) throw new ApiError('E0004');
  return contentData(row, viewer, true);
}

// comments 先查父文章可讀性，只有管理員看得到個別下架留言。
async function comments(db, viewer, id, page, perPage) {
  const parent = await db.query('SELECT author_id,visibility FROM articles WHERE id=$1', [id]);
  if (!parent.rows.length) throw new ApiError('E0004');
  const row = parent.rows[0];
  if (row.visibility === 'hidden' && (!viewer || viewer.role !== 'admin' && Number(viewer.id) !== Number(row.author_id))) throw new ApiError('E0004');
  const base = `FROM comments JOIN accounts ON accounts.id=comments.author_id WHERE comments.article_id=$1 ${viewer?.role === 'admin' ? '' : "AND comments.visibility='visible'"}`;
  return pageRows(db, COMMENT_COLUMNS, base, [id], page, perPage, viewer, false);
}

// mine 以登入者 ID 在資料庫分頁，保留自己的下架內容且不授予父文章讀取權。
async function mine(db, viewer, kind, page, perPage) {
  const article = kind === 'article';
  const table = article ? 'articles' : 'comments';
  const base = `FROM ${table} JOIN accounts ON accounts.id=${table}.author_id WHERE ${table}.author_id=$1`;
  return pageRows(db, article ? ARTICLE_COLUMNS : COMMENT_COLUMNS, base, [viewer.id], page, perPage, viewer, article);
}

// comment 只回獲授權的單則留言，作者可在父文章下架時仍讀自己的留言。
async function comment(db, viewer, id) {
  const found = await db.query(`SELECT ${COMMENT_COLUMNS},articles.visibility AS article_visibility FROM comments
    JOIN accounts ON accounts.id=comments.author_id JOIN articles ON articles.id=comments.article_id WHERE comments.id=$1`, [id]);
  if (!found.rows.length) throw new ApiError('E0004');
  const row = found.rows[0];
  if (viewer.role !== 'admin' && Number(viewer.id) !== Number(row.author_id)) {
    if (row.visibility === 'hidden' || row.article_visibility === 'hidden') throw new ApiError('E0004');
    throw new ApiError('E0003');
  }
  return contentData(row, viewer, false);
}

// readable 先隱藏無權讀取的目標，再由呼叫端依操作檢查 403，維持共同錯誤順序。
function readable(row, parent, viewer) {
  if (!row || viewer.role !== 'admin' && Number(viewer.id) !== Number(row.author_id) &&
      (row.visibility === 'hidden' || parent?.visibility === 'hidden')) throw new ApiError('E0004');
}

// saveChanges 在已鎖列的交易內先比版本再比較實際變動，同值不動版本與時間。
async function saveChanges(client, kind, row, version, changes) {
  if (Number(row.version) !== version) throw new ApiError('E0201');
  // 只挑真正變動的欄位，避免重複操作改動版本或 updated_at。
  const changed = Object.entries(changes).filter(([name, value]) => row[name] !== value);
  if (!changed.length) return;
  const table = kind === 'article' ? 'articles' : 'comments';
  // 欄名只來自伺服器建立的白名單物件；版本條件另保護意外的競爭更新。
  const set = changed.map(([name], index) => `${name}=$${index + 1}`).join(',');
  const values = changed.map(([, value]) => value);
  const result = await client.query(`UPDATE ${table} SET ${set},version=version+1,updated_at=clock_timestamp()
    WHERE id=$${values.length + 1} AND version=$${values.length + 2}`, [...values, row.id, version]);
  if (result.rowCount !== 1) throw new ApiError('E0201');
}

// write 在同一交易驗身分、目標與版本；帳號→session→文章→留言鎖序相容於撤銷與下架。
async function write(db, key, req, kind, action) {
  const client = await db.connect();
  let committed = false;
  try {
    await client.query('BEGIN');
    const { account: viewer } = await lockedIdentity(client, key, req);
    queryInput(req);
    const id = kind === 'article' && action === 'create' ? null :
      urlInteger(req.params[kind === 'article' || action === 'create' ? 'article_id' : 'comment_id'],
        kind === 'article' || action === 'create' ? 'article_id' : 'comment_id');
    const fields = action === 'visibility' ? ['visibility', 'version'] :
      kind === 'article' ? ['title', 'body', ...(action === 'edit' ? ['version'] : [])] :
        ['body', ...(action === 'edit' ? ['version'] : [])];
    const body = bodyInput(req, fields);

    if (action === 'create') {
      // 留言新增先鎖父文章，確保文章下架與留言寫入只有一個先完成。
      if (kind === 'comment') {
        const parent = await client.query('SELECT id,author_id,visibility FROM articles WHERE id=$1 FOR UPDATE', [id]);
        readable(parent.rows[0], null, viewer);
        if (parent.rows[0].visibility === 'hidden') throw new ApiError('E0003');
      }
      const now = (await client.query('SELECT clock_timestamp() AS now')).rows[0].now;
      if (kind === 'article') await client.query(`INSERT INTO articles
        (author_id,title,body,visibility,restore_control,version,created_backend,created_at,updated_at)
        VALUES ($1,$2,$3,'visible',NULL,1,'fastify',$4,$4)`, [viewer.id, body.title, body.body, now]);
      else await client.query(`INSERT INTO comments
        (article_id,author_id,body,visibility,restore_control,version,created_backend,created_at,updated_at)
        VALUES ($1,$2,$3,'visible',NULL,1,'fastify',$4,$4)`, [id, viewer.id, body.body, now]);
    } else {
      let parent = null;
      // 留言先找父 ID，再依固定文章→留言順序鎖列；鎖後重查關聯。
      if (kind === 'comment') {
        const parentId = await client.query('SELECT article_id FROM comments WHERE id=$1', [id]);
        if (parentId.rows.length) {
          const locked = await client.query('SELECT id,visibility FROM articles WHERE id=$1 FOR UPDATE', [parentId.rows[0].article_id]);
          parent = locked.rows[0];
        }
      }
      const table = kind === 'article' ? 'articles' : 'comments';
      const found = await client.query(`SELECT * FROM ${table} WHERE id=$1 FOR UPDATE`, [id]);
      const row = found.rows[0];
      if (kind === 'comment' && (!row || !parent || Number(row.article_id) !== Number(parent.id))) throw new ApiError('E0004');
      readable(row, parent, viewer);
      if (Number(viewer.id) !== Number(row.author_id) && (action === 'edit' || viewer.role !== 'admin') ||
          kind === 'comment' && action === 'edit' && parent.visibility === 'hidden') throw new ApiError('E0003');
      let changes;
      if (action === 'edit') changes = kind === 'article' ? { title: body.title, body: body.body } : { body: body.body };
      else {
        // 恢復權先於版本確認；管理員接管他人下架內容時才轉成 admin 控制。
        if (body.visibility === 'visible' && row.visibility === 'hidden' &&
            viewer.role !== 'admin' && row.restore_control !== 'author') throw new ApiError('E0003');
        let control = body.visibility === 'visible' ? null : row.restore_control || 'author';
        if (body.visibility === 'hidden' && viewer.role === 'admin' && Number(viewer.id) !== Number(row.author_id)) control = 'admin';
        changes = { visibility: body.visibility, restore_control: control };
      }
      await saveChanges(client, kind, row, body.version, changes);
    }
    await client.query('COMMIT'); committed = true;
  } finally {
    if (!committed) { try { await client.query('ROLLBACK'); } catch {} }
    client.release();
  }
}

module.exports = { contentData, pagination, articles, article, comments, mine, comment, write };
