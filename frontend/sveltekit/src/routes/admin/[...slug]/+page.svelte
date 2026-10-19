<script>
  import { getContext, onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { page } from '$app/stores';
  import { BACKENDS, BASE, describeFailure, isArticle, isComment, isLockoutSettings,
    isMember, isPage, positiveNumber, uncertainWrite, validateLockoutSettings } from '$lib/contracts.js';
  import ReLogin from '$lib/ReLogin.svelte';

  const site = getContext('site');
  const { account, authState } = site;
  const path = $page.url.pathname;
  const memberMatch = path.match(/^\/sveltekit\/admin\/members\/([0-9]+)$/);
  const mode = path === `${BASE}/admin/members` ? 'members'
    : path === `${BASE}/admin/articles` ? 'articles'
      : path === `${BASE}/admin/comments` ? 'comments'
        : path === `${BASE}/admin/settings/login-lockout` ? 'settings'
          : memberMatch ? 'member' : 'missing';
  const memberId = positiveNumber(memberMatch?.[1]);
  const pageNumber = positiveNumber($page.url.searchParams.get('page')) || 1;
  const keyword = $page.url.searchParams.get('keyword') || '';
  const labels = { enabled: '啟用鎖定', window_minutes: '累計分鐘', failure_threshold: '失敗次數', lock_seconds: '鎖定秒數', keyword: '搜尋會員' };
  let searchDraft = keyword;
  let ownerId = null;
  let result = null;
  let serverSettings = null;
  let draft = null;
  let loading = mode !== 'missing';
  let readError = '';
  let message = '';
  let fieldErrors = {};
  let failureLogId = '';
  let copyMessage = '';
  let processing = false;
  let unknown = false;
  let readVerified = false;
  let mustRead = false;
  let alive = true;
  let readId = 0;
  let summary;

  // 深連結先驗證目前帳號與管理員角色，再依頁面讀取受保護資料。
  async function initializePage() {
    await site.initialize();
    if (!alive) return;
    ownerId = get(account)?.id || null;
    if (get(authState) === 'active' && get(account)?.role === 'admin' && mode !== 'missing') await load();
    else loading = false;
  }

  // 卸載時讓舊回應失效，避免離開管理頁後洩漏前帳號資料或保留全站等待狀態。
  onMount(() => {
    void initializePage();
    // 管理頁的草稿與等待只屬於本頁，不隨切換版本保存。
    return () => { alive = false; readId++; site.dirty.set(false); site.pending.set(false); };
  });

  // 每次讀取都核對目前世代、帳號與回應形狀；失敗不可偽裝成空列表。
  async function load(resetDraft = false) {
    if (mode === 'missing' || mode === 'member' && !memberId) { loading = false; return false; }
    const current = ++readId;
    const generation = site.version();
    const reader = get(account)?.id;
    loading = true; readError = ''; copyMessage = '';
    try {
      const endpoint = mode === 'members' ? `/api/members?page=${pageNumber}&per_page=20${keyword ? `&filters%5Bkeyword%5D=${encodeURIComponent(keyword)}` : ''}`
        : mode === 'articles' ? `/api/articles?page=${pageNumber}&per_page=20&filters%5Bscope%5D=management`
          : mode === 'comments' ? `/api/comments?page=${pageNumber}&per_page=20`
            : mode === 'member' ? `/api/members/${memberId}` : '/api/settings/login-lockout';
      const data = await site.request(endpoint);
      if (!alive || current !== readId || generation !== site.version() || get(account)?.id !== reader) return false;
      const valid = mode === 'member' ? isMember(data) && data.id === memberId
        : mode === 'settings' ? isLockoutSettings(data)
          : isPage(data, pageNumber) && data.list.every(
            // 管理列表也要逐筆核對內容範圍，不能將錯誤 API 回應顯示為可操作資料。
            (item) => mode === 'members' ? isMember(item) : mode === 'articles' ? isArticle(item) : isComment(item)
          );
      if (!valid) throw new Error('回應資料格式不正確，請確認目前後端。');
      result = data;
      if (mode === 'settings') {
        serverSettings = data;
        if (!draft || resetDraft || !get(site.dirty)) { draft = { ...data }; site.dirty.set(false); }
      }
      readVerified = true;
      if (!unknown) { mustRead = false; failureLogId = ''; fieldErrors = {}; }
      return true;
    } catch (failure) {
      if (!alive || current !== readId || generation !== site.version() || get(account)?.id !== reader) return false;
      result = null; serverSettings = null; readVerified = false;
      // 讀取遭權限或範圍拒絕時，舊設定草稿也可能暴露受保護資料。
      if (['E0003', 'E0004'].includes(failure?.code)) { draft = null; site.dirty.set(false); }
      readError = describeFailure(failure, '讀取失敗，請確認目前後端後重試。');
      failureLogId = failure?.logId || '';
      // 巢狀 Query 錯誤對應到可修正的搜尋欄位，其他錯誤仍留在摘要中。
      const errors = failure?.code === 'E0001' ? { ...(failure.errors || {}) } : {};
      if (mode === 'members' && errors['filters.keyword']) {
        errors.keyword = errors['filters.keyword'];
        delete errors['filters.keyword'];
      }
      fieldErrors = errors;
      message = failure?.code === 'E0001' ? '請先修正標示的欄位。' : '';
      return false;
    } finally { if (alive && current === readId) loading = false; }
  }

  // 搜尋只送核准的會員 keyword，保留 API 字面比對所需的符號與大小寫。
  async function searchMembers(event) {
    event.preventDefault();
    const cleaned = searchDraft.replace(/^ +| +$/g, '');
    if ([...cleaned].length > 254) {
      fieldErrors = { keyword: ['欄位格式或範圍不正確'] }; message = '請先修正標示的欄位。';
      await tick(); summary?.focus(); return;
    }
    window.location.assign(`${BASE}/admin/members${cleaned ? `?keyword=${encodeURIComponent(cleaned)}` : ''}`);
  }

  // 會員狀態只從最新詳情決定下一值，結果不明時停用重送直到人工查證。
  async function changeMemberStatus() {
    if (mode !== 'member' || !result || processing || unknown || mustRead || get(authState) !== 'active') return;
    const next = result.status === 'active' ? 'disabled' : 'active';
    if (!window.confirm(`確定${next === 'disabled' ? '停用' : '恢復'}這位會員？`)) return;
    processing = true; mustRead = true; readVerified = false; site.pending.set(true); message = ''; fieldErrors = {}; failureLogId = '';
    const generation = site.version();
    try {
      await site.request(`/api/members/${memberId}/status`, { method: 'PUT', data: { status: next } });
      if (!alive || generation !== site.version()) return;
      if (await load()) message = '已重新讀取伺服器的會員狀態。';
      else message = '更新已送出，但重讀失敗；請重新讀取確認目前狀態。';
    } catch (failure) {
      if (!alive || generation !== site.version()) return;
      if (['E0003', 'E0004'].includes(failure?.code)) {
        // 權限或目標範圍已改變時，舊詳情不可繼續顯示或再次操作。
        result = null; readVerified = false; mustRead = true;
        readError = describeFailure(failure, '會員資料目前無法讀取。');
        message = ''; failureLogId = failure?.logId || '';
        return;
      }
      unknown = uncertainWrite(failure); mustRead = unknown;
      message = `${unknown ? '結果尚未確認；請先重讀會員狀態，不會自動重送。' : ''}${describeFailure(failure, '更新結果尚未確認，請查證。')}`;
      failureLogId = failure?.logId || '';
      fieldErrors = failure?.code === 'E0001' ? failure.errors || {} : {};
    } finally { processing = false; if (alive && generation === site.version()) site.pending.set(false); }
  }

  // 改動欄位只更新本頁草稿；後端讀回前不宣稱設定已生效。
  function markDraft() { site.dirty.set(true); fieldErrors = {}; message = ''; }

  // 設定 PUT 一次送完整四欄；未知結果保留草稿並要求人工核對伺服器值。
  async function saveSettings(event) {
    event.preventDefault();
    if (!draft || !serverSettings || processing || unknown || mustRead || get(authState) !== 'active') return;
    fieldErrors = validateLockoutSettings(draft);
    if (Object.keys(fieldErrors).length) { message = '請先修正標示的欄位。'; await tick(); summary?.focus(); return; }
    processing = true; mustRead = true; readVerified = false; site.pending.set(true); message = ''; failureLogId = '';
    const generation = site.version();
    try {
      await site.request('/api/settings/login-lockout', { method: 'PUT', data: { ...draft } });
      if (!alive || generation !== site.version()) return;
      if (await load(true)) message = '已儲存並重新讀取伺服器設定。';
      else message = '設定已送出，但重讀失敗；草稿已保留，請重新讀取確認。';
    } catch (failure) {
      if (!alive || generation !== site.version()) return;
      if (['E0003', 'E0004'].includes(failure?.code)) {
        // 設定存取遭拒時移除舊伺服器值與草稿，重新驗證前不再開放寫入。
        result = null; serverSettings = null; draft = null; readVerified = false; mustRead = true;
        site.dirty.set(false);
        readError = describeFailure(failure, '設定目前無法讀取。');
        message = ''; failureLogId = failure?.logId || '';
        return;
      }
      unknown = uncertainWrite(failure); mustRead = unknown;
      fieldErrors = failure?.code === 'E0001' ? failure.errors || {} : {};
      message = `${unknown ? '結果尚未確認；請先讀取伺服器設定，不會自動重送。' : ''}${describeFailure(failure, '設定結果尚未確認，請查證。')}`;
      failureLogId = failure?.logId || '';
      await tick(); summary?.focus();
    } finally { processing = false; if (alive && generation === site.version()) site.pending.set(false); }
  }

  // 重新讀取前先驗證目前角色，避免管理權已變更時用舊帳號狀態重開操作。
  async function reload() {
    const generation = site.version();
    try {
      const me = await site.request('/api/me');
      if (!alive || generation !== site.version() || get(account)?.id !== ownerId) return;
      if (me?.id !== ownerId || me.role !== 'admin' || me.status !== 'active') {
        result = null; serverSettings = null; draft = null; readVerified = false;
        site.dirty.set(false); account.set(me);
        readError = '沒有權限使用此功能。';
        return;
      }
    } catch (failure) {
      if (!alive || generation !== site.version()) return;
      result = null; serverSettings = null; readVerified = false;
      // 暫時斷線保留草稿供查證；明確拒絕才移除舊設定內容。
      if (['E0003', 'E0004'].includes(failure?.code)) { draft = null; site.dirty.set(false); }
      readError = describeFailure(failure, '無法重新驗證目前帳號。');
      return;
    }
    const loaded = await load(!unknown && mustRead);
    if (loaded && !unknown) { mustRead = false; message = '已重新讀取伺服器目前值。'; }
  }

  // 查證後由管理員自行決定是否再次送出，避免結果不明時重複更動。
  function allowRetry() {
    if (!unknown || !readVerified || !window.confirm('已人工比對伺服器目前值，確定允許手動再次送出？')) return;
    unknown = false; mustRead = false; readVerified = false;
    message = '已解除等待；若仍需變更，請手動送出。';
  }

  // 登入另一個帳號時立即移除前一管理員的資料、設定草稿與查證狀態。
  function discardOtherAccount() {
    readId++; result = null; serverSettings = null; draft = null; fieldErrors = {};
    unknown = false; mustRead = false; readVerified = false; site.dirty.set(false);
    ownerId = get(account)?.id || null;
    if (get(account)?.role === 'admin') void load();
  }

  // 同一帳號重登後重驗管理權；未送出的設定草稿仍待人工送出。
  function resumeSameAccount() { if (get(account)?.role === 'admin') void load(); }

  // 有追查碼時提供明確複製操作；剪貼簿失敗仍可從可選文字複製。
  async function copyLogId() {
    if (!failureLogId) return;
    try { await navigator.clipboard.writeText(failureLogId); copyMessage = '已複製追查碼。'; }
    catch { copyMessage = '無法自動複製，請選取追查碼文字。'; }
  }
</script>

<svelte:head><title>{mode === 'members' ? '會員管理' : mode === 'member' ? '會員詳情' : mode === 'articles' ? '文章管理' : mode === 'comments' ? '留言管理' : mode === 'settings' ? '登入鎖定設定' : '找不到頁面'}｜文章集</title></svelte:head>

{#if mode === 'missing' || mode === 'member' && !memberId}
  <div class="state-panel not-found"><h1>找不到頁面<span class="title-dot">.</span></h1><a class="button" href={`${BASE}/`}>返回文章列表</a></div>
{:else}
  <div class="admin-wrap">
    <div class="admin-heading"><div><p class="section-mark">文章集 / 管理介面</p><h1>{mode === 'members' ? '會員管理' : mode === 'member' ? '會員詳情' : mode === 'articles' ? '文章管理' : mode === 'comments' ? '留言管理' : '登入鎖定設定'}<span class="title-dot">.</span></h1></div><span class="admin-number" aria-hidden="true">{mode === 'members' ? '01' : mode === 'member' ? '01—A' : mode === 'articles' ? '02' : mode === 'comments' ? '03' : '04'}</span></div>
    {#if $authState === 'active' && $account?.role === 'admin' && $account?.id === ownerId}
      <nav class="admin-tabs" aria-label="管理導覽"><a href={`${BASE}/admin/members`} aria-current={mode === 'members' || mode === 'member' ? 'page' : undefined}>會員</a><a href={`${BASE}/admin/articles`} aria-current={mode === 'articles' ? 'page' : undefined}>文章</a><a href={`${BASE}/admin/comments`} aria-current={mode === 'comments' ? 'page' : undefined}>留言</a><a href={`${BASE}/admin/settings/login-lockout`} aria-current={mode === 'settings' ? 'page' : undefined}>鎖定設定</a></nav>
    {/if}
    {#if $authState === 'checking'}
      <p class="state-panel" role="status">驗證登入中…</p>
    {:else if $authState !== 'active' || $account?.id !== ownerId}
      {#if ownerId && ['expired', 'guest'].includes($authState)}
        <ReLogin {site} {ownerId} onSameAccount={resumeSameAccount} onDifferentAccount={discardOtherAccount} />
      {:else}
        <div class="state-panel state-error"><p>{$authState === 'disabled' ? '此帳號已停用，無法繼續管理操作。' : '此頁需要管理員登入。'}</p>{#if $authState !== 'disabled'}<a class="button" href={`${BASE}/login?return_to=${encodeURIComponent(path)}`}>前往登入</a>{/if}</div>
      {/if}
    {:else if $account?.role !== 'admin'}
      <div class="state-panel state-error"><p>沒有權限使用此功能。</p><a class="button" href={`${BASE}/`}>返回文章列表</a></div>
    {:else}
      {#if loading}<p class="state-panel" role="status">管理資料載入中…</p>{/if}
      {#if readError}<div class="state-panel state-error" role="alert"><p>{readError}</p><button class="button" type="button" onclick={reload}>重新讀取</button><a class="button button-outline" href={`${BASE}/admin/members`}>返回會員管理</a></div>{/if}
      {#if message}<div class="form-summary" role="alert" tabindex="-1" bind:this={summary}><strong>{message}</strong>
        {#each Object.entries(fieldErrors) as [field, messages]}
          {#if labels[field]}<a href={`#admin-${field}`}>{labels[field]}：{Array.isArray(messages) ? messages.join('、') : String(messages)}</a>
          {:else}<span>{field}：{Array.isArray(messages) ? messages.join('、') : String(messages)}</span>{/if}
        {/each}
      </div>{/if}
      {#if failureLogId}<p class="admin-log">追查碼：<code>{failureLogId}</code> <button class="button button-small button-outline" type="button" onclick={copyLogId}>複製追查碼</button> {#if copyMessage}<span role="status">{copyMessage}</span>{/if}</p>{/if}
      {#if unknown}<div class="state-panel state-error"><p>結果尚未確認。請先讀取伺服器目前值並人工比對，系統不會自動重送。</p><button class="button" type="button" onclick={reload}>讀取伺服器值</button><button class="button button-outline" type="button" disabled={!readVerified} onclick={allowRetry}>已人工查證，允許手動重試</button></div>{/if}
      {#if mode === 'members'}
        <form class="admin-search" onsubmit={searchMembers} novalidate><label for="admin-keyword">搜尋會員</label><div><input id="admin-keyword" bind:value={searchDraft} aria-invalid={Boolean(fieldErrors.keyword)} aria-describedby={fieldErrors.keyword ? 'admin-keyword-error' : undefined} placeholder="暱稱或 Email" /><button class="button" type="submit">搜尋</button>{#if keyword}<a class="button button-outline" href={`${BASE}/admin/members`}>清除搜尋</a>{/if}</div>{#if fieldErrors.keyword}<p class="field-error" id="admin-keyword-error">{fieldErrors.keyword.join('、')}</p>{/if}</form>
      {/if}
      {#if !loading && result && ['members', 'articles', 'comments'].includes(mode)}
        {#if result.list.length === 0}<div class="state-panel"><p>{result.pagination.total === 0 ? mode === 'members' && keyword ? '沒有符合搜尋條件的會員。' : '目前沒有資料。' : '此頁沒有資料。'}</p>{#if result.pagination.total > 0}<a class="button" href={`${path}${keyword ? `?keyword=${encodeURIComponent(keyword)}` : ''}`}>回第一頁</a>{/if}</div>{/if}
        {#if result.list.length}<div class="admin-list">
          {#each result.list as item (item.id)}
            <article class="admin-row"><div class="admin-row-main"><p class="admin-row-label">{mode === 'members' ? 'MEMBER' : mode === 'articles' ? 'ARTICLE' : 'COMMENT'} / {item.id}</p><h2><a href={`${BASE}/${mode === 'members' ? `admin/members/${item.id}` : mode === 'articles' ? `articles/${item.id}` : `comments/${item.id}`}`}>{mode === 'members' ? item.nickname : mode === 'articles' ? item.title : `留言 #${item.id}`}</a></h2><p>{mode === 'members' ? item.email : [...item.body].slice(0, 110).join('')}</p></div>
              <dl><div><dt>{mode === 'members' ? '狀態' : '作者'}</dt><dd>{mode === 'members' ? item.status === 'active' ? '啟用' : '已停用' : item.author.nickname}</dd></div><div><dt>{mode === 'members' ? '加入日期' : '內容狀態'}</dt><dd>{mode === 'members' ? item.created_at.slice(0, 10) : item.visibility === 'hidden' ? '已下架' : '公開'}</dd></div>{#if mode !== 'members'}<div><dt>建立於</dt><dd>{BACKENDS[item.created_backend]?.[0] || item.created_backend}</dd></div>{/if}</dl>
              <a class="button button-small button-outline" href={`${BASE}/${mode === 'members' ? `admin/members/${item.id}` : mode === 'articles' ? `articles/${item.id}` : `comments/${item.id}`}`}>查看詳情</a>
            </article>
          {/each}
        </div>{/if}
        <nav class="pagination" aria-label="管理列表分頁">{#if pageNumber > 1}<a class="button button-outline" href={`${path}?page=${pageNumber - 1}${keyword ? `&keyword=${encodeURIComponent(keyword)}` : ''}`}>上一頁</a>{/if}<span>第 {pageNumber} 頁，共 {result.pagination.total} 筆</span>{#if pageNumber < result.pagination.total_pages}<a class="button button-outline" href={`${path}?page=${pageNumber + 1}${keyword ? `&keyword=${encodeURIComponent(keyword)}` : ''}`}>下一頁</a>{/if}</nav>
      {/if}
      {#if mode === 'member' && !loading && result}<article class="admin-detail author-card"><p class="admin-row-label">MEMBER / {result.id}</p><h2>{result.nickname}</h2><dl><div><dt>Email</dt><dd>{result.email}</dd></div><div><dt>狀態</dt><dd>{result.status === 'active' ? '啟用' : '已停用'}</dd></div><div><dt>加入日期</dt><dd>{result.created_at}</dd></div></dl><div class="author-actions"><button class="button" type="button" disabled={processing || unknown || mustRead} onclick={changeMemberStatus}>{processing ? '處理中…' : result.status === 'active' ? '停用會員' : '恢復會員'}</button><button class="button button-outline" type="button" disabled={processing} onclick={reload}>重新讀取</button><a class="button button-outline" href={`${BASE}/admin/members`}>返回會員管理</a></div></article>{/if}
      {#if mode === 'settings' && draft && !loading}<section class="admin-settings"><div class="admin-server"><h2>目前伺服器值</h2>{#if serverSettings}<p>{serverSettings.enabled ? '啟用' : '停用'} · {serverSettings.window_minutes} 分鐘內 {serverSettings.failure_threshold} 次失敗 · 鎖定 {serverSettings.lock_seconds} 秒</p>{:else}<p>讀取失敗，請重試。</p>{/if}</div><form class="author-form author-card" onsubmit={saveSettings} novalidate><h2>調整登入鎖定</h2><p>一次送出完整四欄；關閉鎖定會清除會員現有的鎖定與失敗計數。</p><label for="admin-enabled">啟用鎖定</label><select id="admin-enabled" bind:value={draft.enabled} onchange={markDraft} aria-invalid={Boolean(fieldErrors.enabled)} disabled={processing || mustRead && !unknown}><option value={true}>啟用</option><option value={false}>停用</option></select>{#if fieldErrors.enabled}<p class="field-error">{fieldErrors.enabled.join('、')}</p>{/if}<label for="admin-window_minutes">累計分鐘</label><input id="admin-window_minutes" type="number" min="1" max="60" step="1" bind:value={draft.window_minutes} oninput={markDraft} aria-invalid={Boolean(fieldErrors.window_minutes)} aria-describedby={fieldErrors.window_minutes ? 'admin-window_minutes-error' : undefined} disabled={processing || mustRead && !unknown} />{#if fieldErrors.window_minutes}<p class="field-error" id="admin-window_minutes-error">{fieldErrors.window_minutes.join('、')}</p>{/if}<label for="admin-failure_threshold">失敗次數</label><input id="admin-failure_threshold" type="number" min="1" max="100" step="1" bind:value={draft.failure_threshold} oninput={markDraft} aria-invalid={Boolean(fieldErrors.failure_threshold)} aria-describedby={fieldErrors.failure_threshold ? 'admin-failure_threshold-error' : undefined} disabled={processing || mustRead && !unknown} />{#if fieldErrors.failure_threshold}<p class="field-error" id="admin-failure_threshold-error">{fieldErrors.failure_threshold.join('、')}</p>{/if}<label for="admin-lock_seconds">鎖定秒數</label><input id="admin-lock_seconds" type="number" min="1" max="86400" step="1" bind:value={draft.lock_seconds} oninput={markDraft} aria-invalid={Boolean(fieldErrors.lock_seconds)} aria-describedby={fieldErrors.lock_seconds ? 'admin-lock_seconds-error' : undefined} disabled={processing || mustRead && !unknown} />{#if fieldErrors.lock_seconds}<p class="field-error" id="admin-lock_seconds-error">{fieldErrors.lock_seconds.join('、')}</p>{/if}<div class="author-actions"><button class="button" type="submit" disabled={processing || unknown || mustRead || !serverSettings}>{processing ? '儲存中…' : '儲存完整設定'}</button><button class="button button-outline" type="button" disabled={processing} onclick={reload}>重新讀取</button></div></form></section>{/if}
    {/if}
  </div>
{/if}
