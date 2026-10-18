export const BACKEND_KEY = 'article-collection.backend.v1';
export const SESSION_KEY = 'article-collection.auth.v1';
export const BASE = '/sveltekit';
export const BACKENDS = {
  laravel: ['Laravel', 'http://localhost:8081'], symfony: ['Symfony', 'http://localhost:8082'],
  'aspnet-core': ['ASP.NET Core Controllers', 'http://localhost:8083'], django: ['Django', 'http://localhost:8084'],
  flask: ['Flask', 'http://localhost:8085'], fastapi: ['FastAPI', 'http://localhost:8086'],
  gin: ['Gin', 'http://localhost:8087'], echo: ['Echo', 'http://localhost:8088'],
  fiber: ['Fiber', 'http://localhost:8089'], express: ['Express', 'http://localhost:8090'],
  fastify: ['Fastify', 'http://localhost:8091'], nestjs: ['NestJS', 'http://localhost:8092']
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
  const match = path.match(/^\/sveltekit\/articles\/([0-9]+)$/);
  return Boolean(match && positiveNumber(match[1]) !== null);
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
  if (nickname !== undefined && ([...nickname].length < 1 || [...nickname].length > 30
    || blank.test(nickname) || nickname.includes('\0'))) errors.nickname = ['暱稱須為 1–30 個字，且不可全為空白或含不支援字元'];
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
    && value.pagination.total_pages === Math.ceil(value.pagination.total / 20);
}

// 文章至少須有閱讀與公開作者欄位，才能顯示完整內容。
export function isArticle(value) {
  return value && positiveNumber(String(value.id)) !== null && typeof value.title === 'string'
    && typeof value.body === 'string' && value.author && positiveNumber(String(value.author.id)) !== null
    && typeof value.author.nickname === 'string' && typeof value.created_at === 'string'
    && typeof value.created_backend === 'string' && positiveNumber(String(value.version)) !== null
    && ['visible', 'hidden'].includes(value.visibility) && typeof value.can_restore === 'boolean';
}

// 留言需有其所屬文章與公開作者，避免將破損資料混入文章頁。
export function isComment(value, articleId) {
  return value && positiveNumber(String(value.id)) !== null && value.article_id === articleId
    && typeof value.body === 'string' && value.author && positiveNumber(String(value.author.id)) !== null
    && typeof value.author.nickname === 'string' && typeof value.created_at === 'string'
    && typeof value.created_backend === 'string' && positiveNumber(String(value.version)) !== null
    && ['visible', 'hidden'].includes(value.visibility) && typeof value.can_restore === 'boolean';
}

// 僅回傳 API 已核准的安全錯誤文案與追查碼，網路失敗另由呼叫端處理。
export function problem(response, body) {
  return {
    status: response.status,
    code: typeof body?.error_code === 'string' ? body.error_code : '',
    message: typeof body?.message === 'string' ? body.message : '回應資料格式不正確，請確認目前後端。',
    errors: body?.errors && typeof body.errors === 'object' ? body.errors : {},
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
  if (!response.ok || body?.success !== true) throw problem(response, body);
  return body.data;
}
