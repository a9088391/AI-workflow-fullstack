<script>
  import { onMount } from 'svelte';
  import { get } from 'svelte/store';
  import { countdown, describeFailure, retryAfterSeconds, validateAccount } from './contracts.js';

  export let site;
  export let ownerId;
  export let onSameAccount;
  export let onDifferentAccount;
  let email = '';
  let password = '';
  let error = '';
  let processing = false;
  let lockText = '';
  let lockEnded = false;
  let lockDeadline = 0;
  let timer;
  let active = true;
  let attempt = 0;

  // 依實際時鐘更新鎖定倒數；歸零只恢復輸入，不自動重試登入。
  function updateCountdown() {
    lockText = countdown(lockDeadline);
    if (!lockText && lockDeadline) {
      lockDeadline = 0; lockEnded = true;
      clearInterval(timer); timer = undefined;
    }
  }

  // 表單離頁後取消倒數和晚到回應，避免舊頁改寫新頁共用登入。
  onMount(() => {
    // 元件卸載時釋放計時器與本次等待狀態。
    return () => { active = false; attempt++; clearInterval(timer); site.finishPending(); };
  });

  // 可編輯時修改帳密代表新的登入輸入，清除前一帳號的錯誤與重試提示。
  function markDirty() {
    site.dirty.set(true);
    lockEnded = false;
    if (error && !lockText) error = '';
  }

  // 原頁重登只恢復同一帳號的草稿；不同帳號登入後立即隔離舊文字。
  async function submit(event) {
    event.preventDefault();
    if (processing || lockText) return;
    // 先檢查欄位，鎖定中的 Enter 也由上方門禁阻止送出。
    const checked = validateAccount(email, password);
    if (Object.keys(checked.errors).length) { error = '請檢查 Email 與密碼格式。'; return; }
    processing = true; error = ''; lockEnded = false;
    const sentAttempt = ++attempt;
    // 共用 site 會在後端切換或身分變更時遞增世代；舊登入結果不得套到新狀態。
    await site.initialize();
    if (!active || sentAttempt !== attempt) { processing = false; return; }
    site.pending.set(true);
    const backend = get(site.backend);
    const generation = site.version();
    try {
      const data = await site.request('/api/auth/login', { method: 'POST', data: { email: checked.email, password } });
      if (!active || sentAttempt !== attempt || generation !== site.version() || backend !== get(site.backend)) return;
      if (!site.acceptLogin(data, checked.email)) { error = '登入回應格式或分頁儲存不可用，請重試。'; return; }
      password = ''; site.finishPending();
      if (data.user.id === ownerId) onSameAccount();
      else onDifferentAccount();
    } catch (failure) {
      if (!active || sentAttempt !== attempt || generation !== site.version() || backend !== get(site.backend)) return;
      // 鎖定秒數只信任 Retry-After；缺漏時不推測剩餘時間。
      error = describeFailure(failure, '登入結果尚未確認，請查證後重試。');
      if (failure?.code === 'E0102') { lockText = ''; lockDeadline = 0; clearInterval(timer); }
      if (failure?.code === 'E0105') {
        const seconds = retryAfterSeconds(failure.retryAfter);
        if (seconds !== null) {
          lockDeadline = Date.now() + seconds * 1000;
          updateCountdown(); clearInterval(timer); timer = setInterval(updateCountdown, 250);
        } else error += '（後端未提供有效的 Retry-After 秒數）';
      }
    } finally {
      // 只清理目前表單的等待狀態，不解除仍在倒數的欄位門禁。
      processing = false; if (active && generation === site.version()) site.finishPending();
    }
  }
</script>

<div class="state-panel state-error">
  <h2>重新登入原帳號</h2>
  <p>未送出的文字只留在此頁記憶體。同一帳號重登後仍須手動送出。</p>
  {#if error}
    <p role="alert">{error}</p>
  {/if}
  {#if lockText}
    <p class="lock-note">登入暫時鎖定，約剩 <span aria-hidden="true">{lockText}</span>
    </p>
  {/if}
  {#if lockEnded}
    <p class="success-note" role="status">可再次嘗試登入</p>
  {/if}
  <form class="author-form" onsubmit={submit}>
    <label for="relogin-email">Email</label>
    <input id="relogin-email" type="email" bind:value={email} disabled={Boolean(lockText) || processing} autocomplete="username" oninput={markDirty} />
    <label for="relogin-password">密碼</label>
    <input id="relogin-password" type="password" bind:value={password} disabled={Boolean(lockText) || processing} autocomplete="current-password" oninput={markDirty} />
    <button class="button" type="submit" disabled={Boolean(lockText) || processing}>{processing ? '驗證中…' : '重新登入'}</button>
  </form>
</div>
