import { afterNextRender, Component, effect, ElementRef, Injectable, inject, Injector, output, signal, ViewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { AccountSession } from './account-session';
import { isReadingPage, Page, positiveNumber } from './contracts';
import { isMember, Member, memberKeyword } from './admin';
import { ManagementRequest } from './management-request';
import { OrdinaryWrites } from './ordinary-writes';

@Injectable()
export class MemberManagement {
  readonly session = inject(AccountSession);
  readonly writes = inject(OrdinaryWrites);
  readonly request = new ManagementRequest(this.session);
  readonly query = new URLSearchParams(window.location.search);
  readonly page = positiveNumber(this.query.get('page')) || 1;
  readonly memberId = window.location.pathname.match(/^\/angular\/admin\/members\/([0-9]+)$/)?.[1] || null;
  readonly validMemberId = this.memberId && positiveNumber(this.memberId) !== null ? this.memberId : null;
  readonly active = window.location.pathname === '/angular/admin/members' || Boolean(this.validMemberId);
  readonly members = signal<Page<Member> | null>(null);
  readonly member = signal<Member | null>(null);
  readonly fieldErrors = signal<Record<string, string[]>>({});
  keywordDraft = this.query.get('keyword') || '';
  navigate: ((url: string) => void) | null = null;

  // 私有會員資料隨認證清除；搜尋輸入仍屬此頁，不把取消當成已完成寫入。
  clear(): void { this.request.clear(); this.members.set(null); this.member.set(null); }

  // 會員列表與詳情各驗證自己的回應，合法超頁保留伺服器總數。
  async read(): Promise<void> {
    if (!this.active) return;
    this.clear();
    const keyword = memberKeyword(this.query.get('keyword') || '');
    if (!this.validMemberId && [...keyword].length > 254 && this.session.account()?.role === 'admin') {
      this.request.error.set({ message: '搜尋文字最多 254 個字。' }); return;
    }
    if (this.validMemberId) {
      await this.request.read(`/api/members/${this.validMemberId}`,
        // 詳情必須是原會員，不能把其他帳號當成可操作目標。
        (value): value is Member => isMember(value) && value.id === Number(this.validMemberId),
        // 只有仍擁有目前請求的完整回應可更新此頁；舊取消不再回寫 null。
        (value) => this.member.set(value),
      );
    } else {
      await this.request.read(`/api/members?page=${this.page}&per_page=20${keyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(keyword)}` : ''}`,
        // 共用分頁結構只檢查完整一般會員；管理員不進入此列表。
        (value): value is Page<Member> => isReadingPage(value, this.page, isMember),
        // 顯示資料仍由會員 owner 持有，不在請求共用層複製。
        (value) => this.members.set(value),
      );
    }
  }

  // 本機與後端拒絕共用同一欄位提示；已改字的草稿不沿用舊 Query 錯誤。
  keywordErrors(): string[] | undefined {
    const failure = this.request.error();
    return this.fieldErrors()['keyword'] || (this.keywordDraft === (this.query.get('keyword') || '')
      && failure?.code === 'E0001' ? failure.errors?.['filters.keyword'] : undefined);
  }

  // 搜尋只修剪 ASCII 空格並回第一頁；字元是否合法仍由共同 API 契約判定。
  search(event: Event): void {
    event.preventDefault();
    const keyword = memberKeyword(this.keywordDraft);
    if ([...keyword].length > 254) { this.fieldErrors.set({ keyword: ['搜尋文字最多 254 個字。'] }); return; }
    this.fieldErrors.set({});
    this.navigate?.(`/angular/admin/members${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ''}`);
  }

  // 空搜尋等同未篩選，避免把真正的空列表說成搜尋無結果。
  hasKeyword(): boolean { return Boolean(memberKeyword(this.query.get('keyword') || '')); }

  // 分頁保留同一個字面條件，重新搜尋則由 search 明確回第一頁。
  pageHref(page: number): string {
    const keyword = memberKeyword(this.query.get('keyword') || '');
    return `/angular/admin/members?page=${page}${keyword ? `&keyword=${encodeURIComponent(keyword)}` : ''}`;
  }

  // 每個會員狀態共用原寫入 owner 的獨立結果，只有完整成功才重讀。
  changeStatus(value: Member): void {
    const key = `member-status:${value.id}`;
    if (this.writes.blocked(key) || this.session.account()?.role !== 'admin') return;
    const next = value.status === 'active' ? 'disabled' : 'active';
    if (!window.confirm(`確定要${next === 'disabled' ? '停用' : '恢復'}會員 ${value.nickname} 嗎？`)) return;
    const operation = this.writes.operation(key, `會員 #${value.id} 狀態`, `/api/members/${value.id}/status`, 'PUT', { kind: 'member', id: value.id });
    void this.writes.run(operation, { status: next },
      // 重讀只更新會員資料，不清除另一列未知結果或授予查證資格。
      () => { operation.problem.set({ message: '會員狀態已更新，正在重新讀取。' }); void this.read(); },
    );
  }
}

@Component({
  selector: 'app-member-management',
  imports: [FormsModule],
  templateUrl: './member-management.html',
  styleUrl: './management.css',
})
export class MemberManagementPage {
  readonly content = inject(MemberManagement);
  readonly copyLog = output<string>();
  private readonly injector = inject(Injector);
  @ViewChild('summary') private summary?: ElementRef<HTMLElement>;

  // 新的 API 拒絕才安排聚焦；改字清除欄位錯誤不能再次搶走輸入焦點。
  constructor() {
    // 只觀察請求錯誤，避免每次清除本機驗證結果都重播舊的伺服器錯誤。
    effect((onCleanup) => {
      const failure = this.content.request.error();
      if (this.content.validMemberId || !failure) return;
      const render = afterNextRender(
        // 只聚焦仍屬這次拒絕的摘要，不把焦點拉回已清除的錯誤。
        () => {
          if (failure === this.content.request.error()) this.summary?.nativeElement.focus();
        }, { injector: this.injector },
      );
      // effect 重跑或卸載時撤掉尚未執行的渲染回呼。
      onCleanup(() => render.destroy());
    });
  }

  // 本機長度驗證只在送出遭拒時聚焦，清除錯誤及連續輸入不觸發聚焦。
  search(event: Event): void {
    this.content.search(event);
    const errors = this.content.fieldErrors()['keyword'];
    if (!errors) return;
    afterNextRender(
      // 渲染前若已修正或卸下表單，就不再把焦點帶回舊錯誤。
      () => { if (errors === this.content.fieldErrors()['keyword']) this.summary?.nativeElement.focus(); },
      { injector: this.injector },
    );
  }
}
