'use strict';

const { ApiError } = require('./input');
const { businessTime } = require('./auth');

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

// owned 以同一組作者條件計數與分頁，包含自己的 hidden 內容而不讀父文章本文。
async function owned(db, viewer, page, perPage, article) {
  const table = article ? 'articles' : 'comments';
  const columns = article ? ARTICLE_COLUMNS : COMMENT_COLUMNS;
  const base = `FROM ${table} JOIN accounts ON accounts.id=${table}.author_id WHERE ${table}.author_id=$1`;
  return pageRows(db, columns, base, [viewer.id], page, perPage, viewer, article);
}

// comment 讓作者與管理員獨立讀取留言；其他會員先判隱藏再判詳情權限。
async function comment(db, viewer, id) {
  const result = await db.query(`SELECT ${COMMENT_COLUMNS},articles.visibility AS parent_visibility FROM comments
    JOIN accounts ON accounts.id=comments.author_id JOIN articles ON articles.id=comments.article_id WHERE comments.id=$1`, [id]);
  if (!result.rows.length) throw new ApiError('E0004');
  const row = result.rows[0];
  if (viewer.role !== 'admin' && Number(viewer.id) !== Number(row.author_id)) {
    if (row.visibility === 'hidden' || row.parent_visibility === 'hidden') throw new ApiError('E0004');
    throw new ApiError('E0003');
  }
  return contentData(row, viewer, false);
}

// createArticle 在認證交易內設定實際來源與相同建立／更新時間，避免用戶端偽造。
async function createArticle(client, viewer, body) {
  await client.query(`WITH t AS (SELECT clock_timestamp() AS value)
    INSERT INTO articles (author_id,title,body,visibility,restore_control,version,created_backend,created_at,updated_at)
    SELECT $1,$2,$3,'visible',NULL,1,'express',t.value,t.value FROM t`, [viewer.id, body.title, body.body]);
}

// createComment 鎖定父文章直到留言提交，使父文章下架與留言建立無法交錯通過。
async function createComment(client, viewer, id, body) {
  const parent = await client.query('SELECT author_id,visibility FROM articles WHERE id=$1 FOR UPDATE', [id]);
  if (!parent.rows.length) throw new ApiError('E0004');
  if (parent.rows[0].visibility === 'hidden') {
    if (viewer.role === 'admin' || Number(viewer.id) === Number(parent.rows[0].author_id)) throw new ApiError('E0003');
    throw new ApiError('E0004');
  }
  await client.query(`WITH t AS (SELECT clock_timestamp() AS value)
    INSERT INTO comments (article_id,author_id,body,visibility,restore_control,version,created_backend,created_at,updated_at)
    SELECT $1,$2,$3,'visible',NULL,1,'express',t.value,t.value FROM t`, [id, viewer.id, body.body]);
}

// mutate 在固定鎖序內裁決可讀性、操作權及版本，只有實際變化才更新資料。
async function mutate(client, viewer, id, body, article, visibility) {
  let row;
  let parentVisibility = 'visible';
  const columns = article ? ARTICLE_COLUMNS : COMMENT_COLUMNS;
  const table = article ? 'articles' : 'comments';
  if (!article) {
    // 先只取父 ID，再鎖父文章與留言；此讀取本身不授予內容權限。
    const found = await client.query('SELECT article_id FROM comments WHERE id=$1', [id]);
    if (!found.rows.length) throw new ApiError('E0004');
    const parentId = found.rows[0].article_id;
    const parent = await client.query('SELECT visibility FROM articles WHERE id=$1 FOR UPDATE', [parentId]);
    if (!parent.rows.length) throw new ApiError('E0004');
    parentVisibility = parent.rows[0].visibility;
    const locked = await client.query(`SELECT ${columns} FROM comments JOIN accounts ON accounts.id=comments.author_id
      WHERE comments.id=$1 FOR UPDATE OF comments`, [id]);
    if (!locked.rows.length || String(locked.rows[0].article_id) !== String(parentId)) throw new ApiError('E0004');
    row = locked.rows[0];
  } else {
    const locked = await client.query(`SELECT ${columns} FROM articles JOIN accounts ON accounts.id=articles.author_id
      WHERE articles.id=$1 FOR UPDATE OF articles`, [id]);
    if (!locked.rows.length) throw new ApiError('E0004');
    row = locked.rows[0];
  }
  const owns = Number(viewer.id) === Number(row.author_id);
  // 隱藏內容的可讀資格先於操作權；可讀的他人公開內容才回 403。
  if (!owns && viewer.role !== 'admin' && (row.visibility === 'hidden' || parentVisibility === 'hidden')) throw new ApiError('E0004');
  if ((!owns && (!visibility || viewer.role !== 'admin')) || (!article && !visibility && parentVisibility === 'hidden')) throw new ApiError('E0003');
  let control = row.restore_control;
  // 管理員再次下架別人的內容會接管恢復權，作者不能以重複下架解除限制。
  if (visibility) {
    if (body.visibility === 'visible' && row.visibility === 'hidden' && viewer.role !== 'admin' && control !== 'author') throw new ApiError('E0003');
    if (body.visibility === 'visible') control = null;
    else if (viewer.role === 'admin' && !owns) control = 'admin';
    else control ??= 'author';
  }
  // 權限已確認後才比版本；stale 同值仍須 409，正確同值則不碰時間欄位。
  if (Number(row.version) !== body.version) throw new ApiError('E0201');
  const changed = visibility ? row.visibility !== body.visibility || row.restore_control !== control :
    article ? row.title !== body.title || row.body !== body.body : row.body !== body.body;
  if (!changed) return;
  // WHERE version 是跨後端保險；來源、作者及建立時間都不在更新集合中。
  let updated;
  if (visibility) {
    updated = await client.query(`UPDATE ${table} SET visibility=$1,restore_control=$2,version=version+1,updated_at=clock_timestamp()
      WHERE id=$3 AND version=$4`, [body.visibility, control, id, row.version]);
  } else if (article) {
    updated = await client.query(`UPDATE articles SET title=$1,body=$2,version=version+1,updated_at=clock_timestamp()
      WHERE id=$3 AND version=$4`, [body.title, body.body, id, row.version]);
  } else {
    updated = await client.query(`UPDATE comments SET body=$1,version=version+1,updated_at=clock_timestamp()
      WHERE id=$2 AND version=$3`, [body.body, id, row.version]);
  }
  if (updated.rowCount !== 1) throw Error('locked content changed before update');
}

module.exports = { contentData, pagination, articles, article, comments, owned, comment, createArticle, createComment, mutate };
