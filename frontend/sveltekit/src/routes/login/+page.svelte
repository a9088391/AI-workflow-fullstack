<script>
  import { getContext, onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { page } from '$app/stores';
  import { BASE, countdown, describeFailure, normalizeEmail, retryAfterSeconds, validReturn, validateAccount } from '$lib/contracts.js';

  const site = getContext('site');
  let email = '';
  let password = '';
  let fieldErrors = {};
  let error = '';
  let lockText = '';
  let lockEnded = false;
  let lockDeadline = 0;
  let processing = false;
  let summary;
  let timer;
  let active = true;
  let attempt = 0;
  const candidate = $page.url.searchParams.get('return_to') || '';
  const returnTo = validReturn(candidate) ? candidate : `${BASE}/`;

  // 鎖定倒數按牆上時間更新；歸零只恢復表單，不自動送出。
  function updateCountdown() {
    const remaining = countdown(lockDeadline);
    lockText = remaining;
    if (!remaining && lockDeadline) {
      lockDeadline = 0; lockEnded = true;
      clearInterval(timer); timer = undefined;
    }
  }

  // 倒數僅屬目前頁面；離開時取消計時與晚到的登入回應。
  onMount(() => {
    void site.initialize();
    // 離頁時取消倒數與晚到回應，避免舊表單更新下一頁的共用狀態。
    return () => { active = false; attempt++; clearInterval(timer); site.dirty.set(false); site.pending.set(false); };
  });

  // 修改帳密表示新的一次輸入，先清除舊的可重試提示。
  function markDirty() {
    site.dirty.set(true);
    lockEnded = false;
    if (error && !lockText) error = '';
  }

  // 先檢查本地欄位，再由目前後端決定登入、停用與鎖定結果。
  async function submit(event) {
    event.preventDefault();
    if (processing || lockText) return;
    const checked = validateAccount(email, password);
    fieldErrors = checked.errors;
    if (Object.keys(fieldErrors).length) {
      error = '請先修正標示的欄位。';
      await tick(); summary?.focus(); return;
    }
    error = ''; lockEnded = false; processing = true;
    const sentEmail = checked.email;
    const sentAttempt = ++attempt;
    // 先等舊登入重驗結束，再記錄本次操作世代，避免失效回應吞掉新登入。
    await site.initialize();
    if (!active || sentAttempt !== attempt) { processing = false; return; }
    site.pending.set(true);
    const sentBackend = get(site.backend);
    const sentVersion = site.version();
    try {
      const data = await site.request('/api/auth/login', { method: 'POST', data: { email: sentEmail, password } });
      if (!active || sentAttempt !== attempt || site.version() !== sentVersion || get(site.backend) !== sentBackend || normalizeEmail(email) !== sentEmail) return;
      if (!site.acceptLogin(data)) { error = '登入回應格式或分頁儲存不可用，請確認目前後端後重新登入。'; return; }
      site.dirty.set(false); site.pending.set(false);
      window.location.assign(returnTo);
    } catch (failure) {
      if (!active || sentAttempt !== attempt || site.version() !== sentVersion || get(site.backend) !== sentBackend || normalizeEmail(email) !== sentEmail) return;
      fieldErrors = failure?.errors || {};
      error = describeFailure(failure, '登入結果尚未確認，請確認目前後端後再試。');
      if (failure?.code === 'E0102') { lockText = ''; lockDeadline = 0; clearInterval(timer); }
      if (failure?.code === 'E0105') {
        const seconds = retryAfterSeconds(failure.retryAfter);
        if (seconds !== null) {
          lockDeadline = Date.now() + seconds * 1000;
          updateCountdown();
          clearInterval(timer);
          timer = setInterval(updateCountdown, 250);
        } else error += '（後端未提供有效的 Retry-After 秒數）';
      }
      if (Object.keys(fieldErrors).length) { await tick(); summary?.focus(); }
    } finally { processing = false; if (active && site.version() === sentVersion) site.pending.set(false); }
  }
</script>

<svelte:head><title>會員登入｜文章集</title></svelte:head>

<div class="auth-wrap">
  <div class="auth-intro"><h1>會員登入<span class="title-dot">.</span></h1><p>登入後即可發表文章與留言。</p></div>
  <form class="auth-card" novalidate onsubmit={submit}>
    {#if error}<div class="form-summary" role="alert" tabindex="-1" bind:this={summary}>
      <strong>{error}</strong>
      {#each Object.entries(fieldErrors) as [field, messages]}
        <a href={`#${field}`}>{field === 'email' ? 'Email' : '密碼'}：{messages.join('、')}</a>
      {/each}
    </div>{/if}
    {#if lockText}<p class="lock-note">登入暫時鎖定，約剩 <span aria-hidden="true">{lockText}</span></p>{/if}
    {#if lockEnded}<p class="success-note" role="status">可再次嘗試登入</p>{/if}
    <label for="email">Email</label>
    <input id="email" name="email" type="text" inputmode="email" autocomplete="username" bind:value={email}
      disabled={Boolean(lockText) || processing} aria-invalid={Boolean(fieldErrors.email)} aria-describedby={fieldErrors.email ? 'email-error' : undefined} oninput={markDirty} />
    {#if fieldErrors.email}<p id="email-error" class="field-error">{fieldErrors.email.join('、')}</p>{/if}
    <label for="password">密碼</label>
    <input id="password" name="password" type="password" autocomplete="current-password" bind:value={password}
      disabled={Boolean(lockText) || processing} aria-invalid={Boolean(fieldErrors.password)} aria-describedby={fieldErrors.password ? 'password-error' : undefined} oninput={markDirty} />
    {#if fieldErrors.password}<p id="password-error" class="field-error">{fieldErrors.password.join('、')}</p>{/if}
    <button class="button button-primary auth-submit" type="submit" disabled={Boolean(lockText) || processing}>{processing ? '登入中…' : '登入'}</button>
    <div class="auth-foot">還沒有帳號？ <a href={`${BASE}/register`}>註冊會員</a></div>
  </form>
</div>
