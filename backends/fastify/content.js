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

module.exports = { contentData, pagination, articles, article, comments };
