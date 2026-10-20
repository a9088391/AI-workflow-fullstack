<script>
  import { getContext, onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { page } from '$app/stores';
  import { BACKENDS, BASE, describeFailure, isArticle, isComment, isPage, positiveNumber, uncertainWrite, validateContent } from '$lib/contracts.js';
  import ReLogin from '$lib/ReLogin.svelte';

  const site = getContext('site');
  const { account, authState } = site;
  const path = $page.url.pathname;
  const articleMatch = path.match(/^\/sveltekit\/articles\/([0-9]+)\/edit$/);
  const commentMatch = path.match(/^\/sveltekit\/comments\/([0-9]+)(\/edit)?$/);
  const mode = path === `${BASE}/articles/new` ? 'new' : path === `${BASE}/me/articles` ? 'articles'
    : path === `${BASE}/me/comments` ? 'comments' : articleMatch ? 'article-edit'
      : commentMatch ? commentMatch[2] ? 'comment-edit' : 'comment' : 'missing';
  const id = positiveNumber(articleMatch?.[1] || commentMatch?.[1]);
  const pageNumber = positiveNumber($page.url.searchParams.get('page')) || 1;
  const editing = mode.endsWith('-edit');
  const commentMode = mode.startsWith('comment') || mode === 'comments';
  let ownerId = null;
  let result = null;
  let resource = null;
  let parent = null;
  let parentState = 'loading';
  let parentError = '';
  let loading = mode !== 'new' && mode !== 'missing';
  let error = '';
  let message = '';
  let fieldErrors = {};
  let title = '';
  let body = '';
  let version = 0;
  let comparison = null;
  let conflict = false;
  let unknown = false;
  let checkedUnknown = false;
  let verification = null;
  let verificationPage = 1;
  let processing = false;
  let initialized = false;
  let alive = true;
  let requestId = 0;
  let parentRequestId = 0;
  let verificationRequestId = 0;
  let summary;

  // 頁面啟動先驗證帳號，再讀取原帳號可見的作者資料。
  async function initializePage() {
    await site.initialize();
    if (!alive) return;
    ownerId = get(account)?.id || null;
    if (get(authState) === 'active' && mode !== 'new' && mode !== 'missing') await load();
    else loading = false;
  }

  // 離頁後不讓晚到讀取覆蓋新頁，草稿也不寫入任何持久儲存。
  onMount(() => {
    void initializePage();
    // 卸載時清除原頁的離頁警示與請求狀態。
    return () => { alive = false; requestId++; site.dirty.set(false); site.pending.set(false); };
  });

  // 只在目前身分有效時讀受保護資料；編輯中重讀不覆蓋未送出的文字。
  async function load() {
    if (!id && ['article-edit', 'comment', 'comment-edit'].includes(mode)) { error = '找不到指定資料或 API'; loading = false; return; }
    const current = ++requestId;
    loading = true; error = ''; message = '';
    try {
      const list = mode === 'articles' || mode === 'comments';
      const endpoint = list ? `/api/me/${mode}?page=${pageNumber}&per_page=20`
        : mode === 'article-edit' ? `/api/articles/${id}` : `/api/comments/${id}`;
      const data = await site.request(endpoint);
      if (!alive || current !== requestId) return false;
      if (list) {
        if (!isPage(data, pageNumber) || !data.list.every(
          // 「我的」列表仍核對作者，避免錯置資料洩漏到畫面。
          (item) => (commentMode ? isComment(item) : isArticle(item)) && item.author.id === get(account)?.id
        )) throw new Error('回應資料格式不正確，請確認目前後端。');
        result = data;
      } else {
        if (!(commentMode ? isComment(data) : isArticle(data)) || data.id !== id) throw new Error('回應資料格式不正確，請確認目前後端。');
        resource = data;
        if (editing) {
          if (!initialized) { title = data.title || ''; body = data.body; version = data.version; initialized = true; }
          else comparison = data;
        }
        if (commentMode) void loadParent(data.article_id);
      }
      if (unknown) checkedUnknown = true;
      return true;
    } catch (failure) {
      if (!alive || current !== requestId) return false;
      if (!editing || !initialized) resource = null;
      if (mode === 'articles' || mode === 'comments') result = null;
      error = describeFailure(failure, '讀取失敗，請確認目前後端後重試。');
      return false;
    } finally { if (alive && current === requestId) loading = false; }
  }

  // 父文章獨立查核；404 只表示不可讀，不能推測標題或下架原因。
  async function loadParent(articleId = resource?.article_id) {
    if (!articleId) return;
    const current = ++parentRequestId;
    const generation = site.version();
    parent = null; parentState = 'loading'; parentError = '';
    try {
      const data = await site.request(`/api/articles/${articleId}`);
      // 較舊的父文章回應不可覆蓋較新的權限查核。
      if (!alive || current !== parentRequestId || generation !== site.version() || resource?.article_id !== articleId) return;
      if (!isArticle(data) || data.id !== articleId) throw new Error('回應資料格式不正確，請確認目前後端。');
      parent = data; parentState = data.visibility === 'visible' ? 'visible' : 'hidden';
    } catch (failure) {
      if (!alive || current !== parentRequestId || generation !== site.version() || resource?.article_id !== articleId) return;
      parentState = failure?.code === 'E0004' ? 'unreadable' : 'error';
      parentError = describeFailure(failure, '讀取所屬文章失敗，請單獨重試。');
    }
  }

  // 輸入只留於目前頁面；切換版本與離頁共用未儲存確認。
  function markDirty() { site.dirty.set(true); fieldErrors = {}; message = ''; }

  // 先檢查文字，再以讀到的版本送出；衝突與結果不明不能自動重試。
  async function submit(event) {
    event.preventDefault();
    if (processing || unknown || conflict || get(authState) !== 'active' || get(account)?.id !== ownerId) return;
    if (editing && (!resource || resource.author.id !== ownerId || commentMode && parentState !== 'visible')) return;
    fieldErrors = validateContent(title, body, commentMode);
    if (Object.keys(fieldErrors).length) { message = '請先修正標示的欄位。'; await tick(); summary?.focus(); return; }
    const endpoint = mode === 'new' ? '/api/articles' : commentMode ? `/api/comments/${id}` : `/api/articles/${id}`;
    const data = mode === 'new' ? { title, body } : commentMode ? { body, version } : { title, body, version };
    processing = true; site.pending.set(true); message = '';
    const generation = site.version();
    try {
      await site.request(endpoint, { method: mode === 'new' ? 'POST' : 'PUT', data });
      if (!alive || generation !== site.version()) return;
      site.dirty.set(false); site.pending.set(false);
      window.location.assign(mode === 'new' ? `${BASE}/me/articles` : `${BASE}/${commentMode ? 'comments' : 'articles'}/${id}`);
    } catch (failure) {
      if (!alive || generation !== site.version()) return;
      fieldErrors = failure?.code === 'E0001' ? failure.errors || {} : {};
      conflict = failure?.code === 'E0201'; unknown = uncertainWrite(failure); checkedUnknown = false;
      // 409 只讀取最新版本供比較；草稿與送出版本仍由使用者決定。
      if (conflict) { comparison = null; await load(); }
      if (!alive || generation !== site.version()) return;
      message = `${unknown ? '結果尚未確認；請先查證，不會自動重送。' : ''}${describeFailure(failure, '寫入結果尚未確認，請查證後再決定。')}`;
      await tick(); summary?.focus();
    } finally { processing = false; if (alive && generation === site.version()) site.pending.set(false); }
  }

  // 409 必須顯示最新內容讓人比較；確認版本後仍須手動送出。
  function acceptComparison(discard) {
    if (!comparison || !window.confirm(discard ? '確定捨棄自己的輸入，採用最新內容？' : '已比較並整合自己的輸入，確定採用最新版本供手動送出？')) return;
    version = comparison.version;
    if (discard) { title = comparison.title || ''; body = comparison.body; site.dirty.set(false); }
    comparison = null; conflict = false; unknown = false; message = '已採用最新版本；若仍需更新，請手動送出。';
  }

  // 新增結果不明時逐頁查證；列表排序不能證明最新一筆就是本次新增。
  async function verifyCreation(pageToRead = 1) {
    const current = ++verificationRequestId;
    const generation = site.version();
    const accountId = ownerId;
    verification = null; verificationPage = pageToRead;
    try {
      const endpoint = mode === 'new' ? `/api/me/articles?page=${pageToRead}&per_page=20`
        : `/api/articles/${id}/comments?page=${pageToRead}&per_page=20`;
      const data = await site.request(endpoint);
      // 換帳號或較新的查證開始後，舊清單不能重新寫入畫面。
      if (!alive || current !== verificationRequestId || generation !== site.version() || get(account)?.id !== accountId) return;
      if (!isPage(data, pageToRead) || !data.list.every(
        // 查證清單亦須和目前文章或作者範圍一致。
        (item) => mode === 'new' ? isArticle(item) && item.author.id === ownerId : isComment(item, id)
      )) throw new Error('回應資料格式不正確，請確認目前後端。');
      verification = data;
      checkedUnknown = true;
    } catch (failure) {
      if (alive && current === verificationRequestId && generation === site.version() && get(account)?.id === accountId)
        message = describeFailure(failure, '查證失敗，請重試。');
    }
  }

  // 查證後是否重送仍由使用者決定，避免結果不明時產生重複內容。
  function allowRetry() {
    if (!checkedUnknown) return;
    if (!window.confirm('已人工查證，確定要自行決定是否再次送出？重送可能產生重複內容。')) return;
    unknown = false; checkedUnknown = false; message = '已解除鎖定；如需重試，請手動送出。';
  }

  // 可見性操作只用目前版本；管理員接管他人已下架留言時維持 hidden。
  async function changeVisibility(requested = false) {
    const takeOver = requested === true;
    if (!resource || processing || get(authState) !== 'active') return;
    if (takeOver && (get(account)?.role !== 'admin' || resource.author.id === get(account)?.id || resource.visibility !== 'hidden')) return;
    const next = takeOver || resource.visibility === 'visible' ? 'hidden' : 'visible';
    if (!window.confirm(takeOver ? '確定接管這則留言的恢復權？留言保持下架，原作者將不能恢復。' : `確定${next === 'hidden' ? '下架' : '恢復'}這則留言？`)) return;
    processing = true; site.pending.set(true); message = '';
    const generation = site.version();
    try {
      await site.request(`/api/comments/${id}/visibility`, { method: 'PUT', data: { visibility: next, version: resource.version } });
      if (alive && generation === site.version() && await load()) message = '已重新讀取伺服器目前狀態。';
    } catch (failure) {
      if (!alive || generation !== site.version()) return;
      conflict = failure?.code === 'E0201'; unknown = uncertainWrite(failure);
      message = `${unknown ? '更新結果尚未確認；請先重讀同一則留言查證。' : ''}${describeFailure(failure, '更新結果尚未確認，請查證。')}`;
    } finally { processing = false; if (alive && generation === site.version()) site.pending.set(false); }
  }

  // 管理員再次下架他人留言，沿用同一版本衝突與不明結果處理。
  function takeOverVisibility() { void changeVisibility(true); }

  // 只有同一留言重新讀取成功後才解除過期狀態，避免沿用舊 version。
  async function reloadVisibility() {
    if (await load()) { conflict = false; unknown = false; }
  }

  // 不同帳號不能接收原帳號的文字、版本或已讀取的受保護資料。
  function discardOtherAccount() {
    title = ''; body = ''; resource = null; result = null; comparison = null; initialized = false;
    verificationRequestId++; parentRequestId++; verification = null; verificationPage = 1; parent = null; parentState = 'loading';
    ownerId = get(account)?.id || null; site.dirty.set(false); unknown = false; checkedUnknown = false; conflict = false;
    message = '已切換帳號，原帳號的未送出文字已清除。';
    if (mode !== 'new') void load();
  }

  // 同帳號重登後只重新取得伺服器資料，保留尚未手動送出的草稿。
  function resumeSameAccount() { if (mode !== 'new') void load(); }

  // 父文章讀取失敗時由使用者單獨重試，不抹去已讀取的留言。
  function retryParent() { void loadParent(); }

  // 版本比較的兩種明確選擇都由使用者觸發。
  function acceptMerged() { acceptComparison(false); }
  // 捨棄自己的文字前，仍由 acceptComparison 再次要求明確確認。
  function acceptDiscarded() { acceptComparison(true); }

  // 查證列表使用目前頁碼，只有使用者操作才翻頁。
  function verifyFirstPage() { void verifyCreation(); }
  // 向前翻頁仍逐頁核對作者及列表 envelope。
  function verifyPreviousPage() { void verifyCreation(verificationPage - 1); }
  // 向後翻頁仍逐頁核對作者及列表 envelope。
  function verifyNextPage() { void verifyCreation(verificationPage + 1); }
</script>

<svelte:head><title>{mode === 'new' ? '發表文章' : mode === 'articles' ? '我的文章' : mode === 'comments' ? '我的留言' : mode === 'article-edit' ? '編輯文章' : mode === 'comment-edit' ? '編輯留言' : mode === 'comment' ? '留言詳情' : '找不到頁面'}｜文章集</title></svelte:head>

{#if mode === 'missing' || (id === null && ['article-edit', 'comment', 'comment-edit'].includes(mode))}
  <div class="state-panel not-found"><h1>找不到頁面<span class="title-dot">.</span></h1><p>此頁面尚未提供，或網址不正確。</p><a class="button" href={`${BASE}/`}>返回文章列表</a></div>
{:else}
  <div class="author-wrap">
    <div class="author-heading"><h1>{mode === 'new' ? '發表文章' : mode === 'articles' ? '我的文章' : mode === 'comments' ? '我的留言' : mode === 'article-edit' ? '編輯文章' : mode === 'comment-edit' ? '編輯留言' : '留言詳情'}<span class="title-dot">.</span></h1>
      {#if mode === 'articles'}<a class="button" href={`${BASE}/articles/new`}>發表文章</a>{/if}
    </div>
    {#if $authState === 'checking'}
      <p class="state-panel" role="status">驗證登入中…</p>
    {:else if $authState !== 'active' || $account?.id !== ownerId}
      {#if ownerId && ['expired', 'guest'].includes($authState)}
        <ReLogin {site} {ownerId} onSameAccount={resumeSameAccount} onDifferentAccount={discardOtherAccount} />
      {:else}
        <div class="state-panel state-error"><p>{$authState === 'disabled' ? '此帳號已停用，無法繼續作者操作。' : '此頁需要有效登入。'}</p>
          {#if $authState !== 'disabled'}<a class="button" href={`${BASE}/login?return_to=${encodeURIComponent(path)}`}>前往登入</a>{/if}
        </div>
      {/if}
    {:else if loading}
      <p class="state-panel" role="status">資料載入中…</p>
    {:else if error && !resource && !result}
      <div class="state-panel state-error" role="alert"><p>{error}</p><button class="button" type="button" onclick={load}>重新讀取</button><a class="button button-outline" href={`${BASE}/`}>返回文章列表</a></div>
    {:else}
      {#if error}<div class="state-panel state-error" role="alert"><p>{error}</p><button class="button" type="button" onclick={load}>重新讀取</button></div>{/if}
      {#if message}<div class="form-summary" role="alert" tabindex="-1" bind:this={summary}><strong>{message}</strong>
        {#each Object.entries(fieldErrors) as [field, messages]}
          <a href={`#author-${field}`}>{field === 'title' ? '標題' : field === 'body' ? '內文' : field}：{Array.isArray(messages) ? messages.join('、') : String(messages)}</a>
        {/each}
      </div>{/if}
      {#if mode === 'articles' || mode === 'comments'}
        <!-- 自己的內容零筆超頁保留原頁碼，返回入口指回同一列表。 -->
        {#if result?.list.length === 0}<div class="state-panel"><p>{result.pagination.page > 1 || result.pagination.total > 0 ? '此頁沒有資料。' : '目前沒有自己的內容。'}</p>{#if result.pagination.page > 1}<a class="button" href={`${BASE}/me/${mode}`}>回第一頁</a>{/if}</div>{/if}
        {#if result?.list.length}<div class="author-list">
          {#each result.list as item (item.id)}
            <article class="article-card"><h2><a href={`${BASE}/${commentMode ? 'comments' : 'articles'}/${item.id}`}>{commentMode ? `留言 #${item.id}` : item.title}</a></h2>
              <p class="card-excerpt">{[...item.body].slice(0, 110).join('')}{[...item.body].length > 110 ? '…' : ''}</p>
              <dl class="meta-row"><div><dt>狀態</dt><dd>{item.visibility === 'hidden' ? '已下架' : '公開'}</dd></div><div><dt>發布日期</dt><dd>{item.created_at.slice(0, 10)}</dd></div><div><dt>建立於</dt><dd>{BACKENDS[item.created_backend]?.[0] || item.created_backend}</dd></div></dl>
            </article>
          {/each}
        </div>{/if}
        {#if result}<nav class="pagination" aria-label="我的內容分頁">
          {#if pageNumber > 1}<a class="button button-outline" href={`${BASE}/me/${mode}?page=${pageNumber - 1}`}>上一頁</a>{/if}
          <span>第 {pageNumber} 頁，共 {result.pagination.total} 筆</span>
          {#if pageNumber < result.pagination.total_pages}<a class="button button-outline" href={`${BASE}/me/${mode}?page=${pageNumber + 1}`}>下一頁</a>{/if}
        </nav>{/if}
      {:else if mode === 'new' || editing}
        {#if editing && resource?.author.id !== ownerId}<div class="state-panel state-error"><p>沒有權限編輯此內容。</p><a class="button" href={`${BASE}/`}>返回文章列表</a></div>
        {:else}
          {#if mode === 'comment-edit'}
            {#if parentState === 'loading'}<p role="status">確認所屬文章中…</p>
            {:else if parentState === 'visible'}<p>所屬文章：<a href={`${BASE}/articles/${resource.article_id}`}>{parent.title}</a></p>
            {:else}<div class="state-panel state-error"><p>{parentState === 'unreadable' ? '所屬文章目前無法讀取。' : parentState === 'hidden' ? '所屬文章未公開，暫不能修改留言內文。' : parentError}</p><button class="button" type="button" onclick={retryParent}>單獨重試文章讀取</button></div>{/if}
          {/if}
          <p class="author-hint">{mode === 'new' ? '標題和內文會以純文字公開；發表後回到我的文章。' : '修改後以原 ID 重新讀取；建立來源由伺服器保留。'}</p>
          <form class="author-form author-card" novalidate onsubmit={submit}>
            {#if !commentMode}<label for="author-title">標題</label><input id="author-title" bind:value={title} oninput={markDirty} aria-invalid={Boolean(fieldErrors.title)} disabled={processing} /><p class="form-hint">1–200 個字，不可全為空白。</p>{#if fieldErrors.title}<p class="field-error">{fieldErrors.title.join('、')}</p>{/if}{/if}
            <label for="author-body">{commentMode ? '留言內文' : '文章內文'}</label><textarea id="author-body" bind:value={body} oninput={markDirty} aria-invalid={Boolean(fieldErrors.body)} disabled={processing}></textarea><p class="form-hint">{commentMode ? '1–2,000' : '1–20,000'} 個字，保留換行。</p>{#if fieldErrors.body}<p class="field-error">{fieldErrors.body.join('、')}</p>{/if}
            <button class="button" type="submit" disabled={processing || unknown || conflict || mode === 'comment-edit' && parentState !== 'visible'}>{processing ? '處理中…' : mode === 'new' ? '發表文章' : '儲存修改'}</button>
          </form>
          {#if editing}<div class="author-actions"><button class="button button-outline" type="button" disabled={processing} onclick={load}>重新讀取最新內容</button><a class="button button-outline" href={`${BASE}/${commentMode ? 'comments' : 'articles'}/${id}`}>返回詳情</a></div>{/if}
          {#if comparison}<section class="state-panel"><h2>最新內容（版本 {comparison.version}）</h2><p>請與上方自己的輸入人工比較；確認後仍須手動送出。</p>{#if !commentMode}<h3>{comparison.title}</h3>{/if}<div class="reading-body">{comparison.body}</div><div class="author-actions"><button class="button button-outline" type="button" onclick={acceptMerged}>已整合輸入，採用最新版本</button><button class="button button-outline" type="button" onclick={acceptDiscarded}>捨棄輸入，採用最新內容</button></div></section>{/if}
          {#if unknown}
            <div class="state-panel state-error"><p>結果尚未確認；不會自動重送。{mode === 'new' ? '請從自己的文章列表逐頁查證。' : '請依原 ID 查證伺服器內容。'}</p>
              {#if mode === 'new'}<button class="button" type="button" onclick={verifyFirstPage}>讀取我的文章查證</button>{:else}<button class="button" type="button" onclick={load}>重讀原 ID 查證</button>{/if}
              <button class="button button-outline" type="button" disabled={!checkedUnknown} onclick={allowRetry}>已人工查證，允許手動重試</button>
            </div>
          {/if}
          {#if verification}<section class="state-panel"><h2>我的文章（第 {verificationPage} 頁）</h2>{#each verification.list as item (item.id)}<p><a href={`${BASE}/articles/${item.id}`}>{item.title}</a> · {item.created_at}</p>{/each}<div class="author-actions">{#if verificationPage > 1}<button class="button button-outline" type="button" onclick={verifyPreviousPage}>上一頁</button>{/if}{#if verificationPage < verification.pagination.total_pages}<button class="button button-outline" type="button" onclick={verifyNextPage}>下一頁</button>{/if}</div></section>{/if}
        {/if}
      {:else if mode === 'comment' && resource}
        <article class="reading-card"><p class="section-mark">{resource.visibility === 'hidden' ? '已下架留言' : '留言'}</p><h2>留言 #{resource.id}</h2><dl class="meta-row"><div><dt>作者</dt><dd>{resource.author.nickname}</dd></div><div><dt>發布日期</dt><dd>{resource.created_at.slice(0, 10)}</dd></div><div><dt>建立於</dt><dd>{BACKENDS[resource.created_backend]?.[0] || resource.created_backend}</dd></div></dl><div class="reading-body">{resource.body}</div></article>
        <div class="author-parent">{#if parentState === 'loading'}<p role="status">確認所屬文章中…</p>{:else if parentState === 'visible' || parentState === 'hidden'}<p>所屬文章：<a href={`${BASE}/articles/${resource.article_id}`}>{parent.title}</a></p>{:else}<p>所屬文章目前無法讀取。</p><button class="button button-outline" type="button" onclick={retryParent}>單獨重試文章讀取</button>{/if}</div>
        <div class="author-actions">{#if resource.author.id === ownerId && parentState === 'visible'}<a class="button" href={`${BASE}/comments/${id}/edit`}>編輯留言</a>{/if}{#if resource.visibility === 'visible' || resource.can_restore}<button class="button button-outline" type="button" disabled={processing || conflict || unknown} onclick={changeVisibility}>{resource.visibility === 'visible' ? '下架留言' : '恢復留言'}</button>{/if}{#if resource.visibility === 'hidden' && $account?.role === 'admin' && resource.author.id !== $account?.id}<button class="button button-outline" type="button" disabled={processing || conflict || unknown} onclick={takeOverVisibility}>接管下架權</button>{/if}{#if $account?.role === 'admin'}<a class="button button-outline" href={`${BASE}/admin/comments`}>返回留言管理</a>{/if}<a class="button button-outline" href={`${BASE}/me/comments`}>我的留言</a></div>
        {#if conflict || unknown}<div class="state-panel state-error"><p>{unknown ? '更新結果尚未確認；請重讀同一則留言查證。' : '內容版本已改變；請重讀後重新確認操作。'}</p><button class="button" type="button" onclick={reloadVisibility}>重新讀取</button></div>{/if}
      {/if}
    {/if}
  </div>
{/if}
