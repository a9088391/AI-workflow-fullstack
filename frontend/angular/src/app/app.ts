import { CommonModule } from '@angular/common';
import { Component, HostListener, OnDestroy, OnInit, computed, inject, signal, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import {
  apiRequest, Article, BACKENDS, Comment,
  FRONTENDS, frontendTarget, Page, positiveNumber, problem, Problem,
  Session, supportsSvelteRoute,
  validReturn, validateContent,
} from './contracts';
import { AccountForm } from './account-form';
import { AccountSession, ResumeActor } from './account-session';
import { isLockoutSettings, isMember, LockoutSettings, Member, memberKeyword, settingErrors } from './admin';

@Component({
  selector: 'app-root',
  imports: [CommonModule, FormsModule, AccountForm],
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
  readonly memberId = this.path.match(/^\/angular\/admin\/members\/([0-9]+)$/)?.[1] || null;
  readonly validMemberId = this.memberId && positiveNumber(this.memberId) !== null ? this.memberId : null;
  readonly route = this.path === '/angular/' ? 'list' : this.path === '/angular/login' ? 'login' : this.path === '/angular/register' ? 'register'
    : this.path === '/angular/articles/new' ? 'article-new' : this.path === '/angular/me/articles' ? 'my-articles'
    : this.path === '/angular/me/comments' ? 'my-comments' : this.path === '/angular/admin/members' ? 'members'
    : this.path === '/angular/admin/articles' ? 'managed-articles' : this.path === '/angular/admin/comments' ? 'managed-comments'
    : this.path === '/angular/admin/settings/login-lockout' ? 'lockout-settings' : this.validMemberId ? 'member'
    : this.validEditArticleId ? 'article-edit'
    : this.validArticleId ? 'article' : this.validCommentId ? this.path.endsWith('/edit') ? 'comment-edit' : 'comment' : 'unknown';
  readonly page = positiveNumber(this.query.get('page')) || 1;
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
  readonly members = signal<Page<Member> | null>(null);
  readonly member = signal<Member | null>(null);
  readonly managedComments = signal<Page<Comment> | null>(null);
  readonly settings = signal<LockoutSettings | null>(null);
  readonly writePending = signal(false);
  readonly writeProblem = signal<Problem | null>(null);
  readonly writeUnknown = signal(false);
  readonly unknownVerified = signal(false);
  readonly conflictLatest = signal<Article | Comment | null>(null);
  readonly conflictLoading = signal(false);
  readonly unknownList = signal<Page<Article | Comment> | null>(null);
  readonly fieldErrors = signal<Record<string, string[]>>({});
  keywordDraft = this.query.get('keyword') || '';
  settingDraft: LockoutSettings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  titleDraft = '';
  bodyDraft = '';
  versionDraft = 0;
  private writeAttempt = 0;
  private verificationAttempt = 0;
  private unknownNeedsComments = false;
  dirty = false;
  private readAbort: AbortController | null = null;
  private ignoreUnload = false;

  // 初次進頁先辨識共用後端與登入，再讀目前頁面，避免私有狀態短暫閃現。
  ngOnInit(): void { void this.initialize(); }

  // 離開頁面時中止帳號等待、讀取與倒數，舊結果不得改寫新頁面。
  ngOnDestroy(): void { this.cancelAuthentication(); }

  // 表單有未送出文字或請求時保留瀏覽器離開提醒；程式確認後不重複詢問。
  @HostListener('window:beforeunload', ['$event'])
  beforeUnload(event: BeforeUnloadEvent): void {
    if (this.ignoreUnload || (!this.dirty && !this.accountDirty && !this.pending() && !this.writePending())) return;
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
    this.clearProtectedContent();
    if (await this.session.initialize()) await this.readContent();
  }

  // 內容回應使用同一 session 的原憑證比對，不建立自己的登入快照。
  private owns(sent: Session): boolean { return this.session.owns(sent); }

  // API 的失效／停用判定沿用單一 session 責任，5xx 不撤權。
  private isAuthFailure(status: number, failure: Problem): boolean { return this.session.isAuthFailure(status, failure); }

  // 確定失權後立即取消讀取、撤掉內容與操作；原帳號文字仍保留供手動重登。
  private invalidate(sent: Session, failure: Problem): void {
    if (!this.session.invalidate(sent, failure)) return;
    // 其他讀取的失權不能證明在途寫入失敗；保留未知結果，重登後仍須人工查證。
    if (this.writePending()) {
      this.writeUnknown.set(true); this.unknownVerified.set(false);
      this.writeProblem.set({ message: '結果尚未確認；登入失效前送出的內容仍可能完成，請重新登入後手動查證。' });
    }
    this.clearProtectedContent();
    void this.readContent();
  }

  // 同步撤掉各頁已失權資料及舊請求資格；不清草稿，也不宣稱取消寫入等於伺服器回滾。
  private clearProtectedContent(): void {
    this.readAbort?.abort(); this.readAbort = null;
    this.loading.set(false); this.commentsLoading.set(false); this.parentLoading.set(false); this.conflictLoading.set(false);
    this.articles.set(null); this.article.set(null); this.comments.set(null); this.comment.set(null);
    this.myComments.set(null); this.parentArticle.set(null); this.clearVerification();
    this.members.set(null); this.member.set(null); this.managedComments.set(null); this.settings.set(null);
    this.writeAttempt++; this.writePending.set(false);
  }

  // 新讀取或認證恢復會撤銷舊查證；序號阻止較早回應重新授予人工確認資格。
  private clearVerification(): number {
    this.unknownVerified.set(false); this.unknownList.set(null); this.conflictLatest.set(null);
    this.conflictLoading.set(false);
    return ++this.verificationAttempt;
  }

  // 接受切換或真正離頁才終止認證／倒數，取消對話框仍保留目前頁面的狀態。
  private cancelAuthentication(): void {
    this.accountForm?.cancel(); this.session.cancel(); this.clearProtectedContent();
  }

  // 用新 AbortController 讀列表或單篇；只有明確的手動查證可解除目前登入下的未知結果。
  async readContent(manualVerification = false): Promise<void> {
    if (this.authState() === 'checking') return;
    if (manualVerification && (this.authState() !== 'active' || this.pending() || this.writePending())) return;
    if (!['list', 'article', 'my-articles', 'my-comments', 'article-edit', 'comment', 'comment-edit',
      'members', 'member', 'managed-articles', 'managed-comments', 'lockout-settings'].includes(this.route)) return;
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
    this.members.set(null); this.member.set(null); this.managedComments.set(null); this.settings.set(null);
    // 一般載入與重登自動重讀只更新畫面；新查證須撤銷先前成功或仍在途的資格。
    const verification = this.clearVerification();
    const verifyUnknown = manualVerification && this.writeUnknown();
    const verifyAttempt = this.writeAttempt;
    let verified = false;
    const credential = this.authState() === 'active' ? this.credential() : null;
    if (this.route !== 'list' && this.route !== 'article' && !credential) { this.loading.set(false); return; }
    // 管理頁先依目前 /api/me 角色阻擋顯示；後端仍逐次驗證真正權限。
    if (this.isAdminRoute() && this.account()?.role !== 'admin') {
      this.contentError.set({ message: '沒有權限使用此功能', code: 'E0003' });
      this.loading.set(false); return;
    }
    const keyword = memberKeyword(this.query.get('keyword') || '');
    if (this.route === 'members' && [...keyword].length > 254) {
      this.contentError.set({ message: '搜尋文字最多 254 個字。' }); this.loading.set(false); return;
    }
    const path = this.route === 'list' ? `/api/articles?page=${this.page}&per_page=20`
      : this.route === 'my-articles' ? `/api/me/articles?page=${this.page}&per_page=20`
      : this.route === 'my-comments' ? `/api/me/comments?page=${this.page}&per_page=20`
      : this.route === 'members' ? `/api/members?page=${this.page}&per_page=20${keyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(keyword)}` : ''}`
      : this.route === 'member' ? `/api/members/${this.validMemberId}`
      : this.route === 'managed-articles' ? `/api/articles?page=${this.page}&per_page=20&filters%5Bscope%5D=management`
      : this.route === 'managed-comments' ? `/api/comments?page=${this.page}&per_page=20`
      : this.route === 'lockout-settings' ? '/api/settings/login-lockout'
      : this.route === 'comment' || this.route === 'comment-edit' ? `/api/comments/${this.validCommentId}`
      : `/api/articles/${this.validArticleId || this.validEditArticleId}`;
    try {
      const { response, body } = await apiRequest(this.backend(), path, { token: credential?.accessToken, signal: controller.signal });
      if (controller.signal.aborted) return;
      if (response.ok && body.success === true && ['list', 'my-articles', 'managed-articles'].includes(this.route) && this.isPage<Article>(body.data) && body.data.list.every(
        // 列表只在每篇文章都具備閱讀欄位時顯示。
        (item: unknown) => this.isArticle(item),
      )) { this.articles.set(body.data); verified = true; }
      else if (response.ok && body.success === true && this.route === 'members' && this.isPage<Member>(body.data) && body.data.list.every(
        // 會員清單只顯示一般會員完整資料，不能把管理員或破損資料當可操作目標。
        (item: unknown) => isMember(item),
      )) { this.members.set(body.data); verified = true; }
      else if (response.ok && body.success === true && this.route === 'member' && isMember(body.data) && body.data.id === Number(this.validMemberId)) {
        this.member.set(body.data); verified = true;
      } else if (response.ok && body.success === true && this.route === 'managed-comments' && this.isPage<Comment>(body.data) && body.data.list.every(
        // 管理清單包含下架與父文章下架留言，需逐筆驗證基本欄位。
        (item: unknown) => this.isComment(item),
      )) { this.managedComments.set(body.data); verified = true; }
      else if (response.ok && body.success === true && this.route === 'lockout-settings' && isLockoutSettings(body.data)) {
        this.settings.set(body.data); verified = true;
        if (!this.dirty) this.settingDraft = { ...body.data };
      }
      else if (response.ok && body.success === true && this.route === 'my-comments' && this.isPage<Comment>(body.data) && body.data.list.every(
        // 我的留言包含個別下架資料，逐筆確認形狀後才顯示。
        (item: unknown) => this.isComment(item),
      )) { this.myComments.set(body.data); verified = true; }
      else if (response.ok && body.success === true && ['article', 'article-edit'].includes(this.route) && this.isArticle(body.data)
        && body.data.id === Number(this.validArticleId || this.validEditArticleId)) {
        if (this.route === 'article-edit' && body.data.author.id !== this.account()?.id) { this.contentError.set({ message: '沒有權限使用此功能', code: 'E0003' }); return; }
        this.article.set(body.data);
        verified = true;
        if (this.route === 'article') void this.readComments(controller);
        if (this.route === 'article-edit' && !this.dirty) this.loadArticleDraft(body.data);
      } else if (response.ok && body.success === true && ['comment', 'comment-edit'].includes(this.route) && this.isComment(body.data)
        && body.data.id === Number(this.validCommentId)) {
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
        if (verified && verifyUnknown && credential && this.owns(credential) && this.authState() === 'active' && !this.pending()
          && verification === this.verificationAttempt && verifyAttempt === this.writeAttempt && this.writeUnknown()
          && !this.unknownNeedsComments) this.unknownVerified.set(true);
        // 可見性衝突或管理寫入失敗須讀回目前資料，才能清除舊警告並讓使用者重新判斷。
        if (verified && !this.writeUnknown() && verifyAttempt === this.writeAttempt && !['article-edit', 'comment-edit'].includes(this.route)
          && (this.writeProblem()?.code === 'E0201' || this.isAdminRoute() && this.writeProblem()?.code === 'E9999')) this.writeProblem.set(null);
        this.loading.set(false);
      }
    }
  }

  // 只接受該次請求的頁碼與精確總頁數；結果不明查證固定讀第一頁，不能沿用畫面頁碼。
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

  // 留言與文章分開呈現載入／錯誤；閱讀頁分頁不授予查證資格，查證須由 inspectUnknown 讀第一頁。
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
      )) {
        this.comments.set(body.data);
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
    if (!credential || this.authState() !== 'active' || this.pending() || this.writePending() || this.writeUnknown()) return;
    const actor = this.account()?.id;
    const attempt = ++this.writeAttempt;
    // 送出前記住新留言的查證入口，其他請求先撤權時也能保留原操作的上下文。
    this.unknownNeedsComments = method === 'POST' && /\/comments$/.test(path);
    this.writePending.set(true); this.writeProblem.set(null); this.writeUnknown.set(false); this.unknownVerified.set(false); this.unknownList.set(null); this.conflictLatest.set(null);
    try {
      const { response, body } = await apiRequest(this.backend(), path, { method, body: payload, token: credential.accessToken });
      if (attempt !== this.writeAttempt || !this.owns(credential) || actor !== this.account()?.id) return;
      if (response.status === (method === 'POST' ? 201 : 200) && body.success === true) { onSuccess(); return; }
      const failure = problem(response.status, body);
      this.writeProblem.set(failure); this.fieldErrors.set(failure.errors || {});
      if (failure.code === 'E0201') this.conflictLatest.set(null);
      if (this.isAuthFailure(response.status, failure)) {
        // 本寫入已確定遭認證拒絕，先結束自己的等待，不能被撤權清理誤標為未知。
        this.writePending.set(false);
        this.invalidate(credential, failure);
      }
    } catch {
      if (attempt === this.writeAttempt && this.owns(credential)) {
        this.writeUnknown.set(true);
        this.unknownVerified.set(false);
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
    if (!resource || !credential || this.authState() !== 'active' || this.pending() || this.conflictLoading() || this.writePending()) return;
    const verification = this.clearVerification();
    this.conflictLoading.set(true);
    const article = this.route === 'article-edit';
    // 舊比較讀取不能在後續寫入結果不明時變成新的查證依據。
    const verifyAttempt = this.writeAttempt;
    try {
      const { response, body } = await apiRequest(this.backend(), `${article ? '/api/articles/' : '/api/comments/'}${resource.id}`, { token: credential.accessToken });
      if (!this.owns(credential) || this.authState() !== 'active' || this.pending()
        || verifyAttempt !== this.writeAttempt || verification !== this.verificationAttempt) return;
      if (response.ok && body.success === true && (article ? this.isArticle(body.data) : this.isComment(body.data)) && body.data.id === resource.id) {
        this.conflictLatest.set(body.data);
        this.unknownVerified.set(this.writeUnknown());
      } else {
        const failure = problem(response.status, body);
        this.writeProblem.set(this.writeUnknown() ? { ...failure, message: `結果尚未確認；${failure.message}` } : failure);
        if (this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch { if (this.owns(credential) && verifyAttempt === this.writeAttempt && verification === this.verificationAttempt) this.writeProblem.set({ message: '查證失敗，結果仍未確認；請稍後重試。' }); }
    finally { if (verification === this.verificationAttempt) this.conflictLoading.set(false); }
  }

  // 使用者看過兩版後才採用最新版本；可保留草稿整合或明確捨棄。
  acceptLatest(discard: boolean): void {
    const latest = this.conflictLatest();
    if (!latest || this.authState() !== 'active' || !this.credential() || this.pending() || this.writePending()
      || this.writeUnknown() && !this.canAcknowledgeUnknown()) return;
    this.versionDraft = latest.version;
    if (discard) {
      this.bodyDraft = latest.body;
      if ('title' in latest) this.titleDraft = latest.title;
      this.dirty = false;
    }
    this.conflictLatest.set(null); this.writeProblem.set(null);
    this.writeUnknown.set(false); this.unknownVerified.set(false);
  }

  // 新建結果不明時只列出伺服器目前資料供人工辨識，不能把最新一筆當成自己的提交。
  async inspectUnknown(): Promise<void> {
    const credential = this.credential();
    if (!credential || this.authState() !== 'active' || this.pending() || this.writePending() || !this.writeUnknown() || this.conflictLoading()) return;
    const isArticle = this.route === 'article-new';
    const path = isArticle ? '/api/me/articles?page=1&per_page=20'
      : this.route === 'article' && this.unknownNeedsComments ? `/api/articles/${this.validArticleId}/comments?page=1&per_page=20` : '';
    if (!path) return;
    const verification = this.clearVerification();
    this.conflictLoading.set(true);
    const verifyAttempt = this.writeAttempt;
    try {
      const { response, body } = await apiRequest(this.backend(), path, { token: credential.accessToken });
      if (!this.owns(credential) || this.authState() !== 'active' || this.pending() || verifyAttempt !== this.writeAttempt
        || verification !== this.verificationAttempt || !this.writeUnknown()) return;
      if (response.ok && body.success === true && this.isPage<Article | Comment>(body.data, 1) && body.data.list.every(
        // 逐筆確認可比較的內容，避免格式錯誤被誤認為查無資料。
        (item: unknown) => isArticle ? this.isArticle(item) : this.isComment(item),
      )) { this.unknownList.set(body.data); this.unknownVerified.set(true); }
      else {
        const failure = problem(response.status, body);
        this.writeProblem.set({ ...failure, message: `結果尚未確認；${failure.message}` });
        // 查證同樣可能失權；保留未知結果與草稿，讓原頁提供重新登入。
        if (this.isAuthFailure(response.status, failure)) this.invalidate(credential, failure);
      }
    } catch { if (this.owns(credential) && verifyAttempt === this.writeAttempt && verification === this.verificationAttempt && this.writeUnknown()) this.writeProblem.set({ message: '查證失敗，結果仍未確認；請稍後重試。' }); }
    finally { if (verification === this.verificationAttempt) this.conflictLoading.set(false); }
  }

  // 人工比較後才能解除未知狀態；這不表示先前提交失敗，仍由使用者決定是否再送。
  // 查證必須屬於目前有效登入且已結束等待，不能沿用訪客或重登前的資料。
  canAcknowledgeUnknown(): boolean {
    return this.writeUnknown() && this.unknownVerified() && this.authState() === 'active' && Boolean(this.credential())
      && !this.pending() && !this.writePending() && !this.loading() && !this.conflictLoading();
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
    if (this.writePending() || this.writeUnknown() || this.writeProblem()?.code === 'E0201'
      || resource.visibility === 'hidden' && !resource.can_restore) return;
    const next = resource.visibility === 'visible' ? 'hidden' : 'visible';
    if (!window.confirm(`確定要${next === 'hidden' ? '下架' : '恢復'}這${kind === 'article' ? '篇文章' : '則留言'}嗎？`)) return;
    void this.write(`/api/${kind === 'article' ? 'articles' : 'comments'}/${resource.id}/visibility`, 'PUT', { visibility: next, version: resource.version },
      // 可見性變更後重新取得伺服器版本與恢復資格。
      () => {
      this.notice.set({ message: '狀態已更新，正在重新讀取。' });
      void this.readContent();
      });
  }

  // 註冊只轉登入；原頁同帳重登保留文字與版本，異帳／角色或後端改變須清除舊草稿。
  accountCompleted(result: { register: boolean; previous: ResumeActor | null }): void {
    this.accountDirty = false;
    if (result.register || this.route === 'login') {
      this.dirty = false; this.ignoreUnload = true;
      window.location.assign(result.register ? '/angular/login' : this.returnTo);
      return;
    }
    const current = this.account();
    // 同帳重登保留草稿與未知結果，但認證前的讀取及查證資格一律失效。
    this.clearProtectedContent();
    const same = current && result.previous?.id === current.id && result.previous.role === current.role
      && result.previous.backend === this.backend();
    if (!same) {
      this.titleDraft = ''; this.bodyDraft = ''; this.versionDraft = 0; this.dirty = false;
      this.settingDraft = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
      this.fieldErrors.set({}); this.conflictLatest.set(null); this.writeUnknown.set(false);
      this.unknownVerified.set(false); this.unknownList.set(null); this.writeProblem.set(null);
      this.notice.set({ message: '已登入其他帳號或權限已變更，原帳號的未儲存文字已清除。' });
    } else {
      this.notice.set({ message: '已重新登入原帳號；請確認內容後手動送出。' });
      // 重登只恢復身分，不能確認先前寫入結果；僅移除已知失效錯誤，未知警告繼續保留。
      if (!this.writeUnknown()) this.writeProblem.set(null);
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
  hasResumeAccount(): boolean { return this.session.resumeActor !== null; }

  // 新留言須查留言列表；文章可見性結果不明時改讀文章本體與版本。
  unknownNeedsCommentList(): boolean { return this.unknownNeedsComments; }

  // 比較區只在最新資源確為文章時顯示標題。
  isLatestArticle(value: Article | Comment): value is Article { return 'title' in value; }

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
    if (!this.dirty && !this.accountDirty && !this.pending() && !this.writePending()) return true;
    const warning = this.writePending() ? '內容請求已送出，切換後伺服器仍可能完成處理，結果尚未確認。'
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
      this.titleDraft = ''; this.bodyDraft = ''; this.versionDraft = 0; this.dirty = false; this.accountDirty = false;
      this.settingDraft = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
      this.fieldErrors.set({}); this.writeUnknown.set(false); this.unknownVerified.set(false); this.writeProblem.set(null);
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
    if (this.writePending() || this.writeUnknown() || this.account()?.role !== 'admin') return;
    const next = value.status === 'active' ? 'disabled' : 'active';
    if (!window.confirm(`確定要${next === 'disabled' ? '停用' : '恢復'}會員 ${value.nickname} 嗎？`)) return;
    void this.write(`/api/members/${value.id}/status`, 'PUT', { status: next },
      // 寫入回應不帶會員資料，重讀才呈現實際狀態。
      () => { this.notice.set({ message: '會員狀態已更新，正在重新讀取。' }); void this.readContent(); });
  }

  // 使用者改設定時保留草稿；已知欄位錯誤可在摘要跳往對應輸入。
  settingChanged(): void { this.dirty = true; this.fieldErrors.set({}); this.notice.set(null); }

  // 四欄一次送出，只有有效伺服器回應後才重讀；未知結果不自動重送。
  submitSettings(event: Event): void {
    event.preventDefault();
    if (!this.settings() || this.writePending() || this.writeUnknown()) return;
    const errors = settingErrors(this.settingDraft);
    this.fieldErrors.set(errors);
    if (Object.keys(errors).length) return;
    const sent = { ...this.settingDraft };
    void this.write('/api/settings/login-lockout', 'PUT', sent,
      // 完整 PUT 成功後取消離開提醒，再以 GET 確認目前最後提交值。
      () => { this.dirty = false; this.notice.set({ message: '設定已提交，正在重新讀取伺服器目前值。' }); void this.readContent(); });
  }

  // 只有有欄位控制的錯誤能連到錨點；其他錯誤仍須在摘要可讀。
  settingErrorEntries(): [string, string[]][] { return Object.entries(this.fieldErrors()).filter(
    // 設定表單四個欄位都具有對應的輸入位置。
    ([key]) => ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(key),
  ); }

  // API 可能回非表單欄位的錯誤；摘要仍呈現完整訊息但不建立無效錨點。
  settingOtherErrors(): string[] { return Object.entries(this.fieldErrors()).filter(
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
