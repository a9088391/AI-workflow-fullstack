<script>
  import { getContext, onMount } from 'svelte';
  import { page } from '$app/stores';
  import { BACKENDS, BASE, describeFailure, isArticle, isPage, positiveNumber } from '$lib/contracts.js';

  const site = getContext('site');
  let loading = true;
  let result = null;
  let error = null;
  const requestedPage = positiveNumber($page.url.searchParams.get('page')) || 1;

  // 每次讀取都明確傳必填分頁欄位；失敗不偽裝成空列表。
  async function load() {
    loading = true; error = null; result = null;
    const path = `/api/articles?page=${requestedPage}&per_page=20`;
    try {
      let data;
      try { data = await site.request(path); }
      catch (failure) {
        if (!['E0002', 'E0102'].includes(failure?.code)) throw failure;
        // 舊憑證失效後，公開列表仍可用訪客身分重新讀取。
        data = await site.request(path);
      }
      if (!isPage(data, requestedPage) || !data.list.every(
        // 單筆資料格式錯誤須呈現讀取錯誤，不可漏掉該篇後顯示為完整清單。
        (item) => isArticle(item)
      )) throw new Error('回應資料格式不正確，請確認目前後端。');
      result = data;
    } catch (failure) {
      error = describeFailure(failure, '讀取文章失敗，請確認目前後端後重試。');
    } finally { loading = false; }
  }

  // 首次畫面在登入重驗後載入，避免把受保護結果先顯示給舊帳號。
  onMount(() => { void load(); });
</script>

<svelte:head><title>所有文章｜文章集</title></svelte:head>

<section class="page-intro list-intro">
  <div><p class="section-mark">寫下來，也讀下去。</p><h1>所有文章<span class="title-dot">.</span></h1></div>
  <p class="intro-note">來自不同後端的文字，在這裡一起被看見。</p>
</section>

{#if loading}
  <p class="state-panel" role="status">文章載入中…</p>
{:else if error}
  <div class="state-panel state-error" role="alert"><p>{error}</p><button class="button" type="button" onclick={load}>重新讀取</button></div>
{:else if result?.list.length === 0}
  <div class="state-panel">
    <p>{result.pagination.total === 0 ? '尚無文章，稍後再來看看。' : '此頁沒有文章。'}</p>
    {#if result.pagination.total > 0}<a class="button" href={`${BASE}/`}>回第一頁</a>{/if}
  </div>
{:else}
  <div class="article-grid">
    {#each result.list as article (article.id)}
      <article class="article-card">
        <h2><a href={`${BASE}/articles/${article.id}`}>{article.title}</a></h2>
        <p class="card-excerpt">{[...article.body].slice(0, 110).join('')}{[...article.body].length > 110 ? '…' : ''}</p>
        <dl class="meta-row">
          <div><dt>作者</dt><dd>{article.author.nickname}</dd></div>
          <div><dt>發布日期</dt><dd>{article.created_at.slice(0, 10)}</dd></div>
          <div><dt>建立於</dt><dd class="accent-text">{BACKENDS[article.created_backend]?.[0] || article.created_backend}</dd></div>
        </dl>
      </article>
    {/each}
  </div>
  <nav class="pagination" aria-label="文章分頁">
    {#if requestedPage > 1}<a class="button button-outline" href={`${BASE}/?page=${requestedPage - 1}`}>上一頁</a>{/if}
    <span>第 {result.pagination.page} 頁，共 {result.pagination.total} 篇</span>
    {#if requestedPage < result.pagination.total_pages}<a class="button button-outline" href={`${BASE}/?page=${requestedPage + 1}`}>下一頁</a>{/if}
  </nav>
{/if}
