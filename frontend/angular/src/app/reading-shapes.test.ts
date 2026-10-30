import assert from 'node:assert/strict';
import test from 'node:test';
import { isAuthSuccess, isReadingArticle, isReadingComment, isReadingPage } from './contracts.ts';

const article = { id: 101, title: '林間的文字', body: '第一行\r\n第二行', author: { id: 7, nickname: '小草' },
  version: 2, created_backend: 'laravel', created_at: '2026-10-30 09:00:00', updated_at: '2026-10-30 10:00:00',
  visibility: 'visible', can_restore: false };
const comment = { id: 201, body: '原文\n不裁切', author: article.author, article_id: 101, version: 1,
  created_backend: 'nestjs', created_at: article.created_at, updated_at: article.created_at,
  visibility: 'hidden', can_restore: true };

// 成功旗標與 data 不能替代完整 GET envelope，避免破損列表被誤認為已讀成功。
test('閱讀只接受 200／OK／object 成功 envelope', function readingEnvelope() {
  const body = { success: true, message: 'OK', data: article };
  assert.equal(isAuthSuccess(200, body, 200), true);
  for (const broken of [{ success: true, data: article }, { ...body, message: '' }, { ...body, data: null }, { ...body, data: [] }]) {
    assert.equal(isAuthSuccess(200, broken, 200), false);
  }
  assert.equal(isAuthSuccess(201, body, 200), false);
});

// 單篇及父文章必須吻合 requested ID；錯誤父文不得提供標題或留言編修資格。
test('閱讀文章形狀與目標識別', function articleShape() {
  assert.equal(isReadingArticle(article, 101), true);
  assert.equal(isReadingArticle(article, 999), false);
  assert.equal(isReadingArticle({ ...article, visibility: 'hidden' }, 101), true);
  for (const broken of [{ ...article, id: 0 }, { ...article, author: { id: 0, nickname: '小草' } },
    { ...article, version: 0 }, { ...article, created_at: null }, { ...article, updated_at: undefined },
    { ...article, created_backend: 'unknown' }, { ...article, title: null }, { ...article, can_restore: null }]) {
    assert.equal(isReadingArticle(broken), false);
  }
});

// 留言可獨立於父文可見性讀取，但仍須有合法父 ID 及完整操作欄位。
test('閱讀留言保留父關聯與個別下架資料', function commentShape() {
  assert.equal(isReadingComment(comment, 201), true);
  assert.equal(isReadingComment(comment, 999), false);
  for (const broken of [{ ...comment, article_id: 0 }, { ...comment, article_id: '101' }, { ...comment, article_id: null },
    { ...comment, body: null }, { ...comment, version: true }, { ...comment, visibility: 'unknown' }]) {
    assert.equal(isReadingComment(broken), false);
  }
});

// 正常、首頁空資料與合法超頁均保留 API 分頁；錯頁、錯筆數與殘缺資源必須回錯誤。
test('閱讀列表與超頁契約', function pageShape() {
  const page = { list: [article], pagination: { page: 1, per_page: 20, total: 1, total_pages: 1 } };
  assert.equal(isReadingPage(page, 1, isReadingArticle), true);
  assert.equal(isReadingPage({ list: [comment], pagination: page.pagination }, 1, isReadingComment), true);
  assert.equal(isReadingPage({ list: [], pagination: { page: 1, per_page: 20, total: 0, total_pages: 0 } }, 1, isReadingArticle), true);
  assert.equal(isReadingPage({ list: [], pagination: { page: 3, per_page: 20, total: 21, total_pages: 2 } }, 3, isReadingArticle), true);
  assert.equal(isReadingPage(page, 2, isReadingArticle), false);
  for (const pagination of [{ ...page.pagination, page: null }, { ...page.pagination, per_page: 10 },
    { ...page.pagination, total: -1 }, { ...page.pagination, total: '1' }, { ...page.pagination, total_pages: 0 },
    { ...page.pagination, page: 2 }]) {
    assert.equal(isReadingPage({ ...page, pagination }, pagination.page as number, isReadingArticle), false);
  }
  assert.equal(isReadingPage({ list: [{ ...article, version: 0 }], pagination: page.pagination }, 1, isReadingArticle), false);
  assert.equal(isReadingPage({ list: [article, article], pagination: { ...page.pagination, total: 2 } }, 1, isReadingArticle), false);
});
