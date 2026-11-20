<script>
  import { getContext, onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { page } from '$app/stores';
  import { replaceState } from '$app/navigation';
  import { BACKENDS, BASE, describeFailure, isArticle, isComment, isPage, positiveNumber, uncertainWrite, validateContent } from '$lib/contracts.js';
  import ReLogin from '$lib/ReLogin.svelte';

  const site = getContext('site');
  const { account, authState } = site;
  const path = $page.url.pathname;
  const articleId = positiveNumber($page.params.article_id);
  let requestedPage = positiveNumber($page.url.searchParams.get('page')) || 1;
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
  let visibilitySubmitted = false;
  let visibilityMessage = '';
  let commentDirty = false;
  let articleRequestId = 0;
  let commentsRequestId = 0;
  let processing = false;
  let ownerId = null;
  let alive = true;
  let summary;

  // 留言草稿只屬於目前頁面，切換與離頁時須先確認捨棄。
  function markCommentDirty() { commentDirty = true; commentErrors = {}; }

  // 未確認結果也是離頁責任；另一操作成功不能解除本頁尚未完成的查證警示。
  $: site.dirty.set(commentDirty || writeUnknown || visibilityUnknown || visibilityConflict || visibilitySubmitted);

  // 登入失權先撤下原身分讀到的內容；後續訪客重讀只能重新顯示公開資料，草稿仍只留記憶體。
  $: if ($authState !== 'active') { article = null; comments = null; }

  // 寫入結果無法確定時保留草稿，只有使用者查證後才能允許再次送出。
  function allowRetry() {
    if (!checkedUnknown) return;
    if (!window.confirm('已人工查證，確定要自行決定是否重送？重送可能產生重複留言。')) return;
    writeUnknown = false; checkedUnknown = false; writeMessage = '已解除鎖定；如需重試，請手動送出。';
  }

  // 登入後僅原帳號可接續本頁草稿；不同帳號立即清除舊文字。
  function discardOtherAccount() {
    articleRequestId++; commentsRequestId++;
    commentBody = ''; commentErrors = {}; writeUnknown = false; checkedUnknown = false; commentDirty = false;
    writeMessage = ''; visibilityMessage = ''; visibilityUnknown = false; visibilityConflict = false; visibilitySubmitted = false;
    article = null; comments = null;
    ownerId = get(account)?.id || null;
    void loadArticle();
  }

  // 原帳號重登後重新確認文章權限，草稿仍待本人手動送出。
  async function resumeSameAccount() {
    if (await loadArticle() && visibilitySubmitted) {
      visibilitySubmitted = false;
      visibilityMessage = '已完成寫入並已重新讀取伺服器目前狀態。';
    }
    site.dirty.set(commentDirty || processing || writeUnknown || visibilityUnknown || visibilityConflict || visibilitySubmitted);
  }

  // 發表留言只使用目前文章 ID，成功後回第一頁重讀，不猜新增 ID。
  async function submitComment(event) {
    event.preventDefault();
    // 先確認公開文章、原帳號與本次輸入，避免在失效身分下送出草稿。
    if (!article || article.visibility !== 'visible' || processing || writeUnknown
      || get(authState) !== 'active' || get(account)?.id !== ownerId) return;
    commentErrors = validateContent('', commentBody, true);
    if (Object.keys(commentErrors).length) { writeMessage = '請先修正留言內文。'; await tick(); summary?.focus(); return; }
    processing = true; site.pending.set(true); writeMessage = '';
    const generation = site.writeVersion();
    const sentToken = get(site.session)?.accessToken;
    try {
      // 寫入成功不猜留言 ID，只回第一頁重新讀取後端結果。
      await site.request(`/api/articles/${articleId}/comments`, { method: 'POST', data: { body: commentBody } });
      if (!alive || generation !== site.writeVersion()) return;
      commentBody = ''; commentDirty = false; site.finishPending();
      if (get(authState) !== 'active' || get(site.session)?.accessToken !== sentToken) {
        writeMessage = '留言已完成；重新登入後請重讀留言確認。';
        return;
      }
      // 原頁重讀第一頁，保留另一個可見性操作仍未確認的結果；不用重新建立整頁狀態。
      requestedPage = 1;
      replaceState(`${BASE}/articles/${articleId}`, $page.state);
      const loaded = await loadComments();
      if (alive && generation === site.writeVersion()) writeMessage = loaded
        ? '留言已完成，已重新讀取留言第一頁。' : '留言已完成，但重讀失敗；請重新讀取留言確認。';
    } catch (failure) {
      if (!alive || generation !== site.writeVersion()) return;
      // 已送出的結果若不明確，保留文字並鎖住重送直到人工查證。
      commentErrors = failure?.code === 'E0001' ? failure.errors || {} : {};
      writeUnknown = uncertainWrite(failure); checkedUnknown = false;
      writeMessage = `${writeUnknown ? '留言結果尚未確認；請先查證，不會自動重送。' : ''}${describeFailure(failure, '留言結果尚未確認，請查證。')}`;
      await tick(); summary?.focus();
    } finally {
      // 僅清理本次世代的等待狀態，避免影響切換後的新頁。
      processing = false; if (alive && generation === site.writeVersion()) site.finishPending();
    }
  }

  // 下架／恢復以目前版本送出；管理員接管他人已下架內容時維持 hidden。
  async function changeVisibility(requested = false) {
    const takeOver = requested === true;
    if (!article || processing || visibilityConflict || visibilityUnknown || visibilitySubmitted || get(authState) !== 'active') return;
    if (takeOver && (get(account)?.role !== 'admin' || article.author.id === get(account)?.id || article.visibility !== 'hidden')) return;
    const next = takeOver || article.visibility === 'visible' ? 'hidden' : 'visible';
    if (!window.confirm(takeOver ? '確定接管這篇文章的恢復權？文章保持下架，原作者將不能恢復。' : `確定${next === 'hidden' ? '下架' : '恢復'}這篇文章？`)) return;
    processing = true; site.pending.set(true);
    const generation = site.writeVersion();
    visibilityMessage = '';
    try {
      await site.request(`/api/articles/${articleId}/visibility`, { method: 'PUT', data: { visibility: next, version: article.version } });
      if (!alive || generation !== site.writeVersion()) return;
      visibilitySubmitted = true;
      visibilityMessage = '已完成寫入；請重新讀取伺服器目前狀態。';
      if (get(authState) === 'active' && await loadArticle()) {
        visibilitySubmitted = false;
        visibilityMessage = '已重新讀取伺服器目前狀態。';
      }
    } catch (failure) {
      if (!alive || generation !== site.writeVersion()) return;
      visibilityConflict = failure?.code === 'E0201'; visibilityUnknown = uncertainWrite(failure);
      visibilityMessage = `${visibilityUnknown ? '更新結果尚未確認；請先重讀同一篇文章查證。' : ''}${describeFailure(failure, '更新失敗，請查證。')}`;
    } finally { processing = false; if (alive && generation === site.writeVersion()) site.finishPending(); }
  }

  // 管理員再次下架他人內容，沿用同一版本衝突與不明結果處理。
  function takeOverVisibility() { void changeVisibility(true); }

  // 重讀成功才解除版本衝突，避免用舊文章版本再次操作。
  async function reloadVisibility() {
    const generation = site.version();
    if (await loadArticle() && generation === site.version()) {
      visibilityConflict = false; visibilityUnknown = false; visibilitySubmitted = false;
      visibilityMessage = '已重新讀取伺服器目前狀態。';
    }
  }

  // 文章與留言分開讀取，留言失敗不抹去已確認可讀的文章。
  async function loadArticle() {
    if (!articleId) { articleError = '找不到指定資料或 API'; articleLoading = false; return false; }
    const current = ++articleRequestId;
    let generation = site.version();
    commentsRequestId++;
    articleLoading = true; articleError = null; article = null; comments = null;
    try {
      let data;
      try { data = await site.request(`/api/articles/${articleId}`); }
      catch (failure) {
        if (!['E0002', 'E0102'].includes(failure?.code)) throw failure;
        if (!alive || current !== articleRequestId) return false;
        generation = site.version();
        // 無效登入清除後仍可按訪客身分嘗試公開文章。
        data = await site.request(`/api/articles/${articleId}`);
      }
      if (!alive || current !== articleRequestId || generation !== site.version()) return false;
      if (!isArticle(data) || data.id !== articleId || data.visibility === 'hidden'
        && (get(authState) !== 'active' || data.author.id !== get(account)?.id && get(account)?.role !== 'admin'))
        throw new Error('回應資料格式不正確，請確認目前後端。');
      article = data;
      void loadComments();
      return true;
    } catch (failure) {
      if (!alive || current !== articleRequestId || generation !== site.version()) return false;
      articleError = describeFailure(failure, '讀取文章失敗，請確認目前後端後重試。');
      return false;
    } finally { if (alive && current === articleRequestId) articleLoading = false; }
  }

  // 留言分頁與文章讀取各自保留載入、空、錯誤及超頁狀態。
  async function loadComments() {
    if (!article) return;
    const current = ++commentsRequestId;
    const generation = site.version();
    commentsLoading = true; commentsError = null; comments = null;
    const path = `/api/articles/${articleId}/comments?page=${requestedPage}&per_page=20`;
    try {
      let data;
      try { data = await site.request(path); }
      catch (failure) {
        if (!['E0002', 'E0102'].includes(failure?.code)) throw failure;
        // 憑證失效後文章可能已下架，須先撤下舊內文並以訪客重新驗證。
        if (alive && current === commentsRequestId) void loadArticle();
        return;
      }
      if (!alive || current !== commentsRequestId || generation !== site.version()) return;
      if (!isPage(data, requestedPage) || !data.list.every(
        // 每則留言都必須屬於目前文章，錯置資料不可混入畫面。
        (item) => isComment(item, articleId) && (item.visibility === 'visible' || get(account)?.role === 'admin')
      )) throw new Error('回應資料格式不正確，請確認目前後端。');
      comments = data;
      if (writeUnknown) checkedUnknown = true;
      return true;
    } catch (failure) {
      if (!alive || current !== commentsRequestId || generation !== site.version()) return;
      commentsError = describeFailure(failure, '讀取留言失敗，請確認目前後端後重試。');
      return false;
    } finally { if (alive && current === commentsRequestId) commentsLoading = false; }
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
    return () => { alive = false; articleRequestId++; commentsRequestId++; site.dirty.set(false); site.finishPending(); };
  });
</script>

<svelte:head>
  <title>{article?.title || '文章閱讀'}｜文章集</title>
</svelte:head>

<div class="reading-wrap">
  <a class="back-link" href={`${BASE}/`}>← 返回文章列表</a>
  {#if articleLoading}
    <p class="state-panel" role="status">文章載入中…</p>
  {:else if articleError}
    <div class="state-panel state-error" role="alert">
      <p>{articleError}</p>
      <button class="button" type="button" onclick={loadArticle}>重新讀取</button>
    </div>
  {:else if !article}
    <div class="state-panel" role="status">
      <p>{$authState === 'disabled' ? '此帳號已停用，不能繼續寫入；仍可重新讀取公開文章。' : '登入狀態已改變，請重新讀取公開文章。'}</p>
      <button class="button" type="button" onclick={loadArticle}>重新讀取公開文章</button>
    </div>
  {:else if article}
    <article class="reading-card">
      <h1>{article.title}</h1>
      {#if article.visibility === 'hidden'}
        <p class="notice">這篇文章目前已下架。</p>
      {/if}
      <dl class="meta-row">
        <div>
          <dt>作者</dt>
          <dd>{article.author.nickname}</dd>
        </div>
        <div>
          <dt>發布日期</dt>
          <dd>{article.created_at.slice(0, 10)}</dd>
        </div>
        <div>
          <dt>建立於</dt>
          <dd class="accent-text">{BACKENDS[article.created_backend]?.[0] || article.created_backend}</dd>
        </div>
      </dl>
      <div class="reading-body">{article.body}</div>
      {#if $authState === 'active' && (article.author.id === $account?.id || $account?.role === 'admin')}
        <div class="author-actions">
          {#if article.author.id === $account?.id}
            <a class="button button-outline" href={`${BASE}/articles/${articleId}/edit`}>編輯文章</a>
          {/if}
          {#if article.visibility === 'visible' || article.can_restore}
            <button class="button" type="button" disabled={processing || visibilityConflict || visibilityUnknown || visibilitySubmitted} onclick={changeVisibility}>{article.visibility === 'visible' ? '下架文章' : '恢復文章'}</button>
          {/if}
          {#if article.visibility === 'hidden' && $account?.role === 'admin' && article.author.id !== $account?.id}
            <button class="button button-outline" type="button" disabled={processing || visibilityConflict || visibilityUnknown || visibilitySubmitted} onclick={takeOverVisibility}>接管下架權</button>
          {/if}
        </div>
      {/if}
      {#if visibilityConflict || visibilityUnknown || visibilitySubmitted}
        <div class="state-panel state-error">
          <p>{visibilityUnknown ? '更新結果尚未確認；請重讀同一篇文章查證。' : visibilitySubmitted ? '寫入已完成，請重讀目前狀態。' : '版本已改變；請重讀後重新確認操作。'}</p>
          <button class="button" type="button" onclick={reloadVisibility}>重新讀取</button>
        </div>
      {/if}
    </article>
    <section class="comments-section" aria-labelledby="comments-heading">
      <h2 id="comments-heading">留言</h2>
      {#if commentsLoading}
        <p class="state-panel" role="status">留言載入中…</p>
      {:else if commentsError}
        <div class="state-panel state-error" role="alert">
          <p>{commentsError}</p>
          <button class="button" type="button" onclick={loadComments}>重新讀取留言</button>
        </div>
      {:else if comments?.list.length === 0}
        <!-- 留言零筆超頁不表示整篇沒有留言，須保留原頁碼與返回入口。 -->
        <div class="state-panel">
          <p>{comments.pagination.page > 1 || comments.pagination.total > 0 ? '此頁沒有留言。' : '尚無留言。'}</p>
          {#if comments.pagination.page > 1 || comments.pagination.total > 0}
            <p>第 {comments.pagination.page} 頁，共 {comments.pagination.total} 則</p>
            <a class="button" data-sveltekit-reload href={`${BASE}/articles/${articleId}`}>回留言第一頁</a>
          {/if}
        </div>
      {:else if comments}
        <div class="comment-list">
          {#each comments.list as comment (comment.id)}
            <article class="comment-item">
              <dl class="meta-row">
                <div>
                  <dt>作者</dt>
                  <dd>{comment.author.nickname}</dd>
                </div>
                <div>
                  <dt>發布日期</dt>
                  <dd>{comment.created_at.slice(0, 10)}</dd>
                </div>
                <div>
                  <dt>建立於</dt>
                  <dd class="accent-text">{BACKENDS[comment.created_backend]?.[0] || comment.created_backend}</dd>
                </div>
              </dl>
              <p class="comment-body">{comment.body}</p>
              {#if $authState === 'active' && (comment.author.id === $account?.id || $account?.role === 'admin')}
                <a class="back-link" href={`${BASE}/comments/${comment.id}`}>查看留言詳情</a>
              {/if}
            </article>
          {/each}
        </div>
        <!-- SvelteKit 2 的淺層網址更新保留原路由頁；人工換頁以完整導覽重讀，避免沿用成功前的頁碼與元件。 -->
        <nav class="pagination" aria-label="留言分頁">
          {#if requestedPage > 1}
            <a class="button button-outline" data-sveltekit-reload href={`${BASE}/articles/${articleId}?page=${requestedPage - 1}`}>上一頁</a>
          {/if}
          <span>第 {comments.pagination.page} 頁，共 {comments.pagination.total} 則</span>
          {#if requestedPage < comments.pagination.total_pages}
            <a class="button button-outline" data-sveltekit-reload href={`${BASE}/articles/${articleId}?page=${requestedPage + 1}`}>下一頁</a>
          {/if}
        </nav>
      {/if}
      {#if article.visibility === 'visible'}
        {#if $authState === 'active' && $account?.id === ownerId}
          <form class="author-form author-card" novalidate onsubmit={submitComment}>
            <label for="comment-body">發表留言</label>
            <textarea id="comment-body" bind:value={commentBody} oninput={markCommentDirty} aria-invalid={Boolean(commentErrors.body)} disabled={processing}>
            </textarea>
            <p class="form-hint">1–2,000 個字；純文字並保留換行。</p>
            {#if commentErrors.body}
              <p class="field-error">{commentErrors.body.join('、')}</p>
            {/if}
            <button class="button" type="submit" disabled={processing || writeUnknown}>{processing ? '發表中…' : '發表留言'}</button>
          </form>
        {:else if ownerId && $authState === 'expired'}
          <ReLogin {site} {ownerId} onSameAccount={resumeSameAccount} onDifferentAccount={discardOtherAccount} />
        {:else if $authState !== 'disabled'}
          <p>
            <a class="button button-outline" href={`${BASE}/login?return_to=${encodeURIComponent(path)}`}>登入後留言</a>
          </p>
        {/if}
      {/if}
    </section>
  {/if}
  <!-- 各項結果獨立於文章顯示分支；另一操作或重讀失敗不能移除原留言的查證入口。 -->
  {#if ownerId && ($authState !== 'active' || $account?.id === ownerId)}
    {#if visibilityMessage}
      <p class="notice" role="status">{visibilityMessage}</p>
    {/if}
    {#if writeMessage}
      <div class="form-summary" role="alert" tabindex="-1" bind:this={summary}>
        <strong>{writeMessage}</strong>
        {#each Object.entries(commentErrors) as [field, messages]}
          <a href="#comment-body">{field}：{Array.isArray(messages) ? messages.join('、') : String(messages)}</a>
        {/each}
      </div>
    {/if}
    {#if writeUnknown}
      <div class="state-panel state-error">
        <p>留言結果尚未確認。請先重讀留言列表或我的留言查證，不會自動重送。</p>
        <div class="author-actions">
          <!-- 先重新確認父文章可讀，再由 loadArticle 讀留言；失敗仍保留草稿與查證鎖定。 -->
          <button class="button" type="button" disabled={$authState !== 'active'} onclick={article ? loadComments : loadArticle}>重讀留言查證</button>
          <a class="button button-outline" href={`${BASE}/me/comments`}>我的留言</a>
          <button class="button button-outline" type="button" disabled={!checkedUnknown || $authState !== 'active'} onclick={allowRetry}>已人工查證，允許手動重試</button>
        </div>
      </div>
    {/if}
    {#if !article && (visibilityConflict || visibilityUnknown || visibilitySubmitted)}
      <div class="state-panel state-error">
        <p>可見性結果仍需查證，重新讀取不會重送寫入。</p>
        <button class="button" type="button" disabled={$authState !== 'active'} onclick={reloadVisibility}>重新讀取可見性</button>
      </div>
    {/if}
  {/if}
  {#if !article && ownerId && $authState === 'expired'}
    <ReLogin {site} {ownerId} onSameAccount={resumeSameAccount} onDifferentAccount={discardOtherAccount} />
  {/if}
</div>
