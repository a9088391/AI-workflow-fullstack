import { afterNextRender, Component, ElementRef, Injectable, inject, Injector, output, signal, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { AccountSession } from './account-session';
import { isLockoutSettings, LockoutSettings, settingErrors } from './admin';
import { ManagementRequest } from './management-request';
import { OrdinaryWrites, WriteOperation } from './ordinary-writes';

@Injectable()
export class LockoutSettingsPage {
  readonly session = inject(AccountSession);
  readonly writes = inject(OrdinaryWrites);
  readonly request = new ManagementRequest(this.session);
  readonly active = window.location.pathname === '/angular/admin/settings/login-lockout';
  readonly settings = signal<LockoutSettings | null>(null);
  draft: LockoutSettings = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  dirty = false;
  private currentOperation: WriteOperation | null = null;

  // 初始化發生於外殼 ngOnInit；模板不可建立 signals 或另一套操作結果。
  initializeOperation(): void {
    if (this.active && !this.currentOperation) {
      this.currentOperation = this.writes.operation('settings', '登入鎖定設定', '/api/settings/login-lockout', 'PUT', { kind: 'settings' });
    }
  }

  // 只讀初始化的同一筆操作，欄位錯誤與未知查證皆沿用共同 owner。
  get operation(): WriteOperation { return this.currentOperation!; }

  // 撤掉私有伺服器資料及請求，但同帳重登仍可保留記憶體草稿。
  clear(): void { this.request.clear(); this.settings.set(null); }

  // 成功登出或異帳／角色才清原稿；不向新身分帶入舊設定。
  clearDraft(): void {
    this.dirty = false;
    this.draft = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 };
  }

  // 一般 GET 只更新目前伺服器值；dirty 草稿不因失敗、重讀或同帳登入而被覆蓋。
  async read(): Promise<void> {
    if (!this.active) return;
    this.clear();
    await this.request.read('/api/settings/login-lockout', isLockoutSettings,
      // 只在沒有未儲存輸入時初始化，查證資格仍由 OrdinaryWrites.inspect 提供。
      (value) => { this.settings.set(value); if (!this.dirty) this.draft = { ...value }; },
    );
  }

  // 改字只清本表單欄位錯誤，未知結果與其他操作警告不能一起清除。
  changed(): void { this.dirty = true; this.operation.errors.set({}); }

  // 四欄完整驗證並一次提交，關閉時仍保留數值；未知結果不可盲目再送。
  async submit(event: Event): Promise<void> {
    event.preventDefault();
    if (!this.settings() || this.writes.blocked('settings') || this.session.account()?.role !== 'admin') return;
    const errors = settingErrors(this.draft);
    this.operation.errors.set(errors);
    if (Object.keys(errors).length) return;
    const sent = { ...this.draft };
    await this.writes.run(this.operation, sent,
      // 完整成功才清 dirty；畫面重讀失敗仍明示已提交，不改成未知結果。
      () => { this.dirty = false; this.operation.problem.set({ message: '設定已提交，正在重新讀取伺服器目前值。' }); void this.read(); },
    );
  }

  // 四個已知欄位可連到輸入，未知欄位仍保留在摘要中。
  errorEntries(): [string, string[]][] { return Object.entries(this.operation.errors()).filter(
    // 不為後端未知欄位建立不存在的焦點目標。
    ([key]) => ['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(key),
  ); }

  // 顯示無對應控制的驗證明細，避免只剩無法理解的通用錯誤。
  otherErrors(): string[] { return Object.entries(this.operation.errors()).filter(
    // 已知欄位已由可聚焦連結及行內訊息呈現。
    ([key]) => !['enabled', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(key),
  ).flatMap(
    // 保留後端欄位名稱與所有安全訊息。
    ([key, errors]) => errors.map(
      // 同欄多則錯誤不互相覆蓋。
      (message) => `${key}：${message}`,
    ),
  ); }
}

@Component({
  selector: 'app-lockout-settings',
  imports: [FormsModule],
  templateUrl: './lockout-settings.html',
  styleUrl: './management.css',
})
export class LockoutSettingsForm {
  readonly content = inject(LockoutSettingsPage);
  readonly copyLog = output<string>();
  readonly edited = output<void>();
  readonly Object = Object;
  readonly path = window.location.pathname + window.location.search;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  @ViewChild('summary') private summary?: ElementRef<HTMLElement>;

  // 本機與伺服器欄位拒絕後均聚焦摘要；等待 DOM 更新才找目標，不在每次改字搶焦點。
  async submit(event: Event): Promise<void> {
    await this.content.submit(event);
    if (Object.keys(this.content.operation.errors()).length) {
      afterNextRender(
        // 若已失權卸下表單，摘要不存在就不把焦點拉回私有資料。
        () => this.summary?.nativeElement.focus(), { injector: this.injector },
      );
    }
  }

  // 摘要連到實際控制並給鍵盤焦點，避免只有 hash 或捲動變化。
  focusField(event: Event, key: string): void {
    event.preventDefault();
    this.element.nativeElement.querySelector<HTMLInputElement>(`#${key}`)?.focus();
  }
}
