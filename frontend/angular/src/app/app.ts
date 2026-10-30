import { Component, HostListener, OnDestroy, OnInit, computed, inject, signal, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  apiRequest, Article, BACKENDS, Comment,
  FRONTENDS, frontendTarget, Page, positiveNumber, problem, Problem,
  Session, supportsSvelteRoute,
  validReturn,
} from './contracts';
import { AccountForm } from './account-form';
import { AccountSession, ResumeActor } from './account-session';
import { ReadingContent } from './reading-content';
import { ReadingPage } from './reading-page';
import { AuthorEditor } from './author-editor';
import { AuthorForm } from './author-form';
import { OrdinaryWrites, WriteOperation } from './ordinary-writes';
import { isLockoutSettings, isMember, LockoutSettings, Member, memberKeyword, settingErrors } from './admin';

@Component({
  selector: 'app-root',
  imports: [FormsModule, AccountForm, ReadingPage, AuthorForm],
  providers: [ReadingContent, OrdinaryWrites, AuthorEditor],
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
  readonly reading = inject(ReadingContent);
  readonly writes = inject(OrdinaryWrites);
  readonly author = inject(AuthorEditor);
  readonly writePending = this.writes.pending;
  // 作者成功後只要求原讀取 owner 更新畫面，外殼不複製其內容資料。
  readonly authorRefresh = (): Promise<void> => this.readContent();
  // 成功導覽仍由外殼設定離頁狀態，避免對已儲存作者內容重複提醒。
  readonly authorCompleted = (url: string): void => { this.ignoreUnload = true; window.location.assign(url); };
  // 閱讀元件逐筆詢問可見性資格，不讓另一操作的未知結果封鎖整頁。
  readonly visibilityBlocked = (kind: 'article' | 'comment', id: number): boolean => this.writes.blocked(`${kind}-visibility:${id}`);
  readonly path = this.reading.path;
  readonly search = window.location.search;
  readonly query = this.reading.query;
  readonly encodeURIComponent = encodeURIComponent;
  readonly Object = Object;
  readonly articleId = this.path.match(/^\/angular\/articles\/([0-9]+)$/)?.[1] || null;
  readonly validArticleId = this.articleId && positiveNumber(this.articleId) !== null ? this.articleId : null;
  readonly editArticleId = this.path.match(/^\/angular\/articles\/([0-9]+)\/edit$/)?.[1] || null;
  readonly commentId = this.path.match(/^\/angular\/comments\/([0-9]+)(?:\/edit)?$/)?.[1] || null;
  readonly validEditArticleId = this.editArticleId && positiveNumber(this.editArticleId) !== null ? this.editArticleId : null;
  readonly validCommentId = this.commentId && positiveNumber(this.commentId) !== null ? this.commentId : null;
  readonly memberId = this.path.match(/^\/angular\/admin\/members\/([0-9]+)$/)?.[1] || null;
  readonly validMemberId = this.memberId && positiveNumber(this.memberId) !== null ? this.memberId : null;
  readonly route = this.path === '/angular/' ? 'list' : this.path === '/angular/login' ? 'login' : this.path === '/angular/register' ? 'register'
    : this.path === '/angular/articles/new' ? 'article-new' : this.path === '/angular/me/articles' ? 'my-articles'
    : this.path === '/angular/me/comments' ? 'my-comments' : this.path === '/angular/admin/members' ? 'members'
    : this.path === '/angular/admin/articles' ? 'managed-articles' : this.path === '/angular/admin/comments' ? 'managed-comments'
    : this.path === '/angular/admin/settings/login-lockout' ? 'lockout-settings' : this.validMemberId ? 'member'
    : this.validEditArticleId ? 'article-edit'
    : this.validArticleId ? 'article' : this.validCommentId ? this.path.endsWith('/edit') ? 'comment-edit' : 'comment' : 'unknown';
  readonly page = this.reading.page;
  readonly returnTo = validReturn(this.query.get('return_to') || '') ? this.query.get('return_to')! : '/angular/';

  readonly session = inject(AccountSession);
  readonly backend = this.session.backend;
  readonly credential = this.session.credential;
  readonly account = this.session.account;
  readonly authState = this.session.state;
  readonly authError = this.session.error;
  readonly storageProblem = this.session.storageProblem;
  readonly pending = computed(
    // 外殼僅讀單一 session 的等待摘要，不另存一份認證狀態。
    () => this.session.operation() !== null,
  );
  @ViewChild(AccountForm) private accountForm?: AccountForm;
  private accountDirty = false;
  readonly notice = signal<Problem | null>(null);
  private readonly adminLoading = signal(false);
  private readonly adminError = signal<Problem | null>(null);
  private readonly managedArticles = signal<Page<Article> | null>(null);
  readonly loading = computed(
    // 外殼僅讀取頁面等待摘要，管理頁在後續整理前維持自己的狀態。
    () => this.reading.route ? this.reading.loading() : this.adminLoading(),
  );
  readonly contentError = computed(
    // 作者編輯沿用讀取 owner 的錯誤；管理資料仍由原管理流程判定。
    () => this.reading.route ? this.reading.error() : this.adminError(),
  );
  readonly articles = this.managedArticles.asReadonly();
  // 閱讀 owner 與其元件只回報明確失權，外殼負責保留交界中的在途寫入結果。
  readonly invalidateReading = (sent: Session, failure: Problem): void => this.invalidate(sent, failure);
  readonly members = signal<Page<Member> | null>(null);
  readonly member = signal<Member | null>(null);
  readonly managedComments = signal<Page<Comment> | null>(null);
  readonly settings = signal<LockoutSettings | null>(null);
  readonly fieldErrors = signal<Record<string, string[]>>({});
  keywordDraft = this.query.get('keyword') || '';
  settingDraft: LockoutSettings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  private settingsDirty = false;
  private settingsWrite: WriteOperation | null = null;

  // 外殼只讀離頁所需摘要；作者文字與欄位錯誤由作者 owner 持有。
  get dirty(): boolean { return this.author.dirty || this.settingsDirty; }

  private readAbort: AbortController | null = null;
  private ignoreUnload = false;

  // 初次進頁先辨識共用後端與登入，再讀目前頁面，避免私有狀態短暫閃現。
  ngOnInit(): void {
    // 操作只在實際作者／設定路由建立，且須早於模板的純讀取，避免 render 寫入 signals。
    this.author.initializeOperation();
    if (this.route === 'lockout-settings') {
      this.settingsWrite = this.writes.operation('settings', '登入鎖定設定', '/api/settings/login-lockout', 'PUT', { kind: 'settings' });
    }
    this.writes.invalidated = this.invalidateReading;
    this.author.completed = this.authorCompleted;
    this.author.refresh = this.authorRefresh;
    void this.initialize();
  }

  // 離開頁面時中止帳號等待、讀取與倒數，舊結果不得改寫新頁面。
  ngOnDestroy(): void { this.cancelAuthentication(); }

  // 表單有未送出文字或請求時保留瀏覽器離開提醒；程式確認後不重複詢問。
  @HostListener('window:beforeunload', ['$event'])
  beforeUnload(event: BeforeUnloadEvent): void {
    if (this.ignoreUnload || (!this.dirty && !this.accountDirty && !this.pending() && !this.writePending() && !this.writes.unknown())) return;
    event.preventDefault();
    event.returnValue = '';
  }

  // 同分頁 BFCache 可能保留舊後端與憑證；回復時重載並重新驗證。
  @HostListener('window:pageshow', ['$event'])
  restoreFromCache(event: PageTransitionEvent): void { if (event.persisted) window.location.reload(); }

  // 離開文件時作廢舊結果；beforeunload 尚可能取消，因此不能在提醒時就撤掉請求。
  @HostListener('window:pagehide')
  pageHidden(): void { this.cancelAuthentication(); }

  // 原生 details 以 Escape 關閉並回到觸發控制，窄螢幕與鍵盤使用者不會困在面板內。
  @HostListener('document:keydown.escape', ['$event'])
  closePanel(event: Event): void {
    const target = event.target instanceof Element ? event.target.closest<HTMLDetailsElement>('details[open]') : null;
    const panel = target || document.querySelector<HTMLDetailsElement>('.switch-panel[open], .mobile-nav[open]');
    if (panel) { event.preventDefault(); panel.open = false; panel.querySelector('summary')?.focus(); }
  }

  // 外殼只持有帳號表單的離頁摘要，輸入與欄位錯誤留在帳號元件。
  accountDirtyChanged(dirty: boolean): void { this.accountDirty = dirty; }

  // 初次或手動重驗先撤掉舊內容，完成目前 /me 後才決定讀取身分。
  async initialize(): Promise<void> {
    if (this.writePending() || this.session.operation() && this.session.operation() !== 'verify') return;
    const actor = this.account();
    const previous = actor ? { id: actor.id, role: actor.role, backend: this.backend() } : this.session.resumeActor;
    this.clearProtectedContent();
    if (await this.session.initialize()) {
      // 手動重驗也可能讀到變更的角色，原稿不能因此繞過異身分清理。
      if (previous && this.account() && !this.sameActor(previous)) this.clearDrafts();
      await this.readContent();
    }
  }

  // API 的失效／停用判定沿用單一 session 責任，5xx 不撤權。
  private isAuthFailure(status: number, failure: Problem): boolean { return this.session.isAuthFailure(status, failure); }

  // 確定失權後立即取消讀取、撤掉內容與操作；原帳號文字仍保留供手動重登。
  private invalidate(sent: Session, failure: Problem): void {
    if (!this.session.invalidate(sent, failure)) return;
    this.clearProtectedContent();
    void this.readContent();
  }

  // 同步撤掉各頁已失權資料及舊請求資格；不清草稿，也不宣稱取消寫入等於伺服器回滾。
  private clearProtectedContent(): void {
    this.readAbort?.abort(); this.readAbort = null;
    this.reading.clear();
    this.adminLoading.set(false);
    this.managedArticles.set(null);
    this.members.set(null); this.member.set(null); this.managedComments.set(null); this.settings.set(null);
    this.writes.suspend();
  }

  // 接受切換或真正離頁才終止認證／倒數，取消對話框仍保留目前頁面的狀態。
  private cancelAuthentication(): void {
    this.accountForm?.cancel(); this.session.cancel(); this.clearProtectedContent();
  }

  // 閱讀 owner 維持唯一資料；一般重讀不替任何未知寫入授予人工查證資格。
  async readContent(): Promise<void> {
    if (this.authState() === 'checking' || !this.reading.route && !this.isAdminRoute()) return;
    if (this.reading.route) {
      if (await this.reading.read(this.invalidateReading)) this.author.initializeDraft();
    } else await this.readManagement();
  }

  // 管理資料暫留原責任與角色防護，避免本票提前接管管理頁或改寫其寫入契約。
  private async readManagement(): Promise<boolean> {
    this.readAbort?.abort();
    const controller = new AbortController();
    this.readAbort = controller;
    this.adminLoading.set(true); this.adminError.set(null); this.managedArticles.set(null);
    this.members.set(null); this.member.set(null); this.managedComments.set(null); this.settings.set(null);
    const credential = this.authState() === 'active' ? this.credential() : null;
    // 先用目前 /me 結果阻擋管理顯示；後端仍逐次驗證真正權限。
    if (!credential) { this.adminLoading.set(false); return false; }
    if (this.account()?.role !== 'admin') {
      this.adminError.set({ message: '沒有權限使用此功能', code: 'E0003' });
      this.adminLoading.set(false); return false;
    }
    const keyword = memberKeyword(this.query.get('keyword') || '');
    if (this.route === 'members' && [...keyword].length > 254) {
      this.adminError.set({ message: '搜尋文字最多 254 個字。' }); this.adminLoading.set(false); return false;
    }
    const path = this.route === 'members' ? `/api/members?page=${this.page}&per_page=20${keyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(keyword)}` : ''}`
      : this.route === 'member' ? `/api/members/${this.validMemberId}`
      : this.route === 'managed-articles' ? `/api/articles?page=${this.page}&per_page=20&filters%5Bscope%5D=management`
      : this.route === 'managed-comments' ? `/api/comments?page=${this.page}&per_page=20` : '/api/settings/login-lockout';
    try {
      const { response, body } = await apiRequest(this.backend(), path, { token: credential.accessToken, signal: controller.signal });
      if (controller.signal.aborted) return false;
      if (response.ok && body.success === true && this.route === 'managed-articles' && this.isPage<Article>(body.data) && body.data.list.every(
        // 管理清單仍沿用原欄位檢查，閱讀 owner 不持有管理範圍資料。
        (item: unknown) => this.isArticle(item),
      )) this.managedArticles.set(body.data);
      else if (response.ok && body.success === true && this.route === 'members' && this.isPage<Member>(body.data) && body.data.list.every(
        // 會員清單只顯示一般會員完整資料，不能把管理員或破損資料當可操作目標。
        (item: unknown) => isMember(item),
      )) this.members.set(body.data);
      else if (response.ok && body.success === true && this.route === 'member' && isMember(body.data) && body.data.id === Number(this.validMemberId)) this.member.set(body.data);
      else if (response.ok && body.success === true && this.route === 'managed-comments' && this.isPage<Comment>(body.data) && body.data.list.every(
        // 管理清單包含下架與父文章下架留言，需逐筆驗證基本欄位。
        (item: unknown) => this.isComment(item),
      )) this.managedComments.set(body.data);
      else if (response.ok && body.success === true && this.route === 'lockout-settings' && isLockoutSettings(body.data)) {
        this.settings.set(body.data);
        if (!this.settingsDirty) this.settingDraft = { ...body.data };
      } else {
        const failure = problem(response.status, body);
        this.adminError.set(response.ok && body.success === true ? { message: '回應資料格式不正確，請確認目前後端。' } : failure);
        if (this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
        return false;
      }
      return true;
    } catch {
      if (!controller.signal.aborted) this.adminError.set({ message: '讀取失敗，請確認目前後端後重試。' });
      return false;
    } finally { if (!controller.signal.aborted) this.adminLoading.set(false); }
  }

  // 管理列表保留原請求頁碼與精確總頁數；人工查證另由操作 owner 驗證目標。
  private isPage<T>(value: any, expectedPage = this.page): value is Page<T> {
    return value && Array.isArray(value.list) && value.pagination && Number.isSafeInteger(value.pagination.page)
      && value.pagination.page === expectedPage && value.pagination.per_page === 20
      && Number.isSafeInteger(value.pagination.total) && value.pagination.total >= 0
      && value.pagination.total_pages === Math.ceil(value.pagination.total / 20);
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

  // 每個可見性動作保留自己的資源及查證目標，其他操作結果不會解鎖它。
  changeVisibility(kind: 'article' | 'comment', resource: Article | Comment): void {
    const key = `${kind}-visibility:${resource.id}`;
    if (this.writes.blocked(key) || resource.visibility === 'hidden' && !resource.can_restore) return;
    const next = resource.visibility === 'visible' ? 'hidden' : 'visible';
    if (!window.confirm(`確定要${next === 'hidden' ? '下架' : '恢復'}這${kind === 'article' ? '篇文章' : '則留言'}嗎？`)) return;
    const operation = this.writes.operation(key, `${kind === 'article' ? '文章' : '留言'} #${resource.id} 可見性`,
      `/api/${kind === 'article' ? 'articles' : 'comments'}/${resource.id}/visibility`, 'PUT', { kind, id: resource.id });
    void this.writes.run(operation, { visibility: next, version: resource.version },
      // 完整成功後重讀伺服器版本；只在本操作留下成功訊息。
      () => { operation.problem.set({ message: '狀態已更新，正在重新讀取。' }); void this.readContent(); },
    );
  }

  // 使用者明確要求才讀本操作目標；確定可見性衝突讀回後重新取得可操作版本。
  async inspectWrite(operation: WriteOperation): Promise<void> {
    if (await this.writes.inspect(operation) && operation.conflict() && !operation.unknown() && !this.author.owns(operation)) {
      operation.conflict.set(false); operation.problem.set(null);
      void this.readContent();
    }
  }

  // 人工確認只解鎖對應操作；重讀畫面供後續手動操作使用伺服器目前版本。
  acknowledgeWrite(operation: WriteOperation): void {
    if (this.writes.acknowledge(operation)) void this.readContent();
  }

  // 身分比較只取已驗證本人及後端，重登與手動重驗採相同草稿隔離規則。
  private sameActor(previous: ResumeActor | null): boolean {
    const current = this.account();
    return Boolean(current && previous && previous.id === current.id && previous.role === current.role
      && previous.backend === this.backend());
  }

  // 異身分或成功登出才清頁面記憶體，不新增持久草稿或伺服器補償操作。
  private clearDrafts(): void {
    this.author.clear(); this.settingsDirty = false;
    this.settingDraft = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
    this.fieldErrors.set({}); this.writes.clear();
  }

  // 註冊只轉登入；原頁同帳重登保留文字與版本，異帳／角色或後端改變須清除舊草稿。
  accountCompleted(result: { register: boolean; previous: ResumeActor | null }): void {
    this.accountDirty = false;
    if (result.register || this.route === 'login') {
      this.settingsDirty = false; this.ignoreUnload = true;
      window.location.assign(result.register ? '/angular/login' : this.returnTo);
      return;
    }
    // 同帳重登保留草稿與未知結果，但認證前的讀取及查證資格一律失效。
    this.clearProtectedContent();
    const same = this.sameActor(result.previous);
    if (!same) {
      this.clearDrafts();
      this.notice.set({ message: '已登入其他帳號或權限已變更，原帳號的未儲存文字已清除。' });
    } else {
      this.notice.set({ message: '已重新登入原帳號；請確認內容後手動送出。' });
      // 重登只恢復身分，不能確認先前寫入結果；僅移除已知失效錯誤，未知警告繼續保留。
      this.writes.resumed();
    }
    void this.readContent();
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

  // 原頁重登只在已知原帳號時提供，以免無法判定草稿能否保留。
  hasResumeAccount(): boolean { return this.session.resumeActor !== null; }

  // SvelteKit 的登入返回仍須落在已完成頁；Nuxt 已支援全部十二個後端。
  canSwitchFrontend(id: string): boolean {
    if (id === 'sveltekit') return supportsSvelteRoute(this.path)
      && (this.route !== 'login' || supportsSvelteRoute(this.returnTo));
    return Object.hasOwn(FRONTENDS, id);
  }

  // 切換前端只搬合法語意路徑；接受捨棄後先作廢回應，保留共用 JWT 與原期限。
  switchFrontend(event: Event): void {
    const select = event.target as HTMLSelectElement;
    const id = select.value as keyof typeof FRONTENDS;
    if (id === 'angular') return;
    if (!(id in FRONTENDS) || !this.canSwitchFrontend(id) || this.session.operation() === 'logout') { select.value = 'angular'; return; }
    if (!this.confirmDiscard('前端')) { select.value = 'angular'; return; }
    this.ignoreUnload = true; this.cancelAuthentication();
    window.location.assign(frontendTarget(this.path, id, this.returnTo));
  }

  // 後端變更先撤憑證再導向安全登入目標，取消或儲存失敗保留原選單值及必要警告。
  switchBackend(event: Event): void {
    const select = event.target as HTMLSelectElement;
    const next = select.value;
    if (next === this.backend()) return;
    if (!this.backends.some(
      // 選單值仍須在固定 API 清單內，不能把任意網址當成 JWT 接收端。
      (entry) => entry.base === next,
    ) || this.session.operation() === 'logout' || !this.confirmDiscard('後端')) { select.value = this.backend(); return; }
    const uncertain = this.pending() || this.writePending();
    const result = this.session.changeBackend(next);
    if (!result.success) {
      select.value = this.backend();
      if (result.removed) { this.cancelAuthentication(); void this.readContent(); }
      this.notice.set({ message: result.removed && uncertain
        ? '結果尚未確認；無法儲存後端選擇。請先查證帳號或內容狀態，再手動重試。'
        : '無法儲存後端選擇，請檢查瀏覽器設定。' });
      return;
    }
    this.ignoreUnload = true; this.cancelAuthentication();
    const target = this.route === 'login' ? this.returnTo : validReturn(this.path) && this.route !== 'register' ? this.path : '/angular/';
    window.location.assign(`/angular/login?return_to=${encodeURIComponent(target)}`);
  }

  // 帳號及內容的離頁摘要一起判斷，已送出請求須明示伺服器仍可能完成。
  private confirmDiscard(destination: string): boolean {
    if (!this.dirty && !this.accountDirty && !this.pending() && !this.writePending() && !this.writes.unknown()) return true;
    const warning = this.writePending() || this.writes.unknown() ? '內容請求已送出，切換後伺服器仍可能完成處理，結果尚未確認。'
      : this.pending() ? '帳號請求已送出，切換後伺服器仍可能完成處理。' : '目前表單有未儲存文字。';
    return window.confirm(`${warning}要捨棄目前表單並切換${destination}嗎？`);
  }

  // 登出確認及草稿清理由外殼協調，真正撤銷與憑證狀態由 session 單一責任判定。
  async logout(): Promise<void> {
    if (!this.credential() || this.pending() || this.writePending() || !this.confirmDiscard('帳號')) return;
    this.notice.set(null);
    const result = await this.session.logout();
    if (!result) return;
    if (result.success) {
      this.clearProtectedContent();
      this.clearDrafts(); this.accountDirty = false;
      this.notice.set({ message: result.cleared ? '已登出。' : '已登出，但無法確認本機舊登入資料已清除；請關閉此分頁。' });
      void this.readContent();
    } else {
      if (!this.credential()) { this.clearProtectedContent(); void this.readContent(); }
      this.notice.set({ ...result.problem, message: `${result.unknown ? '登出結果尚未確認，請查證登入狀態後手動重試' : '登出未完成'}：${result.problem.message}` });
    }
  }

  // 五個管理語意路由共用角色防護；真正授權仍由每個 API 判定。
  isAdminRoute(): boolean { return ['members', 'member', 'managed-articles', 'managed-comments', 'lockout-settings'].includes(this.route); }

  // 搜尋只把已核准的 keyword 放在會員列表網址，搜尋時回第一頁。
  searchMembers(event: Event): void {
    event.preventDefault();
    const keyword = memberKeyword(this.keywordDraft);
    if ([...keyword].length > 254) { this.fieldErrors.set({ keyword: ['搜尋文字最多 254 個字。'] }); return; }
    this.ignoreUnload = true;
    window.location.assign(`/angular/admin/members${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ''}`);
  }

  // 空 keyword 等同未篩選，空列表文案不能誤稱搜尋無結果。
  hasMemberKeyword(): boolean { return Boolean(memberKeyword(this.query.get('keyword') || '')); }

  // 會員狀態由管理員確認後送出；成功仍重讀伺服器，不推測寫入後狀態。
  changeMemberStatus(value: Member): void {
    const key = `member-status:${value.id}`;
    if (this.writes.blocked(key) || this.account()?.role !== 'admin') return;
    const next = value.status === 'active' ? 'disabled' : 'active';
    if (!window.confirm(`確定要${next === 'disabled' ? '停用' : '恢復'}會員 ${value.nickname} 嗎？`)) return;
    const operation = this.writes.operation(key, `會員 #${value.id} 狀態`, `/api/members/${value.id}/status`, 'PUT', { kind: 'member', id: value.id });
    void this.writes.run(operation, { status: next },
      // 只有該列完整成功才提示，並重新取得伺服器會員狀態。
      () => { operation.problem.set({ message: '會員狀態已更新，正在重新讀取。' }); void this.readContent(); },
    );
  }

  // ngOnInit 已為設定路由建立操作，模板與欄位摘要只讀既有狀態，不在 render 建立 signals。
  get settingsOperation(): WriteOperation { return this.settingsWrite!; }

  // 使用者改設定時保留草稿；已知欄位錯誤可在摘要跳往對應輸入。
  settingChanged(): void { this.settingsDirty = true; this.settingsOperation.errors.set({}); this.notice.set(null); }

  // 四欄一次送出，只有有效伺服器回應後才重讀；未知結果不自動重送。
  submitSettings(event: Event): void {
    event.preventDefault();
    if (!this.settings() || this.writes.blocked('settings')) return;
    const errors = settingErrors(this.settingDraft);
    this.settingsOperation.errors.set(errors);
    if (Object.keys(errors).length) return;
    const sent = { ...this.settingDraft };
    void this.writes.run(this.settingsOperation, sent,
      // 完整 PUT 成功後取消離開提醒，再以 GET 確認目前最後提交值。
      () => { this.settingsDirty = false; this.settingsOperation.problem.set({ message: '設定已提交，正在重新讀取伺服器目前值。' }); void this.readContent(); });
  }

  // 只有有欄位控制的錯誤能連到錨點；其他錯誤仍須在摘要可讀。
  settingErrorEntries(): [string, string[]][] { return Object.entries(this.settingsOperation.errors()).filter(
    // 設定表單四個欄位都具有對應的輸入位置。
    ([key]) => ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(key),
  ); }

  // API 可能回非表單欄位的錯誤；摘要仍呈現完整訊息但不建立無效錨點。
  settingOtherErrors(): string[] { return Object.entries(this.settingsOperation.errors()).filter(
    // 只排除已有輸入控制的四個欄位。
    ([key]) => !['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(key),
  ).flatMap(
    // 保留後端欄位名稱及每則訊息供排查。
    ([key, errors]) => errors.map(
      // 每則錯誤獨立顯示，不吞掉同欄的後續訊息。
      (message) => `${key}：${message}`,
    ),
  ); }

  // log_id 是後端追查依據；複製失敗要明示，畫面仍保留可手選文字。
  async copyLogId(value: string): Promise<void> {
    try { await navigator.clipboard.writeText(value); this.notice.set({ message: '已複製 log_id。' }); }
    catch { this.notice.set({ message: '無法自動複製；請選取畫面上的 log_id。' }); }
  }

  // 分頁只依 API 提供的總頁數導覽；超頁保持原頁碼並提供回首頁。
  pageHref(page: number): string {
    const base = this.route === 'article' ? `/angular/articles/${this.validArticleId}`
      : this.route === 'my-articles' ? '/angular/me/articles' : this.route === 'my-comments' ? '/angular/me/comments'
      : this.route === 'members' ? '/angular/admin/members' : this.route === 'managed-articles' ? '/angular/admin/articles'
      : this.route === 'managed-comments' ? '/angular/admin/comments' : '/angular/';
    const keyword = this.route === 'members' ? memberKeyword(this.query.get('keyword') || '') : '';
    return `${base}?page=${page}${keyword ? `&keyword=${encodeURIComponent(keyword)}` : ''}`;
  }
}
