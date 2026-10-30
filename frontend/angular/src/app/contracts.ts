export const BACKEND_KEY = 'article-collection.backend.v1';
export const SESSION_KEY = 'article-collection.auth.v1';
export const BACKENDS = {
  laravel: ['Laravel', 'http://localhost:8081'], symfony: ['Symfony', 'http://localhost:8082'],
  'aspnet-core': ['ASP.NET Core Controllers', 'http://localhost:8083'], django: ['Django', 'http://localhost:8084'],
  flask: ['Flask', 'http://localhost:8085'], fastapi: ['FastAPI', 'http://localhost:8086'],
  gin: ['Gin', 'http://localhost:8087'], echo: ['Echo', 'http://localhost:8088'],
  fiber: ['Fiber', 'http://localhost:8089'], express: ['Express', 'http://localhost:8090'],
  fastify: ['Fastify', 'http://localhost:8091'], nestjs: ['NestJS', 'http://localhost:8092'],
} as const;
export const FRONTENDS = { vue: 'Vue 3＋Vite', nuxt: 'Nuxt', react: 'React＋Vite', next: 'Next.js', angular: 'Angular', sveltekit: 'SvelteKit' } as const;
export type Session = { backend: string; accessToken: string; expiresAt: string };
export type Account = { id: number; email: string; nickname: string; role: 'member' | 'admin'; status: 'active'; created_at: string };
export type Article = { id: number; title: string; body: string; author: { id: number; nickname: string }; created_at: string; created_backend: keyof typeof BACKENDS; visibility: 'visible' | 'hidden'; version: number; can_restore: boolean };
export type Comment = { id: number; body: string; author: { id: number; nickname: string }; article_id: number; created_at: string; created_backend: keyof typeof BACKENDS; visibility: 'visible' | 'hidden'; version: number; can_restore: boolean };
export type Page<T> = { list: T[]; pagination: { page: number; per_page: number; total: number; total_pages: number } };
export type Problem = { message: string; code?: string; errors?: Record<string, string[]>; logId?: string };

// 認證只接受完整成功 envelope，避免破損回應提前保存登入或宣稱登出。
export function isAuthSuccess(status: number, body: any, expected: number): boolean {
  return status === expected && body?.success === true && body.message === 'OK'
    && body.data !== null && typeof body.data === 'object' && !Array.isArray(body.data);
}

// 本人資料須有有效識別、角色與啟用狀態，才可開放會員導覽及受保護操作。
export function isAccount(value: any): value is Account {
  return value && Number.isSafeInteger(value.id) && value.id > 0 && typeof value.email === 'string'
    && typeof value.nickname === 'string' && (value.role === 'member' || value.role === 'admin')
    && value.status === 'active' && typeof value.created_at === 'string' && timestamp.test(value.created_at);
}

// JWT、Bearer、本人及原到期格式均齊全，才能保存同分頁共用的三欄登入資料。
export function isLogin(value: any): value is { access_token: string; token_type: 'Bearer'; expires_at: string; user: Account } {
  return value && typeof value.access_token === 'string' && value.access_token.length > 0
    && value.token_type === 'Bearer' && typeof value.expires_at === 'string' && timestamp.test(value.expires_at)
    && isAccount(value.user);
}

// 錯誤明細先驗訊息陣列，避免不完整錯誤被當作確定撤權或讓表單摘要中斷。
export function isAuthProblem(body: any): boolean {
  return body?.success === false && typeof body.message === 'string' && typeof body.error_code === 'string'
    && typeof body.log_id === 'string' && (body.errors === undefined || body.errors !== null
      && typeof body.errors === 'object' && !Array.isArray(body.errors) && Object.values(body.errors).every(
        // 每欄都必須可安全顯示為字串陣列，保留 API 未繪製欄位的完整訊息。
        (errors) => Array.isArray(errors) && errors.every(
          // 摘要只顯示文字，不接受巢狀物件或可執行內容。
          (message) => typeof message === 'string',
        ),
      ));
}

const bases: string[] = Object.values(BACKENDS).map(
  // 儲存與送出請求只接受固定清單內的 API 基底。
  (entry) => entry[1],
);
const timestamp = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/;
const blank = /^[\u0009-\u000d\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;
const messages: Record<string, string> = {
  E0001: '欄位資料不正確，請確認後再送出', E0002: '登入已逾期，請重新登入',
  E0003: '沒有權限使用此功能', E0004: '找不到指定資料或 API',
  E0101: '帳號或密碼錯誤', E0102: '此帳號已停用，請聯絡系統管理員',
  E0105: '登入失敗次數過多，請稍後再試', E9999: '系統暫時無法處理，請稍後再試',
  E0201: '內容已更新，請確認最新內容後再送出',
};

// 只信任專案固定的 API 入口，避免儲存值讓 JWT 傳到任意網址。
export function readBackend(storage: Storage): string {
  try { const value = storage.getItem(BACKEND_KEY); return value && bases.includes(value) ? value : BACKENDS.laravel[1]; }
  catch { return BACKENDS.laravel[1]; }
}

// 共用三欄登入資料先驗型態與後端；真正權限仍由 /api/me 重驗。
export function readSession(storage: Storage): Session | null {
  try {
    const raw = storage.getItem(SESSION_KEY);
    if (!raw) return null;
    const selected = storage.getItem(BACKEND_KEY);
    if (selected !== null && !bases.includes(selected)) { storage.removeItem(SESSION_KEY); return null; }
    const value: unknown = JSON.parse(raw);
    if (value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === 3) {
      const session = value as Session;
      if (session.backend === readBackend(storage) && typeof session.accessToken === 'string' && session.accessToken
        && typeof session.expiresAt === 'string' && timestamp.test(session.expiresAt)) return session;
    }
    storage.removeItem(SESSION_KEY);
  } catch { try { storage.removeItem(SESSION_KEY); } catch { /* 被拒絕儲存時仍可公開閱讀。 */ } }
  return null;
}

// 只刪除發出請求時持有的憑證，避免舊 401 清掉新的登入。
export function clearSessionIfOwned(storage: Storage, sent: Session): boolean {
  try {
    const current = readSession(storage);
    if (!current || current.backend !== sent.backend || current.accessToken !== sent.accessToken || current.expiresAt !== sent.expiresAt) return false;
    storage.removeItem(SESSION_KEY);
    return true;
  } catch { return false; }
}

// 原始到期時間不改寫，讓跨前端讀到同一次登入而非重新簽發。
export function saveSession(storage: Storage, session: Session): boolean {
  if (!bases.includes(session.backend) || !session.accessToken || !timestamp.test(session.expiresAt)) return false;
  storage.setItem(BACKEND_KEY, session.backend);
  storage.setItem(SESSION_KEY, JSON.stringify(session));
  return true;
}

// 頁碼與 ID 僅接受可安全表示的正整數；無效頁碼回第一頁。
export function positiveNumber(value: string | null): number | null {
  if (!value || !/^[0-9]+$/.test(value)) return null;
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

// 編輯前先按 API 的碼點長度與純文字規則提示；伺服器仍負責最後授權與驗證。
export function validateContent(body: string, title?: string): Record<string, string[]> {
  const errors: Record<string, string[]> = {};
  // 前端只提示 Unicode 長度與明顯無效文字，實際權限與驗證仍以後端為準。
  const invalid = (value: string, max: number) => [...value].length < 1 || [...value].length > max || blank.test(value) || value.includes('\0');
  if (title !== undefined && invalid(title, 200)) errors['title'] = ['標題須為 1–200 個字，且不可全為空白或含不支援字元'];
  if (invalid(body, title === undefined ? 2000 : 20000)) errors['body'] = [`內文須為 1–${title === undefined ? '2,000' : '20,000'} 個字，且不可全為空白或含不支援字元`];
  return errors;
}

// 返回目標限制在已知頁面及合法 ID，防止登入後跳到外站。
export function validReturn(path: string): boolean {
  if (['/angular/', '/angular/articles/new', '/angular/me/articles', '/angular/me/comments', '/angular/admin/members', '/angular/admin/articles', '/angular/admin/comments', '/angular/admin/settings/login-lockout'].includes(path)) return true;
  const match = path.match(/^\/angular\/(?:(?:articles|comments)\/([0-9]+)(?:\/edit)?|admin\/members\/([0-9]+))$/);
  return Boolean(match && positiveNumber(match[1] || match[2]) !== null);
}

// SvelteKit 已可接收相同語意的公開、作者及管理頁；沿用返回白名單驗證 ID。
export function supportsSvelteRoute(path: string): boolean {
  return path === '/angular/login' || path === '/angular/register' || validReturn(path);
}

// 切換版本只搬已知語意路徑；登入頁的返回目標也映射到目標基底路徑。
export function frontendTarget(path: string, frontend: keyof typeof FRONTENDS, returnTo = '/angular/'): string {
  if (path === '/angular/login') {
    const target = validReturn(returnTo) ? frontendTarget(returnTo, frontend) : `/${frontend}/`;
    return `/${frontend}/login${target !== `/${frontend}/` ? `?return_to=${encodeURIComponent(target)}` : ''}`;
  }
  if (path === '/angular/register') return `/${frontend}/register`;
  return validReturn(path) ? path.replace(/^\/angular/, `/${frontend}`) : `/${frontend}/`;
}

// 正規化與前後端一致，只修剪 ASCII 空格並折 ASCII 大寫。
export function normalizeEmail(value: string): string { return value.replace(/^ +| +$/g, '').replace(/[A-Z]/g,
  // 僅折 ASCII 大寫，避免改變後端不會改寫的其他字元。
  (letter) => letter.toLowerCase(),
); }

// 用專案簡化 Email 格式先回饋明確錯誤，後端仍決定唯一性與最終結果。
export function validEmail(value: string): boolean {
  if (value.length > 254 || !/^[\x00-\x7f]+$/.test(value)) return false;
  const parts = value.split('@');
  if (parts.length !== 2) return false;
  const [local, domain] = parts;
  if (!local || local.length > 64 || !/^[a-z0-9._%+-]+$/.test(local) || local.startsWith('.') || local.endsWith('.') || local.includes('..')) return false;
  const labels = domain.split('.');
  return labels.length >= 2 && labels.every(
    // 每段網域標籤須符合長度與 ASCII 字元限制。
    (label) => label.length <= 63 && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label),
  ) && /^[a-z]{2,63}$/.test(labels.at(-1)!);
}

// 不裁切密碼；暱稱保留原文，只排除明確不合法的長度、全空白與 NUL。
export function validateAccount(emailInput: string, password: string, nickname?: string): { email: string; errors: Record<string, string[]> } {
  const email = normalizeEmail(emailInput);
  const errors: Record<string, string[]> = {};
  if (!validEmail(email)) errors['email'] = ['請輸入符合格式的 Email'];
  if (!/^[A-Za-z0-9]{4,16}$/.test(password)) errors['password'] = ['密碼須為 4–16 碼英文字母或數字'];
  if (nickname !== undefined && ([...nickname].length < 1 || [...nickname].length > 30 || blank.test(nickname) || nickname.includes('\0'))) errors['nickname'] = ['暱稱須為 1–30 個字，且不可全為空白或含不支援字元'];
  return { email, errors };
}

// 只有十進位正整數秒能啟動倒數，無效標頭不得推測鎖定長度。
export function retryAfterSeconds(value: string | null): number | null {
  if (!value || !/^[1-9][0-9]*$/.test(value)) return null;
  const seconds = Number(value);
  return Number.isSafeInteger(seconds) && seconds <= Number.MAX_SAFE_INTEGER / 1000 ? seconds : null;
}

// 依實際時間計算，背景分頁的計時器節流不會延長畫面倒數。
export function countdown(deadline: number, now = Date.now()): string {
  const seconds = Math.max(0, Math.ceil((deadline - now) / 1000));
  if (!seconds) return '';
  // 時分秒固定兩位數，讓倒數讀數不隨數字位數跳動。
  const two = (number: number) => String(number).padStart(2, '0');
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  return hours ? `${two(hours)}:${two(minutes)}:${two(seconds % 60)}` : `${two(minutes)}:${two(seconds % 60)}`;
}

// API 錯誤碼決定既定文案；未知碼才顯示後端安全訊息與追查編號。
export function problem(status: number, body: any): Problem {
  const code = typeof body?.error_code === 'string' ? body.error_code : undefined;
  return {
    message: code && messages[code] ? messages[code] : typeof body?.message === 'string' ? body.message : status === 0 ? '無法連線，請確認目前後端。' : '系統暫時無法處理，請稍後再試',
    code, errors: body?.errors && typeof body.errors === 'object' ? body.errors : undefined,
    logId: typeof body?.log_id === 'string' ? body.log_id : undefined,
  };
}

// 每次請求先檢查固定入口與回應 envelope；不解析 HTML 或把錯誤當空列表。
export async function apiRequest(backend: string, path: string, options: { method?: string; body?: object; token?: string; signal?: AbortSignal } = {}) {
  if (!bases.includes(backend) || !path.startsWith('/api/')) throw new Error('Invalid API target');
  const response = await fetch(backend + path, {
    method: options.method || 'GET', signal: options.signal,
    headers: { Accept: 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}) },
    ...(options.body ? { body: JSON.stringify(options.body) } : {}),
  });
  const body = await response.json().catch(
    // 非 JSON 不符合共用 envelope，交由下方統一判定為失敗。
    () => null,
  );
  if (!body || typeof body !== 'object' || typeof body.success !== 'boolean') throw new Error('Invalid API response');
  return { response, body, retryAfter: response.headers.get('Retry-After') };
}

// API 識別碼與版本須是可精確表示的正整數，避免不同資源被誤認為同一筆。
function isId(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0;
}

// 閱讀前確認會呈現或參與操作的共同欄位，不把殘缺資料當成可操作資源。
function isContent(value: any): boolean {
  return Boolean(value && isId(value.id) && typeof value.body === 'string'
    && value.author && isId(value.author.id) && typeof value.author.nickname === 'string'
    && isId(value.version) && typeof value.created_at === 'string'
    && timestamp.test(value.created_at)
    && typeof value.updated_at === 'string'
    && timestamp.test(value.updated_at)
    && typeof value.created_backend === 'string' && Object.hasOwn(BACKENDS, value.created_backend)
    && (value.visibility === 'visible' || value.visibility === 'hidden') && typeof value.can_restore === 'boolean');
}

// 單篇與父文章共用同形契約；有指定目標時必須與原請求 ID 一致。
export function isReadingArticle(value: any, expectedId?: number): value is Article {
  return isContent(value) && typeof value.title === 'string' && (expectedId === undefined || value.id === expectedId);
}

// 留言須保留父文章識別，詳情不能用其他留言冒充原請求。
export function isReadingComment(value: any, expectedId?: number): value is Comment {
  return isContent(value) && isId(value.article_id) && (expectedId === undefined || value.id === expectedId);
}

// 分頁保留原請求與超頁語意；拒絕錯頁、重複 ID 或不可能的筆數，錯誤不能冒充空資料。
export function isReadingPage<T extends Article | Comment>(value: any, expectedPage: number,
  accepts: (item: unknown) => item is T): value is Page<T> {
  if (!value || !Array.isArray(value.list) || !value.pagination) return false;
  const pagination = value.pagination;
  return Number.isSafeInteger(pagination.page) && pagination.page > 0
    && pagination.page === expectedPage && pagination.per_page === 20
    && Number.isSafeInteger(pagination.total) && pagination.total >= 0
    && pagination.total_pages === Math.ceil(pagination.total / 20)
    && value.list.length <= 20 && value.list.length <= pagination.total
    && (expectedPage <= pagination.total_pages || value.list.length === 0)
    && value.list.every(
      // 只傳資源本身，避免 Array.every 的索引被 guard 誤認為可選的 requested ID。
      (item: unknown) => accepts(item),
    )
    && new Set(value.list.map(
      // 同頁重複 ID 無法作為可靠的列表或 Angular track 依據。
      (item: T) => item.id,
    )).size === value.list.length;
}
