import { Injectable, OnDestroy, inject, signal } from '@angular/core';
import { AccountSession } from './account-session';
import {
  apiRequest, Article, BACKENDS, Comment, isAuthSuccess, isReadingArticle, isReadingComment, isReadingPage,
  Page, positiveNumber, Problem, problem, Session,
} from './contracts';

@Injectable()
export class ReadingContent implements OnDestroy {
  readonly session = inject(AccountSession);
  readonly path = window.location.pathname === '/angular' ? '/angular/' : window.location.pathname;
  readonly query = new URLSearchParams(window.location.search);
  readonly page = positiveNumber(this.query.get('page')) || 1;
  readonly articleId = this.path.match(/^\/angular\/articles\/([0-9]+)(?:\/edit)?$/)?.[1] || null;
  readonly commentId = this.path.match(/^\/angular\/comments\/([0-9]+)(?:\/edit)?$/)?.[1] || null;
  readonly validArticleId = this.articleId && positiveNumber(this.articleId) !== null ? this.articleId : null;
  readonly validCommentId = this.commentId && positiveNumber(this.commentId) !== null ? this.commentId : null;
  readonly route = this.path === '/angular/' ? 'list' : this.path === '/angular/me/articles' ? 'my-articles'
    : this.path === '/angular/me/comments' ? 'my-comments' : this.validArticleId ? this.path.endsWith('/edit') ? 'article-edit' : 'article'
    : this.validCommentId ? this.path.endsWith('/edit') ? 'comment-edit' : 'comment' : null;
  readonly rendered = this.route !== null && this.route !== 'article-edit' && this.route !== 'comment-edit';
  private readonly loadingValue = signal(false);
  private readonly errorValue = signal<Problem | null>(null);
  private readonly articlesValue = signal<Page<Article> | null>(null);
  private readonly articleValue = signal<Article | null>(null);
  private readonly commentsValue = signal<Page<Comment> | null>(null);
  private readonly commentsErrorValue = signal<Problem | null>(null);
  private readonly commentsLoadingValue = signal(false);
  private readonly myCommentsValue = signal<Page<Comment> | null>(null);
  private readonly commentValue = signal<Comment | null>(null);
  private readonly parentArticleValue = signal<Article | null>(null);
  private readonly parentErrorValue = signal<Problem | null>(null);
  private readonly parentLoadingValue = signal(false);
  readonly loading = this.loadingValue.asReadonly();
  readonly error = this.errorValue.asReadonly();
  readonly articles = this.articlesValue.asReadonly();
  readonly article = this.articleValue.asReadonly();
  readonly comments = this.commentsValue.asReadonly();
  readonly commentsError = this.commentsErrorValue.asReadonly();
  readonly commentsLoading = this.commentsLoadingValue.asReadonly();
  readonly myComments = this.myCommentsValue.asReadonly();
  readonly comment = this.commentValue.asReadonly();
  readonly parentArticle = this.parentArticleValue.asReadonly();
  readonly parentError = this.parentErrorValue.asReadonly();
  readonly parentLoading = this.parentLoadingValue.asReadonly();
  private controller: AbortController | null = null;
  private commentsController: AbortController | null = null;
  private parentController: AbortController | null = null;

  // 讀取 owner 隨外殼銷毀而撤掉請求，較早回應不能回填已離開的文件。
  ngOnDestroy(): void { this.clear(); }

  // 認證或文件生命週期改變即清除資料及等待；草稿與寫入結果由原 writer 保留。
  clear(): void {
    this.controller?.abort(); this.controller = null;
    this.commentsController?.abort(); this.commentsController = null;
    this.parentController?.abort(); this.parentController = null;
    this.loadingValue.set(false); this.commentsLoadingValue.set(false); this.parentLoadingValue.set(false);
    this.errorValue.set(null); this.commentsErrorValue.set(null); this.parentErrorValue.set(null);
    this.articlesValue.set(null); this.articleValue.set(null); this.commentsValue.set(null);
    this.myCommentsValue.set(null); this.commentValue.set(null); this.parentArticleValue.set(null);
  }

  // 舊成功與舊失敗都須屬於目前後端／登入；abort 也隔離同 JWT 的較早一輪讀取。
  private current(controller: AbortController, backend: string, credential: Session | null): boolean {
    return !controller.signal.aborted && backend === this.session.backend()
      && (credential ? this.session.state() === 'active' && this.session.owns(credential)
        : this.session.state() !== 'checking' && this.session.state() !== 'active');
  }

  // 只有明確失效才交還外殼撤權；外殼仍須保留在途寫入的未知結果。
  private authFailure(status: number, failure: Problem, credential: Session | null,
    invalidate: (sent: Session, failure: Problem) => void): void {
    if (credential && this.session.isAuthFailure(status, failure)) invalidate(credential, failure);
  }

  // 下架文章只接受目前作者或管理員範圍，父文仍須獨立確認，不能從留言權限推導。
  private readableArticle(value: unknown, expectedId: number): value is Article {
    return isReadingArticle(value, expectedId) && (value.visibility === 'visible'
      || value.author.id === this.session.account()?.id || this.session.account()?.role === 'admin');
  }

  // 主資料成功才展開留言／父文；回傳成功摘要讓既有 writer 處理草稿及人工查證資格。
  async read(invalidate: (sent: Session, failure: Problem) => void): Promise<boolean> {
    if (!this.route || this.session.state() === 'checking') return false;
    // 新主讀取取消前輪與全部子請求，留言或父文不能晚到混進另一輪主資料。
    this.clear();
    const credential = this.session.state() === 'active' ? this.session.credential() : null;
    if (this.route !== 'list' && this.route !== 'article' && !credential) return false;
    const controller = new AbortController();
    this.controller = controller;
    const backend = this.session.backend();
    this.loadingValue.set(true);
    const path = this.route === 'list' ? `/api/articles?page=${this.page}&per_page=20`
      : this.route === 'my-articles' ? `/api/me/articles?page=${this.page}&per_page=20`
      : this.route === 'my-comments' ? `/api/me/comments?page=${this.page}&per_page=20`
      : this.validCommentId ? `/api/comments/${this.validCommentId}` : `/api/articles/${this.validArticleId}`;
    try {
      const { response, body } = await apiRequest(backend, path, { token: credential?.accessToken, signal: controller.signal });
      if (!this.current(controller, backend, credential)) return false;
      // 完整 200/OK 與資料範圍一起確認，成功旗標本身不能證明取得原請求的內容。
      const complete = isAuthSuccess(response.status, body, 200);
      if (complete && (this.route === 'list' || this.route === 'my-articles') && isReadingPage(body.data, this.page,
        // 公開列表不得夾帶下架內容；個人列表只能包含目前已驗證作者。
        (item): item is Article => isReadingArticle(item) && (this.route === 'list'
          ? item.visibility === 'visible' : item.author.id === this.session.account()?.id),
      )) this.articlesValue.set(body.data);
      else if (complete && this.route === 'my-comments' && isReadingPage(body.data, this.page,
        // 自己的下架留言仍可讀，但其他作者資料不能混入此列表。
        (item): item is Comment => isReadingComment(item) && item.author.id === this.session.account()?.id,
      )) this.myCommentsValue.set(body.data);
      else if (complete && (this.route === 'article' || this.route === 'article-edit')
        && this.readableArticle(body.data, Number(this.validArticleId))) {
        if (this.route === 'article-edit' && body.data.author.id !== this.session.account()?.id) {
          this.errorValue.set({ message: '沒有權限使用此功能', code: 'E0003' }); return false;
        }
        this.articleValue.set(body.data);
        if (this.route === 'article') void this.readComments(invalidate);
      } else if (complete && (this.route === 'comment' || this.route === 'comment-edit')
        && isReadingComment(body.data, Number(this.validCommentId))) {
        if (this.session.account()?.role !== 'admin' && body.data.author.id !== this.session.account()?.id
          || this.route === 'comment-edit' && body.data.author.id !== this.session.account()?.id) {
          this.errorValue.set({ message: '沒有權限使用此功能', code: 'E0003' }); return false;
        }
        this.commentValue.set(body.data);
        void this.readParent(invalidate);
      } else {
        const failure = problem(response.status, body);
        this.errorValue.set(response.status < 400 ? { message: '回應資料格式不正確，請確認目前後端。' } : failure);
        this.authFailure(response.status, failure, credential, invalidate);
        return false;
      }
      return true;
    } catch {
      if (this.current(controller, backend, credential)) this.errorValue.set({ message: '讀取失敗，請確認目前後端後重試。' });
      return false;
    } finally {
      // 清理只結束自己的等待；取消流程已同步清除狀態，不干擾較新的請求。
      if (this.controller === controller) this.loadingValue.set(false);
    }
  }

  // 留言有自己的重試控制器，較早重試不能覆寫新資料；只有父文章已讀成功才發送。
  async readComments(invalidate: (sent: Session, failure: Problem) => void): Promise<void> {
    const parent = this.controller;
    const article = this.article();
    if (!article || !parent || parent.signal.aborted || this.route !== 'article') return;
    // 單獨重試先撤銷舊留言請求，但不動主文章或作者草稿。
    this.commentsController?.abort();
    const controller = new AbortController();
    this.commentsController = controller;
    const credential = this.session.state() === 'active' ? this.session.credential() : null;
    const backend = this.session.backend();
    this.commentsLoadingValue.set(true); this.commentsErrorValue.set(null); this.commentsValue.set(null);
    try {
      const { response, body } = await apiRequest(backend, `/api/articles/${article.id}/comments?page=${this.page}&per_page=20`, {
        token: credential?.accessToken, signal: AbortSignal.any([parent.signal, controller.signal]),
      });
      if (parent.signal.aborted || !this.current(controller, backend, credential)) return;
      if (isAuthSuccess(response.status, body, 200) && isReadingPage(body.data, this.page,
        // 父文章關聯與可見範圍都須吻合，不能顯示另一篇文章或一般讀者不可見的留言。
        (item): item is Comment => isReadingComment(item) && item.article_id === article.id
          && (item.visibility === 'visible' || this.session.account()?.role === 'admin'),
      )) this.commentsValue.set(body.data);
      else {
        const failure = problem(response.status, body);
        this.commentsErrorValue.set(response.status < 400 ? { message: '留言回應格式不正確，請確認目前後端。' } : failure);
        this.authFailure(response.status, failure, credential, invalidate);
      }
    } catch {
      if (!parent.signal.aborted && this.current(controller, backend, credential)) this.commentsErrorValue.set({ message: '留言讀取失敗，請手動重試。' });
    } finally {
      if (this.commentsController === controller) this.commentsLoadingValue.set(false);
    }
  }

  // 父文另行授權且核對原留言 article_id；404 僅表示目前無法讀取，不推測不存在或下架。
  async readParent(invalidate: (sent: Session, failure: Problem) => void): Promise<void> {
    const parent = this.controller;
    const comment = this.comment();
    if (!comment || !parent || parent.signal.aborted || this.session.state() !== 'active') return;
    // 再次確認父文先撤掉舊標題與可見性，等待期間不能送出留言修改。
    this.parentController?.abort();
    const controller = new AbortController();
    this.parentController = controller;
    const credential = this.session.credential();
    const backend = this.session.backend();
    this.parentLoadingValue.set(true); this.parentErrorValue.set(null); this.parentArticleValue.set(null);
    try {
      const { response, body } = await apiRequest(backend, `/api/articles/${comment.article_id}`, {
        token: credential?.accessToken, signal: AbortSignal.any([parent.signal, controller.signal]),
      });
      if (parent.signal.aborted || !this.current(controller, backend, credential)) return;
      if (isAuthSuccess(response.status, body, 200) && this.readableArticle(body.data, comment.article_id)) this.parentArticleValue.set(body.data);
      else {
        const failure = problem(response.status, body);
        this.parentErrorValue.set(response.status < 400 ? { message: '所屬文章回應格式不正確，請重試。' }
          : response.status === 404 ? { message: '所屬文章目前無法讀取。', code: 'E0004' } : failure);
        this.authFailure(response.status, failure, credential, invalidate);
      }
    } catch {
      if (!parent.signal.aborted && this.current(controller, backend, credential)) this.parentErrorValue.set({ message: '所屬文章讀取失敗，請單獨重試。' });
    } finally {
      if (this.parentController === controller) this.parentLoadingValue.set(false);
    }
  }

  // 閱讀頁只產生自身路徑的分頁，保留原 ID 與原頁碼語意。
  pageHref(page: number): string { return `${this.path}?page=${page}`; }

  // 建立來源使用現有固定清單顯示；未知值不冒充已知後端。
  backendLabel(id: string): string { return Object.entries(BACKENDS).find(
    // 只以固定 BackendId 對照名稱，不能由回應指定 API 目的地。
    ([key]) => key === id,
  )?.[1][0] || '未知來源'; }
}
