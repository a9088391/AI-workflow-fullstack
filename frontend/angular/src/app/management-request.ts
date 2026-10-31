import { signal } from '@angular/core';
import { AccountSession } from './account-session';
import { apiRequest, isAuthSuccess, Problem, problem, Session } from './contracts';

// 每個管理 owner 各持有一個請求生命週期；共用驗權及取消，不另存帳號或資源真相。
export class ManagementRequest {
  readonly loading = signal(false);
  readonly error = signal<Problem | null>(null);
  invalidated: ((sent: Session, failure: Problem) => void) | null = null;
  private controller: AbortController | null = null;

  // 使用既有 session 判斷目前權限，避免管理頁各自保存憑證。
  constructor(private readonly session: AccountSession) {}

  // 失權、重讀或離頁立即結束等待；abort 不能被舊回應的 finally 重新覆蓋。
  clear(): void {
    this.controller?.abort(); this.controller = null;
    this.loading.set(false); this.error.set(null);
  }

  // 完整管理讀取才交由頁面更新資料；頁面決定資源形狀及如何保留未送出表單。
  async read<T>(path: string, accepts: (value: unknown) => value is T, accept: (value: T) => void): Promise<void> {
    // 先取消上次讀取，再依目前憑證與角色擋住管理資料；不從資源回應推測授權。
    this.clear();
    const credential = this.session.state() === 'active' ? this.session.credential() : null;
    if (!credential) return;
    if (this.session.account()?.role !== 'admin') {
      this.error.set({ message: '沒有權限使用此功能', code: 'E0003' });
      return;
    }
    // 每次重讀各有取消資格，即使 JWT 相同也不能讓較早請求回填新頁面狀態。
    const controller = new AbortController();
    this.controller = controller; this.loading.set(true);
    try {
      const { response, body } = await apiRequest(credential.backend, path,
        { token: credential.accessToken, signal: controller.signal });
      // 寫入頁面前先確認請求仍屬目前身分；完整 envelope 與資源形狀都通過才採用。
      if (!this.current(controller, credential)) return;
      if (isAuthSuccess(response.status, body, 200) && accepts(body.data)) { accept(body.data); return; }
      // 破損成功只當讀取失敗；僅 session 明確判定的失權交還外殼，5xx 不撤銷登入。
      const failure = problem(response.status, body);
      this.error.set(response.ok && body.success === true
        ? { message: '回應資料格式不正確，請確認目前後端。' } : failure);
      if (this.session.isAuthFailure(response.status, failure)) this.invalidated?.(credential, failure);
    } catch {
      // 取消或較早請求的失敗不可蓋過新結果，清理也只能結束自己的等待。
      if (this.current(controller, credential)) this.error.set({ message: '讀取失敗，請確認目前後端後重試。' });
    } finally {
      if (this.current(controller, credential)) this.loading.set(false);
    }
  }

  // 同 JWT 的重讀也需隔離；較早請求、異身分與新後端都不能回填目前頁面。
  private current(controller: AbortController, credential: Session): boolean {
    return this.controller === controller && !controller.signal.aborted
      && this.session.state() === 'active' && this.session.owns(credential);
  }
}
