import { afterNextRender, Component, ElementRef, inject, Injector, input, OnDestroy, output, signal, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { AccountSession, ResumeActor } from './account-session';
import { countdown, Problem, retryAfterSeconds, validateAccount } from './contracts';

@Component({
  selector: 'app-account-form',
  imports: [FormsModule],
  templateUrl: './account-form.html',
  styleUrl: './account-form.css',
})
export class AccountForm implements OnDestroy {
  readonly session = inject(AccountSession);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  readonly mode = input<'login' | 'register' | 'reauth'>('login');
  readonly blocked = input(false);
  readonly dirtyChange = output<boolean>();
  readonly completed = output<{ register: boolean; previous: ResumeActor | null }>();
  readonly errors = signal<Record<string, string[]>>({});
  readonly notice = signal<Problem | null>(null);
  readonly lockedTime = signal('');
  readonly lockEnded = signal(false);
  @ViewChild('summary') private summary?: ElementRef<HTMLElement>;
  email = '';
  password = '';
  nickname = '';
  private deadline = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private generation = 0;

  // 本元件銷毀後舊帳號回應及倒數不再回寫；內容寫入仍由原頁持有。
  ngOnDestroy(): void { this.cancel(); }

  // 欄位 ID 區分原頁重登，摘要只聚焦同一張表單內的控制。
  fieldId(key: string): string { return `${this.mode() === 'reauth' ? 'reauth-' : ''}${key}`; }

  // 只有實際存在的欄位有連結；未知 API 欄位仍以文字呈現，不能吞掉錯誤。
  errorEntries(): [string, string[]][] { return Object.entries(this.errors()); }

  // 重登沒有暱稱輸入，不為後端額外明細建立無效錨點。
  hasField(key: string): boolean { return key === 'email' || key === 'password' || key === 'nickname' && this.mode() === 'register'; }

  // 保留實際路徑與查詢供原生連結語意，處理器只移焦，不重新載入或遺失草稿。
  fieldHref(key: string): string { return `${window.location.pathname}${window.location.search}#${this.fieldId(key)}`; }

  // 摘要點擊只移到對應欄位，避免頁內錯誤導覽變成重新載入。
  focusField(event: Event, key: string): void {
    event.preventDefault();
    this.element.nativeElement.querySelector<HTMLInputElement>(`#${this.fieldId(key)}`)?.focus();
  }

  // 送出後才聚焦摘要；每次輸入不搶焦點，讓鍵盤可連續修正欄位。
  private focusSummary(): void {
    afterNextRender(
      // 等 Angular 實際繪製錯誤摘要後再聚焦，避免只更新 signal 卻找不到 DOM。
      () => this.summary?.nativeElement.focus(), { injector: this.injector },
    );
  }

  // 已送出的帳號請求不能以新欄位繼續採用；清掉本表單倒數但不聲稱伺服器解除鎖定。
  fieldChanged(): void {
    const pending = this.session.operation() === 'login' || this.session.operation() === 'register';
    this.cancel();
    this.errors.set({}); this.lockEnded.set(false);
    this.notice.set(pending ? { message: '先前帳號請求的結果尚未確認；請先確認帳號或登入狀態，再手動重試。' } : null);
    this.dirtyChange.emit(true);
  }

  // 外殼接受離頁或輸入變更時即終止倒數與回應資格，不等待 unload 才清理。
  cancel(): void {
    this.generation++;
    this.clearCountdown();
    if (this.session.operation() === 'login' || this.session.operation() === 'register') this.session.cancel();
  }

  // 先做欄位驗證再送一次帳號操作，倒數及其他認證／內容等待中連 Enter 也不能送出。
  async submit(event: Event): Promise<void> {
    event.preventDefault();
    if (this.blocked() || this.session.operation() || this.lockedTime()) return;
    const register = this.mode() === 'register';
    const checked = validateAccount(this.email, this.password, register ? this.nickname : undefined);
    this.errors.set(checked.errors);
    if (Object.keys(checked.errors).length) {
      this.notice.set({ message: '請修正標示欄位後再送出。' }); this.focusSummary(); return;
    }
    const generation = ++this.generation;
    // 登入成功後 service 會清掉舊身分摘要，必須先捕捉才能在原頁判斷草稿是否屬同一人。
    const previous = this.session.resumeActor;
    this.notice.set(null); this.lockEnded.set(false);
    const result = await this.session.submit(register, checked.email, this.password, this.nickname);
    if (generation !== this.generation || !result) return;
    if (result.success) {
      this.password = ''; this.clearCountdown(); this.dirtyChange.emit(false);
      this.completed.emit({ register, previous });
      return;
    }
    this.errors.set(result.problem.errors || {});
    this.notice.set(result.unknown ? { ...result.problem, message: `結果尚未確認；${result.problem.message} 請先查證${register ? '帳號' : '登入'}狀態，再手動重試。` } : result.problem);
    if (Object.keys(this.errors()).length) this.focusSummary();
    if (!register && result.status === 429 && result.problem.code === 'E0105') {
      if (retryAfterSeconds(result.retryAfter) === null) {
        this.notice.set({ ...result.problem, message: `${result.problem.message}；回應缺少有效 Retry-After，無法顯示倒數。` });
      } else this.startCountdown(result.retryAfter);
    }
  }

  // 只接受伺服器的有效 Retry-After，不輪詢或自行延長鎖定。
  private startCountdown(value: string | null): void {
    this.clearCountdown();
    const seconds = retryAfterSeconds(value);
    if (!seconds) return;
    this.deadline = Date.now() + seconds * 1000;
    this.tickCountdown();
    this.timer = setInterval(
      // 每次依絕對截止重算，背景分頁節流不會讓使用者多等。
      () => this.tickCountdown(), 250,
    );
  }

  // 歸零只恢復控制及提示，不自動登入、不自動重送帳密。
  private tickCountdown(): void {
    const remaining = countdown(this.deadline);
    this.lockedTime.set(remaining);
    if (!remaining) { this.clearCountdown(); this.notice.set(null); this.lockEnded.set(true); }
  }

  // 清除本表單的 timer；這不會改寫後端的鎖定或其他分頁。
  private clearCountdown(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null; this.deadline = 0; this.lockedTime.set('');
  }

  // 追查編號可手動複製，失敗時保留畫面文字供使用者選取。
  async copyLogId(value: string): Promise<void> {
    try { await navigator.clipboard.writeText(value); }
    catch { this.notice.set({ ...this.notice()!, message: '無法自動複製；請選取畫面上的 log_id。' }); }
  }
}
