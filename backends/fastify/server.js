'use strict';

const { readFileSync } = require('node:fs');
const { randomUUID } = require('node:crypto');
const Fastify = require('fastify');
const { Pool } = require('pg');
const { ApiError, fieldError, urlInteger, queryInput, getBody, bodyInput } = require('./input');
const { accountData, hashPassword, identity, login, logout } = require('./auth');
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

// loadKey 嚴格讀取持久化的專屬 32-byte 金鑰，避免重啟時悄悄更換登入身分。
function loadKey(path = '/run/secrets/fastify-jwt.key') {
  const raw = readFileSync(path, 'utf8').trim();
  if (!/^[A-Za-z0-9+/]{43}=$/.test(raw)) throw Error('Fastify JWT key must be base64-encoded 32 bytes');
  const key = Buffer.from(raw, 'base64');
  if (key.length !== 32 || key.toString('base64') !== raw) throw Error('Fastify JWT key must be base64-encoded 32 bytes');
  return key;
}

// ok 維持所有成功操作的共同 envelope，不讓框架預設格式進入 API。
function ok(data = {}) {
  return { success: true, message: 'OK', data };
}

// createApp 為本框架註冊原始本文、共同錯誤、CORS 與已核准的 API 操作。
function createApp(db, key, origin = 'http://localhost:8080') {
  const app = Fastify({ logger: false, bodyLimit: 1048576, genReqId: randomUUID,
    // 讓可達的過長 article_id 進入共同欄位驗證，而非提前變成框架 400。
    routerOptions: { maxParamLength: 16384 },
    // 路由器的壞 URL 在一般 hook 前被攔截，這裡補共同 envelope 及追查記錄。
    frameworkErrors: function routerError(error, req, reply) {
      const logId = randomUUID();
      console.log(`IN log_id=${logId} method=${req.method} path=invalid-url`);
      console.log(`EX log_id=${logId} status=400`);
      console.log(`OUT log_id=${logId} status=400`);
      if (req.headers.origin === origin) {
        reply.header('Access-Control-Allow-Origin', origin);
        reply.header('Vary', 'Origin');
        reply.header('Access-Control-Expose-Headers', 'Retry-After');
      }
      return reply.code(400).send({ success: false, message: MESSAGES.E0005[1], error_code: 'E0005', log_id: logId });
    },
  });
  // 登入要先完成共同驗證，故保留原始 JSON 位元組而不採框架的預設解析順序。
  app.removeAllContentTypeParsers();
  // parser 只負責有界收集；業務欄位、JSON 語法與 Content-Type 由 bodyInput 裁決。
  app.addContentTypeParser('*', { parseAs: 'buffer' }, function rawBodyParser(req, body, done) { done(null, body); });

  // onRequest 加入核准來源的 CORS 並拒絕未支援的本文編碼；log 不收個資。
  app.addHook('onRequest', function requestStart(req, reply, done) {
    req.logId = req.id;
    if (req.headers.origin === origin) {
      reply.header('Access-Control-Allow-Origin', origin);
      reply.header('Vary', 'Origin');
      reply.header('Access-Control-Expose-Headers', 'Retry-After');
    }
    console.log(`IN log_id=${req.logId} method=${req.method} path=${req.url.split('?')[0]}`);
    // Fastify 的自訂 parser 不會自行拒絕無法解碼的 Content-Encoding。
    if (req.headers['content-encoding'] && req.headers['content-encoding'].toLowerCase() !== 'identity') {
      return done(new ApiError('E0005', undefined, undefined, 415));
    }
    done();
  });
  // onResponse 共用 log_id 記錄狀態，讓使用者回報的編號能查到請求進出。
  app.addHook('onResponse', function requestEnd(req, reply, done) {
    if (reply.statusCode >= 400) console.log(`EX log_id=${req.logId} status=${reply.statusCode}`);
    console.log(`OUT log_id=${req.logId} status=${reply.statusCode}`);
    done();
  });

  // 預檢不讀身分或本文，僅核准的前端來源能讀取 CORS 標頭。
  app.options('/api/*', function preflight(req, reply) {
    if (req.headers.origin === origin) {
      reply.header('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS');
      reply.header('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept');
    }
    return reply.code(204).send();
  });

  // register 由共用唯一索引裁決跨後端並行，碰撞仍轉為相同欄位錯誤。
  app.post('/api/auth/register', async function register(req, reply) {
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
    return reply.code(201).send(ok());
  });

  // login 在完整驗證後由交易鎖住共用設定與帳號，保留失敗計數的提交語意。
  app.post('/api/auth/login', async function signIn(req) {
    queryInput(req);
    return ok(await login(db, key, bodyInput(req, ['email', 'password'])));
  });

  // logout 先查有效身分再檢查參數，成功提交後才表示目前登入已撤銷。
  app.post('/api/auth/logout', async function signOut(req) {
    const subject = await identity(db, key, req, true);
    queryInput(req);
    bodyInput(req, [], true);
    await logout(db, subject);
    return ok();
  });

  // me 回目前資料庫身分，不重簽憑證或延長原到期時間。
  app.get('/api/me', async function me(req) {
    const subject = await identity(db, key, req, true);
    await getBody(req);
    queryInput(req);
    return ok(accountData(subject.account));
  });

  // articles 明確管理範圍先驗角色，其餘情形以訪客或有效會員查可見清單。
  app.get('/api/articles', async function articles(req) {
    const management = new URLSearchParams(req.raw.url.split('?').slice(1).join('?')).getAll('filters[scope]').includes('management');
    const subject = await identity(db, key, req, management);
    if (management && subject.account.role !== 'admin') throw new ApiError('E0003');
    await getBody(req);
    const query = queryInput(req, ['page', 'per_page', 'filters[scope]']);
    return ok(await content.articles(db, subject?.account, query.page, query.per_page, query['filters[scope]'] === 'management'));
  });

  // article 驗證攜帶的憑證，再讓作者及管理員閱讀可授權的隱藏文章。
  app.get('/api/articles/:article_id', async function article(req) {
    const subject = await identity(db, key, req);
    await getBody(req);
    queryInput(req);
    return ok(await content.article(db, subject?.account, urlInteger(req.params.article_id, 'article_id')));
  });

  // comments 先判父文章可讀，再以同一條件計數及取得留言頁面。
  app.get('/api/articles/:article_id/comments', async function comments(req) {
    const subject = await identity(db, key, req);
    await getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    const id = urlInteger(req.params.article_id, 'article_id');
    return ok(await content.comments(db, subject?.account, id, query.page, query.per_page));
  });

  // 發文先在交易中重驗登入，再由資料庫建立作者、來源與時間。
  app.post('/api/articles', async function createArticle(req, reply) {
    await content.write(db, key, req, 'article', 'create');
    return reply.code(201).send(ok());
  });

  // 文章編修在列鎖下依作者權限與版本更新，避免覆蓋較新的內容。
  app.put('/api/articles/:article_id', async function editArticle(req) {
    await content.write(db, key, req, 'article', 'edit');
    return ok();
  });

  // 個人文章清單依交易外已驗證的登入者查詢，包含自己的下架內容。
  app.get('/api/me/articles', async function myArticles(req) {
    const subject = await identity(db, key, req, true);
    await getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    return ok(await content.mine(db, subject.account, 'article', query.page, query.per_page));
  });

  // 個人留言清單不從父文章擴張讀取權，仍可列出自己下架的留言。
  app.get('/api/me/comments', async function myComments(req) {
    const subject = await identity(db, key, req, true);
    await getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    return ok(await content.mine(db, subject.account, 'comment', query.page, query.per_page));
  });

  // 文章下架或恢復在列鎖下重驗恢復控制者與版本。
  app.put('/api/articles/:article_id/visibility', async function articleVisibility(req) {
    await content.write(db, key, req, 'article', 'visibility');
    return ok();
  });

  // 新留言與父文章可見性共用交易及文章鎖，避免下架競爭。
  app.post('/api/articles/:article_id/comments', async function createComment(req, reply) {
    await content.write(db, key, req, 'comment', 'create');
    return reply.code(201).send(ok());
  });

  // 單則留言只允許作者或管理員閱讀，對其他人先隱藏下架內容。
  app.get('/api/comments/:comment_id', async function comment(req) {
    const subject = await identity(db, key, req, true);
    await getBody(req);
    queryInput(req);
    return ok(await content.comment(db, subject.account, urlInteger(req.params.comment_id, 'comment_id')));
  });

  // 留言編修重驗父文章公開狀態，並在留言鎖下比對版本。
  app.put('/api/comments/:comment_id', async function editComment(req) {
    await content.write(db, key, req, 'comment', 'edit');
    return ok();
  });

  // 留言下架與恢復沿用內容交易，父文章下架不改留言自己的狀態。
  app.put('/api/comments/:comment_id/visibility', async function commentVisibility(req) {
    await content.write(db, key, req, 'comment', 'visibility');
    return ok();
  });

  // members 先驗管理員身分，才檢查 GET Body 與 Query 並讀一般會員清單。
  app.get('/api/members', async function members(req) {
    const subject = await identity(db, key, req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    await getBody(req);
    const query = queryInput(req, ['page', 'per_page', 'filters[keyword]']);
    return ok(await admin.members(db, query.page, query.per_page, query['filters[keyword]']));
  });

  // memberDetail 對非會員目標隱藏存在，避免透過 ID 列出管理員。
  app.get('/api/members/:member_id', async function memberDetail(req) {
    const subject = await identity(db, key, req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    await getBody(req);
    queryInput(req);
    return ok(await admin.memberDetail(db, urlInteger(req.params.member_id, 'member_id')));
  });

  // memberStatus 在驗證後由交易重新查核管理員與會員，再原子停用／撤銷。
  app.put('/api/members/:member_id/status', async function memberStatus(req) {
    const subject = await identity(db, key, req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    queryInput(req);
    const id = urlInteger(req.params.member_id, 'member_id');
    const body = bodyInput(req, ['status']);
    await admin.memberStatus(db, key, req, subject, id, body.status);
    return ok();
  });

  // managementComments 包含下架留言；角色門禁在參數與資料查詢前。
  app.get('/api/comments', async function managementComments(req) {
    const subject = await identity(db, key, req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    await getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    return ok(await admin.managementComments(db, subject.account, query.page, query.per_page));
  });

  // lockoutSettings 僅管理員可讀；四欄從同一筆共用設定取得。
  app.get('/api/settings/login-lockout', async function lockoutSettings(req) {
    const subject = await identity(db, key, req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    await getBody(req);
    queryInput(req);
    return ok(await admin.lockoutSettings(db));
  });

  // updateLockoutSettings 先驗完整四欄，再在設定列鎖下提交完整新值。
  app.put('/api/settings/login-lockout', async function updateLockoutSettings(req) {
    const subject = await identity(db, key, req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    queryInput(req);
    const body = bodyInput(req, ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds']);
    await admin.updateLockoutSettings(db, key, req, body);
    return ok();
  });

  // up 驗連線及共用表讀取權；LIMIT 0 不掃描資料，避免表變大後拖垮資料庫。
  app.get('/up', async function health(req, reply) {
    try {
      await db.query('SELECT 1 FROM accounts,login_sessions,login_failures,login_lockout_settings,articles,comments LIMIT 0');
      return reply.type('text/plain').send('OK');
    } catch {
      return reply.code(503).type('text/plain').send('unavailable');
    }
  });

  // 已實作路徑的錯誤方法是 405，其餘未知路徑是共同 404。
  app.setNotFoundHandler(function notFound(req) {
    const path = req.url.split('?')[0];
    if (['/api/auth/register', '/api/auth/login', '/api/auth/logout', '/api/me', '/api/me/articles', '/api/me/comments', '/api/articles', '/api/members', '/api/comments', '/api/settings/login-lockout'].includes(path) ||
        /^\/api\/articles\/[^/]+(?:\/comments|\/visibility)?$/.test(path) ||
        /^\/api\/comments\/[^/]+(?:\/visibility)?$/.test(path) ||
        /^\/api\/members\/[^/]+(?:\/status)?$/.test(path)) throw new ApiError('E0005', undefined, undefined, 405);
    throw new ApiError('E0004');
  });
  // 框架解析與意外錯誤一律改成安全的共同 envelope，保留可追查 log_id。
  app.setErrorHandler(function handleError(error, req, reply) {
    let failure = error;
    if (!(failure instanceof ApiError)) {
      const status = error.statusCode;
      if (Number.isInteger(status) && status >= 400 && status < 500) failure = new ApiError('E0005', undefined, undefined, status);
      else {
        console.error(`ERROR log_id=${req.logId} err=${error.message}`);
        failure = new ApiError('E9999');
      }
    }
    if (failure.code === 'E0104') console.error(`ERROR log_id=${req.logId} invalid authentication data`);
    const [defaultStatus, message] = MESSAGES[failure.code];
    if (failure.retry) reply.header('Retry-After', String(failure.retry));
    const body = { success: false, message, error_code: failure.code, log_id: req.logId };
    if (failure.errors) body.errors = failure.errors;
    return reply.code(failure.status || defaultStatus).send(body);
  });
  return app;
}

// run 先驗金鑰與資料庫，關閉時讓 Fastify 清理連線後釋放資料庫池。
async function run() {
  const key = loadKey();
  const db = new Pool({ host: process.env.DB_HOST || 'db', port: Number(process.env.DB_PORT || 5432),
    database: process.env.DB_DATABASE || 'articles', user: process.env.DB_USERNAME || 'articles_app',
    password: process.env.DB_PASSWORD, options: '-c timezone=Asia/Taipei',
    max: 20, connectionTimeoutMillis: 10000, statement_timeout: 10000 });
  // 閒置連線因資料庫重啟而報錯時記錄並由 Pool 淘汰，不吞掉請求失敗。
  db.on('error', function idleError(error) { console.error(`ERROR idle database connection: ${error.message}`); });
  const app = createApp(db, key, process.env.CORS_ORIGIN || 'http://localhost:8080');
  try {
    await db.query('SELECT 1');
    await app.listen({ host: '0.0.0.0', port: 8000 });
  } catch (error) {
    await app.close();
    await db.end();
    throw error;
  }
  // SIGTERM／SIGINT 走同一有界關閉路徑，避免新請求使用已釋放的池。
  async function shutdown() {
    // forceClose 在寬限期限後關閉殘留連線，避免停機被長連線無限拖住。
    const timer = setTimeout(function forceClose() { app.server.closeAllConnections(); }, 10000);
    timer.unref();
    try { await app.close(); }
    finally { clearTimeout(timer); await db.end(); }
  }
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);
}

if (require.main === module) {
  // 啟動失敗標成非零結果，避免 Compose 把不可用服務當成功。
  run().catch(function startupError(error) { console.error(`Fastify startup failed: ${error.message}`); process.exitCode = 1; });
}
module.exports = { createApp, loadKey };
