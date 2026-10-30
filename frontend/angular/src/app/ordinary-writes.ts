import { Injectable, computed, inject, signal } from '@angular/core';
import { AccountSession } from './account-session';
import {
  apiRequest, Article, classifyWrite, Comment, isAuthSuccess, isReadingArticle, isReadingComment,
  isReadingPage, Page, Problem, problem, Session,
} from './contracts';
import { isLockoutSettings, isMember, LockoutSettings, Member } from './admin';

type ProofTarget = { kind: 'new-article' } | { kind: 'new-comment'; id: number }
  | { kind: 'article' | 'comment' | 'member'; id: number } | { kind: 'settings' };
type Origin = { credential: Session; actor: number; role: string; generation: number; payload: object };
type Invalidate = (sent: Session, failure: Problem) => void;

export class WriteOperation {
  readonly problem = signal<Problem | null>(null);
  readonly errors = signal<Record<string, string[]>>({});
  readonly unknown = signal(false);
  readonly submitted = signal(false);
  readonly conflict = signal(false);
  readonly inspecting = signal(false);
  readonly verified = signal(false);
  readonly list = signal<Page<Article | Comment> | null>(null);
  readonly latest = signal<Article | Comment | null>(null);
  readonly member = signal<Member | null>(null);
  readonly settings = signal<LockoutSettings | null>(null);
  generation = 0;
  proofGeneration = 0;
  origin: Origin | null = null;
  proofCredential: Session | null = null;

  // 每個實際操作保存獨立結果與查證目標；同資源的編修與可見性也不能互相完成。
  constructor(readonly key: string, readonly label: string, readonly path: string,
    readonly method: 'POST' | 'PUT', readonly target: ProofTarget) {}
}

@Injectable()
export class OrdinaryWrites {
  readonly session = inject(AccountSession);
  readonly operations = signal<WriteOperation[]>([]);
  private readonly active = signal<WriteOperation | null>(null);
  readonly pending = computed(
    // 所有內容寫入維持原本互斥，但未知結果只禁止原操作盲目重送。
    () => this.active() !== null,
  );
  readonly unknown = computed(
    // 外殼離頁只讀摘要，不需要存取各操作的私有輸入或結果。
    () => this.operations().some(
      // 任何未確認提交都仍可能在伺服器完成，切換前必須明示此限制。
      (operation) => operation.unknown(),
    ),
  );
  invalidated: Invalidate | null = null;

  // 只在初始化或使用者事件建立操作；模板 getter 必須純讀，不能在 render 修改此 signal。
  operation(key: string, label: string, path: string, method: 'POST' | 'PUT', target: ProofTarget): WriteOperation {
    const existing = this.find(key);
    if (existing) return existing;
    const operation = new WriteOperation(key, label, path, method, target);
    this.operations.update(
      // 新增結果不取代先前其他操作的警告或查證資料。
      (items) => [...items, operation],
    );
    return operation;
  }

  // 模板只查現有記錄，不在畫面更新時建立狀態或修改其他資源。
  find(key: string): WriteOperation | undefined {
    return this.operations().find(
      // 操作鍵同時包含動作及已知資源，避免不同列共用一份結果。
      (operation) => operation.key === key,
    );
  }

  // 自動文件導覽前檢查其他未知結果，不能以另一成功讓它們隨文件消失。
  hasOtherUnknown(operation: WriteOperation): boolean {
    return this.operations().some(
      // 只看其他操作，原操作完整成功不會代替它們取得確認。
      (item) => item !== operation && item.unknown(),
    );
  }

  // 認證等待與寫入互斥；衝突須先比較，未知結果須先人工查證。
  blocked(key: string): boolean {
    const operation = this.find(key);
    return this.session.state() !== 'active' || this.session.operation() !== null || this.pending()
      || Boolean(operation?.unknown() || operation?.conflict());
  }

  // 新查證先撤舊資格及資料，序號防止同 JWT 較早的成功在新失敗後復活。
  private revoke(operation: WriteOperation): number {
    operation.verified.set(false); operation.proofCredential = null;
    operation.list.set(null); operation.latest.set(null); operation.member.set(null); operation.settings.set(null);
    operation.inspecting.set(false);
    return ++operation.proofGeneration;
  }

  // 身分與文件邊界作廢舊回應；在途寫入仍可能完成，不能宣稱取消等於回滾。
  suspend(): void {
    for (const operation of this.operations()) {
      if (operation === this.active()) {
        operation.unknown.set(true);
        operation.problem.set({ message: '結果尚未確認；登入失效前送出的內容仍可能完成，請重新登入後手動查證。' });
      }
      operation.generation++;
      this.revoke(operation);
    }
    this.active.set(null);
  }

  // 異帳號、角色、後端或成功登出才移除原作者結果，避免向新身分洩漏內容。
  clear(): void {
    this.suspend();
    for (const operation of this.operations()) {
      operation.origin = null;
      operation.problem.set(null); operation.errors.set({}); operation.unknown.set(false); operation.submitted.set(false); operation.conflict.set(false);
    }
  }

  // 同身分重登只清確定拒絕；未知結果保留，查證則必須用新憑證重做。
  resumed(): void {
    for (const operation of this.operations()) {
      this.revoke(operation);
      // 已確認提交不因重登或讀取失敗降成未知；同身分仍可手動重讀原結果。
      if (!operation.unknown() && !operation.conflict() && !operation.submitted()) operation.problem.set(null);
    }
  }

  // 同帳號／角色／後端才可接續原操作，新登入不能借用原身分的結果。
  private sameActor(operation: WriteOperation): boolean {
    const actor = this.session.account();
    return Boolean(actor && operation.origin && actor.id === operation.origin.actor
      && actor.role === operation.origin.role && this.session.backend() === operation.origin.credential.backend);
  }

  // 完整成功才呼叫完成動作；所有拒絕、未知及晚到結果都留在自己的操作中。
  async run(operation: WriteOperation, payload: object, completed: () => void): Promise<void> {
    const credential = this.session.credential();
    const actor = this.session.account();
    if (!credential || !actor || this.blocked(operation.key)) return;
    const generation = ++operation.generation;
    operation.origin = { credential, actor: actor.id, role: actor.role, generation, payload };
    this.revoke(operation);
    // 只有真正開始下一次寫入才撤前次成功，欄位驗證失敗不能冒充新提交。
    operation.problem.set(null); operation.errors.set({}); operation.submitted.set(false);
    this.active.set(operation);
    try {
      const { response, body } = await apiRequest(credential.backend, operation.path,
        { method: operation.method, body: payload, token: credential.accessToken });
      if (generation !== operation.generation || !this.session.owns(credential) || !this.sameActor(operation)) return;
      const result = classifyWrite(response.status, body, operation.method);
      if (result.kind === 'success') {
        operation.submitted.set(true);
        this.active.set(null); completed(); return;
      }
      operation.problem.set(result.problem);
      operation.unknown.set(result.kind === 'unknown');
      operation.errors.set(result.kind === 'rejected' ? result.problem.errors || {} : {});
      operation.conflict.set(result.kind === 'rejected' && result.problem.code === 'E0201');
      if (result.kind === 'rejected' && this.session.isAuthFailure(response.status, result.problem)) {
        // 自己確定遭拒絕先結束等待，外殼撤權時才不會把它誤認為結果不明。
        this.active.set(null);
        this.invalidated?.(credential, result.problem);
      }
    } catch {
      if (generation === operation.generation && this.session.owns(credential) && this.sameActor(operation)) {
        const result = classifyWrite(0, null, operation.method);
        if (result.kind !== 'success') operation.problem.set(result.problem);
        operation.unknown.set(true);
      }
    } finally {
      if (this.active() === operation && generation === operation.generation) this.active.set(null);
    }
  }

  // 查證資格只屬於本操作最新一次人工讀取及目前憑證，其他讀取不授予資格。
  canAcknowledge(operation: WriteOperation): boolean {
    const credential = operation.proofCredential;
    return operation.unknown() && this.proofCurrent(operation) && Boolean(credential);
  }

  // 比較內容與解除警告共用同一資格，避免失權或重登前的比較資料被採用。
  proofCurrent(operation: WriteOperation): boolean {
    const credential = operation.proofCredential;
    return Boolean(operation.verified() && credential && this.session.owns(credential)
      && this.session.state() === 'active' && !this.session.operation() && !this.pending()
      && !operation.inspecting() && (!operation.origin || this.sameActor(operation)));
  }

  // 使用者只解除自己的未知結果；這不能證明先前提交未完成，也不自動重送。
  acknowledge(operation: WriteOperation): boolean {
    if (!this.canAcknowledge(operation)) return false;
    operation.unknown.set(false); operation.problem.set(null);
    this.revoke(operation);
    return true;
  }

  // 已知 ID 讀直接詳情；新建只讀固定第一頁，不能猜哪一筆是剛才送出的內容。
  async inspect(operation: WriteOperation): Promise<boolean> {
    const credential = this.session.credential();
    if (!credential || this.session.state() !== 'active' || this.session.operation() || this.pending()) return false;
    const proofGeneration = this.revoke(operation);
    const generation = operation.generation;
    const actor = this.session.account();
    const target = operation.target;
    const path = target.kind === 'new-article' ? '/api/me/articles?page=1&per_page=20'
      : target.kind === 'new-comment' ? `/api/articles/${target.id}/comments?page=1&per_page=20`
      : target.kind === 'article' ? `/api/articles/${target.id}` : target.kind === 'comment' ? `/api/comments/${target.id}`
      : target.kind === 'member' ? `/api/members/${target.id}` : '/api/settings/login-lockout';
    operation.inspecting.set(true);
    try {
      const { response, body } = await apiRequest(credential.backend, path, { token: credential.accessToken });
      if (!this.currentInspection(operation, credential, generation, proofGeneration)) return false;
      let valid = false;
      // 查證形狀及關聯依原目標驗證，不能以另一資源或其他頁的成功替代。
      if (isAuthSuccess(response.status, body, 200)) {
        if (target.kind === 'new-article' && isReadingPage<Article>(body.data, 1,
          // 本人列表不能混入其他作者而誤導原提交的人工比對。
          (item): item is Article => isReadingArticle(item) && item.author.id === actor?.id,
        )) { operation.list.set(body.data); valid = true; }
        else if (target.kind === 'new-comment' && isReadingPage<Comment>(body.data, 1,
          // 留言列表固定原文章，即使格式完整也不能接受另一父文章資料。
          (item): item is Comment => isReadingComment(item) && item.article_id === target.id && this.readable(item),
        )) { operation.list.set(body.data); valid = true; }
        else if (target.kind === 'article' && isReadingArticle(body.data, target.id) && this.readable(body.data)) {
          operation.latest.set(body.data); valid = true;
        } else if (target.kind === 'comment' && isReadingComment(body.data, target.id) && this.readable(body.data)) {
          operation.latest.set(body.data); valid = true;
        } else if (target.kind === 'member' && isMember(body.data) && body.data.id === target.id) {
          operation.member.set(body.data); valid = true;
        } else if (target.kind === 'settings' && isLockoutSettings(body.data)) {
          operation.settings.set(body.data); valid = true;
        }
      }
      if (valid) {
        operation.proofCredential = credential;
        operation.verified.set(true);
        return true;
      }
      const failure = problem(response.status, body);
      operation.problem.set(this.inspectionFailure(operation, failure));
      if (this.session.isAuthFailure(response.status, failure)) this.invalidated?.(credential, failure);
    } catch {
      if (this.currentInspection(operation, credential, generation, proofGeneration)) {
        operation.problem.set(this.inspectionFailure(operation, { message: '讀取失敗，請稍後重試。' }));
      }
    } finally {
      if (proofGeneration === operation.proofGeneration) operation.inspecting.set(false);
    }
    return false;
  }

  // GET 失敗只表示讀不回現況，不改寫已知提交、版本拒絕與真正未知三種寫入結果。
  private inspectionFailure(operation: WriteOperation, failure: Problem): Problem {
    const message = operation.unknown() ? `結果尚未確認；${failure.message}`
      : operation.submitted() ? `已提交，但無法讀回伺服器目前資料；${failure.message}`
      : operation.conflict() ? `本次修改因版本衝突未儲存；無法讀取最新資料，請稍後重試。${failure.message}`
      : `無法讀取最新資料；${failure.message}`;
    return { ...failure, errors: undefined, message };
  }

  // 直接查證沿用閱讀範圍，下架資料只能由目前作者或管理員確認。
  private readable(value: Article | Comment): boolean {
    return value.visibility === 'visible' || value.author.id === this.session.account()?.id
      || this.session.account()?.role === 'admin';
  }

  // 同 JWT 仍用兩個世代隔離舊寫入及舊查證；登入等待或撤權立即使資格失效。
  private currentInspection(operation: WriteOperation, credential: Session, generation: number, proofGeneration: number): boolean {
    return generation === operation.generation && proofGeneration === operation.proofGeneration
      && this.session.owns(credential) && this.session.state() === 'active' && !this.session.operation()
      && (!operation.origin || this.sameActor(operation));
  }
}
