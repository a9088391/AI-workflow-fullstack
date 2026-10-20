<script>
  import { getContext, onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { page } from '$app/stores';
  import { BACKENDS, BASE, describeFailure, isArticle, isComment, isPage, positiveNumber, uncertainWrite, validateContent } from '$lib/contracts.js';
  import ReLogin from '$lib/ReLogin.svelte';

  const site = getContext('site');
  const { account, authState } = site;
  const path = $page.url.pathname;
  const articleId = positiveNumber($page.params.article_id);
  const requestedPage = positiveNumber($page.url.searchParams.get('page')) || 1;
  let article = null;
  let comments = null;
  let articleLoading = true;
  let commentsLoading = false;
  let articleError = null;
  let commentsError = null;
  let commentBody = '';
  let commentErrors = {};
  let writeMessage = '';
  let writeUnknown = false;
  let checkedUnknown = false;
  let visibilityConflict = false;
  let visibilityUnknown = false;
  let processing = false;
  let ownerId = null;
  let alive = true;
  let summary;

  // 留言草稿只屬於目前頁面，切換與離頁時須先確認捨棄。
  function markCommentDirty() { site.dirty.set(true); commentErrors = {}; }

  // 寫入結果無法確定時保留草稿，只有使用者查證後才能允許再次送出。
  function allowRetry() {
    if (!checkedUnknown) return;
    if (!window.confirm('已人工查證，確定要自行決定是否重送？重送可能產生重複留言。')) return;
    writeUnknown = false; checkedUnknown = false; writeMessage = '已解除鎖定；如需重試，請手動送出。';
  }

  // 登入後僅原帳號可接續本頁草稿；不同帳號立即清除舊文字。
  function discardOtherAccount() {
    commentBody = ''; commentErrors = {}; writeUnknown = false; checkedUnknown = false; site.dirty.set(false);
    ownerId = get(account)?.id || null;
    void loadArticle();
  }

  // 原帳號重登後重新確認文章權限，草稿仍待本人手動送出。
  function resumeSameAccount() { void loadArticle(); }

  // 發表留言只使用目前文章 ID，成功後回第一頁重讀，不猜新增 ID。
  async function submitComment(event) {
    event.preventDefault();
    // 先確認公開文章、原帳號與本次輸入，避免在失效身分下送出草稿。
    if (!article || article.visibility !== 'visible' || processing || writeUnknown
      || get(authState) !== 'active' || get(account)?.id !== ownerId) return;
    commentErrors = validateContent('', commentBody, true);
    if (Object.keys(commentErrors).length) { writeMessage = '請先修正留言內文。'; await tick(); summary?.focus(); return; }
    processing = true; site.pending.set(true); writeMessage = '';
    const generation = site.version();
    try {
      // 寫入成功不猜留言 ID，只回第一頁重新讀取後端結果。
      await site.request(`/api/articles/${articleId}/comments`, { method: 'POST', data: { body: commentBody } });
      if (!alive || generation !== site.version()) return;
      commentBody = ''; site.dirty.set(false); site.pending.set(false);
      window.location.assign(`${BASE}/articles/${articleId}`);
    } catch (failure) {
      if (!alive || generation !== site.version()) return;
      // 已送出的結果若不明確，保留文字並鎖住重送直到人工查證。
      commentErrors = failure?.code === 'E0001' ? failure.errors || {} : {};
      writeUnknown = uncertainWrite(failure); checkedUnknown = false;
      writeMessage = `${writeUnknown ? '留言結果尚未確認；請先查證，不會自動重送。' : ''}${describeFailure(failure, '留言結果尚未確認，請查證。')}`;
      await tick(); summary?.focus();
    } finally {
      // 僅清理本次世代的等待狀態，避免影響切換後的新頁。
      processing = false; if (alive && generation === site.version()) site.pending.set(false);
    }
  }

  // 下架／恢復以目前版本送出；管理員接管他人已下架內容時維持 hidden。
  async function changeVisibility(requested = false) {
    const takeOver = requested === true;
    if (!article || processing || visibilityConflict || visibilityUnknown || get(authState) !== 'active') return;
    if (takeOver && (get(account)?.role !== 'admin' || article.author.id === get(account)?.id || article.visibility !== 'hidden')) return;
    const next = takeOver || article.visibility === 'visible' ? 'hidden' : 'visible';
    if (!window.confirm(takeOver ? '確定接管這篇文章的恢復權？文章保持下架，原作者將不能恢復。' : `確定${next === 'hidden' ? '下架' : '恢復'}這篇文章？`)) return;
    processing = true; site.pending.set(true);
    const generation = site.version();
    try {
      await site.request(`/api/articles/${articleId}/visibility`, { method: 'PUT', data: { visibility: next, version: article.version } });
      if (alive && generation === site.version()) await loadArticle();
    } catch (failure) {
      if (!alive || generation !== site.version()) return;
      visibilityConflict = failure?.code === 'E0201'; visibilityUnknown = uncertainWrite(failure);
      writeMessage = `${visibilityUnknown ? '更新結果尚未確認；請先重讀同一篇文章查證。' : ''}${describeFailure(failure, '更新失敗，請查證。')}`;
    } finally { processing = false; if (alive && generation === site.version()) site.pending.set(false); }
  }

  // 管理員再次下架他人內容，沿用同一版本衝突與不明結果處理。
  function takeOverVisibility() { void changeVisibility(true); }

  // 重讀成功才解除版本衝突，避免用舊文章版本再次操作。
  async function reloadVisibility() {
    if (await loadArticle()) { visibilityConflict = false; visibilityUnknown = false; }
  }

  // 文章與留言分開讀取，留言失敗不抹去已確認可讀的文章。
  async function loadArticle() {
    if (!articleId) { articleError = '找不到指定資料或 API'; articleLoading = false; return false; }
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
      return true;
    } catch (failure) {
      articleError = describeFailure(failure, '讀取文章失敗，請確認目前後端後重試。');
      return false;
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
      if (writeUnknown) checkedUnknown = true;
    } catch (failure) {
      commentsError = describeFailure(failure, '讀取留言失敗，請確認目前後端後重試。');
    } finally { commentsLoading = false; }
  }

  // 深層網址直接開啟與重新整理時，先驗證登入再由後端判定文章可讀性。
  async function initializePage() {
    await site.initialize();
    ownerId = get(account)?.id || null;
    if (alive) await loadArticle();
  }

  // 離開文章頁時清除草稿與進行中的本頁狀態。
  onMount(() => {
    void initializePage();
    // 卸載後不再顯示此頁的離頁警示。
    return () => { alive = false; site.dirty.set(false); site.pending.set(false); };
  });
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
      {#if article.visibility === 'hidden'}<p class="notice">這篇文章目前已下架。</p>{/if}
      <dl class="meta-row">
        <div><dt>作者</dt><dd>{article.author.nickname}</dd></div>
        <div><dt>發布日期</dt><dd>{article.created_at.slice(0, 10)}</dd></div>
        <div><dt>建立於</dt><dd class="accent-text">{BACKENDS[article.created_backend]?.[0] || article.created_backend}</dd></div>
      </dl>
      <div class="reading-body">{article.body}</div>
      {#if $authState === 'active' && (article.author.id === $account?.id || $account?.role === 'admin')}
        <div class="author-actions">
          {#if article.author.id === $account?.id}<a class="button button-outline" href={`${BASE}/articles/${articleId}/edit`}>編輯文章</a>{/if}
          {#if article.visibility === 'visible' || article.can_restore}<button class="button" type="button" disabled={processing || visibilityConflict || visibilityUnknown} onclick={changeVisibility}>{article.visibility === 'visible' ? '下架文章' : '恢復文章'}</button>{/if}
          {#if article.visibility === 'hidden' && $account?.role === 'admin' && article.author.id !== $account?.id}<button class="button button-outline" type="button" disabled={processing || visibilityConflict || visibilityUnknown} onclick={takeOverVisibility}>接管下架權</button>{/if}
        </div>
      {/if}
      {#if visibilityConflict || visibilityUnknown}<div class="state-panel state-error"><p>{visibilityUnknown ? '更新結果尚未確認；請重讀同一篇文章查證。' : '版本已改變；請重讀後重新確認操作。'}</p><button class="button" type="button" onclick={reloadVisibility}>重新讀取</button></div>{/if}
    </article>
    <section class="comments-section" aria-labelledby="comments-heading">
      <h2 id="comments-heading">留言<span class="title-dot">.</span></h2>
      {#if commentsLoading}
        <p class="state-panel" role="status">留言載入中…</p>
      {:else if commentsError}
        <div class="state-panel state-error" role="alert"><p>{commentsError}</p><button class="button" type="button" onclick={loadComments}>重新讀取留言</button></div>
      {:else if comments?.list.length === 0}
        <!-- 留言零筆超頁不表示整篇沒有留言，須保留原頁碼與返回入口。 -->
        <div class="state-panel"><p>{comments.pagination.page > 1 || comments.pagination.total > 0 ? '此頁沒有留言。' : '尚無留言。'}</p>
          {#if comments.pagination.page > 1 || comments.pagination.total > 0}
            <p>第 {comments.pagination.page} 頁，共 {comments.pagination.total} 則</p>
            <a class="button" href={`${BASE}/articles/${articleId}`}>回留言第一頁</a>
          {/if}
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
              {#if $authState === 'active' && (comment.author.id === $account?.id || $account?.role === 'admin')}<a class="back-link" href={`${BASE}/comments/${comment.id}`}>查看留言詳情</a>{/if}
            </article>
          {/each}
        </div>
        <nav class="pagination" aria-label="留言分頁">
          {#if requestedPage > 1}<a class="button button-outline" href={`${BASE}/articles/${articleId}?page=${requestedPage - 1}`}>上一頁</a>{/if}
          <span>第 {comments.pagination.page} 頁，共 {comments.pagination.total} 則</span>
          {#if requestedPage < comments.pagination.total_pages}<a class="button button-outline" href={`${BASE}/articles/${articleId}?page=${requestedPage + 1}`}>下一頁</a>{/if}
        </nav>
      {/if}
      {#if writeMessage}<div class="form-summary" role="alert" tabindex="-1" bind:this={summary}><strong>{writeMessage}</strong>{#each Object.entries(commentErrors) as [field, messages]}<a href="#comment-body">{field}：{Array.isArray(messages) ? messages.join('、') : String(messages)}</a>{/each}</div>{/if}
      {#if writeUnknown}<div class="state-panel state-error"><p>留言結果尚未確認。請先重讀留言列表或我的留言查證，不會自動重送。</p><div class="author-actions"><button class="button" type="button" onclick={loadComments}>重讀留言查證</button><a class="button button-outline" href={`${BASE}/me/comments`}>我的留言</a><button class="button button-outline" type="button" disabled={!checkedUnknown} onclick={allowRetry}>已人工查證，允許手動重試</button></div></div>{/if}
      {#if article.visibility === 'visible'}
        {#if $authState === 'active' && $account?.id === ownerId}
          <form class="author-form author-card" novalidate onsubmit={submitComment}><label for="comment-body">發表留言</label><textarea id="comment-body" bind:value={commentBody} oninput={markCommentDirty} aria-invalid={Boolean(commentErrors.body)} disabled={processing}></textarea><p class="form-hint">1–2,000 個字；純文字並保留換行。</p>{#if commentErrors.body}<p class="field-error">{commentErrors.body.join('、')}</p>{/if}<button class="button" type="submit" disabled={processing || writeUnknown}>{processing ? '發表中…' : '發表留言'}</button></form>
        {:else if ownerId && $authState === 'expired'}
          <ReLogin {site} {ownerId} onSameAccount={resumeSameAccount} onDifferentAccount={discardOtherAccount} />
        {:else if $authState !== 'disabled'}<p><a class="button button-outline" href={`${BASE}/login?return_to=${encodeURIComponent(path)}`}>登入後留言</a></p>{/if}
      {/if}
    </section>
  {/if}
</div>
