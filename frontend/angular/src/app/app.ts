import { Component, HostListener, OnDestroy, OnInit, computed, inject, signal, ViewChild } from '@angular/core';
import {
  Article, BACKENDS, Comment,
  FRONTENDS, frontendTarget, positiveNumber, Problem,
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
import { MemberManagement, MemberManagementPage } from './member-management';
import { ContentManagement, ContentManagementPage } from './content-management';
import { LockoutSettingsPage, LockoutSettingsForm } from './lockout-settings';

@Component({
  selector: 'app-root',
  imports: [AccountForm, ReadingPage, AuthorForm, MemberManagementPage, ContentManagementPage, LockoutSettingsForm],
  providers: [ReadingContent, OrdinaryWrites, AuthorEditor, MemberManagement, ContentManagement, LockoutSettingsPage],
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
  readonly memberManagement = inject(MemberManagement);
  readonly contentManagement = inject(ContentManagement);
  readonly lockoutSettings = inject(LockoutSettingsPage);
  readonly writePending = this.writes.pending;
  // 作者成功後只要求原讀取 owner 更新畫面，外殼不複製其內容資料。
  readonly authorRefresh = (): Promise<void> => this.readContent();
  // 頁面完成或搜尋後由外殼設定離頁狀態，避免對已確認的文件導覽重複提醒。
  readonly authorCompleted = (url: string): void => { this.ignoreUnload = true; window.location.assign(url); };
  // 閱讀元件逐筆詢問可見性資格，不讓另一操作的未知結果封鎖整頁。
  readonly visibilityBlocked = (kind: 'article' | 'comment', id: number): boolean => this.writes.blocked(`${kind}-visibility:${id}`);
  readonly path = this.reading.path;
  readonly search = window.location.search;
  readonly query = this.reading.query;
  readonly encodeURIComponent = encodeURIComponent;
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
  // 閱讀 owner 與其元件只回報明確失權，外殼負責保留交界中的在途寫入結果。
  readonly invalidateReading = (sent: Session, failure: Problem): void => this.invalidate(sent, failure);
  // 外殼只讀離頁摘要；作者文字與設定草稿／欄位錯誤各由自己的頁面持有。
  get dirty(): boolean { return this.author.dirty || this.lockoutSettings.dirty; }

  private ignoreUnload = false;

  // 初次進頁先辨識共用後端與登入，再讀目前頁面，避免私有狀態短暫閃現。
  ngOnInit(): void {
    // 操作在初始化建立，模板只讀；各頁共用 session 撤權協調而不複製帳號狀態。
    this.author.initializeOperation();
    this.lockoutSettings.initializeOperation();
    this.memberManagement.request.invalidated = this.invalidateReading;
    this.contentManagement.request.invalidated = this.invalidateReading;
    this.lockoutSettings.request.invalidated = this.invalidateReading;
    // 搜尋仍可能遇到在途或未知寫入，先確認捨棄，再抑制同次導覽的重複離頁提醒。
    this.memberManagement.navigate = (url: string): void => {
      if (this.confirmDiscard('搜尋結果')) this.authorCompleted(url);
    };
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

  // 確定失權後立即取消讀取、撤掉內容與操作；原帳號文字仍保留供手動重登。
  private invalidate(sent: Session, failure: Problem): void {
    if (!this.session.invalidate(sent, failure)) return;
    this.clearProtectedContent();
    void this.readContent();
  }

  // 同步撤掉各頁已失權資料及舊請求資格；不清草稿，也不宣稱取消寫入等於伺服器回滾。
  private clearProtectedContent(): void {
    this.reading.clear();
    this.memberManagement.clear(); this.contentManagement.clear(); this.lockoutSettings.clear();
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
    } else if (this.memberManagement.active) await this.memberManagement.read();
    else if (this.contentManagement.active) await this.contentManagement.read();
    else await this.lockoutSettings.read();
  }

  // 各頁可見性事件交唯一寫入 owner，外殼只協調成功後目前頁面的重讀。
  changeVisibility(kind: 'article' | 'comment', resource: Article | Comment): void {
    this.writes.changeVisibility(kind, resource,
      // 閱讀／作者仍使用自己的資料 owner，不把管理範圍混入閱讀真相。
      () => { void this.readContent(); },
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
    this.author.clear(); this.lockoutSettings.clearDraft(); this.writes.clear();
  }

  // 註冊只轉登入；原頁同帳重登保留文字與版本，異帳／角色或後端改變須清除舊草稿。
  accountCompleted(result: { register: boolean; previous: ResumeActor | null }): void {
    this.accountDirty = false;
    if (result.register || this.route === 'login') {
      this.lockoutSettings.clearDraft(); this.ignoreUnload = true;
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

  // log_id 是後端追查依據；複製失敗要明示，畫面仍保留可手選文字。
  async copyLogId(value: string): Promise<void> {
    try { await navigator.clipboard.writeText(value); this.notice.set({ message: '已複製 log_id。' }); }
    catch { this.notice.set({ message: '無法自動複製；請選取畫面上的 log_id。' }); }
  }

}
