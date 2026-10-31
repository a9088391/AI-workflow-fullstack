import { Component, Injectable, inject, output, signal } from '@angular/core';
import { AccountSession } from './account-session';
import { Article, BACKENDS, Comment, isReadingArticle, isReadingComment, isReadingPage, Page, positiveNumber } from './contracts';
import { ManagementRequest } from './management-request';
import { OrdinaryWrites } from './ordinary-writes';

@Injectable()
export class ContentManagement {
  readonly session = inject(AccountSession);
  readonly writes = inject(OrdinaryWrites);
  readonly request = new ManagementRequest(this.session);
  readonly articleList = window.location.pathname === '/angular/admin/articles';
  readonly active = this.articleList || window.location.pathname === '/angular/admin/comments';
  readonly page = positiveNumber(new URLSearchParams(window.location.search).get('page')) || 1;
  readonly articles = signal<Page<Article> | null>(null);
  readonly comments = signal<Page<Comment> | null>(null);

  // 管理範圍資料只屬此 owner；撤權時不留在閱讀 owner 或下一個身分中。
  clear(): void { this.request.clear(); this.articles.set(null); this.comments.set(null); }

  // 管理文章明確傳 management scope，留言使用既有管理端點，皆保持原頁碼。
  async read(): Promise<void> {
    if (!this.active) return;
    this.clear();
    if (this.articleList) {
      await this.request.read(`/api/articles?page=${this.page}&per_page=20&filters%5Bscope%5D=management`,
        // 共用內容 guard 防止破損項目被呈現成可下架資源。
        (value): value is Page<Article> => isReadingPage(value, this.page, isReadingArticle),
        // 管理列表只接受目前請求；閱讀資料不會被此範圍取代。
        (value) => this.articles.set(value),
      );
    } else {
      await this.request.read(`/api/comments?page=${this.page}&per_page=20`,
        // 管理留言仍包含個別及父文章下架資料，不套用一般閱讀篩選。
        (value): value is Page<Comment> => isReadingPage(value, this.page, isReadingComment),
        // 留言資料獨立於文章列表與其他操作的查證結果。
        (value) => this.comments.set(value),
      );
    }
  }

  // 寫入規則與結果由唯一共同 owner 判讀；此頁只負責自己的資料重讀。
  changeVisibility(kind: 'article' | 'comment', resource: Article | Comment): void {
    this.writes.changeVisibility(kind, resource,
      // 自動重讀不代表使用者已查證任何其他操作。
      () => { void this.read(); },
    );
  }

  // 來源只映射既有固定清單，未知來源維持中性顯示。
  backendLabel(id: string): string { return Object.hasOwn(BACKENDS, id) ? BACKENDS[id as keyof typeof BACKENDS][0] : '未知來源'; }

  // 管理分頁只搬目前範圍與伺服器頁碼，不加入新排序或搜尋。
  pageHref(page: number): string { return `/angular/admin/${this.articleList ? 'articles' : 'comments'}?page=${page}`; }
}

@Component({
  selector: 'app-content-management',
  templateUrl: './content-management.html',
  styleUrl: './management.css',
})
export class ContentManagementPage {
  readonly content = inject(ContentManagement);
  readonly copyLog = output<string>();
}
