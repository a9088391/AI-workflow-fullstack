import { Injectable, inject, signal } from '@angular/core';
import { Article, BACKENDS, Comment, validateContent } from './contracts';
import { ReadingContent } from './reading-content';
import { OrdinaryWrites, WriteOperation } from './ordinary-writes';

@Injectable()
export class AuthorEditor {
  readonly content = inject(ReadingContent);
  readonly session = this.content.session;
  readonly writes = inject(OrdinaryWrites);
  readonly route = this.content.path === '/angular/articles/new' ? 'article-new' : this.content.route;
  readonly articleEdit = this.route === 'article-edit';
  readonly commentNew = this.route === 'article';
  readonly hasTitle = this.route === 'article-new' || this.articleEdit;
  private currentOperation: WriteOperation | null = null;

  // 外殼初始化先準備作者路由的操作；模板只能讀取，不能在 render 中修改 signals。
  initializeOperation(): void {
    if (this.currentOperation || !['article-new', 'article-edit', 'comment-edit', 'article'].includes(this.route || '')) return;
    this.currentOperation = this.writes.operation(
      this.route === 'article-new' ? 'article-new' : this.commentNew ? `comment-new:${this.content.validArticleId}`
        : `${this.articleEdit ? 'article-edit' : 'comment-edit'}:${this.articleEdit ? this.content.validArticleId : this.content.validCommentId}`,
      this.route === 'article-new' ? '發表文章' : this.commentNew ? `文章 #${this.content.validArticleId} 的新留言`
        : `${this.articleEdit ? '文章' : '留言'} #${this.articleEdit ? this.content.validArticleId : this.content.validCommentId} 編修`,
      this.route === 'article-new' ? '/api/articles' : this.commentNew ? `/api/articles/${this.content.validArticleId}/comments`
        : `/api/${this.articleEdit ? 'articles' : 'comments'}/${this.articleEdit ? this.content.validArticleId : this.content.validCommentId}`,
      this.route === 'article-new' || this.commentNew ? 'POST' : 'PUT',
      this.route === 'article-new' ? { kind: 'new-article' } : this.commentNew ? { kind: 'new-comment', id: Number(this.content.validArticleId) }
        : { kind: this.articleEdit ? 'article' : 'comment', id: Number(this.articleEdit ? this.content.validArticleId : this.content.validCommentId) },
    );
  }

  // App.ngOnInit 已為作者路由建立操作，getter 僅回傳同一筆狀態供表單讀取。
  get operation(): WriteOperation { return this.currentOperation!; }

  // 外殼可辨識作者比較責任，不需要取得作者可寫狀態或強迫建立操作。
  owns(operation: WriteOperation): boolean { return operation === this.currentOperation; }

  titleDraft = '';
  bodyDraft = '';
  versionDraft = 0;
  dirty = false;
  readonly completedUrl = signal<string | null>(null);
  private completedAttempt: number | null = null;
  private completionReadGeneration = 0;
  completed: ((url: string) => void) | null = null;
  refresh: (() => Promise<void>) | null = null;

  // 作者 owner 活在外殼 DI 範圍，重新登入卸下表單時仍保留文字及原版本。
  initializeDraft(): void {
    if (this.dirty) return;
    const value = this.articleEdit ? this.content.article() : this.route === 'comment-edit' ? this.content.comment() : null;
    if (!value) return;
    this.bodyDraft = value.body; this.versionDraft = value.version;
    if ('title' in value) this.titleDraft = value.title;
  }

  // 異身分與成功登出明確清除原稿，清除時不宣稱伺服器寫入已回滾。
  clear(): void {
    this.titleDraft = ''; this.bodyDraft = ''; this.versionDraft = 0; this.dirty = false;
    this.completedUrl.set(null); this.completedAttempt = null; this.completionReadGeneration++;
  }

  // 改字作廢在途完成讀回的草稿更新；前次成功仍保留到下一次有效提交，其他結果保持原樣。
  changed(): void {
    this.dirty = true; this.completionReadGeneration++; this.operation.errors.set({});
    if (!this.operation.unknown() && !this.operation.conflict() && !this.operation.submitted()) this.operation.problem.set(null);
  }

  // 不把失權、等待、未查證或尚未整合的版本開放送出；父文章權限仍獨立確認。
  blocked(): boolean {
    return this.writes.blocked(this.operation.key) || Boolean(this.operation.latest() && !this.completedUrl())
      || this.route === 'comment-edit' && this.content.parentArticle()?.visibility !== 'visible';
  }

  // 作者四種送出共用已核准判讀，完整成功才清稿並交外殼執行合法導覽。
  submit(event: Event): void {
    event.preventDefault();
    if (this.blocked()) return;
    const article = this.content.article();
    const resource = this.articleEdit ? article : this.content.comment();
    if (this.commentNew && (!article || article.visibility !== 'visible')) return;
    if ((this.articleEdit || this.route === 'comment-edit')
      && (!resource || resource.author.id !== this.session.account()?.id || !this.versionDraft)) return;
    const errors = validateContent(this.bodyDraft, this.hasTitle ? this.titleDraft : undefined);
    this.operation.errors.set(errors);
    if (Object.keys(errors).length) return;
    const payload = this.hasTitle ? { title: this.titleDraft, body: this.bodyDraft,
      ...(this.articleEdit ? { version: this.versionDraft } : {}) }
      : { body: this.bodyDraft, ...(this.route === 'comment-edit' ? { version: this.versionDraft } : {}) };
    // 通過本機驗證且即將送出才撤前次完成，第二次409／未知不能借用第一次的成功入口。
    this.completedUrl.set(null); this.completedAttempt = null; this.completionReadGeneration++;
    // 一旦送出即保留文字與原版本，重登後不能因尚未改字而自動重初始化。
    this.dirty = true;
    void this.writes.run(this.operation, payload,
      // 新建不猜新 ID；既有資源仍回詳情重讀伺服器最新值。
      () => {
        this.dirty = false;
        if (this.commentNew) { this.bodyDraft = ''; this.operation.errors.set({}); }
        const destination = this.route === 'article-new' ? '/angular/me/articles?created=1'
          : this.commentNew ? `/angular/articles/${article!.id}?commented=1`
          : `/angular/${this.articleEdit ? 'articles' : 'comments'}/${resource!.id}`;
        if (this.writes.hasOtherUnknown(this.operation)) {
          this.completedAttempt = this.operation.origin!.generation;
          this.completedUrl.set(destination);
          void this.readCompleted();
        } else this.completed?.(destination);
      },
    );
  }

  // 其他未知結果仍在本文件時讀回該次已提交結果；新提交、改字與較新的讀回均隔離舊回應。
  async readCompleted(): Promise<void> {
    const operation = this.operation;
    if (!this.completedUrl() || !operation.submitted() || this.completedAttempt !== operation.origin?.generation
      || this.session.state() !== 'active' || this.session.operation() || this.writes.pending()) return;
    const generation = operation.generation;
    const readGeneration = ++this.completionReadGeneration;
    operation.problem.set({ message: '已提交；其他操作的結果仍待查證，請先確認伺服器目前資料。' });
    await this.refresh?.();
    if (!this.currentCompletion(readGeneration, generation)) return;
    if (await this.writes.inspect(operation) && this.currentCompletion(readGeneration, generation)) {
      const latest = operation.latest();
      if (latest && !this.dirty) {
        this.versionDraft = latest.version; this.bodyDraft = latest.body;
        if ('title' in latest) this.titleDraft = latest.title;
      }
      operation.problem.set({ message: '已提交並讀回伺服器目前資料；其他操作的結果仍待查證。' });
    }
  }

  // 成功入口綁真正寫入序號；撤權只作廢在途讀回，同身分重登後仍能明確重試已知成功。
  private currentCompletion(readGeneration: number, generation: number): boolean {
    return readGeneration === this.completionReadGeneration && generation === this.operation.generation
      && this.completedUrl() !== null && this.operation.submitted()
      && this.completedAttempt === this.operation.origin?.generation;
  }

  // 比較只能讀原 ID；草稿及原版本直到人工選擇前都不被覆寫。
  inspectLatest(): void { void this.writes.inspect(this.operation); }

  // 使用者比較兩版後選保留整合或明確捨棄，採用新版本也不自動送出。
  acceptLatest(discard: boolean): void {
    const latest = this.operation.latest();
    if (!latest || !this.writes.proofCurrent(this.operation)) return;
    this.versionDraft = latest.version;
    if (discard) {
      this.bodyDraft = latest.body;
      if ('title' in latest) this.titleDraft = latest.title;
      this.dirty = false;
    }
    this.operation.latest.set(null); this.operation.problem.set(null); this.operation.errors.set({});
    this.operation.conflict.set(false); this.operation.unknown.set(false); this.operation.verified.set(false);
    this.operation.proofCredential = null;
  }

  // 摘要只為實際存在的控制建立錨點，未知欄位仍以文字保留。
  errorEntries(): [string, string[]][] {
    return Object.entries(this.operation.errors()).filter(
      // 留言沒有標題欄，不能建立會跳到不存在位置的連結。
      ([key]) => key === 'body' || this.hasTitle && key === 'title',
    );
  }

  // 無控制的驗證欄位仍呈現完整訊息，避免已知拒絕只留下空白摘要。
  otherErrors(): string[] {
    return Object.entries(this.operation.errors()).filter(
      // 排除真正顯示的控制，其餘欄位只作安全文字。
      ([key]) => key !== 'body' && (!this.hasTitle || key !== 'title'),
    ).flatMap(
      // 保留同欄的所有 API 訊息供人工修正。
      ([key, messages]) => messages.map(
        // 訊息附欄位名稱，避免無法辨識後端指出的輸入。
        (message) => `${key}：${message}`,
      ),
    );
  }

  // 已驗證建立來源映射固定後端名稱，維持原作者畫面的來源文案。
  backendLabel(value: keyof typeof BACKENDS): string { return BACKENDS[value][0]; }

  // 比較區只在文章顯示標題，留言仍沿用其純文字契約。
  isArticle(value: Article | Comment): value is Article { return 'title' in value; }
}
