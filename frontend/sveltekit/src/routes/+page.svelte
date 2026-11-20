<script>
  import { getContext, onMount } from 'svelte';
  import { page } from '$app/stores';
  import { BACKENDS, BASE, describeFailure, isArticle, isPage, positiveNumber } from '$lib/contracts.js';

  const site = getContext('site');
  let loading = true;
  let result = null;
  let error = null;
  let alive = true;
  let requestId = 0;
  const requestedPage = positiveNumber($page.url.searchParams.get('page')) || 1;

  // 每次讀取都明確傳必填分頁欄位；失敗不偽裝成空列表。
  async function load() {
    const current = ++requestId;
    await site.initialize();
    if (!alive || current !== requestId) return;
    let generation = site.version();
    loading = true; error = null; result = null;
    const path = `/api/articles?page=${requestedPage}&per_page=20`;
    try {
      let data;
      try { data = await site.request(path); }
      catch (failure) {
        if (!['E0002', 'E0102'].includes(failure?.code)) throw failure;
        if (!alive || current !== requestId) return;
        generation = site.version();
        // 舊憑證失效後，公開列表仍可用訪客身分重新讀取。
        data = await site.request(path);
      }
      if (!alive || current !== requestId || generation !== site.version()) return;
      if (!isPage(data, requestedPage) || !data.list.every(
        // 單筆資料格式錯誤須呈現讀取錯誤，不可漏掉該篇後顯示為完整清單。
        (item) => isArticle(item) && item.visibility === 'visible'
      )) throw new Error('回應資料格式不正確，請確認目前後端。');
      result = data;
    } catch (failure) {
      if (!alive || current !== requestId || generation !== site.version()) return;
      error = describeFailure(failure, '讀取文章失敗，請確認目前後端後重試。');
    } finally {
      if (alive && current === requestId) {
        // 同頁登出會取消舊身分讀取；提供訪客重讀入口，不能把 null 清單當成已載入。
        if (generation !== site.version()) error = '登入狀態已改變，請重新讀取公開文章。';
        loading = false;
      }
    }
  }

  // 首次畫面在登入重驗後載入，避免把受保護結果先顯示給舊帳號。
  onMount(() => {
    void load();
    // 路由卸載後作廢本頁回應，避免舊列表重新進入下一頁畫面。
    return () => { alive = false; requestId++; };
  });
</script>

<svelte:head>
  <title>所有文章｜文章集</title>
</svelte:head>

<section class="poster-list" aria-labelledby="articles-heading">
  <div class="poster-heading"><h1 id="articles-heading">所有文章</h1></div>
  <div class="poster-content">
    {#if loading}
      <p class="state-panel" role="status">文章載入中…</p>
    {:else if error}
      <div class="state-panel state-error" role="alert">
        <p>{error}</p>
        <button class="button" type="button" onclick={load}>重新讀取</button>
      </div>
    {:else if result?.list.length === 0}
      <div class="state-panel">
        <!-- 零筆超頁仍保留 API 原頁碼和總數，首頁才顯示整體尚無文章。 -->
        <p>{result.pagination.page > 1 || result.pagination.total > 0 ? '此頁沒有文章。' : '尚無文章，稍後再來看看。'}</p>
        {#if result.pagination.page > 1 || result.pagination.total > 0}
          <p>第 {result.pagination.page} 頁，共 {result.pagination.total} 篇</p>
          <a class="button" href={`${BASE}/`}>回第一頁</a>
        {/if}
      </div>
    {:else}
      <div class="article-list">
        {#each result.list as article (article.id)}
          <article class="article-card">
            <h2>
              <a href={`${BASE}/articles/${article.id}`}>{article.title}</a>
            </h2>
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
            <p class="card-excerpt">{[...article.body].slice(0, 110).join('')}{[...article.body].length > 110 ? '…' : ''}</p>
          </article>
        {/each}
      </div>
      <nav class="pagination" aria-label="文章分頁">
        {#if requestedPage > 1}
          <a class="button button-outline" href={`${BASE}/?page=${requestedPage - 1}`}>上一頁</a>
        {/if}
        <span>第 {result.pagination.page} 頁，共 {result.pagination.total} 篇</span>
        {#if requestedPage < result.pagination.total_pages}
          <a class="button button-outline" href={`${BASE}/?page=${requestedPage + 1}`}>下一頁</a>
        {/if}
      </nav>
    {/if}
  </div>
</section>
