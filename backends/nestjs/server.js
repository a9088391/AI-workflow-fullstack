'use strict';

require('reflect-metadata');
const { readFileSync } = require('node:fs');
const { randomUUID } = require('node:crypto');
const { Pool } = require('pg');
const { NestFactory } = require('@nestjs/core');
const { Controller, Get, Post, Put, Req, Res, HttpCode, Injectable, Inject, Module } = require('@nestjs/common');
const { ApiError, fieldError, urlInteger, queryInput, getBody, bodyInput } = require('./input');
const { accountData, hashPassword, identity, login, logout, adminRead, memberStatus, lockoutValues, lockoutSettings } = require('./auth');
const content = require('./content');

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

// loadKey 嚴格讀取持久化的專屬金鑰，避免重啟悄悄換掉所有登入身分。
function loadKey(path = '/run/secrets/nestjs-jwt.key') {
  const raw = readFileSync(path, 'utf8').trim();
  if (!/^[A-Za-z0-9+/]{43}=$/.test(raw)) throw Error('NestJS JWT key must be base64-encoded 32 bytes');
  const key = Buffer.from(raw, 'base64');
  if (key.length !== 32 || key.toString('base64') !== raw) throw Error('NestJS JWT key must be base64-encoded 32 bytes');
  return key;
}

// ok 讓所有成功操作沿用相同 envelope，避免框架預設回應進入共同 API。
function ok(data = {}) { return { success: true, message: 'OK', data }; }

// ApiService 保存資料庫與本後端金鑰，讓 Nest controller 只負責端點順序與回應。
class ApiService {
  // constructor 由模組提供資料庫池及金鑰，所有端點共用同一組連線設定。
  constructor(db, key) { this.db = db; this.key = key; }

  // register 先檢查已存在帳號，再讓唯一索引裁決跨後端同時註冊。
  async register(body) {
    const exists = await this.db.query('SELECT EXISTS(SELECT 1 FROM accounts WHERE email=$1) AS found', [body.email]);
    if (exists.rows[0].found) throw fieldError('email', '此 Email 已註冊');
    const hash = await hashPassword(body.password);
    try {
      await this.db.query("INSERT INTO accounts (email,password_hash,nickname,role,status) VALUES ($1,$2,$3,'member','active')", [body.email, hash, body.nickname]);
    } catch (error) {
      if (error.code === '23505' && error.constraint === 'accounts_email_key') throw fieldError('email', '此 Email 已註冊');
      throw error;
    }
  }

  // login 在同一交易內完成設定／帳號鎖定、計數與登入身分建立。
  async login(body) { return login(this.db, this.key, body); }

  // identity 驗本後端 JWT、資料庫登入紀錄及目前帳號狀態。
  async identity(req, required = false) { return identity(this.db, this.key, req, required); }

  // logout 在帳號與 session 鎖下撤銷目前登入，只有提交成功才回應成功。
  async logout(subject) { return logout(this.db, subject); }

  // articles 直接於 SQL 套用可見性、排序、分頁與總數。
  async articles(viewer, query) { return content.articles(this.db, viewer, query.page, query.per_page, query['filters[scope]'] === 'management'); }

  // article 讀取單篇文章並依目前身分隱藏不可讀資料。
  async article(viewer, id) { return content.article(this.db, viewer, id); }

  // comments 先查父文章可讀性，再取得授權範圍內的留言頁。
  async comments(viewer, id, query) { return content.comments(this.db, viewer, id, query.page, query.per_page); }

  // mine 由登入身分限制 SQL 分頁，包含自己的隱藏內容。
  async mine(viewer, kind, query) { return content.mine(this.db, viewer, kind, query.page, query.per_page); }

  // comment 讀單則留言時檢查作者與管理員資格，不附帶父文章內容。
  async comment(viewer, id) { return content.comment(this.db, viewer, id); }

  // write 讓內容寫入在同一交易重驗身分及完成版本條件更新。
  async write(req, kind, action) { return content.write(this.db, this.key, req, kind, action); }

  // adminRead 在同一交易重驗管理員資格並完成資料查詢，避免失效身分繼續讀管理資料。
  async adminRead(req, read) { return adminRead(this.db, this.key, req, read); }

  // memberStatus 於帳號及 session 鎖下完成停用與撤銷。
  async memberStatus(req, id, status) { return memberStatus(this.db, this.key, req, id, status); }

  // lockoutSettings 以設定列為第一把鎖完成完整更新及關閉副作用。
  async lockoutSettings(req, body) { return lockoutSettings(this.db, this.key, req, body); }

  // health 查必要共用表的讀取權，讓 Compose 不把空 HTTP socket 當成就緒。
  async health() { await this.db.query('SELECT 1 FROM accounts,login_sessions,login_failures,login_lockout_settings,articles,comments LIMIT 1'); }
}
Injectable()(ApiService);

// AuthController 按共同驗證順序處理註冊、登入、登出與目前帳號。
class AuthController {
  // constructor 接受 Nest 注入的 service，避免 controller 自建連線或金鑰。
  constructor(api) { this.api = api; }

  // register 不沿用舊 JWT，依新帳密驗證後只建立 member 帳號。
  async register(req) {
    queryInput(req);
    await this.api.register(bodyInput(req, ['email', 'password', 'nickname']));
    return ok();
  }

  // login 不沿用舊 JWT，完整驗證本文後才進入鎖定交易。
  async login(req) {
    queryInput(req);
    return ok(await this.api.login(bodyInput(req, ['email', 'password'])));
  }

  // logout 先驗身分，後驗 Query／空本文，成功提交才視為登出。
  async logout(req) {
    const subject = await this.api.identity(req, true);
    queryInput(req);
    bodyInput(req, [], true);
    await this.api.logout(subject);
    return ok();
  }
}
Reflect.defineMetadata('design:paramtypes', [ApiService], AuthController);
Controller('api/auth')(AuthController);

// ReadController 集中公開與必要登入的唯讀端點，避免 Nest 預設轉型改變共同契約。
class ReadController {
  // constructor 共用資料庫 service，讀取時依每次請求重新驗目前帳號。
  constructor(api) { this.api = api; }

  // me 僅回資料庫目前帳號，絕不重簽 JWT 或延長到期。
  async me(req) {
    const subject = await this.api.identity(req, true);
    getBody(req);
    queryInput(req);
    return ok(accountData(subject.account));
  }

  // articles 指定管理範圍時先驗 JWT 與角色，再檢查本文和 Query。
  async articles(req) {
    const management = new URLSearchParams(req.originalUrl.split('?').slice(1).join('?')).getAll('filters[scope]').includes('management');
    if (management) {
      // 管理範圍的驗權、驗參與資料查詢在同一交易完成。
      // readManagement callback 沿用共用文章 SQL 與映射，不在路由另做篩選。
      return ok(await this.api.adminRead(req, async function readManagement(client, viewer) {
        getBody(req);
        const query = queryInput(req, ['page', 'per_page', 'filters[scope]']);
        return content.articles(client, viewer, query.page, query.per_page, true);
      }));
    }
    const subject = await this.api.identity(req);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page', 'filters[scope]']);
    return ok(await this.api.articles(subject?.account, query));
  }

  // article 驗可選 JWT 之後再處理本文、Query、ID 與文章可見性。
  async article(req) {
    const subject = await this.api.identity(req);
    getBody(req);
    queryInput(req);
    return ok(await this.api.article(subject?.account, urlInteger(req.params.article_id, 'article_id')));
  }

  // comments 驗可選 JWT 及欄位後先判父文章可讀，再列留言。
  async comments(req) {
    const subject = await this.api.identity(req);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    const id = urlInteger(req.params.article_id, 'article_id');
    return ok(await this.api.comments(subject?.account, id, query));
  }
}
Reflect.defineMetadata('design:paramtypes', [ApiService], ReadController);
Controller('api')(ReadController);

// AuthorController 統一作者端點的登入、輸入與寫入順序，避免框架預設行為改變契約。
class AuthorController {
  // constructor 共用注入的 service，使內容操作使用同一資料庫與 NestJS 金鑰。
  constructor(api) { this.api = api; }

  // createArticle 在交易中驗身分及本文，再以伺服器欄位建立公開文章。
  async createArticle(req) { await this.api.write(req, 'article', 'create'); return ok(); }

  // editArticle 依作者權限及版本條件修改文章，保留同值時的時間與版本。
  async editArticle(req) { await this.api.write(req, 'article', 'edit'); return ok(); }

  // articleVisibility 依作者或管理員的恢復權轉換單篇文章狀態。
  async articleVisibility(req) { await this.api.write(req, 'article', 'visibility'); return ok(); }

  // myArticles 先驗登入與 GET 本文，再查只屬於自己的文章分頁。
  async myArticles(req) {
    const subject = await this.api.identity(req, true);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    return ok(await this.api.mine(subject.account, 'article', query));
  }

  // myComments 只查自己的留言與所屬 ID，即使父文章隱藏也不洩漏父內容。
  async myComments(req) {
    const subject = await this.api.identity(req, true);
    getBody(req);
    const query = queryInput(req, ['page', 'per_page']);
    return ok(await this.api.mine(subject.account, 'comment', query));
  }

  // createComment 在父文章列鎖下確認仍公開，再建立自己的留言。
  async createComment(req) { await this.api.write(req, 'comment', 'create'); return ok(); }

  // comment 依登入者身分及隱藏狀態讀單則留言，不授予父文章讀取權。
  async comment(req) {
    const subject = await this.api.identity(req, true);
    getBody(req);
    queryInput(req);
    return ok(await this.api.comment(subject.account, urlInteger(req.params.comment_id, 'comment_id')));
  }

  // editComment 在父文章公開時才允許作者依版本條件修改留言。
  async editComment(req) { await this.api.write(req, 'comment', 'edit'); return ok(); }

  // commentVisibility 保留父文章自己的狀態，僅修改留言個別下架與恢復權。
  async commentVisibility(req) { await this.api.write(req, 'comment', 'visibility'); return ok(); }
}
Reflect.defineMetadata('design:paramtypes', [ApiService], AuthorController);
Controller('api')(AuthorController);

// AdministrationController 集中管理路由的角色優先驗證，避免輸入或目標狀態洩漏給會員。
class AdministrationController {
  // constructor 共用 service 與資料庫交易接縫，所有管理請求使用相同身分規則。
  constructor(api) { this.api = api; }

  // members 在交易中驗權後才讀 GET 本文及搜尋 Query。
  async members(req) {
    // readMembers callback 在持有有效身分鎖的期間交給共用 SQL 處理搜尋。
    return ok(await this.api.adminRead(req, async function readMembers(client) {
      getBody(req);
      const query = queryInput(req, ['page', 'per_page', 'filters[keyword]']);
      return content.members(client, query.page, query.per_page, query['filters[keyword]']);
    }));
  }

  // member 先驗管理身分與輸入，管理員目標同不存在資料回 404。
  async member(req) {
    // readMember callback 只查核准的一般會員欄位。
    return ok(await this.api.adminRead(req, async function readMember(client) {
      getBody(req);
      queryInput(req);
      return content.member(client, urlInteger(req.params.member_id, 'member_id'));
    }));
  }

  // status 先做端點角色與格式驗證，寫入交易仍重新驗目前管理身分。
  async status(req) {
    const subject = await this.api.identity(req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    queryInput(req);
    const id = urlInteger(req.params.member_id, 'member_id');
    const body = bodyInput(req, ['status']);
    await this.api.memberStatus(req, id, body.status);
    return ok();
  }

  // comments 包含文章下架及個別下架留言，以管理員目前身分映射恢復權。
  async comments(req) {
    // readComments callback 避免在路由逐筆查作者或父文章。
    return ok(await this.api.adminRead(req, async function readComments(client, viewer) {
      getBody(req);
      const query = queryInput(req, ['page', 'per_page']);
      return content.managementComments(client, viewer, query.page, query.per_page);
    }));
  }

  // settings 僅讀共用設定四欄，先驗目前管理身分與 GET 格式。
  async settings(req) {
    // readSettings callback 在交易內讀共用設定並轉為固定公開型態。
    return ok(await this.api.adminRead(req, async function readSettings(client) {
      getBody(req);
      queryInput(req);
      const found = await client.query('SELECT enabled,window_minutes,failure_threshold,lock_seconds FROM login_lockout_settings WHERE id=1');
      if (!found.rows.length) throw new ApiError('E0104');
      return lockoutValues(found.rows[0]);
    }));
  }

  // updateSettings 要求四欄完整驗證；交易內重驗並只在實際關閉時清鎖。
  async updateSettings(req) {
    const subject = await this.api.identity(req, true);
    if (subject.account.role !== 'admin') throw new ApiError('E0003');
    queryInput(req);
    const body = bodyInput(req, ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds']);
    await this.api.lockoutSettings(req, body);
    return ok();
  }
}
Reflect.defineMetadata('design:paramtypes', [ApiService], AdministrationController);
Controller('api')(AdministrationController);

// HealthController 讓 Compose 健康檢查觀察 SQL 依賴，而不洩漏系統錯誤。
class HealthController {
  // constructor 共用 service 的資料庫池，避免健康檢查另開無界連線。
  constructor(api) { this.api = api; }

  // up 只有共用表可讀才回 200；資料庫失效時明確回 503。
  async up(res) {
    try { await this.api.health(); return res.type('text/plain').send('OK'); }
    catch { return res.status(503).type('text/plain').send('unavailable'); }
  }
}
Reflect.defineMetadata('design:paramtypes', [ApiService], HealthController);
Controller()(HealthController);

// route 在 JavaScript 原始碼套用 Nest 路由與參數 decorator，保留真正的 controller 管線。
function route(controller, name, decorator, path, responseCode) {
  const target = controller.prototype;
  decorator(path)(target, name, Object.getOwnPropertyDescriptor(target, name));
  if (responseCode) HttpCode(responseCode)(target, name, Object.getOwnPropertyDescriptor(target, name));
  (controller === HealthController ? Res() : Req())(target, name, 0);
}
route(AuthController, 'register', Post, 'register');
route(AuthController, 'login', Post, 'login', 200);
route(AuthController, 'logout', Post, 'logout', 200);
route(ReadController, 'me', Get, 'me');
route(ReadController, 'articles', Get, 'articles');
route(ReadController, 'article', Get, 'articles/:article_id');
route(ReadController, 'comments', Get, 'articles/:article_id/comments');
route(AuthorController, 'createArticle', Post, 'articles', 201);
route(AuthorController, 'editArticle', Put, 'articles/:article_id');
route(AuthorController, 'articleVisibility', Put, 'articles/:article_id/visibility');
route(AuthorController, 'myArticles', Get, 'me/articles');
route(AuthorController, 'myComments', Get, 'me/comments');
route(AuthorController, 'createComment', Post, 'articles/:article_id/comments', 201);
route(AuthorController, 'comment', Get, 'comments/:comment_id');
route(AuthorController, 'editComment', Put, 'comments/:comment_id');
route(AuthorController, 'commentVisibility', Put, 'comments/:comment_id/visibility');
route(AdministrationController, 'members', Get, 'members');
route(AdministrationController, 'member', Get, 'members/:member_id');
route(AdministrationController, 'status', Put, 'members/:member_id/status');
route(AdministrationController, 'comments', Get, 'comments');
route(AdministrationController, 'settings', Get, 'settings/login-lockout');
route(AdministrationController, 'updateSettings', Put, 'settings/login-lockout');
route(HealthController, 'up', Get, 'up');

// createApp 組裝 Nest 模組、原始本文、共同 CORS 與錯誤回應；測試可注入隔離 DB。
async function createApp(db, key, origin = 'http://localhost:8080') {
  class AppModule {}
  Module({ controllers: [AuthController, ReadController, AuthorController, AdministrationController, HealthController],
    providers: [{ provide: ApiService,
      // useFactory 將本次應用的池與金鑰注入 Nest service，避免每個 controller 重建。
      useFactory: () => new ApiService(db, key) }] })(AppModule);
  const app = await NestFactory.create(AppModule, { bodyParser: false, logger: false });

  // 每次請求先產生追查 ID 與 CORS，並在完成時記錄同一 ID 的狀態。
  app.use(function requestStart(req, res, next) {
    req.logId = randomUUID();
    if (req.headers.origin === origin) {
      res.setHeader('Access-Control-Allow-Origin', origin);
      res.setHeader('Vary', 'Origin');
      res.setHeader('Access-Control-Expose-Headers', 'Retry-After');
    }
    console.log(`IN log_id=${req.logId} method=${req.method} path=${req.originalUrl.split('?')[0]}`);
    res.once('finish', function requestEnd() {
      if (res.statusCode >= 400) console.log(`EX log_id=${req.logId} status=${res.statusCode}`);
      console.log(`OUT log_id=${req.logId} status=${res.statusCode}`);
    });
    // 預檢只回核准來源，尚未執行身分或本文檢查。
    if (req.method === 'OPTIONS' && req.originalUrl.startsWith('/api/')) {
      if (req.headers.origin === origin) {
        res.setHeader('Access-Control-Allow-Methods', 'GET, POST, PUT, OPTIONS');
        res.setHeader('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept');
      }
      return res.status(204).end();
    }
    next();
  });
  // Express raw parser 限制本文大小，讓 JSON/GET/空本文由共同驗證器依順序裁決。
  app.useBodyParser('raw', {
    // 所有方法與 Content-Type 都交由共同驗證器裁決，避免框架先改寫本文。
    type: () => true, limit: 1048576,
  });

  // Nest 的例外過濾器統一安全文案，保留解析錯誤、方法及未知路徑差別。
  app.useGlobalFilters({ catch(error, host) {
    const req = host.switchToHttp().getRequest();
    const res = host.switchToHttp().getResponse();
    let failure = error;
    if (!(failure instanceof ApiError)) {
      const status = error.getStatus?.() || error.statusCode || error.status;
      if (status === 404) {
        const path = req.originalUrl.split('?')[0];
        const known = ['/api/auth/register', '/api/auth/login', '/api/auth/logout', '/api/me', '/api/articles',
          '/api/me/articles', '/api/me/comments', '/api/members', '/api/comments', '/api/settings/login-lockout'].includes(path) ||
          /^\/api\/articles\/[^/]+(?:\/comments|\/visibility)?$/.test(path) ||
          /^\/api\/comments\/[^/]+(?:\/visibility)?$/.test(path) ||
          /^\/api\/members\/[^/]+(?:\/status)?$/.test(path);
        failure = new ApiError(known ? 'E0005' : 'E0004', undefined, undefined, known ? 405 : 404);
      } else if (Number.isInteger(status) && status >= 400 && status < 500) failure = new ApiError('E0005', undefined, undefined, status);
      else {
        console.error(`ERROR log_id=${req.logId} err=${error.message}`);
        failure = new ApiError('E9999');
      }
    }
    if (failure.code === 'E0104') console.error(`ERROR log_id=${req.logId} invalid authentication data`);
    const [defaultStatus, message] = MESSAGES[failure.code];
    if (failure.retry) res.setHeader('Retry-After', String(failure.retry));
    const body = { success: false, message, error_code: failure.code, log_id: req.logId || randomUUID() };
    if (failure.errors) body.errors = failure.errors;
    res.status(failure.status || defaultStatus).json(body);
  } });
  await app.init();
  return app;
}

// run 先驗金鑰與 DB，再啟動 Nest；停止時有界關閉 HTTP 與資料庫池。
async function run() {
  const key = loadKey();
  const db = new Pool({ host: process.env.DB_HOST || 'db', port: Number(process.env.DB_PORT || 5432),
    database: process.env.DB_DATABASE || 'articles', user: process.env.DB_USERNAME || 'articles_app',
    password: process.env.DB_PASSWORD, options: '-c timezone=Asia/Taipei',
    max: 20, connectionTimeoutMillis: 10000, statement_timeout: 10000 });
  // 閒置 DB 連線錯誤由 pool 記錄，不能被當成下一個請求的成功。
  db.on('error', function idleError(error) { console.error(`ERROR idle database connection: ${error.message}`); });
  let app;
  try {
    await db.query('SELECT 1');
    app = await createApp(db, key, process.env.CORS_ORIGIN || 'http://localhost:8080');
    await app.listen(8000, '0.0.0.0');
  } catch (error) {
    if (app) await app.close();
    await db.end();
    throw error;
  }
  // shutdown 先停 HTTP 並給進行中的請求寬限，再釋放池。
  async function shutdown() {
    const timer = setTimeout(function forceClose() { app.getHttpServer().closeAllConnections(); }, 10000);
    timer.unref();
    try { await app.close(); }
    finally { clearTimeout(timer); await db.end(); }
  }
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);
}

if (require.main === module) {
  // 啟動失敗以非零狀態回報 Compose，避免未就緒的服務看似成功。
  run().catch(function startupError(error) { console.error(`NestJS startup failed: ${error.message}`); process.exitCode = 1; });
}
module.exports = { createApp, loadKey };
