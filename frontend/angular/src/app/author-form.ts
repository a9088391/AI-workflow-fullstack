import { Component, inject, input, output } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Comment, Problem, Session } from './contracts';
import { AuthorEditor } from './author-editor';

@Component({
  selector: 'app-author-form',
  imports: [FormsModule],
  templateUrl: './author-form.html',
  styleUrl: './author-form.css',
})
export class AuthorForm {
  readonly editor = inject(AuthorEditor);
  readonly content = this.editor.content;
  readonly session = this.editor.session;
  readonly writes = this.editor.writes;
  readonly mode = input<'page' | 'comment-new'>('page');
  readonly reload = output<void>();
  readonly copyLog = output<string>();
  readonly visibility = output<{ kind: 'comment'; resource: Comment }>();
  readonly Object = Object;
  readonly search = window.location.search;
  readonly encodeURIComponent = encodeURIComponent;

  // 父文章讀取仍回報外殼失權協調，作者元件不建立第二份認證或閱讀真相。
  readonly invalidated = output<{ sent: Session; failure: Problem }>();

  // 父文章重新確認由原讀取 owner 執行，只有明確失權才交回外殼。
  readParent(): void {
    void this.content.readParent(
      // 在途寫入是否未知仍由共同寫入 owner 隨外殼撤權處理。
      (sent, failure) => this.invalidated.emit({ sent, failure }),
    );
  }
}
