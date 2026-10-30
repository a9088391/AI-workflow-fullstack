import { Injectable, signal } from '@angular/core';
import {
  Account, apiRequest, BACKENDS, BACKEND_KEY, clearSessionIfOwned, isAccount, isAuthProblem,
  isAuthSuccess, isLogin, Problem, problem, readBackend, readSession, saveSession, Session, SESSION_KEY,
} from './contracts';

type AuthOperation = 'verify' | 'login' | 'register' | 'logout';
export type ResumeActor = { id: number; role: Account['role']; backend: string };
export type AuthResult = { success: true; cleared?: boolean }
  | { success: false; problem: Problem; status: number; retryAfter: string | null; unknown: boolean };

@Injectable({ providedIn: 'root' })
export class AccountSession {
  private readonly backendValue = signal<string>(BACKENDS.laravel[1]);
  private readonly credentialValue = signal<Session | null>(null);
  private readonly accountValue = signal<Account | null>(null);
  private readonly stateValue = signal<'checking' | 'guest' | 'active' | 'expired' | 'disabled' | 'error'>('checking');
  private readonly errorValue = signal<Problem | null>(null);
  private readonly operationValue = signal<AuthOperation | null>(null);
  private readonly storageProblemValue = signal(false);
  readonly backend = this.backendValue.asReadonly();
  readonly credential = this.credentialValue.asReadonly();
  readonly account = this.accountValue.asReadonly();
  readonly state = this.stateValue.asReadonly();
  readonly error = this.errorValue.asReadonly();
  readonly operation = this.operationValue.asReadonly();
  readonly storageProblem = this.storageProblemValue.asReadonly();
  resumeActor: ResumeActor | null = null;
  private controller: AbortController | null = null;
  private generation = 0;

  // 儲存不可用時仍允許公開閱讀，不能偷偷換用另一種憑證儲存位置。
  private storage(): Storage | null {
    try { return sessionStorage; }
    catch { this.storageProblemValue.set(true); return null; }
  }

  // 比對原 backend、JWT 與期限，舊失敗不得撤除較新的同分頁登入。
  owns(sent: Session): boolean {
    const current = this.credential();
    return Boolean(current && current.backend === sent.backend && current.accessToken === sent.accessToken
      && current.expiresAt === sent.expiresAt);
  }

  // 只有明確的失效或停用才撤權，網路與伺服器故障保留原憑證供手動重試。
  isAuthFailure(status: number, failure: Problem): boolean {
    return status === 401 && failure.code === 'E0002' || status === 403 && failure.code === 'E0102';
  }

  // 只撤銷本頁仍持有的身分，保留最小原作者摘要供原頁重登隔離草稿。
  invalidate(sent: Session, failure: Problem): boolean {
    if (!this.owns(sent)) return false;
    const account = this.account();
    if (account) this.resumeActor = { id: account.id, role: account.role, backend: sent.backend };
    this.cancel();
    const storage = this.storage();
    const cleared = Boolean(storage && clearSessionIfOwned(storage, sent));
    this.credentialValue.set(null);
    this.accountValue.set(null);
    this.stateValue.set(failure.code === 'E0102' ? 'disabled' : 'expired');
    this.errorValue.set(cleared ? failure : { ...failure, message: `${failure.message}；無法確認本機舊登入資料已清除，請關閉此分頁。` });
    return true;
  }

  // 導覽或欄位變更即作廢回應並結束等待；取消連線不代表伺服器已回滾。
  cancel(): void {
    this.generation++;
    this.controller?.abort();
    this.controller = null;
    this.operationValue.set(null);
  }

  // 重新讀取同分頁三欄並向目前後端驗證，不延長期限或以儲存資料推測權限。
  async initialize(): Promise<boolean> {
    if (this.operation() && this.operation() !== 'verify') return false;
    this.cancel();
    const storage = this.storage();
    this.backendValue.set(storage ? readBackend(storage) : BACKENDS.laravel[1]);
    this.credentialValue.set(storage ? readSession(storage) : null);
    this.accountValue.set(null);
    this.errorValue.set(null);
    if (!this.credential()) { this.stateValue.set('guest'); return true; }
    this.stateValue.set('checking');
    return await this.run('verify') !== null;
  }

  // 公開帳號操作不帶舊 JWT，並與驗權、登出共用唯一認證請求生命週期。
  submit(register: boolean, email: string, password: string, nickname: string): Promise<AuthResult | null> {
    return this.run(register ? 'register' : 'login', register ? { email, password, nickname } : { email, password });
  }

  // 登出只撤銷目前這次登入；失敗不先刪本機憑證，讓使用者可手動重試。
  logout(): Promise<AuthResult | null> { return this.run('logout'); }

  // 切換後端先刪憑證；若儲存中途失敗，回報是否已撤本頁權限供外殼清資料。
  changeBackend(next: string): { success: boolean; removed: boolean } {
    const storage = this.storage();
    if (!storage) return { success: false, removed: false };
    let removed = false;
    try {
      storage.removeItem(SESSION_KEY);
      removed = true;
      storage.setItem(BACKEND_KEY, next);
      this.backendValue.set(next);
      return { success: true, removed };
    } catch { return { success: false, removed }; }
    finally {
      if (removed) {
        this.cancel();
        this.credentialValue.set(null); this.accountValue.set(null); this.stateValue.set('guest');
        this.errorValue.set(null); this.resumeActor = null;
      }
    }
  }

  // 四種認證操作共用互斥、逾時及世代判斷，但各自驗證明確的成功資料契約。
  private async run(kind: AuthOperation, payload?: object): Promise<AuthResult | null> {
    if (this.operation()) return null;
    const sent = this.credential();
    if ((kind === 'logout' || kind === 'verify') && !sent) return null;
    const backend = this.backend();
    const generation = ++this.generation;
    const controller = new AbortController();
    this.controller = controller;
    this.operationValue.set(kind);
    let result: AuthResult;
    let data: any;
    try {
      const { response, body, retryAfter } = await apiRequest(backend, kind === 'verify' ? '/api/me' : `/api/auth/${kind}`, {
        method: kind === 'verify' ? 'GET' : 'POST', body: payload,
        token: kind === 'verify' || kind === 'logout' ? sent?.accessToken : undefined,
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20_000)]),
      });
      data = body.data;
      const complete = isAuthSuccess(response.status, body, kind === 'register' ? 201 : 200)
        && (kind === 'verify' ? isAccount(data) : kind === 'login' ? isLogin(data) : Object.keys(data).length === 0);
      if (complete) result = { success: true };
      else if (response.status >= 400 && isAuthProblem(body)) {
        result = { success: false, problem: problem(response.status, body), status: response.status, retryAfter,
          unknown: response.status >= 500 };
      } else result = { success: false, problem: { message: '回應資料格式不正確，請確認目前後端。' }, status: 0, retryAfter: null, unknown: true };
    } catch {
      result = { success: false, problem: { message: '無法確認伺服器回應，請確認目前後端後手動重試。' }, status: 0, retryAfter: null, unknown: true };
    } finally {
      // 原請求先結束等待，之後才可能撤權；較新請求的 controller 與 pending 不受影響。
      if (generation === this.generation) { this.controller = null; this.operationValue.set(null); }
    }
    if (controller.signal.aborted || generation !== this.generation || backend !== this.backend()
      || sent && !this.owns(sent)) return null;

    // 僅目前請求可提交狀態；不完整或失敗回應不保存新 JWT、不宣稱註冊或登出成功。
    if (result.success) {
      if (kind === 'verify') { this.accountValue.set(data); this.stateValue.set('active'); }
      else if (kind === 'login') {
        const credential = { backend, accessToken: data.access_token, expiresAt: data.expires_at };
        // 先確認同分頁憑證保存成功才公開 active 狀態，避免顯示已登入卻無法延續或重驗。
        try {
          const storage = this.storage();
          if (!storage || !saveSession(storage, credential)) throw new Error('storage');
        } catch {
          return { success: false, problem: { message: '無法保存登入狀態，請檢查瀏覽器設定後重試。' }, status: 0, retryAfter: null, unknown: true };
        }
        this.credentialValue.set(credential); this.accountValue.set(data.user); this.stateValue.set('active');
        this.errorValue.set(null); this.resumeActor = null;
      } else if (kind === 'logout' && sent) {
        const storage = this.storage();
        result.cleared = Boolean(storage && clearSessionIfOwned(storage, sent));
        this.credentialValue.set(null); this.accountValue.set(null); this.stateValue.set('guest');
        this.errorValue.set(null); this.resumeActor = null;
      }
    } else if (sent && (kind === 'verify' || kind === 'logout') && this.isAuthFailure(result.status, result.problem)) {
      this.invalidate(sent, result.problem);
    } else if (kind === 'verify') {
      this.stateValue.set('error'); this.errorValue.set(result.problem);
    }
    return result;
  }
}
