import { CommonModule } from '@angular/common';
import { Component, HostListener, OnDestroy, OnInit, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  Account, apiRequest, Article, BACKENDS, BACKEND_KEY, clearSessionIfOwned, Comment, countdown,
  FRONTENDS, frontendTarget, normalizeEmail, Page, positiveNumber, problem, Problem,
  readBackend, readSession, retryAfterSeconds, saveSession, Session, SESSION_KEY,
  validReturn, validateAccount, validateContent,
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
  readonly editArticleId = this.path.match(/^\/angular\/articles\/([0-9]+)\/edit$/)?.[1] || null;
  readonly commentId = this.path.match(/^\/angular\/comments\/([0-9]+)(?:\/edit)?$/)?.[1] || null;
  readonly validEditArticleId = this.editArticleId && positiveNumber(this.editArticleId) !== null ? this.editArticleId : null;
  readonly validCommentId = this.commentId && positiveNumber(this.commentId) !== null ? this.commentId : null;
  readonly route = this.path === '/angular/' ? 'list' : this.path === '/angular/login' ? 'login' : this.path === '/angular/register' ? 'register'
    : this.path === '/angular/articles/new' ? 'article-new' : this.path === '/angular/me/articles' ? 'my-articles'
    : this.path === '/angular/me/comments' ? 'my-comments' : this.validEditArticleId ? 'article-edit'
    : this.validArticleId ? 'article' : this.validCommentId ? this.path.endsWith('/edit') ? 'comment-edit' : 'comment' : 'unknown';
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
  readonly myComments = signal<Page<Comment> | null>(null);
  readonly comment = signal<Comment | null>(null);
  readonly parentArticle = signal<Article | null>(null);
  readonly parentError = signal<Problem | null>(null);
  readonly parentLoading = signal(false);
  readonly writePending = signal(false);
  readonly writeProblem = signal<Problem | null>(null);
  readonly writeUnknown = signal(false);
  readonly unknownVerified = signal(false);
  readonly conflictLatest = signal<Article | Comment | null>(null);
  readonly conflictLoading = signal(false);
  readonly unknownList = signal<Page<Article | Comment> | null>(null);
  readonly pending = signal(false);
  readonly fieldErrors = signal<Record<string, string[]>>({});
  readonly lockedTime = signal('');
  readonly lockEnded = signal(false);
  readonly storageProblem = signal(false);
  email = '';
  password = '';
  nickname = '';
  titleDraft = '';
  bodyDraft = '';
  versionDraft = 0;
  reauthEmail = '';
  reauthPassword = '';
  private resumeAccountId: number | null = null;
  private writeAttempt = 0;
  private unknownNeedsComments = false;
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
    if (this.ignoreUnload || (!this.dirty && !this.pending() && !this.writePending())) return;
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
    this.resumeAccountId = this.account()?.id ?? null;
    const storage = this.storage();
    const cleared = Boolean(storage && clearSessionIfOwned(storage, sent));
    this.credential.set(null);
    this.account.set(null);
    this.authState.set(failure.code === 'E0102' ? 'disabled' : 'expired');
    this.authError.set(cleared ? failure : { ...failure, message: `${failure.message}；無法確認本機舊登入資料已清除，請關閉此分頁。` });
    // 憑證失效時清掉可能僅作者可讀的內容，包含原請求已清空畫面的情況，再以訪客身分重讀。
    this.article.set(null); this.comments.set(null); this.comment.set(null); this.myComments.set(null); this.parentArticle.set(null); this.unknownList.set(null);
    this.writeAttempt++;
    this.writePending.set(false);
    void this.readContent();
  }

  // 用新 AbortController 讀列表或單篇；公開資料在登入驗證後才決定是否附 JWT。
  async readContent(): Promise<void> {
    if (!['list', 'article', 'my-articles', 'my-comments', 'article-edit', 'comment', 'comment-edit'].includes(this.route)) return;
    this.readAbort?.abort();
    const controller = new AbortController();
    this.readAbort = controller;
    this.loading.set(true);
    this.contentError.set(null);
    this.articles.set(null);
    this.article.set(null);
    this.comments.set(null);
    this.myComments.set(null);
    this.comment.set(null);
    this.parentArticle.set(null);
    // 只有結果不明之後啟動的讀取可作為查證，舊請求晚回來不能解除警告。
    const verifyUnknown = this.writeUnknown();
    const verifyAttempt = this.writeAttempt;
    let verified = false;
    const credential = this.authState() === 'active' ? this.credential() : null;
    if (this.route !== 'list' && this.route !== 'article' && !credential) { this.loading.set(false); return; }
    const path = this.route === 'list' ? `/api/articles?page=${this.page}&per_page=20`
      : this.route === 'my-articles' ? `/api/me/articles?page=${this.page}&per_page=20`
      : this.route === 'my-comments' ? `/api/me/comments?page=${this.page}&per_page=20`
      : this.route === 'comment' || this.route === 'comment-edit' ? `/api/comments/${this.validCommentId}`
      : `/api/articles/${this.validArticleId || this.validEditArticleId}`;
    try {
      const { response, body } = await apiRequest(this.backend(), path, { token: credential?.accessToken, signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.ok && body.success === true && ['list', 'my-articles'].includes(this.route) && this.isPage<Article>(body.data) && body.data.list.every(
        // 列表只在每篇文章都具備閱讀欄位時顯示。
        (item: unknown) => this.isArticle(item),
      )) { this.articles.set(body.data); verified = true; }
      else if (response.ok && body.success === true && this.route === 'my-comments' && this.isPage<Comment>(body.data) && body.data.list.every(
        // 我的留言包含個別下架資料，逐筆確認形狀後才顯示。
        (item: unknown) => this.isComment(item),
      )) { this.myComments.set(body.data); verified = true; }
      else if (response.ok && body.success === true && ['article', 'article-edit'].includes(this.route) && this.isArticle(body.data)) {
        if (this.route === 'article-edit' && body.data.author.id !== this.account()?.id) { this.contentError.set({ message: '沒有權限使用此功能', code: 'E0003' }); return; }
        this.article.set(body.data);
        verified = true;
        if (this.route === 'article') void this.readComments(controller);
        if (this.route === 'article-edit' && !this.dirty) this.loadArticleDraft(body.data);
      } else if (response.ok && body.success === true && ['comment', 'comment-edit'].includes(this.route) && this.isComment(body.data)) {
        if (this.route === 'comment-edit' && body.data.author.id !== this.account()?.id) { this.contentError.set({ message: '沒有權限使用此功能', code: 'E0003' }); return; }
        this.comment.set(body.data);
        verified = true;
        if (this.route === 'comment-edit' && !this.dirty) this.loadCommentDraft(body.data);
        void this.readParent(body.data.article_id, controller);
      } else {
        const failure = problem(response.status, body);
        this.contentError.set(response.ok && body.success === true ? { message: '回應資料格式不正確，請確認目前後端。' } : failure);
        if (credential && this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch {
      if (!controller.signal.aborted) this.contentError.set({ message: '讀取失敗，請確認目前後端後重試。' });
    } finally {
      if (!controller.signal.aborted) {
        if (verified && verifyUnknown && verifyAttempt === this.writeAttempt && this.writeUnknown() && !this.unknownNeedsComments) this.unknownVerified.set(true);
        this.loading.set(false);
      }
    }
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
      && value.author && Number.isSafeInteger(value.author.id) && typeof value.author.nickname === 'string'
      && typeof value.created_at === 'string' && Number.isSafeInteger(value.version) && value.version > 0
      && (value.visibility === 'visible' || value.visibility === 'hidden') && typeof value.can_restore === 'boolean';
  }

  // 留言只讀資料須有公開作者與所屬文章識別，避免破損項目讓閱讀頁中斷。
  private isComment(value: any): value is Comment {
    return value && Number.isSafeInteger(value.id) && typeof value.body === 'string' && value.author
      && Number.isSafeInteger(value.author.id) && typeof value.author.nickname === 'string'
      && Number.isSafeInteger(value.article_id) && typeof value.created_at === 'string'
      && Number.isSafeInteger(value.version) && value.version > 0
      && (value.visibility === 'visible' || value.visibility === 'hidden') && typeof value.can_restore === 'boolean';
  }

  // 留言與文章分開呈現載入／錯誤；留言失敗不抹去已讀到的文章。
  async readComments(parent = this.readAbort): Promise<void> {
    if (!this.validArticleId || !this.article() || !parent || parent.signal.aborted) return;
    this.commentsLoading.set(true);
    this.commentsError.set(null);
    this.comments.set(null);
    const verifyUnknown = this.writeUnknown() && this.unknownNeedsComments;
    const verifyAttempt = this.writeAttempt;
    const credential = this.authState() === 'active' ? this.credential() : null;
    try {
      const { response, body } = await apiRequest(this.backend(), `/api/articles/${this.validArticleId}/comments?page=${this.page}&per_page=20`, { token: credential?.accessToken, signal: parent.signal });
      if (parent.signal.aborted) return;
      if (response.ok && body.success === true && this.isPage<Comment>(body.data) && body.data.list.every(
        // 任一留言形狀錯誤時顯示錯誤，不把部分資料冒充完整列表。
        (item: unknown) => this.isComment(item),
      )) {
        this.comments.set(body.data);
        // 新留言結果不明時，必須真的讀回留言列表，不能只憑父文章就結束查證。
        if (verifyUnknown && verifyAttempt === this.writeAttempt && this.writeUnknown()) this.unknownVerified.set(true);
      }
      else {
        const failure = problem(response.status, body);
        this.commentsError.set(response.ok && body.success === true ? { message: '留言回應格式不正確，請確認目前後端。' } : failure);
        if (credential && this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch {
      if (!parent.signal.aborted) this.commentsError.set({ message: '留言讀取失敗，請手動重試。' });
    } finally { if (!parent.signal.aborted) this.commentsLoading.set(false); }
  }

  // 留言的父文章必須另行授權讀取；失敗時不隱藏已獲准看的留言。
  async readParent(articleId: number, controller = this.readAbort): Promise<void> {
    if (!controller || controller.signal.aborted || this.authState() !== 'active') return;
    this.parentLoading.set(true); this.parentError.set(null); this.parentArticle.set(null);
    const credential = this.credential();
    try {
      const { response, body } = await apiRequest(this.backend(), `/api/articles/${articleId}`, { token: credential?.accessToken, signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.ok && body.success === true && this.isArticle(body.data)) this.parentArticle.set(body.data);
      else {
        const failure = problem(response.status, body);
        this.parentError.set(response.ok && body.success === true ? { message: '所屬文章回應格式不正確，請重試。' }
          : response.status === 404 ? { message: '所屬文章目前無法讀取。', code: 'E0004' } : failure);
        if (credential && this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch { if (!controller.signal.aborted) this.parentError.set({ message: '所屬文章讀取失敗，請單獨重試。' }); }
    finally { if (!controller.signal.aborted) this.parentLoading.set(false); }
  }

  // 後端重讀成功後才更新乾淨的文章草稿與版本，避免覆蓋使用者輸入。
  private loadArticleDraft(value: Article): void {
    this.titleDraft = value.title; this.bodyDraft = value.body; this.versionDraft = value.version;
  }

  // 留言草稿只取同一筆內容，父文章資訊不參與文字初始化。
  private loadCommentDraft(value: Comment): void {
    this.bodyDraft = value.body; this.versionDraft = value.version;
  }

  // 編輯欄位改動後保留文字並清掉舊欄位錯誤；衝突仍須人工處理。
  contentChanged(): void {
    this.dirty = true; this.fieldErrors.set({});
    if (!this.writeUnknown() && this.writeProblem()?.code !== 'E0201') this.writeProblem.set(null);
  }

  // 寫入失敗與驗證錯誤只作用於同一帳號／後端／請求，未知結果不自動重送。
  private async write(path: string, method: 'POST' | 'PUT', payload: object, onSuccess: () => void): Promise<void> {
    const credential = this.credential();
    if (!credential || this.authState() !== 'active' || this.writePending() || this.writeUnknown()) return;
    const actor = this.account()?.id;
    const attempt = ++this.writeAttempt;
    this.writePending.set(true); this.writeProblem.set(null); this.writeUnknown.set(false); this.unknownVerified.set(false); this.unknownList.set(null); this.conflictLatest.set(null);
    try {
      const { response, body } = await apiRequest(this.backend(), path, { method, body: payload, token: credential.accessToken });
      if (attempt !== this.writeAttempt || !this.owns(credential) || actor !== this.account()?.id) return;
      if (response.status === (method === 'POST' ? 201 : 200) && body.success === true) { onSuccess(); return; }
      const failure = problem(response.status, body);
      this.writeProblem.set(failure); this.fieldErrors.set(failure.errors || {});
      if (failure.code === 'E0201') this.conflictLatest.set(null);
      if (this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
    } catch {
      if (attempt === this.writeAttempt && this.owns(credential)) {
        this.writeUnknown.set(true);
        this.unknownVerified.set(false);
        this.unknownNeedsComments = method === 'POST' && /\/comments$/.test(path);
        this.writeProblem.set({ message: '結果尚未確認；請先手動查證目前內容，再決定是否重新送出。' });
      }
    } finally { if (attempt === this.writeAttempt) this.writePending.set(false); }
  }

  // 發文只提交純文字；201 不含 ID，成功後到我的文章第一頁找自己的資料。
  submitArticle(event: Event): void {
    event.preventDefault();
    const errors = validateContent(this.bodyDraft, this.titleDraft);
    this.fieldErrors.set(errors);
    if (Object.keys(errors).length) return;
    void this.write('/api/articles', 'POST', { title: this.titleDraft, body: this.bodyDraft },
      // 寫入回應沒有新 ID，轉往作者列表由伺服器重新辨識。
      () => {
      this.dirty = false; this.ignoreUnload = true; window.location.assign('/angular/me/articles?created=1');
      });
  }

  // 編修使用讀取時版本；409 時不覆寫草稿或自動套用新版本。
  submitEdit(event: Event): void {
    event.preventDefault();
    const article = this.route === 'article-edit';
    const resource = article ? this.article() : this.comment();
    if (!resource || resource.author.id !== this.account()?.id || !this.versionDraft || this.writeUnknown()
      || this.writeProblem()?.code === 'E0201' || !article && this.parentArticle()?.visibility !== 'visible') return;
    const errors = validateContent(this.bodyDraft, article ? this.titleDraft : undefined);
    this.fieldErrors.set(errors);
    if (Object.keys(errors).length) return;
    const path = article ? `/api/articles/${resource.id}` : `/api/comments/${resource.id}`;
    void this.write(path, 'PUT', article ? { title: this.titleDraft, body: this.bodyDraft, version: this.versionDraft }
      : { body: this.bodyDraft, version: this.versionDraft },
      // 既有 ID 可在寫入成功後重讀；不採用送出前物件當作最新資料。
      () => {
        this.dirty = false; this.ignoreUnload = true; window.location.assign(article ? `/angular/articles/${resource.id}` : `/angular/comments/${resource.id}`);
      });
  }

  // 衝突或結果不明時按已知 ID 讀最新版本，維持原輸入供人工比較。
  async inspectLatest(): Promise<void> {
    const resource = this.route === 'article-edit' ? this.article() : this.comment();
    const credential = this.credential();
    if (!resource || !credential || this.conflictLoading() || this.writePending()) return;
    this.conflictLoading.set(true); this.conflictLatest.set(null);
    const article = this.route === 'article-edit';
    // 舊比較讀取不能在後續寫入結果不明時變成新的查證依據。
    const verifyAttempt = this.writeAttempt;
    try {
      const { response, body } = await apiRequest(this.backend(), `${article ? '/api/articles/' : '/api/comments/'}${resource.id}`, { token: credential.accessToken });
      if (!this.owns(credential) || verifyAttempt !== this.writeAttempt) return;
      if (response.ok && body.success === true && (article ? this.isArticle(body.data) : this.isComment(body.data)) && body.data.id === resource.id) {
        this.conflictLatest.set(body.data);
      } else {
        const failure = problem(response.status, body);
        this.writeProblem.set(failure);
        if (this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch { if (this.owns(credential) && verifyAttempt === this.writeAttempt) this.writeProblem.set({ message: '查證失敗，結果仍未確認；請稍後重試。' }); }
    finally { this.conflictLoading.set(false); }
  }

  // 使用者看過兩版後才採用最新版本；可保留草稿整合或明確捨棄。
  acceptLatest(discard: boolean): void {
    const latest = this.conflictLatest();
    if (!latest) return;
    this.versionDraft = latest.version;
    if (discard) {
      this.bodyDraft = latest.body;
      if ('title' in latest) this.titleDraft = latest.title;
      this.dirty = false;
    }
    this.conflictLatest.set(null); this.writeProblem.set(null);
    this.writeUnknown.set(false);
  }

  // 新建結果不明時只列出伺服器目前資料供人工辨識，不能把最新一筆當成自己的提交。
  async inspectUnknown(): Promise<void> {
    const credential = this.credential();
    if (!credential || this.conflictLoading()) return;
    const isArticle = this.route === 'article-new';
    const path = isArticle ? '/api/me/articles?page=1&per_page=20'
      : this.route === 'article' && this.unknownNeedsComments ? `/api/articles/${this.validArticleId}/comments?page=1&per_page=20` : '';
    if (!path) return;
    this.conflictLoading.set(true); this.unknownList.set(null);
    const verifyAttempt = this.writeAttempt;
    try {
      const { response, body } = await apiRequest(this.backend(), path, { token: credential.accessToken });
      if (!this.owns(credential) || verifyAttempt !== this.writeAttempt || !this.writeUnknown()) return;
      if (response.ok && body.success === true && this.isPage<Article | Comment>(body.data) && body.data.list.every(
        // 逐筆確認可比較的內容，避免格式錯誤被誤認為查無資料。
        (item: unknown) => isArticle ? this.isArticle(item) : this.isComment(item),
      )) this.unknownList.set(body.data);
      else this.writeProblem.set(problem(response.status, body));
    } catch { if (this.owns(credential) && verifyAttempt === this.writeAttempt && this.writeUnknown()) this.writeProblem.set({ message: '查證失敗，結果仍未確認；請稍後重試。' }); }
    finally { this.conflictLoading.set(false); }
  }

  // 人工比較後才能解除未知狀態；這不表示先前提交失敗，仍由使用者決定是否再送。
  // 只有相關伺服器資料已讀回，畫面才讓使用者結束結果不明狀態。
  canAcknowledgeUnknown(): boolean {
    return Boolean(this.unknownList() || this.conflictLatest() || this.unknownVerified());
  }

  // 使用者完成查證後自行決定下一步，清除阻止盲目重送的狀態。
  acknowledgeUnknown(): void {
    if (this.canAcknowledgeUnknown()) { this.writeUnknown.set(false); this.unknownVerified.set(false); this.writeProblem.set(null); }
  }

  // 新留言成功不猜 ID，回原文章留言第一頁重新讀取。
  submitComment(event: Event): void {
    event.preventDefault();
    const article = this.article();
    if (!article || article.visibility !== 'visible' || this.writeUnknown()) return;
    const errors = validateContent(this.bodyDraft);
    this.fieldErrors.set(errors);
    if (Object.keys(errors).length) return;
    void this.write(`/api/articles/${article.id}/comments`, 'POST', { body: this.bodyDraft },
      // 留言建立回應沒有 ID，回父文章第一頁重讀留言。
      () => {
      this.bodyDraft = ''; this.dirty = false; this.fieldErrors.set({}); this.ignoreUnload = true;
      window.location.assign(`/angular/articles/${article.id}?commented=1`);
      });
  }

  // 可見性操作與內文編修分開，確認後交由後端再驗權限與版本。
  changeVisibility(kind: 'article' | 'comment', resource: Article | Comment): void {
    if (this.writePending() || this.writeUnknown() || resource.visibility === 'hidden' && !resource.can_restore) return;
    const next = resource.visibility === 'visible' ? 'hidden' : 'visible';
    if (!window.confirm(`確定要${next === 'hidden' ? '下架' : '恢復'}這${kind === 'article' ? '篇文章' : '則留言'}嗎？`)) return;
    void this.write(`/api/${kind === 'article' ? 'articles' : 'comments'}/${resource.id}/visibility`, 'PUT', { visibility: next, version: resource.version },
      // 可見性變更後重新取得伺服器版本與恢復資格。
      () => {
      this.notice.set({ message: '狀態已更新，正在重新讀取。' });
      void this.readContent();
      });
  }

  // 原頁重登只恢復原帳號的草稿，若登入為他人就清除文字與衝突狀態。
  async reauthenticate(event: Event): Promise<void> {
    event.preventDefault();
    if (this.pending() || this.lockedTime() || this.resumeAccountId === null) return;
    const checked = validateAccount(this.reauthEmail, this.reauthPassword);
    this.fieldErrors.set(checked.errors);
    if (Object.keys(checked.errors).length) return;
    this.pending.set(true);
    const backend = this.backend();
    const attempt = ++this.attempt;
    try {
      const { response, body, retryAfter } = await apiRequest(backend, '/api/auth/login', { method: 'POST', body: { email: checked.email, password: this.reauthPassword } });
      if (attempt !== this.attempt || backend !== this.backend() || this.authState() !== 'expired') return;
      if (response.ok && body.success === true && this.isLogin(body.data)) {
        const storage = this.storage();
        if (!storage || !saveSession(storage, { backend, accessToken: body.data.access_token, expiresAt: body.data.expires_at })) throw new Error('storage');
        if (body.data.user.id !== this.resumeAccountId) {
          this.titleDraft = ''; this.bodyDraft = ''; this.versionDraft = 0; this.dirty = false;
          this.conflictLatest.set(null); this.writeUnknown.set(false);
          this.notice.set({ message: '已登入其他帳號，原帳號的未儲存文字已清除。' });
        } else this.notice.set({ message: '已重新登入原帳號；請確認內容後手動送出。' });
        this.credential.set({ backend, accessToken: body.data.access_token, expiresAt: body.data.expires_at });
        this.account.set(body.data.user); this.authState.set('active'); this.authError.set(null); this.resumeAccountId = null;
        // 已知 401 在重登後不再有效；結果不明的寫入警告仍須保留供人工查證。
        if (!this.writeUnknown()) this.writeProblem.set(null);
        this.reauthPassword = ''; void this.readContent();
      } else {
        const failure = problem(response.status, body);
        this.notice.set(failure); this.fieldErrors.set(failure.errors || {});
        if (response.status === 429 && failure.code === 'E0105' && retryAfterSeconds(retryAfter)) this.startCountdown(retryAfter);
      }
    } catch { if (attempt === this.attempt) this.notice.set({ message: '重新登入結果尚未確認，請查證目前狀態後手動重試。' }); }
    finally { if (attempt === this.attempt) this.pending.set(false); }
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

  // 作者表單只連結實際存在的標題與內文欄位，其他 API 錯誤仍另行顯示。
  contentErrorEntries(): [string, string[]][] { return Object.entries(this.fieldErrors()).filter(
    // 僅輸入框存在的欄位能成為頁內錨點。
    ([key]) => key === 'title' || key === 'body',
  ); }

  // 保留欄位外錯誤，例如版本衝突的欄位訊息。
  contentOtherErrors(): string[] { return Object.entries(this.fieldErrors()).filter(
    // 無對應輸入框時在摘要以文字顯示。
    ([key]) => key !== 'title' && key !== 'body',
  ).flatMap(
    // API 可對同一欄回多則訊息。
    ([key, errors]) => errors.map(
      // 欄位名讓使用者可對照 API 診斷。
      (message) => `${key}：${message}`,
    ),
  ); }

  // 原頁重登只在已知原帳號時提供，以免無法判定草稿能否保留。
  hasResumeAccount(): boolean { return this.resumeAccountId !== null; }

  // 新留言須查留言列表；文章可見性結果不明時改讀文章本體與版本。
  unknownNeedsCommentList(): boolean { return this.unknownNeedsComments; }

  // 比較區只在最新資源確為文章時顯示標題。
  isLatestArticle(value: Article | Comment): value is Article { return 'title' in value; }

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
    this.writeAttempt++;
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
      const uncertain = removed && (this.pending() || this.writePending());
      this.notice.set({ message: uncertain
        ? `結果尚未確認；無法儲存後端選擇。請先查證${this.route === 'register' ? '帳號狀態' : '目前內容或登入狀態'}，再手動重試。`
        : '無法儲存後端選擇，請檢查瀏覽器設定。' });
      // JWT 已刪除卻寫不入後端時，作廢舊請求並結束等待，撤除本頁權限後重讀公開內容。
      if (removed) {
        this.attempt++;
        this.pending.set(false);
        this.writeAttempt++; this.writePending.set(false);
        this.credential.set(null); this.account.set(null); this.authState.set('guest'); this.authError.set(null);
        this.article.set(null); this.comments.set(null);
        void this.readContent();
      }
      return;
    }
    this.ignoreUnload = true;
    this.attempt++;
    this.writeAttempt++;
    const target = this.route === 'login' ? this.returnTo : validReturn(this.path) && this.route !== 'register' ? this.path : '/angular/';
    window.location.assign(`/angular/login?return_to=${encodeURIComponent(target)}`);
  }

  // 未儲存或已送出的帳號表單須讓使用者決定取消或捨棄。
  private confirmDiscard(destination: string): boolean {
    if (!this.dirty && !this.pending() && !this.writePending()) return true;
    const warning = this.writePending() ? '內容請求已送出，切換後伺服器仍可能完成處理，結果尚未確認。'
      : this.pending() ? `${this.route === 'register' ? '註冊' : '登入'}請求已送出，切換後伺服器仍可能完成處理。` : '目前表單有未儲存文字。';
    return window.confirm(`${warning}要捨棄目前表單並切換${destination}嗎？`);
  }

  // 帳號欄位變更使先前嘗試作廢；原頁重登欄位不代表文章草稿已修改。
  fieldChanged(markDirty = true): void {
    this.attempt++;
    if (markDirty) this.dirty = true;
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
    if (!credential || this.pending() || this.writePending()) return;
    if (!this.confirmDiscard('帳號')) return;
    this.pending.set(true);
    this.notice.set(null);
    try {
      const { response, body } = await apiRequest(credential.backend, '/api/auth/logout', { method: 'POST', token: credential.accessToken });
      if (!this.owns(credential)) return;
      if (response.ok && body.success === true) {
        const storage = this.storage();
        const cleared = Boolean(storage && clearSessionIfOwned(storage, credential));
        this.credential.set(null); this.account.set(null); this.authState.set('guest');
        this.titleDraft = ''; this.bodyDraft = ''; this.versionDraft = 0; this.dirty = false;
        this.writeAttempt++; this.writePending.set(false); this.conflictLatest.set(null);
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

  // 管理頁仍由下一票實作，未知作者網址則按找不到頁面處理。
  isFutureRoute(): boolean { return this.path.startsWith('/angular/admin/') && validReturn(this.path) && this.route === 'unknown'; }

  // 分頁只依 API 提供的總頁數導覽；超頁保持原頁碼並提供回首頁。
  pageHref(page: number): string {
    const base = this.route === 'article' ? `/angular/articles/${this.validArticleId}`
      : this.route === 'my-articles' ? '/angular/me/articles' : this.route === 'my-comments' ? '/angular/me/comments' : '/angular/';
    return `${base}?page=${page}`;
  }
}
