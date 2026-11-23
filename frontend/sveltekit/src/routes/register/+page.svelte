<script>
  import { getContext, onMount, tick } from 'svelte';
  import { get } from 'svelte/store';
  import { BASE, describeFailure, normalizeEmail, uncertainWrite, validateAccount } from '$lib/contracts.js';

  const site = getContext('site');
  let email = '';
  let password = '';
  let nickname = '';
  let fieldErrors = {};
  let error = '';
  let unknown = false;
  let processing = false;
  let summary;
  let active = true;
  let attempt = 0;

  // 註冊表單只保留在本頁記憶體，切換或離頁不傳送草稿。
  onMount(() => {
    void site.initialize();
    // 離頁時作廢晚到的註冊結果，並清掉只屬於此表單的未送出狀態。
    return () => { active = false; attempt++; site.dirty.set(false); site.finishPending(); };
  });

  // 使用者修改任一欄位時標記未送出內容，以便版本切換前確認。
  function markDirty() { site.dirty.set(true); }

  // 註冊成功只轉登入；未知寫入結果需提示查證，不自動重送。
  async function submit(event) {
    event.preventDefault();
    if (processing || unknown) return;
    const checked = validateAccount(email, password, nickname);
    fieldErrors = checked.errors;
    if (Object.keys(fieldErrors).length) {
      error = '請先修正標示的欄位。';
      await tick(); summary?.focus(); return;
    }
    error = ''; unknown = false; processing = true;
    const sentEmail = checked.email;
    const sentAttempt = ++attempt;
    // 註冊也等初始化讀到選定後端後才開始，避免舊登入重驗改變操作世代。
    await site.initialize();
    if (!active || sentAttempt !== attempt) { processing = false; return; }
    site.pending.set(true);
    const sentBackend = get(site.backend);
    // 註冊不使用舊 JWT；舊登入失效不能吞掉本次結果，接受離頁或切換才作廢操作。
    const sentVersion = site.writeVersion();
    try {
      await site.request('/api/auth/register', { method: 'POST', data: { email: sentEmail, password, nickname } });
      if (!active || sentAttempt !== attempt || site.writeVersion() !== sentVersion || get(site.backend) !== sentBackend || normalizeEmail(email) !== sentEmail) return;
      site.dirty.set(false); site.finishPending();
      window.location.assign(`${BASE}/login`);
    } catch (failure) {
      if (!active || sentAttempt !== attempt || site.writeVersion() !== sentVersion || get(site.backend) !== sentBackend || normalizeEmail(email) !== sentEmail) return;
      fieldErrors = failure?.errors || {};
      unknown = uncertainWrite(failure);
      error = unknown ? describeFailure({ logId: failure?.logId }, '註冊結果尚未確認。請先到登入頁查證帳號，勿立即重送。')
        : describeFailure(failure, '註冊失敗，請確認目前後端後重試。');
      await tick(); summary?.focus();
    } finally { processing = false; if (active && site.writeVersion() === sentVersion) site.finishPending(); }
  }
</script>

<svelte:head>
  <title>註冊會員｜文章集</title>
</svelte:head>

<div class="auth-wrap poster-account poster-register">
  <div class="poster-art" aria-hidden="true"></div>
  <div class="auth-intro">
    <h1>註冊會員</h1>
  </div>
  <form class="auth-card" novalidate onsubmit={submit}>
    {#if error}
      <div class="form-summary" role="alert" tabindex="-1" bind:this={summary}>
        <strong>{error}</strong>
        {#each Object.entries(fieldErrors) as [field, messages]}
          <a href={`#${field}`}>{field === 'email' ? 'Email' : field === 'nickname' ? '暱稱' : '密碼'}：{messages.join('、')}</a>
        {/each}
        {#if unknown}
          <a href={`${BASE}/login`}>前往登入查證</a>
        {/if}
      </div>
    {/if}
    <label for="email">Email</label>
    <input id="email" name="email" type="text" inputmode="email" autocomplete="username" bind:value={email}
    disabled={processing} aria-invalid={Boolean(fieldErrors.email)} aria-describedby={fieldErrors.email ? 'email-error' : undefined} oninput={markDirty} />
    {#if fieldErrors.email}
      <p id="email-error" class="field-error">{fieldErrors.email.join('、')}</p>
    {/if}
    <label for="nickname">暱稱</label>
    <input id="nickname" name="nickname" type="text" autocomplete="nickname" bind:value={nickname}
    disabled={processing} aria-invalid={Boolean(fieldErrors.nickname)} aria-describedby={fieldErrors.nickname ? 'nickname-error' : undefined} oninput={markDirty} />
    {#if fieldErrors.nickname}
      <p id="nickname-error" class="field-error">{fieldErrors.nickname.join('、')}</p>
    {/if}
    <label for="password">密碼</label>
    <input id="password" name="password" type="password" autocomplete="new-password" bind:value={password}
    disabled={processing} aria-invalid={Boolean(fieldErrors.password)} aria-describedby={fieldErrors.password ? 'password-error' : undefined} oninput={markDirty} />
    {#if fieldErrors.password}
      <p id="password-error" class="field-error">{fieldErrors.password.join('、')}</p>
    {/if}
    <p class="form-hint">密碼為 4–16 碼英文字母或數字。</p>
    <button class="button button-primary auth-submit" type="submit" disabled={processing || unknown}>{processing ? '註冊中…' : '註冊'}</button>
    <div class="auth-foot">已經有帳號？ <a href={`${BASE}/login`}>返回登入</a>
    </div>
  </form>
</div>
