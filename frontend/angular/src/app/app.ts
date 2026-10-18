import { CommonModule } from '@angular/common';
import { Component, HostListener, OnDestroy, OnInit, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  Account, apiRequest, Article, BACKENDS, BACKEND_KEY, clearSessionIfOwned, Comment, countdown,
  FRONTENDS, frontendTarget, normalizeEmail, Page, positiveNumber, problem, Problem,
  readBackend, readSession, retryAfterSeconds, saveSession, Session, SESSION_KEY,
  validReturn, validateAccount,
} from './contracts';

@Component({
  selector: 'app-root',
  imports: [CommonModule, FormsModule],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App implements OnInit, OnDestroy {
  // 固定入口轉為畫面選項，避免把儲存值當成可信 API 網址。
  readonly backends = Object.entries(BACKENDS).map(
    // 保留識別碼、顯示名稱與固定基底供切換及顯示使用。
    ([id, [name, base]]) => ({ id, name, base }),
  );
  // 前端顯示名稱與路徑識別碼一一對應，切換時不推測自由輸入。
  readonly frontends = Object.entries(FRONTENDS).map(
    // 選單使用既有清單的識別碼及名稱。
    ([id, name]) => ({ id, name }),
  );
  readonly path = window.location.pathname === '/angular' ? '/angular/' : window.location.pathname;
  readonly search = window.location.search;
  readonly query = new URLSearchParams(window.location.search);
  readonly encodeURIComponent = encodeURIComponent;
  readonly Object = Object;
  readonly articleId = this.path.match(/^\/angular\/articles\/([0-9]+)$/)?.[1] || null;
  readonly validArticleId = this.articleId && positiveNumber(this.articleId) !== null ? this.articleId : null;
  readonly route = this.path === '/angular/' ? 'list' : this.path === '/angular/login' ? 'login' : this.path === '/angular/register' ? 'register' : this.validArticleId ? 'article' : 'unknown';
  readonly page = positiveNumber(this.query.get('page')) || 1;
  readonly returnTo = validReturn(this.query.get('return_to') || '') ? this.query.get('return_to')! : '/angular/';

  readonly backend = signal<string>(BACKENDS.laravel[1]);
  readonly credential = signal<Session | null>(null);
  readonly account = signal<Account | null>(null);
  readonly authState = signal<'checking' | 'guest' | 'active' | 'expired' | 'disabled' | 'error'>('checking');
  readonly authError = signal<Problem | null>(null);
  readonly notice = signal<Problem | null>(null);
  readonly loading = signal(false);
  readonly contentError = signal<Problem | null>(null);
  readonly articles = signal<Page<Article> | null>(null);
  readonly article = signal<Article | null>(null);
  readonly comments = signal<Page<Comment> | null>(null);
  readonly commentsError = signal<Problem | null>(null);
  readonly commentsLoading = signal(false);
  readonly pending = signal(false);
  readonly fieldErrors = signal<Record<string, string[]>>({});
  readonly lockedTime = signal('');
  readonly lockEnded = signal(false);
  readonly storageProblem = signal(false);
  email = '';
  password = '';
  nickname = '';
  dirty = false;
  private lockDeadline = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private authAbort: AbortController | null = null;
  private readAbort: AbortController | null = null;
  private attempt = 0;
  private ignoreUnload = false;

  // 初次進頁先辨識共用後端與登入，再讀目前頁面，避免私有狀態短暫閃現。
  ngOnInit(): void { void this.initialize(); }

  // 離開頁面時中止唯讀請求與倒數，舊結果不得改寫新頁面。
  ngOnDestroy(): void { this.authAbort?.abort(); this.readAbort?.abort(); this.clearCountdown(); }

  // 表單有未送出文字或請求時保留瀏覽器離開提醒；程式確認後不重複詢問。
  @HostListener('window:beforeunload', ['$event'])
  beforeUnload(event: BeforeUnloadEvent): void {
    if (this.ignoreUnload || (!this.dirty && !this.pending())) return;
    event.preventDefault();
    event.returnValue = '';
  }

  // 同分頁 BFCache 可能保留舊後端與憑證；回復時重載並重新驗證。
  @HostListener('window:pageshow', ['$event'])
  restoreFromCache(event: PageTransitionEvent): void { if (event.persisted) window.location.reload(); }

  // 儲存被禁用仍允許訪客閱讀，但不把憑證留在其他儲存位置。
  private storage(): Storage | null {
    try { return sessionStorage; }
    catch { this.storageProblem.set(true); return null; }
  }

  // 只用目前後端驗證原 JWT；暫時故障保留憑證供手動重試。
  async initialize(): Promise<void> {
    this.authAbort?.abort();
    const controller = new AbortController();
    this.authAbort = controller;
    const storage = this.storage();
    const backend = storage ? readBackend(storage) : BACKENDS.laravel[1];
    const credential = storage ? readSession(storage) : null;
    this.backend.set(backend);
    this.credential.set(credential);
    this.account.set(null);
    this.authError.set(null);
    if (!credential) this.authState.set('guest');
    else {
      this.authState.set('checking');
      try {
        const { response, body } = await apiRequest(backend, '/api/me', { token: credential.accessToken, signal: controller.signal });
        if (controller.signal.aborted || !this.owns(credential)) return;
        if (response.ok && body.success === true && this.isAccount(body.data)) {
          this.account.set(body.data); this.authState.set('active');
        } else {
          const failure = problem(response.status, body);
          if (this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
          else { this.authState.set('error'); this.authError.set(failure); }
        }
      } catch {
        if (controller.signal.aborted) return;
        this.authState.set('error');
        this.authError.set({ message: '暫時無法驗證登入，請確認目前後端後重新驗證。' });
      }
    }
    if (!controller.signal.aborted) await this.readContent();
  }

  // 本人資料至少須有核准的識別、角色與啟用狀態，才顯示登入導覽。
  private isAccount(value: any): value is Account {
    return value && Number.isSafeInteger(value.id) && value.id > 0 && typeof value.email === 'string'
      && typeof value.nickname === 'string' && (value.role === 'member' || value.role === 'admin')
      && value.status === 'active' && typeof value.created_at === 'string';
  }

  // 結果只能影響送出時持有的同一後端與憑證。
  private owns(sent: Session): boolean {
    const current = this.credential();
    return Boolean(current && current.backend === sent.backend && current.accessToken === sent.accessToken && current.expiresAt === sent.expiresAt);
  }

  // 已知失效與停用才清除 JWT；連線或 5xx 不等於登出。
  private isAuthFailure(status: number, failure: Problem): boolean {
    return status === 401 && failure.code === 'E0002' || status === 403 && failure.code === 'E0102';
  }

  // 只有原請求持有的登入會失效；即使瀏覽器拒絕刪除儲存，也不能保留受限內容。
  private invalidate(sent: Session, failure: Problem): void {
    if (!this.owns(sent)) return;
    const storage = this.storage();
    const cleared = Boolean(storage && clearSessionIfOwned(storage, sent));
    this.credential.set(null);
    this.account.set(null);
    this.authState.set(failure.code === 'E0102' ? 'disabled' : 'expired');
    this.authError.set(cleared ? failure : { ...failure, message: `${failure.message}；無法確認本機舊登入資料已清除，請關閉此分頁。` });
    // 憑證失效時清掉可能僅作者可讀的內容，包含原請求已清空畫面的情況，再以訪客身分重讀。
    this.article.set(null); this.comments.set(null);
    void this.readContent();
  }

  // 用新 AbortController 讀列表或單篇；公開資料在登入驗證後才決定是否附 JWT。
  async readContent(): Promise<void> {
    if (this.route !== 'list' && this.route !== 'article') return;
    this.readAbort?.abort();
    const controller = new AbortController();
    this.readAbort = controller;
    this.loading.set(true);
    this.contentError.set(null);
    this.articles.set(null);
    this.article.set(null);
    this.comments.set(null);
    const credential = this.authState() === 'active' ? this.credential() : null;
    const path = this.route === 'list' ? `/api/articles?page=${this.page}&per_page=20` : `/api/articles/${this.validArticleId}`;
    try {
      const { response, body } = await apiRequest(this.backend(), path, { token: credential?.accessToken, signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.ok && body.success === true && this.route === 'list' && this.isPage<Article>(body.data) && body.data.list.every(
        // 列表只在每篇文章都具備閱讀欄位時顯示。
        (item: unknown) => this.isArticle(item),
      )) this.articles.set(body.data);
      else if (response.ok && body.success === true && this.route === 'article' && this.isArticle(body.data)) {
        this.article.set(body.data);
        void this.readComments(controller);
      } else {
        const failure = problem(response.status, body);
        this.contentError.set(response.ok && body.success === true ? { message: '回應資料格式不正確，請確認目前後端。' } : failure);
        if (credential && this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch {
      if (!controller.signal.aborted) this.contentError.set({ message: '讀取失敗，請確認目前後端後重試。' });
    } finally { if (!controller.signal.aborted) this.loading.set(false); }
  }

  // 只接受可顯示的分頁形狀；錯誤回應不能冒充空資料。
  private isPage<T>(value: any): value is Page<T> {
    return value && Array.isArray(value.list) && value.pagination && Number.isSafeInteger(value.pagination.page)
      && value.pagination.page >= 1 && Number.isSafeInteger(value.pagination.per_page) && value.pagination.per_page >= 1
      && Number.isSafeInteger(value.pagination.total) && value.pagination.total >= 0
      && Number.isSafeInteger(value.pagination.total_pages) && value.pagination.total_pages >= 0;
  }

  // 單篇需有公開作者與純文字內文，避免不完整回應顯示錯誤資料。
  private isArticle(value: any): value is Article {
    return value && Number.isSafeInteger(value.id) && typeof value.title === 'string' && typeof value.body === 'string'
      && value.author && typeof value.author.nickname === 'string' && typeof value.created_at === 'string';
  }

  // 留言只讀資料須有公開作者與所屬文章識別，避免破損項目讓閱讀頁中斷。
  private isComment(value: any): value is Comment {
    return value && Number.isSafeInteger(value.id) && typeof value.body === 'string' && value.author
      && typeof value.author.nickname === 'string' && Number.isSafeInteger(value.article_id) && typeof value.created_at === 'string';
  }

  // 留言與文章分開呈現載入／錯誤；留言失敗不抹去已讀到的文章。
  async readComments(parent = this.readAbort): Promise<void> {
    if (!this.validArticleId || !this.article() || !parent || parent.signal.aborted) return;
    this.commentsLoading.set(true);
    this.commentsError.set(null);
    this.comments.set(null);
    const credential = this.authState() === 'active' ? this.credential() : null;
    try {
      const { response, body } = await apiRequest(this.backend(), `/api/articles/${this.validArticleId}/comments?page=${this.page}&per_page=20`, { token: credential?.accessToken, signal: parent.signal });
      if (parent.signal.aborted) return;
      if (response.ok && body.success === true && this.isPage<Comment>(body.data) && body.data.list.every(
        // 任一留言形狀錯誤時顯示錯誤，不把部分資料冒充完整列表。
        (item: unknown) => this.isComment(item),
      )) this.comments.set(body.data);
      else {
        const failure = problem(response.status, body);
        this.commentsError.set(response.ok && body.success === true ? { message: '留言回應格式不正確，請確認目前後端。' } : failure);
        if (credential && this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch {
      if (!parent.signal.aborted) this.commentsError.set({ message: '留言讀取失敗，請手動重試。' });
    } finally { if (!parent.signal.aborted) this.commentsLoading.set(false); }
  }

  // 目前後端文字從固定清單找出，不顯示未驗證的儲存值。
  backendName(): string { return this.backends.find(
    // 只有與目前固定 API 基底相同的後端可作為顯示名稱。
    (entry) => entry.base === this.backend(),
  )?.name || 'Laravel'; }

  // 建立來源只對照固定後端名稱，避免未知值顯示成可信服務。
  backendLabel(id: string): string { return this.backends.find(
    // 建立來源未知時維持中性標籤，避免誤認為已知後端。
    (entry) => entry.id === id,
  )?.name || '未知來源'; }

  // 欄位摘要合併前端驗證與 API errors，讓鍵盤使用者可直接跳到輸入欄位。
  errorEntries(): [string, string[]][] { return Object.entries(this.fieldErrors()).filter(
    // 僅把有對應輸入框的錯誤轉成欄位錨點。
    ([key]) => ['email', 'password', 'nickname'].includes(key),
  ); }

  // API 若回未繪製欄位錯誤，摘要仍保留文字供查閱。
  otherErrors(): string[] { return Object.entries(this.fieldErrors()).filter(
    // 不認識的欄位仍須在摘要呈現，不能靜默遺失後端驗證訊息。
    ([key]) => !['email', 'password', 'nickname'].includes(key),
  ).flatMap(
    // 一個欄位可能有多則錯誤，展平成可閱讀的文字。
    ([key, errors]) => errors.map(
      // 保留原欄位名，方便對照後端回應。
      (message) => `${key}：${message}`,
    ),
  ); }

  // SvelteKit 尚未開放；Nuxt 目前只支援前四個後端，避免切到不可用入口。
  canSwitchFrontend(id: string): boolean {
    return id !== 'sveltekit' && (id !== 'nuxt' || ([BACKENDS.laravel[1], BACKENDS.symfony[1], BACKENDS['aspnet-core'][1], BACKENDS.django[1]] as string[]).includes(this.backend()));
  }

  // 切換前端只改 URL，不複製 JWT、後端、列表頁碼或表單文字。
  switchFrontend(event: Event): void {
    const select = event.target as HTMLSelectElement;
    const id = select.value as keyof typeof FRONTENDS;
    if (!(id in FRONTENDS) || !this.canSwitchFrontend(id) || this.account() && this.pending()) { select.value = 'angular'; return; }
    if (!this.confirmDiscard('前端')) { select.value = 'angular'; return; }
    this.ignoreUnload = true;
    this.attempt++;
    window.location.assign(frontendTarget(this.path, id, this.returnTo));
  }

  // 切換後端先清 JWT 再導向登入，保留合法目的地供新後端重驗。
  switchBackend(event: Event): void {
    const select = event.target as HTMLSelectElement;
    const next = select.value;
    if (!this.backends.some(
      // 只允許固定清單內的 API 基底成為下個後端。
      (entry) => entry.base === next,
    ) || this.account() && this.pending() || !this.confirmDiscard('後端')) { select.value = this.backend(); return; }
    const storage = this.storage();
    if (!storage) { select.value = this.backend(); this.notice.set({ message: '無法儲存後端選擇，請檢查瀏覽器設定。' }); return; }
    let removed = false;
    try { storage.removeItem(SESSION_KEY); removed = true; storage.setItem(BACKEND_KEY, next); }
    catch {
      select.value = this.backend();
      // 已送出的帳號請求會被作廢回應；寫入可能仍在後端完成，須留下結果不明提示供手動查證。
      const uncertain = removed && this.pending();
      this.notice.set({ message: uncertain
        ? `${this.route === 'register' ? '註冊' : '登入'}結果尚未確認；無法儲存後端選擇。請先確認${this.route === 'register' ? '帳號狀態，避免重複送出' : '目前登入狀態'}，再手動重試。`
        : '無法儲存後端選擇，請檢查瀏覽器設定。' });
      // JWT 已刪除卻寫不入後端時，作廢舊請求並結束等待，撤除本頁權限後重讀公開內容。
      if (removed) {
        this.attempt++;
        this.pending.set(false);
        this.credential.set(null); this.account.set(null); this.authState.set('guest'); this.authError.set(null);
        this.article.set(null); this.comments.set(null);
        void this.readContent();
      }
      return;
    }
    this.ignoreUnload = true;
    this.attempt++;
    const target = this.route === 'login' ? this.returnTo : validReturn(this.path) && this.route !== 'register' ? this.path : '/angular/';
    window.location.assign(`/angular/login?return_to=${encodeURIComponent(target)}`);
  }

  // 未儲存或已送出的帳號表單須讓使用者決定取消或捨棄。
  private confirmDiscard(destination: string): boolean {
    if (!this.dirty && !this.pending()) return true;
    const warning = this.pending() ? `${this.route === 'register' ? '註冊' : '登入'}請求已送出，切換後伺服器仍可能完成處理。` : '目前表單有未儲存文字。';
    return window.confirm(`${warning}要捨棄目前表單並切換${destination}嗎？`);
  }

  // 任一帳號欄位變更使先前嘗試作廢；鎖定倒數僅在可編輯時才清提示。
  fieldChanged(): void {
    this.attempt++;
    this.dirty = true;
    this.fieldErrors.set({});
    this.notice.set(null);
    if (!this.lockedTime()) this.lockEnded.set(false);
  }

  // 只用目前帳號、後端與最新嘗試的回應更新表單，防止舊鎖定搶回畫面。
  async submitAccount(event: Event): Promise<void> {
    event.preventDefault();
    if (this.pending() || this.lockedTime()) return;
    const register = this.route === 'register';
    const checked = validateAccount(this.email, this.password, register ? this.nickname : undefined);
    this.fieldErrors.set(checked.errors);
    if (Object.keys(checked.errors).length) { this.notice.set({ message: '請修正標示欄位後再送出。' }); return; }
    const sentBackend = this.backend();
    const sentEmail = checked.email;
    const attempt = ++this.attempt;
    this.pending.set(true);
    this.notice.set(null);
    try {
      const { response, body, retryAfter } = await apiRequest(sentBackend, register ? '/api/auth/register' : '/api/auth/login', {
        method: 'POST', body: register ? { email: sentEmail, password: this.password, nickname: this.nickname } : { email: sentEmail, password: this.password },
      });
      if (attempt !== this.attempt || sentBackend !== this.backend() || sentEmail !== normalizeEmail(this.email)) return;
      if (register && response.status === 201 && body.success === true) {
        this.dirty = false; this.ignoreUnload = true; window.location.assign('/angular/login'); return;
      }
      if (!register && response.ok && body.success === true && this.isLogin(body.data)) {
        const storage = this.storage();
        if (!storage || !saveSession(storage, { backend: sentBackend, accessToken: body.data.access_token, expiresAt: body.data.expires_at })) {
          this.notice.set({ message: '無法保存登入狀態，請檢查瀏覽器設定後重試。' }); return;
        }
        this.clearCountdown();
        this.dirty = false; this.ignoreUnload = true;
        window.location.assign(this.returnTo); return;
      }
      const failure = problem(response.status, body);
      this.fieldErrors.set(failure.errors || {});
      this.notice.set(failure);
      if (!register && response.status === 429 && failure.code === 'E0105') {
        if (retryAfterSeconds(retryAfter) === null) this.notice.set({ ...failure, message: `${failure.message}；回應缺少有效 Retry-After，無法顯示倒數。` });
        else this.startCountdown(retryAfter);
      }
      else if (failure.code === 'E0102') this.clearCountdown();
    } catch {
      if (attempt === this.attempt) this.notice.set({ message: register ? '註冊結果尚未確認；請先確認帳號狀態，避免重複送出。' : '登入結果尚未確認，請確認目前後端後再試。' });
    } finally { if (attempt === this.attempt) this.pending.set(false); }
  }

  // 登入成功資料須含 JWT、Bearer 與原到期時間，才可寫入跨版共用狀態。
  private isLogin(value: any): value is { access_token: string; token_type: 'Bearer'; expires_at: string; user: Account } {
    return value && typeof value.access_token === 'string' && value.access_token.length > 0
      && value.token_type === 'Bearer' && typeof value.expires_at === 'string' && this.isAccount(value.user);
  }

  // 429 僅在 Retry-After 是有效整數時倒數；缺標頭只顯示伺服器鎖定錯誤。
  private startCountdown(value: string | null): void {
    this.clearCountdown();
    const seconds = retryAfterSeconds(value);
    if (!seconds) return;
    this.lockDeadline = Date.now() + seconds * 1000;
    this.tickCountdown();
    this.timer = setInterval(
      // 定時從絕對截止時間重算，背景分頁節流也不會延長鎖定顯示。
      () => this.tickCountdown(), 250,
    );
  }

  // 倒數依絕對時間重算；歸零恢復欄位但不自行送出帳密。
  private tickCountdown(): void {
    const remaining = countdown(this.lockDeadline);
    this.lockedTime.set(remaining);
    if (!remaining) { this.clearCountdown(); this.notice.set(null); this.lockEnded.set(true); }
  }

  // 清除目前表單計時器，不影響伺服器既有鎖定或其他前端。
  private clearCountdown(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.lockDeadline = 0;
    this.lockedTime.set('');
  }

  // 等伺服器確認撤銷才清憑證；未知結果保留可手動重試的狀態。
  async logout(): Promise<void> {
    const credential = this.credential();
    if (!credential || this.pending()) return;
    this.pending.set(true);
    this.notice.set(null);
    try {
      const { response, body } = await apiRequest(credential.backend, '/api/auth/logout', { method: 'POST', token: credential.accessToken });
      if (!this.owns(credential)) return;
      if (response.ok && body.success === true) {
        const storage = this.storage();
        const cleared = Boolean(storage && clearSessionIfOwned(storage, credential));
        this.credential.set(null); this.account.set(null); this.authState.set('guest');
        this.notice.set({ message: cleared ? '已登出。' : '已登出，但無法確認本機舊登入資料已清除；請關閉此分頁。' });
        // 登出後立刻抹去可能只有原帳號可讀的文章，再讀公開資料。
        void this.readContent();
      } else {
        const failure = problem(response.status, body);
        if (this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
        this.notice.set({ ...failure, message: `登出未完成：${failure.message}` });
      }
    } catch { this.notice.set({ message: '登出結果尚未確認；請先確認登入狀態，再手動重試。' }); }
    finally { this.pending.set(false); }
  }

  // 若轉入尚未實作的作者／管理頁，保留合法網址並提供登入或返回入口。
  isFutureRoute(): boolean { return validReturn(this.path) && this.route === 'unknown'; }

  // 分頁只依 API 提供的總頁數導覽；超頁保持原頁碼並提供回首頁。
  pageHref(page: number): string {
    const base = this.route === 'article' ? `/angular/articles/${this.validArticleId}` : '/angular/';
    return `${base}?page=${page}`;
  }
}
