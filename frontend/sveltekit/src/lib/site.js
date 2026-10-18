import { writable, get } from 'svelte/store';
import {
  apiRequest, BACKEND_KEY, BACKENDS, BASE, clearSessionIfOwned, describeFailure, expired,
  frontendTarget, readBackend, readSession, saveSession, SESSION_KEY, validReturn
} from './contracts.js';

// 每個版面實例擁有自己的狀態；伺服器渲染時不會跨訪客共用 JWT。
export function createSite() {
  const backend = writable(BACKENDS.laravel[1]);
  const session = writable(null);
  const account = writable(null);
  const authState = writable('checking');
  const authError = writable(null);
  const notice = writable(null);
  const dirty = writable(false);
  const pending = writable(false);
  let initPromise = null;
  let generation = 0;
  let suppressUnload = false;

  // sessionStorage 可能被瀏覽器拒絕；公開閱讀仍可使用預設後端。
  function storage() {
    try { return window.sessionStorage; }
    catch { notice.set('瀏覽器未開放分頁儲存，無法跨頁保留登入或後端選擇。'); return null; }
  }

  // 同次登入先向目前後端驗證；網路失敗保留憑證供手動重驗。
  async function initialize() {
    if (initPromise) return initPromise;
    // 非同步初始化只執行一次，避免多個頁面重複驗證同一 JWT。
    initPromise = (async () => {
      const current = storage();
      backend.set(current ? readBackend(current) : BACKENDS.laravel[1]);
      const saved = current ? readSession(current) : null;
      if (!saved) { authState.set('guest'); return; }
      if (expired(saved)) {
        clearSessionIfOwned(current, saved);
        authState.set('expired'); authError.set('登入已逾期，請重新登入');
        return;
      }
      session.set(saved);
      const attempt = generation;
      try {
        const me = await apiRequest(get(backend), '/api/me', { token: saved.accessToken });
        if (attempt !== generation) return;
        if (!me || !Number.isSafeInteger(me.id) || me.id < 1 || !['member', 'admin'].includes(me.role)
          || me.status !== 'active' || typeof me.nickname !== 'string') throw new Error('回應資料格式不正確');
        account.set(me); authState.set('active');
      } catch (failure) {
        if (attempt !== generation) return;
        if (failure?.code === 'E0002' || failure?.code === 'E0102') invalidate(saved, failure);
        else { authState.set('error'); authError.set('暫時無法驗證登入，請確認目前後端後重新驗證。'); }
      }
    })();
    return initPromise;
  }

  // 只有原憑證的確失效或停用才清除；較新登入和連線錯誤都不能被舊回應覆蓋。
  function invalidate(sent, failure) {
    const current = get(session);
    if (!current || current.backend !== sent.backend || current.accessToken !== sent.accessToken
      || current.expiresAt !== sent.expiresAt) return;
    generation++;
    const local = storage();
    const cleared = local && clearSessionIfOwned(local, sent);
    session.set(null); account.set(null);
    pending.set(false);
    authState.set(failure.code === 'E0102' ? 'disabled' : 'expired');
    const detail = describeFailure(failure, '登入已逾期，請重新登入');
    authError.set(cleared ? detail : `${detail}；無法確認本機舊登入資料已清除，請關閉此分頁。`);
  }

  // 登入與註冊只驗本次輸入；其他讀取在 /api/me 確認後才附目前憑證。
  async function request(path, options = {}) {
    await initialize();
    const publicAccountRequest = path === '/api/auth/login' || path === '/api/auth/register';
    const sent = !publicAccountRequest && get(authState) === 'active' ? get(session) : null;
    try {
      return await apiRequest(get(backend), path, { ...options, token: sent?.accessToken });
    } catch (failure) {
      if (sent && ['E0002', 'E0102'].includes(failure?.code)) invalidate(sent, failure);
      throw failure;
    }
  }

  // 前端切換維持同源 session 與路徑；表單有未送出內容時先讓使用者決定。
  function switchFrontend(frontend, path, returnTo) {
    if (!canSwitchFrontend(frontend)) return;
    if (get(dirty) || get(pending)) {
      if (!window.confirm('目前輸入或請求可能尚未完成。要捨棄並切換版本嗎？')) return;
    }
    generation++;
    suppressUnload = true;
    window.location.assign(frontendTarget(path, frontend, returnTo));
  }

  // Nuxt 目前只接收前四個後端，選單和程式切換共用同一限制。
  function canSwitchFrontend(frontend, selectedBackend = get(backend)) {
    return frontend !== 'nuxt' || [BACKENDS.laravel[1], BACKENDS.symfony[1], BACKENDS['aspnet-core'][1], BACKENDS.django[1]].includes(selectedBackend);
  }

  // 表單以切換世代辨識晚到回應，避免離開時把舊結果存入共用 session。
  function version() { return generation; }

  // 後端改選必須先移除舊 JWT，再到登入頁讓新後端重新簽發。
  function switchBackend(next, path, returnTo) {
    if (!Object.values(BACKENDS).some(
      // 選單值只能是固定後端，不從儲存值組任意來源。
      (entry) => entry[1] === next
    ) || next === get(backend)) return;
    if (get(dirty) || get(pending)) {
      if (!window.confirm('目前輸入或請求可能尚未完成。要捨棄並切換後端嗎？')) return;
    }
    const local = storage();
    if (!local) { notice.set('無法儲存目前後端，請允許分頁儲存後重試。'); return; }
    try {
      local.removeItem(SESSION_KEY);
      local.setItem(BACKEND_KEY, next);
    } catch { notice.set('無法儲存目前後端，請允許分頁儲存後重試。'); return; }
    generation++;
    session.set(null); account.set(null); backend.set(next); authState.set('guest');
    const target = path === `${BASE}/login` ? returnTo : path;
    const safe = validReturn(target) ? target : `${BASE}/`;
    suppressUnload = true;
    window.location.assign(`${BASE}/login${safe !== `${BASE}/` ? `?return_to=${encodeURIComponent(safe)}` : ''}`);
  }

  // 登出只在伺服器確認撤銷後清除本地憑證；失敗保留重試能力。
  async function logout() {
    if (get(pending)) return;
    const sent = get(session);
    if (!sent) return;
    const attempt = generation;
    pending.set(true); notice.set(null);
    try {
      await apiRequest(get(backend), '/api/auth/logout', { token: sent.accessToken, method: 'POST' });
      if (attempt !== generation || get(session)?.accessToken !== sent.accessToken) return;
      const local = storage();
      if (!local || !clearSessionIfOwned(local, sent)) {
        notice.set('登出已送出，但無法確認本機登入資料已清除，請關閉此分頁。');
        return;
      }
      pending.set(false);
      generation++;
      session.set(null); account.set(null); authState.set('guest');
      notice.set('已登出目前這次登入。');
      if (window.location.pathname !== `${BASE}/`) {
        suppressUnload = true; window.location.assign(`${BASE}/`);
      }
    } catch (failure) {
      if (attempt !== generation) return;
      if (failure?.code === 'E0002' || failure?.code === 'E0102') invalidate(sent, failure);
      notice.set('登出未完成，請確認目前後端後重試。');
    } finally { if (attempt === generation) pending.set(false); }
  }

  // 登入成功僅存原 JWT 與到期時間；儲存失敗不能宣稱跨頁登入完成。
  function acceptLogin(data) {
    if (!data || typeof data.access_token !== 'string' || !data.access_token
      || data.token_type !== 'Bearer' || typeof data.expires_at !== 'string'
      || !data.user || !Number.isSafeInteger(data.user.id) || !['member', 'admin'].includes(data.user.role)
      || data.user.status !== 'active') return false;
    const local = storage();
    if (!local) return false;
    const next = { backend: get(backend), accessToken: data.access_token, expiresAt: data.expires_at };
    try { if (!saveSession(local, next)) return false; }
    catch { return false; }
    generation++;
    session.set(next); account.set(data.user); authState.set('active'); authError.set(null);
    return true;
  }

  // 編輯中的離頁警示由瀏覽器提供取消與捨棄選擇。
  function beforeUnload(event) {
    if (suppressUnload || (!get(dirty) && !get(pending))) return;
    event.preventDefault(); event.returnValue = '';
  }

  // 從 BFCache 回來可能帶著舊後端狀態；重新載入才會重新驗證。
  function pageshow(event) {
    if (event.persisted) window.location.reload();
  }

  return { backend, session, account, authState, authError, notice, dirty, pending,
    initialize, request, switchFrontend, canSwitchFrontend, switchBackend, logout, acceptLogin, version, beforeUnload, pageshow };
}
