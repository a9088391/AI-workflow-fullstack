'use strict';

const { readFileSync } = require('node:fs');
const { randomUUID } = require('node:crypto');
const express = require('express');
const { Pool } = require('pg');
const { ApiError, fieldError, urlInteger, queryInput, getBody, bodyInput } = require('./input');
const { accountData, hashPassword, identity, login, logout, writeTx } = require('./auth');
const content = require('./content');
const admin = require('./admin');

const MESSAGES = {
  E0001: [422, '欄位資料不正確，請確認後再送出'],
  E0002: [401, '登入已逾期，請重新登入'],
  E0003: [403, '沒有權限使用此功能'],
  E0004: [404, '找不到指定資料或 API'],
  E0005: [400, '系統暫時無法處理，請稍後再試'],
  E0101: [401, '帳號或密碼錯誤'],
  E0102: [403, '此帳號已停用，請聯絡系統管理員'],
  E0104: [500, '後台認證設定異常，請聯絡系統管理員'],
  E0105: [429, '登入失敗次數過多，請稍後再試'],
  E0201: [409, '內容已更新，請確認最新內容後再送出'],
  E9999: [500, '系統暫時無法處理，請稍後再試'],
};

// loadKey 要求持久化的專屬 32-byte 金鑰；設定錯誤時拒絕啟動。
function loadKey(path = '/run/secrets/express-jwt.key') {
  const raw = readFileSync(path, 'utf8').trim();
  if (!/^[A-Za-z0-9+/]{43}=$/.test(raw)) throw Error('Express JWT key must be base64-encoded 32 bytes');
  const key = Buffer.from(raw, 'base64');
  if (key.length !== 32 || key.toString('base64') !== raw) throw Error('Express JWT key must be base64-encoded 32 bytes');
  return key;
}

// respondOK 只送共同成功欄位；寫入操作的 data 為空物件。
function respondOK(res, data = {}, status = 200) {
  res.status(status).json({ success: true, message: 'OK', data });
}

// adminSubject 先查目前身分與角色，管理端點不會先解析參數或查目標。
async function adminSubject(db, key, req) {
  const subject = await identity(db, key, req, true);
  if (subject.account.role !== 'admin') throw new ApiError('E0003');
  return subject;
}

// createApp 配置共同記錄、CORS、原始 Body 與全部 22 個共用 API 操作。
function createApp(db, key, origin = 'http://localhost:8080') {
  const app = express();
  app.disable('x-powered-by');
  // 在路由前配置 log_id；錯誤與請求進出以同一識別碼查找，日誌不寫憑證或本文。
  app.use((req, res, next) => {
    req.logId = randomUUID();
    if (req.headers.origin === origin) {
      res.set('Access-Control-Allow-Origin', origin);
      res.set('Vary', 'Origin');
      res.set('Access-Control-Expose-Headers', 'Retry-After');
    }
    console.log(`IN log_id=${req.logId} method=${req.method} path=${req.path}`);
    // finish 只記錄 HTTP 狀態，與入口共用 log_id 且不洩漏 Body 或 token。
    res.on('finish', () => {
      if (res.statusCode >= 400) console.log(`EX log_id=${req.logId} status=${res.statusCode}`);
      console.log(`OUT log_id=${req.logId} status=${res.statusCode}`);
    });
    next();
  });
  // 預檢只對核准來源回 CORS 標頭，不需要登入或讀取本文。
  app.options('/api/*splat', (req, res) => {
    if (req.headers.origin === origin) {
      res.set('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS');
      res.set('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept');
    }
    res.sendStatus(204);
  });
  // 保留 GET 本文原始位元組與 JSON 錯誤分類，避免框架預設解析順序改變契約。
  app.use('/api', express.raw({ type: () => true, limit: '1mb' }));

  // register 由唯一索引裁決跨後端並行重複 Email，成功後仍須自行登入。
  app.post('/api/auth/register', async (req, res) => {
    queryInput(req);
    const body = bodyInput(req, ['email', 'password', 'nickname']);
    const exists = await db.query('SELECT EXISTS(SELECT 1 FROM accounts WHERE email=$1) AS found', [body.email]);
    if (exists.rows[0].found) throw fieldError('email', '此 Email 已註冊');
    const hash = await hashPassword(body.password);
    try {
      await db.query("INSERT INTO accounts (email,password_hash,nickname,role,status) VALUES ($1,$2,$3,'member','active')", [body.email, hash, body.nickname]);
    } catch (error) {
      if (error.code === '23505' && error.constraint === 'accounts_email_key') throw fieldError('email', '此 Email 已註冊');
      throw error;
    }
    respondOK(res, {}, 201);
  });

  // login 先完整驗輸入，交易內才接觸共用鎖定設定與失敗事件。
  app.post('/api/auth/login', async (req, res) => {
    queryInput(req);
    const body = bodyInput(req, ['email', 'password']);
    respondOK(res, await login(db, key, body));
  });

  // logout 先驗目前登入，撤銷成功才回 200，其他登入不受影響。
  app.post('/api/auth/logout', async (req, res) => {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    bodyInput(req, [], true);
    await logout(db, subject);
    respondOK(res);
  });

  // me 讀當下帳號資料，不簽新 JWT 或延長到期。
  app.get('/api/me', async (req, res) => {
    const subject = await identity(db, key, req, true);
    getBody(req);
    queryInput(req);
    respondOK(res, accountData(subject.account));
  });

  // articles 預設公開列表；明確管理範圍先過角色門禁。
  app.get('/api/articles', async (req, res) => {
    // 明確管理範圍先檢查身分及角色，再處理格式錯誤，避免揭露管理內容。
    const management = new URLSearchParams(req.originalUrl.split('?').slice(1).join('?')).getAll('filters[scope]').includes('management');
    const subject = await identity(db, key, req, management);
    if (management && subject.account.role !== 'admin') throw new ApiError('E0003');
    getBody(req);
    const query = queryInput(req, ['page', 'per_page', 'filters[scope]']);
    respondOK(res, await content.articles(db, subject?.account, query.page, query.per_page, query['filters[scope]'] === 'management'));
  });

  // 發文先完成認證與本文格式，再在認證鎖內記錄實際作者及來源。
  app.post('/api/articles', async (req, res) => {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    const body = bodyInput(req, ['title', 'body']);
    // callback 使用交易內重驗的帳號，確保建立者不是等待鎖前的快照。
    await writeTx(db, subject, (client, viewer) => content.createArticle(client, viewer, body));
    respondOK(res, {}, 201);
  });

  // 自己的文章包含已下架項目，採用公開列表相同欄位與排序。
  app.get('/api/me/articles', async (req, res) => {
    const subject = await identity(db, key, req, true);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    respondOK(res, await content.owned(db, subject.account, query.page, query.per_page, true));
  });

  // 自己的留言只回 article_id，不藉下架父文章洩漏其內容。
  app.get('/api/me/comments', async (req, res) => {
    const subject = await identity(db, key, req, true);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    respondOK(res, await content.owned(db, subject.account, query.page, query.per_page, false));
  });

  // article 先查攜帶的憑證，再依作者與管理員身分判 hidden 可讀性。
  app.get('/api/articles/:article_id', async (req, res) => {
    const subject = await identity(db, key, req);
    getBody(req);
    queryInput(req);
    const id = urlInteger(req.params.article_id, 'article_id');
    respondOK(res, await content.article(db, subject?.account, id));
  });

  // 修改文章只接受作者本人；鎖內先判可讀及權限，再比對 version。
  app.put('/api/articles/:article_id', async (req, res) => {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    const id = urlInteger(req.params.article_id, 'article_id');
    const body = bodyInput(req, ['title', 'body', 'version']);
    // callback 在鎖內讀取現況，將權限和 version 裁決與更新綁成一次交易。
    await writeTx(db, subject, (client, viewer) => content.mutate(client, viewer, id, body, true, false));
    respondOK(res);
  });

  // 下架與恢復沿用相同交易裁決，管理員可接管他人的恢復控制權。
  app.put('/api/articles/:article_id/visibility', async (req, res) => {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    const id = urlInteger(req.params.article_id, 'article_id');
    const body = bodyInput(req, ['visibility', 'version']);
    // callback 於同一交易裁決恢復權，防止並行下架覆蓋新控制者。
    await writeTx(db, subject, (client, viewer) => content.mutate(client, viewer, id, body, true, true));
    respondOK(res);
  });

  // comments 先確定父文章可讀，之後才篩選個別下架留言。
  app.get('/api/articles/:article_id/comments', async (req, res) => {
    const subject = await identity(db, key, req);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    const id = urlInteger(req.params.article_id, 'article_id');
    respondOK(res, await content.comments(db, subject?.account, id, query.page, query.per_page));
  });

  // 新增留言先鎖父文章，確認其仍公開後才寫入。
  app.post('/api/articles/:article_id/comments', async (req, res) => {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    const id = urlInteger(req.params.article_id, 'article_id');
    const body = bodyInput(req, ['body']);
    // callback 使用交易中的父文鎖，確保提交前仍可新增留言。
    await writeTx(db, subject, (client, viewer) => content.createComment(client, viewer, id, body));
    respondOK(res, {}, 201);
  });

  // 留言詳情限作者與管理員；對其他人先隱藏不可讀內容的存在。
  app.get('/api/comments/:comment_id', async (req, res) => {
    const subject = await identity(db, key, req, true);
    getBody(req);
    queryInput(req);
    const id = urlInteger(req.params.comment_id, 'comment_id');
    respondOK(res, await content.comment(db, subject.account, id));
  });

  // 留言內文修改鎖定父文章後才鎖留言，防止下架與修改交錯。
  app.put('/api/comments/:comment_id', async (req, res) => {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    const id = urlInteger(req.params.comment_id, 'comment_id');
    const body = bodyInput(req, ['body', 'version']);
    // callback 先鎖父文再鎖留言，與文章下架維持相同順序。
    await writeTx(db, subject, (client, viewer) => content.mutate(client, viewer, id, body, false, false));
    respondOK(res);
  });

  // 留言下架及恢復不改父文章狀態，仍在父文章與留言鎖內裁決。
  app.put('/api/comments/:comment_id/visibility', async (req, res) => {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    const id = urlInteger(req.params.comment_id, 'comment_id');
    const body = bodyInput(req, ['visibility', 'version']);
    // callback 在父文與留言鎖內判權限，避免版本和恢復控制競爭。
    await writeTx(db, subject, (client, viewer) => content.mutate(client, viewer, id, body, false, true));
    respondOK(res);
  });

  // 會員列表僅含一般會員，先驗管理員再解析搜尋與分頁。
  app.get('/api/members', async (req, res) => {
    await adminSubject(db, key, req);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page', 'filters[keyword]']);
    respondOK(res, await admin.members(db, query.page, query.per_page, query['filters[keyword]']));
  });

  // 會員詳情對管理員目標也回 404，不暴露管理員帳號資料。
  app.get('/api/members/:member_id', async (req, res) => {
    await adminSubject(db, key, req);
    getBody(req);
    queryInput(req);
    const id = urlInteger(req.params.member_id, 'member_id');
    respondOK(res, await admin.memberDetail(db, id));
  });

  // 狀態寫入在交易內重驗管理員，停用時一併撤銷舊登入。
  app.put('/api/members/:member_id/status', async (req, res) => {
    const subject = await adminSubject(db, key, req);
    queryInput(req);
    const id = urlInteger(req.params.member_id, 'member_id');
    const body = bodyInput(req, ['status']);
    await admin.memberStatus(db, subject, id, body.status);
    respondOK(res);
  });

  // 管理留言列表包含隱藏留言及隱藏父文的留言，沿用共同內容欄位。
  app.get('/api/comments', async (req, res) => {
    const subject = await adminSubject(db, key, req);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    respondOK(res, await content.managementComments(db, subject.account, query.page, query.per_page));
  });

  // 共用鎖定設定只向管理員回傳契約指定四欄。
  app.get('/api/settings/login-lockout', async (req, res) => {
    await adminSubject(db, key, req);
    getBody(req);
    queryInput(req);
    respondOK(res, await admin.lockoutSettings(db));
  });

  // 設定寫入先驗四欄，再以設定列鎖和帳號鎖裁決關閉副作用。
  app.put('/api/settings/login-lockout', async (req, res) => {
    const subject = await adminSubject(db, key, req);
    queryInput(req);
    const body = bodyInput(req, ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds']);
    await admin.updateLockoutSettings(db, subject, body);
    respondOK(res);
  });

  // 健康檢查驗連線及共用表讀取權；LIMIT 0 不掃描資料，避免表變大後拖垮資料庫。
  app.get('/up', async (req, res) => {
    try {
      await db.query('SELECT 1 FROM accounts,login_sessions,login_failures,login_lockout_settings,articles,comments LIMIT 0');
      res.type('text').send('OK');
    } catch { res.status(503).type('text').send('unavailable'); }
  });
  // 已知路徑但方法錯誤回 405；未知 API 仍由 404 處理。
  for (const path of ['/api/auth/register', '/api/auth/login', '/api/auth/logout', '/api/me', '/api/articles', '/api/me/articles', '/api/me/comments', '/api/articles/:article_id', '/api/articles/:article_id/visibility', '/api/articles/:article_id/comments', '/api/comments', '/api/comments/:comment_id', '/api/comments/:comment_id/visibility', '/api/members', '/api/members/:member_id', '/api/members/:member_id/status', '/api/settings/login-lockout']) {
    // 每個已知路徑共用同一方法錯誤，不把 Express 的預設 HTML 送給前端。
    app.all(path, () => { throw new ApiError('E0005', undefined, undefined, 405); });
  }
  // 未知路徑以共同 404 envelope 呈現。
  app.use((req, res, next) => next(new ApiError('E0004')));
  // Express 5 會把非同步處理器的拒絕送到此處；只回安全的共同錯誤。
  app.use((error, req, res, next) => {
    if (res.headersSent) return next(error);
    let failure = error;
    if (!(failure instanceof ApiError)) {
      // 僅接納框架明示的 4xx；路徑解碼與 Body 解析錯誤不應偽裝成系統故障。
      const status = error.status ?? error.statusCode;
      if (Number.isInteger(status) && status >= 400 && status < 500 &&
          (error.expose === true || error instanceof URIError)) failure = new ApiError('E0005', undefined, undefined, status);
      else {
        console.error(`ERROR log_id=${req.logId} err=${error.message}`);
        failure = new ApiError('E9999');
      }
    }
    if (failure.code === 'E0104') console.error(`ERROR log_id=${req.logId} invalid authentication data`);
    const [defaultStatus, message] = MESSAGES[failure.code];
    if (failure.retry) res.set('Retry-After', String(failure.retry));
    const body = { success: false, message, error_code: failure.code, log_id: req.logId };
    if (failure.errors) body.errors = failure.errors;
    res.status(failure.status || defaultStatus).json(body);
  });
  return app;
}

// run 僅在金鑰與資料庫均可用後開始監聽，關閉時清理池與 socket。
async function run() {
  const key = loadKey();
  const db = new Pool({ host: process.env.DB_HOST || 'db', port: Number(process.env.DB_PORT || 5432),
    database: process.env.DB_DATABASE || 'articles', user: process.env.DB_USERNAME || 'articles_app',
    password: process.env.DB_PASSWORD, max: 20, connectionTimeoutMillis: 10000, statement_timeout: 10000 });
  // 資料庫重啟會使閒置連線報錯；記錄後由 Pool 丟棄該連線，不能讓未處理事件終止服務。
  db.on('error', error => console.error(`ERROR idle database connection: ${error.message}`));
  try { await db.query('SELECT 1'); }
  catch (error) { await db.end(); throw error; }
  const server = createApp(db, key, process.env.CORS_ORIGIN || 'http://localhost:8080').listen(8000);
  // shutdown 停止接新請求，十秒後關閉殘留連線，再釋放資料庫池。
  function shutdown() {
    const timer = setTimeout(() => server.closeAllConnections(), 10000);
    timer.unref();
    server.close(() => { clearTimeout(timer); db.end().catch(error => console.error(`ERROR database shutdown: ${error.message}`)); });
  }
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);
}

if (require.main === module) run().catch(error => { console.error(`Express startup failed: ${error.message}`); process.exitCode = 1; });
module.exports = { createApp, loadKey };
