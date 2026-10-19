<script>
  import { onMount, setContext } from 'svelte';
  import { get } from 'svelte/store';
  import { beforeNavigate } from '$app/navigation';
  import { page } from '$app/stores';
  import { BASE, BACKENDS, FRONTENDS, backendName, validReturn } from '$lib/contracts.js';
  import { createSite } from '$lib/site.js';
  import './style.css';

  let { children } = $props();
  const site = createSite();
  setContext('site', site);
  const { backend, account, authState, authError, notice, dirty, pending } = site;

  // 站內導覽也須詢問是否捨棄草稿；瀏覽器離頁由 beforeUnload 處理。
  beforeNavigate(({ cancel, to }) => {
    if (!to || (!get(dirty) && !get(pending))) return;
    // 欄位錯誤摘要的錨點只移動頁內位置，不會捨棄草稿。
    if (to.url.pathname === window.location.pathname && to.url.search === window.location.search) return;
    if (!window.confirm('目前輸入或請求可能尚未完成。要捨棄並離開嗎？')) cancel();
    else site.dirty.set(false);
  });

  // 只在瀏覽器讀取分頁儲存與驗證登入，SSR 不接觸共享的業務狀態。
  onMount(() => {
    void site.initialize();
    window.addEventListener('beforeunload', site.beforeUnload);
    window.addEventListener('pageshow', site.pageshow);
    // 版面卸載時移除瀏覽器事件，避免熱更新或導覽後重複處理。
    return () => {
      window.removeEventListener('beforeunload', site.beforeUnload);
      window.removeEventListener('pageshow', site.pageshow);
    };
  });

  // 選單改選前端時保留目前語意頁面；登入頁另帶合法返回目標。
  function chooseFrontend(event) {
    const selected = event.currentTarget.value;
    const path = $page.url.pathname;
    const returnTo = $page.url.searchParams.get('return_to') || `${BASE}/`;
    site.switchFrontend(selected, path, returnTo);
    event.currentTarget.value = 'sveltekit';
  }

  // 改選後端清除原 JWT，登入後回到目前合法的閱讀目標。
  function chooseBackend(event) {
    const selected = event.currentTarget.value;
    const path = $page.url.pathname;
    const returnTo = $page.url.searchParams.get('return_to') || `${BASE}/`;
    site.switchBackend(selected, path, returnTo);
    event.currentTarget.value = $backend;
  }

  // 登出失敗仍保留按鈕供手動重試，成功則由共用狀態更新畫面。
  function submitLogout() { void site.logout(); }

  // 驗證失敗時只重載目前頁，重新向選定後端查核同一憑證。
  function retryAuth() { window.location.reload(); }
</script>

<svelte:head>
  <meta name="theme-color" content="#fff4bd" />
</svelte:head>

<div class="site-shell">
  <header class="site-header">
    <a class="wordmark" href={`${BASE}/`}>文章集<span class="wordmark-dot">.</span></a>
    <nav class="main-nav" aria-label="主要導覽">
      <a aria-current={$page.url.pathname === `${BASE}/` ? 'page' : undefined} href={`${BASE}/`}>所有文章</a>
      {#if $authState === 'active'}
        <a aria-current={$page.url.pathname === `${BASE}/articles/new` ? 'page' : undefined} href={`${BASE}/articles/new`}>發文</a>
        <a aria-current={$page.url.pathname === `${BASE}/me/articles` ? 'page' : undefined} href={`${BASE}/me/articles`}>我的文章</a>
        <a aria-current={$page.url.pathname === `${BASE}/me/comments` ? 'page' : undefined} href={`${BASE}/me/comments`}>我的留言</a>
      {:else}
        <a aria-current={$page.url.pathname === `${BASE}/login` ? 'page' : undefined} href={`${BASE}/login`}>登入</a>
        <a aria-current={$page.url.pathname === `${BASE}/register` ? 'page' : undefined} href={`${BASE}/register`}>註冊</a>
      {/if}
    </nav>
    <details class="mobile-nav">
      <summary>選單</summary>
      <nav aria-label="手機導覽">
        <a href={`${BASE}/`}>所有文章</a>
        {#if $authState !== 'active'}
          <a href={`${BASE}/login`}>登入</a>
          <a href={`${BASE}/register`}>註冊</a>
        {:else}
          <a href={`${BASE}/articles/new`}>發文</a>
          <a href={`${BASE}/me/articles`}>我的文章</a>
          <a href={`${BASE}/me/comments`}>我的留言</a>
        {/if}
      </nav>
    </details>
    <div class="header-account">
      {#if $authState === 'checking'}
        <span>驗證登入中…</span>
      {:else if $authState === 'active'}
        <strong class="account-name">{$account?.nickname}</strong>
        <button class="button button-small button-outline" type="button" disabled={$pending} onclick={submitLogout}>登出</button>
      {:else if $authState === 'error'}
        <button class="button button-small button-outline" type="button" onclick={retryAuth}>重新驗證</button>
      {/if}
    </div>
  </header>

  <div class="switch-bar">
    <div class="current-switches">
      <span>目前前端 <strong>SvelteKit</strong></span>
      <span>目前後端 <strong>{backendName($backend)}</strong></span>
    </div>
    <details class="switch-panel">
      <summary>切換版本</summary>
      <div class="switch-fields">
        <label>前端
          <select value="sveltekit" onchange={chooseFrontend}>
            {#each Object.entries(FRONTENDS) as [id, name]}
              <option value={id} disabled={!site.canSwitchFrontend(id, $backend)}>{name}{site.canSwitchFrontend(id, $backend) ? '' : '（此後端尚未提供）'}</option>
            {/each}
          </select>
        </label>
        <label>後端
          <select value={$backend} onchange={chooseBackend}>
            {#each Object.entries(BACKENDS) as [id, entry]}
              <option value={entry[1]}>{entry[0]}</option>
            {/each}
          </select>
        </label>
      </div>
      <p class="switch-hint">切換前端保留同次登入；切換後端須重新登入。</p>
    </details>
  </div>

  {#if $notice}<p class="notice" role="status">{$notice}</p>{/if}
  {#if $authError}<p class="notice notice-error" role="alert">{$authError} {#if $authState === 'expired'}<a href={`${BASE}/login${validReturn($page.url.pathname) ? `?return_to=${encodeURIComponent($page.url.pathname)}` : ''}`}>重新登入</a>{/if}</p>{/if}

  <!-- 頁碼變更須重讀資料；欄位錨點只移動焦點，不可重建並清空表單。 -->
  <main id="main-content">{#key $page.url.pathname + $page.url.search}{@render children()}{/key}</main>
  <footer class="site-footer"><span>文章集 / SvelteKit</span><span>閱讀與分享，從一篇文章開始。</span></footer>
</div>
