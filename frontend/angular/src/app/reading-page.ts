import { CommonModule } from '@angular/common';
import { Component, TemplateRef, inject, input, output } from '@angular/core';
import { Article, Comment, Problem, Session } from './contracts';
import { ReadingContent } from './reading-content';

export type ReadingWriterContext = { article?: Article; comment?: Comment };

@Component({
  selector: 'app-reading-page',
  imports: [CommonModule],
  templateUrl: './reading-page.html',
  styleUrl: './reading-page.css',
})
export class ReadingPage {
  readonly content = inject(ReadingContent);
  readonly session = this.content.session;
  readonly writeBlocked = input(false);
  readonly writer = input<TemplateRef<ReadingWriterContext> | null>(null);
  readonly reload = output<void>();
  readonly visibility = output<{ kind: 'article'; resource: Article } | { kind: 'comment'; resource: Comment }>();
  readonly invalidated = output<{ sent: Session; failure: Problem }>();
  readonly copyLog = output<string>();

  // 子請求確定失權時只回報外殼，保留既有 writer 對在途未知結果的處理順序。
  private readonly invalidate = (sent: Session, failure: Problem): void => this.invalidated.emit({ sent, failure });

  // 單獨重讀留言不授予人工寫入查證資格，主文章與草稿保持原樣。
  readComments(): void { void this.content.readComments(this.invalidate); }

  // 父文章重試沿用原留言關聯，只有確認可讀公開文章才顯示編修入口。
  readParent(): void { void this.content.readParent(this.invalidate); }
}
