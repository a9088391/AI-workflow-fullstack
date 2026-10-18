<script>
  import { getContext, onMount } from 'svelte';
  import { page } from '$app/stores';
  import { BACKENDS, BASE, describeFailure, isArticle, isComment, isPage, positiveNumber } from '$lib/contracts.js';

  const site = getContext('site');
  const articleId = positiveNumber($page.params.article_id);
  const requestedPage = positiveNumber($page.url.searchParams.get('page')) || 1;
  let article = null;
  let comments = null;
  let articleLoading = true;
  let commentsLoading = false;
  let articleError = null;
  let commentsError = null;

  // 文章與留言分開讀取，留言失敗不抹去已確認可讀的文章。
  async function loadArticle() {
    if (!articleId) { articleError = '找不到指定資料或 API'; articleLoading = false; return; }
    articleLoading = true; articleError = null; article = null; comments = null;
    try {
      let data;
      try { data = await site.request(`/api/articles/${articleId}`); }
      catch (failure) {
        if (!['E0002', 'E0102'].includes(failure?.code)) throw failure;
        // 無效登入清除後仍可按訪客身分嘗試公開文章。
        data = await site.request(`/api/articles/${articleId}`);
      }
      if (!isArticle(data) || data.id !== articleId) throw new Error('回應資料格式不正確，請確認目前後端。');
      article = data;
      void loadComments();
    } catch (failure) {
      articleError = describeFailure(failure, '讀取文章失敗，請確認目前後端後重試。');
    } finally { articleLoading = false; }
  }

  // 留言分頁與文章讀取各自保留載入、空、錯誤及超頁狀態。
  async function loadComments() {
    if (!article) return;
    commentsLoading = true; commentsError = null; comments = null;
    const path = `/api/articles/${articleId}/comments?page=${requestedPage}&per_page=20`;
    try {
      let data;
      try { data = await site.request(path); }
      catch (failure) {
        if (!['E0002', 'E0102'].includes(failure?.code)) throw failure;
        // 憑證失效後文章可能已下架，須先撤下舊內文並以訪客重新驗證。
        void loadArticle();
        return;
      }
      if (!isPage(data, requestedPage) || !data.list.every(
        // 每則留言都必須屬於目前文章，錯置資料不可混入畫面。
        (item) => isComment(item, articleId)
      )) throw new Error('回應資料格式不正確，請確認目前後端。');
      comments = data;
    } catch (failure) {
      commentsError = describeFailure(failure, '讀取留言失敗，請確認目前後端後重試。');
    } finally { commentsLoading = false; }
  }

  // 深層網址直接開啟與重新整理，都由目前後端即時判定可讀性。
  onMount(() => { void loadArticle(); });
</script>

<svelte:head><title>{article?.title || '文章閱讀'}｜文章集</title></svelte:head>

<div class="reading-wrap">
  <a class="back-link" href={`${BASE}/`}>← 返回文章列表</a>
  {#if articleLoading}
    <p class="state-panel" role="status">文章載入中…</p>
  {:else if articleError}
    <div class="state-panel state-error" role="alert"><p>{articleError}</p><button class="button" type="button" onclick={loadArticle}>重新讀取</button></div>
  {:else if article}
    <article class="reading-card">
      <h1>{article.title}</h1>
      <dl class="meta-row">
        <div><dt>作者</dt><dd>{article.author.nickname}</dd></div>
        <div><dt>發布日期</dt><dd>{article.created_at.slice(0, 10)}</dd></div>
        <div><dt>建立於</dt><dd class="accent-text">{BACKENDS[article.created_backend]?.[0] || article.created_backend}</dd></div>
      </dl>
      <div class="reading-body">{article.body}</div>
    </article>
    <section class="comments-section" aria-labelledby="comments-heading">
      <h2 id="comments-heading">留言<span class="title-dot">.</span></h2>
      {#if commentsLoading}
        <p class="state-panel" role="status">留言載入中…</p>
      {:else if commentsError}
        <div class="state-panel state-error" role="alert"><p>{commentsError}</p><button class="button" type="button" onclick={loadComments}>重新讀取留言</button></div>
      {:else if comments?.list.length === 0}
        <div class="state-panel"><p>{comments.pagination.total === 0 ? '尚無留言。' : '此頁沒有留言。'}</p>
          {#if comments.pagination.total > 0}<a class="button" href={`${BASE}/articles/${articleId}`}>回留言第一頁</a>{/if}
        </div>
      {:else if comments}
        <div class="comment-list">
          {#each comments.list as comment (comment.id)}
            <article class="comment-item">
              <dl class="meta-row">
                <div><dt>作者</dt><dd>{comment.author.nickname}</dd></div>
                <div><dt>發布日期</dt><dd>{comment.created_at.slice(0, 10)}</dd></div>
                <div><dt>建立於</dt><dd class="accent-text">{BACKENDS[comment.created_backend]?.[0] || comment.created_backend}</dd></div>
              </dl>
              <p class="comment-body">{comment.body}</p>
            </article>
          {/each}
        </div>
        <nav class="pagination" aria-label="留言分頁">
          {#if requestedPage > 1}<a class="button button-outline" href={`${BASE}/articles/${articleId}?page=${requestedPage - 1}`}>上一頁</a>{/if}
          <span>第 {comments.pagination.page} 頁，共 {comments.pagination.total} 則</span>
          {#if requestedPage < comments.pagination.total_pages}<a class="button button-outline" href={`${BASE}/articles/${articleId}?page=${requestedPage + 1}`}>下一頁</a>{/if}
        </nav>
      {/if}
      <p class="read-only-note">此版本目前提供留言閱讀；留言發表將於下一項提供。</p>
    </section>
  {/if}
</div>
