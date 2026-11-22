export const BACKEND_KEY = 'article-collection.backend.v1';
export const SESSION_KEY = 'article-collection.auth.v1';
export const BASE = '/sveltekit';
export const BACKENDS = {
  laravel: ['Laravel', 'http://localhost:8081'], symfony: ['Symfony', 'http://localhost:8082'],
  'aspnet-core': ['ASP.NET Core Controllers', 'http://localhost:8083'], django: ['Django', 'http://localhost:8084'],
  flask: ['Flask', 'http://localhost:8085'], fastapi: ['FastAPI', 'http://localhost:8086'],
  gin: ['Gin', 'http://localhost:8087'], echo: ['Echo', 'http://localhost:8088'],
  fiber: ['Fiber', 'http://localhost:8089'], express: ['Express', 'http://localhost:8090'],
  fastify: ['Fastify', 'http://localhost:8091'], nestjs: ['NestJS', 'http://localhost:8092'],
  rails: ['Ruby on Rails', 'http://localhost:8093']
};
export const FRONTENDS = {
  vue: 'Vue 3＋Vite', nuxt: 'Nuxt', react: 'React＋Vite', next: 'Next.js', angular: 'Angular', sveltekit: 'SvelteKit'
};

// 後端名稱只從固定清單取得，避免畫面顯示不受信任的儲存字串。
export function backendName(base) {
  for (const entry of Object.values(BACKENDS)) if (entry[1] === base) return entry[0];
  return 'Laravel';
}

const bases = Object.values(BACKENDS).map(
  // 只有固定清單可接收憑證，避免儲存值把 JWT 送到其他網址。
  (entry) => entry[1]
);
const timestamp = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;
const blank = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;

// 共用選擇只接受十二個固定 API 位址；無選擇時依契約使用 Laravel。
export function readBackend(storage) {
  try {
    const selected = storage.getItem(BACKEND_KEY);
    return bases.includes(selected) ? selected : BACKENDS.laravel[1];
  } catch { return BACKENDS.laravel[1]; }
}

// 三欄 session 必須與目前後端吻合；真正身分仍由 /api/me 重驗。
export function readSession(storage) {
  try {
    const raw = storage.getItem(SESSION_KEY);
    if (!raw) return null;
    const selected = storage.getItem(BACKEND_KEY);
    if (selected !== null && !bases.includes(selected)) { storage.removeItem(SESSION_KEY); return null; }
    const session = JSON.parse(raw);
    if (session && !Array.isArray(session) && Object.keys(session).length === 3
      && session.backend === readBackend(storage) && typeof session.accessToken === 'string' && session.accessToken
      && typeof session.expiresAt === 'string' && timestamp.test(session.expiresAt)) return session;
    storage.removeItem(SESSION_KEY);
  } catch {
    try { storage.removeItem(SESSION_KEY); } catch { /* 儲存遭拒時仍可公開閱讀。 */ }
  }
  return null;
}

// 舊回應只可清除自己送出時的憑證，不能抹掉較新的登入。
export function clearSessionIfOwned(storage, sent) {
  try {
    const current = readSession(storage);
    if (!current || current.backend !== sent.backend || current.accessToken !== sent.accessToken
      || current.expiresAt !== sent.expiresAt) return false;
    storage.removeItem(SESSION_KEY);
    return true;
  } catch { return false; }
}

// 保留後端簽發的原到期時間，不因前端切換或 /api/me 重新驗證而延長。
export function saveSession(storage, session) {
  if (!bases.includes(session.backend) || !session.accessToken || expired(session)) return false;
  storage.setItem(BACKEND_KEY, session.backend);
  storage.setItem(SESSION_KEY, JSON.stringify(session));
  return true;
}

// 伺服器使用台北時間字串；轉成明確時區後才比較八小時期限。
export function expired(session, now = Date.now()) {
  const end = Date.parse(session.expiresAt.replace(' ', 'T') + '+08:00');
  return !timestamp.test(session.expiresAt) || !Number.isFinite(end) || end <= now;
}

// URL 的頁碼與 ID 只接受安全範圍內的正整數。
export function positiveNumber(value) {
  if (!value || !/^[0-9]+$/.test(value)) return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

// 返回目標限制為核准的語意頁面與合法 ID，不接受外站或任意路徑。
export function validReturn(path) {
  if (path === `${BASE}/`) return true;
  if ([`${BASE}/articles/new`, `${BASE}/me/articles`, `${BASE}/me/comments`,
    `${BASE}/admin/members`, `${BASE}/admin/articles`, `${BASE}/admin/comments`,
    `${BASE}/admin/settings/login-lockout`].includes(path)) return true;
  const match = path.match(/^\/sveltekit\/(?:(?:articles|comments)\/([0-9]+)(?:\/edit)?|admin\/members\/([0-9]+))$/);
  return Boolean(match && positiveNumber(match[1] || match[2]) !== null);
}

// 切換版本只搬移同類已知頁面與 ID，登入返回目標亦映射到目標基底。
export function frontendTarget(path, frontend, returnTo = `${BASE}/`) {
  if (!Object.hasOwn(FRONTENDS, frontend)) return `${BASE}/`;
  if (path === `${BASE}/login`) {
    const target = validReturn(returnTo) ? frontendTarget(returnTo, frontend) : `/${frontend}/`;
    return `/${frontend}/login${target !== `/${frontend}/` ? `?return_to=${encodeURIComponent(target)}` : ''}`;
  }
  if (path === `${BASE}/register`) return `/${frontend}/register`;
  return validReturn(path) ? path.replace(/^\/sveltekit/, `/${frontend}`) : `/${frontend}/`;
}

// Email 只依共用規則修剪 ASCII 空格並折小寫，不更動密碼與暱稱。
export function normalizeEmail(value) {
  return value.replace(/^ +| +$/g, '').replace(/[A-Z]/g,
    // 僅將 ASCII 大寫字母轉小寫，避免改寫其他 Unicode 字元。
    (letter) => letter.toLowerCase());
}

// 即時提示共用 Email 格式；伺服器仍負責唯一性與最後驗證。
export function validEmail(value) {
  if (value.length > 254 || !/^[\x00-\x7f]+$/.test(value)) return false;
  const parts = value.split('@');
  if (parts.length !== 2) return false;
  const [local, domain] = parts;
  if (!local || local.length > 64 || !/^[a-z0-9._%+-]+$/.test(local)
    || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return false;
  const labels = domain.split('.');
  return labels.length >= 2 && labels.every(
    // 網域標籤須為 1–63 字元，首尾不能是連字號。
    (label) => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label)
  ) && /^[a-z]{2,63}$/.test(labels.at(-1));
}

// 前端先提示帳號欄位錯誤，不以瀏覽器預設 trim 或截斷掩蓋輸入。
export function validateAccount(emailInput, password, nickname) {
  const email = normalizeEmail(emailInput);
  const errors = {};
  if (!validEmail(email)) errors.email = ['請輸入符合格式的 Email'];
  if (!/^[A-Za-z0-9]{4,16}$/.test(password)) errors.password = ['密碼須為 4–16 碼英文字母或數字'];
  if (nickname !== undefined && !isNickname(nickname)) errors.nickname = ['暱稱須為 1–30 個字，且不可全為空白或含不支援字元'];
  return { email, errors };
}

// 只接受 Retry-After 的正整數秒數；缺漏標頭不能猜鎖定時間。
export function retryAfterSeconds(value) {
  if (!value || !/^[1-9][0-9]*$/.test(value)) return null;
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) && seconds <= Number.MAX_SAFE_INTEGER / 1000 ? seconds : null;
}

// 用實際時鐘計算倒數，背景分頁計時器節流不會累積偏差。
export function countdown(deadline, now = Date.now()) {
  const seconds = Math.max(0, Math.ceil((deadline - now) / 1000));
  if (!seconds) return '';
  // 固定位數避免倒數顯示時寬度跳動。
  const two = (number) => String(number).padStart(2, '0');
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return hours ? `${two(hours)}:${two(minutes)}:${two(seconds % 60)}` : `${two(minutes)}:${two(seconds % 60)}`;
}

// 只在結構符合共同列表契約時渲染，避免錯誤資料被當作空列表。
export function isPage(value, page) {
  return value && Array.isArray(value.list) && value.pagination
    && value.pagination.page === page && value.pagination.per_page === 20
    && Number.isSafeInteger(value.pagination.total) && value.pagination.total >= 0
    && value.pagination.total_pages === Math.ceil(value.pagination.total / 20)
    && value.list.length === Math.max(0, Math.min(20, value.pagination.total - (page - 1) * 20));
}

// 暱稱輸入與讀取共用既定長度及字元規則，避免破損身分繞過畫面門禁。
function isNickname(value) {
  return typeof value === 'string' && [...value].length >= 1 && [...value].length <= 30
    && !blank.test(value) && !value.includes('\0');
}

// 本人與管理帳號共用完整欄位驗證；破損的身分資料不能開放受保護畫面。
export function isAccount(value) {
  return value && Number.isSafeInteger(value.id) && value.id > 0
    && typeof value.email === 'string' && validEmail(value.email)
    && isNickname(value.nickname)
    && ['member', 'admin'].includes(value.role) && ['active', 'disabled'].includes(value.status)
    && typeof value.created_at === 'string' && timestamp.test(value.created_at);
}

// 文章與留言都需完整來源、作者、時間與版本，才能作為閱讀及後續更新基準。
function isContentMetadata(value) {
  return value && Number.isSafeInteger(value.id) && value.id > 0
    && value.author && Number.isSafeInteger(value.author.id) && value.author.id > 0
    && isNickname(value.author.nickname) && typeof value.created_at === 'string' && timestamp.test(value.created_at)
    && typeof value.updated_at === 'string' && timestamp.test(value.updated_at)
    && typeof value.created_backend === 'string' && Object.hasOwn(BACKENDS, value.created_backend)
    && Number.isSafeInteger(value.version) && value.version > 0
    && ['visible', 'hidden'].includes(value.visibility) && typeof value.can_restore === 'boolean';
}

// 文章內文按既定純文字契約驗證，不讓不完整資料成為可編輯的伺服器基準。
export function isArticle(value) {
  return isContentMetadata(value) && Object.keys(validateContent(value.title, value.body)).length === 0;
}

// 留言需有其所屬文章與公開作者，避免將破損資料混入文章頁。
export function isComment(value, articleId) {
  return isContentMetadata(value) && Number.isSafeInteger(value.article_id) && value.article_id > 0
    && (articleId === undefined || value.article_id === articleId)
    && Object.keys(validateContent('', value.body, true)).length === 0;
}

// 管理頁只顯示一般會員的已核准欄位，避免把管理員或破損資料當成可停用目標。
export function isMember(value) {
  return isAccount(value) && value.role === 'member';
}

// 四個設定值須完整且在後端契約範圍內，才能成為畫面基準或 PUT 草稿。
export function isLockoutSettings(value) {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 4 && typeof value.enabled === 'boolean'
    && Number.isInteger(value.window_minutes) && value.window_minutes >= 1 && value.window_minutes <= 60
    && Number.isInteger(value.failure_threshold) && value.failure_threshold >= 1 && value.failure_threshold <= 100
    && Number.isInteger(value.lock_seconds) && value.lock_seconds >= 1 && value.lock_seconds <= 86400;
}

// number input 的空值或小數不可直接送出；欄位錯誤留在欄旁與摘要供修正。
export function validateLockoutSettings(value) {
  const errors = {};
  if (typeof value.enabled !== 'boolean') errors.enabled = ['請選擇是否啟用。'];
  for (const [field, maximum] of Object.entries({ window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 })) {
    if (!Number.isInteger(value[field]) || value[field] < 1 || value[field] > maximum)
      errors[field] = [`請輸入 1 到 ${maximum} 的整數。`];
  }
  return errors;
}

const blankContent = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;

// 本地提示沿用共同 Unicode 碼點與空白規則；伺服器仍是最後驗證者。
export function validateContent(title, body, comment = false) {
  const errors = {};
  if (!comment && (typeof title !== 'string' || ![...title].length || [...title].length > 200
    || blankContent.test(title) || title.includes('\0'))) errors.title = ['欄位格式或範圍不正確'];
  if (typeof body !== 'string' || ![...body].length || [...body].length > (comment ? 2000 : 20000)
    || blankContent.test(body) || body.includes('\0')) errors.body = ['欄位格式或範圍不正確'];
  return errors;
}

// 已送出的寫入若沒有確定拒絕，先查證伺服器值，避免重複建立或覆寫。
export function uncertainWrite(failure) {
  return !['E0001', 'E0002', 'E0003', 'E0004', 'E0102', 'E0201'].includes(failure?.code)
    || rejectionStatus[failure.code] !== failure.status;
}

// 只有契約一致的狀態與代碼能證明拒絕；5xx 或破損回應仍可能已寫入。
const rejectionStatus = { E0001: 422, E0002: 401, E0003: 403, E0004: 404,
  E0101: 401, E0102: 403, E0104: 500, E0105: 429, E0201: 409, E9999: 500 };

// 僅回傳 API 已核准的安全錯誤文案與追查碼，網路失敗另由呼叫端處理。
export function problem(response, body) {
  const validErrors = body?.errors && typeof body.errors === 'object' && !Array.isArray(body.errors)
    && Object.values(body.errors).every(
      // 欄位訊息只接受字串陣列，破損驗證回應不能證明寫入已遭拒絕。
      (messages) => Array.isArray(messages) && messages.every(
        // 不將物件強制轉字串，避免表單顯示不可信的欄位結構。
        (message) => typeof message === 'string'
      )
    );
  // E0005 可搭配任一 4xx；保留合法共同文案，不因此把它當成可直接重送的確定拒絕。
  const validStatus = body?.error_code === 'E0005' ? response.status >= 400 && response.status < 500
    : rejectionStatus[body?.error_code] === response.status;
  const valid = body?.success === false && typeof body.message === 'string' && body.message.trim().length > 0
    && validStatus && (body.error_code !== 'E0001' || validErrors);
  return {
    status: response.status,
    code: valid ? body.error_code : '',
    message: valid ? body.message : '回應資料格式不正確，請確認目前後端。',
    errors: valid && body.error_code === 'E0001' ? body.errors : {},
    logId: typeof body?.log_id === 'string' ? body.log_id : '',
    retryAfter: response.headers.get('Retry-After')
  };
}

// 錯誤連同可選取的追查碼呈現，方便本機示範時複製給開發者。
export function describeFailure(failure, fallback) {
  const message = typeof failure?.status === 'number' && typeof failure?.message === 'string'
    ? failure.message : fallback;
  return failure?.logId ? `${message}　追查碼：${failure.logId}` : message;
}

// 業務 API 永遠直連固定後端，不將 /api 改寫到前端基底路徑。
export async function apiRequest(backend, path, { token, method = 'GET', data, signal } = {}) {
  if (!bases.includes(backend) || !path.startsWith('/api/')) throw new Error('不受信任的 API 位置');
  const response = await fetch(`${backend}${path}`, {
    method, signal,
    headers: { Accept: 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(data ? { 'Content-Type': 'application/json' } : {}) },
    ...(data ? { body: JSON.stringify(data) } : {})
  });
  const body = await response.json().catch(
    // 非 JSON 回應仍須呈現為失敗，不能當作成功或空資料。
    () => null
  );
  const login = path === '/api/auth/login';
  const expected = method === 'POST' && !login && path !== '/api/auth/logout' ? 201 : 200;
  const objectData = body?.data && typeof body.data === 'object' && !Array.isArray(body.data);
  // 新增與更新只接受空 data；登入與讀取的欄位由各自的既有 validator 檢查。
  if (response.status !== expected || body?.success !== true || body.message !== 'OK' || !objectData
    || method !== 'GET' && !login && Object.keys(body.data).length !== 0) throw problem(response, body);
  return body.data;
}
