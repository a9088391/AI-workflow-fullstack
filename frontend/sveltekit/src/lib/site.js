import { writable, get } from 'svelte/store';
import {
  apiRequest, BACKEND_KEY, BACKENDS, BASE, FRONTENDS, clearSessionIfOwned, describeFailure, expired,
  frontendTarget, isAccount, readBackend, readSession, saveSession, SESSION_KEY, validReturn
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
  let sessionGeneration = 0;
  let writeGeneration = 0;
  let lastAccountId = null;
  let logoutPending = false;
  let suppressUnload = false;
  const requests = new Map();

  // 切換可取消舊傳輸；失權只取消讀取，已送出的寫入仍須保留原操作結果供查證。
  function cancelRequests(readsOnly = false) {
    for (const [controller, method] of requests) {
      if (!readsOnly || method === 'GET') controller.abort();
    }
  }

  // 目前表單結束時仍須計入其他在途寫入，避免重登完成提早解除原寫入的等待與登出門禁。
  function finishPending() {
    pending.set(logoutPending || [...requests.entries()].some(
      // 讀取不封鎖登出；任何尚未結束的 POST／PUT 都仍有結果責任。
      ([controller, method]) => !controller.signal.aborted && method !== 'GET'
    ));
  }

  // sessionStorage 可能被瀏覽器拒絕；公開閱讀仍可使用預設後端。
  function storage() {
    try {
      const local = window.sessionStorage;
      local.getItem(BACKEND_KEY);
      local.getItem(SESSION_KEY);
      return local;
    }
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
      // 站內換頁沿用同一外殼與憑證；只有換登入、後端或外殼失效才作廢這次身分重驗。
      const attempt = sessionGeneration;
      try {
        const me = await apiRequest(get(backend), '/api/me', { token: saved.accessToken });
        if (attempt !== sessionGeneration) return;
        if (!isAccount(me) || me.status !== 'active') throw new Error('回應資料格式不正確');
        lastAccountId = me.id;
        account.set(me); authState.set('active');
      } catch (failure) {
        if (attempt !== sessionGeneration) return;
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
    sessionGeneration++;
    cancelRequests(true);
    const local = storage();
    const cleared = local && clearSessionIfOwned(local, sent);
    session.set(null); account.set(null);
    authState.set(failure.code === 'E0102' ? 'disabled' : 'expired');
    const detail = describeFailure(failure, '登入已逾期，請重新登入');
    authError.set(cleared ? detail : `${detail}；無法確認本機舊登入資料已清除，請關閉此分頁。`);
  }

  // 登入與註冊只驗本次輸入；其他讀取在 /api/me 確認後才附目前憑證。
  async function request(path, options = {}) {
    await initialize();
    const publicAccountRequest = path === '/api/auth/login' || path === '/api/auth/register';
    const sent = !publicAccountRequest && get(authState) === 'active' ? get(session) : null;
    const controller = new AbortController();
    const method = options.method || 'GET';
    requests.set(controller, method);
    try {
      const data = await apiRequest(get(backend), path, { ...options, token: sent?.accessToken,
        signal: options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal });
      if (controller.signal.aborted) throw new DOMException('已取消舊頁請求', 'AbortError');
      return data;
    } catch (failure) {
      if (!controller.signal.aborted && sent && ['E0002', 'E0102'].includes(failure?.code)) invalidate(sent, failure);
      throw failure;
    } finally { requests.delete(controller); finishPending(); }
  }

  // 前端切換維持同源 session 與路徑；表單有未送出內容時先讓使用者決定。
  function switchFrontend(frontend, path, returnTo) {
    if (!canSwitchFrontend(frontend)) return;
    if (get(dirty) || get(pending)) {
      if (!window.confirm('目前輸入或請求可能尚未完成。要捨棄並切換版本嗎？')) return;
    }
    generation++;
    sessionGeneration++;
    writeGeneration++;
    cancelRequests();
    suppressUnload = true;
    window.location.assign(frontendTarget(path, frontend, returnTo));
  }

  // 六版前端共用全部十二個後端；只接受固定的前端選項。
  function canSwitchFrontend(frontend) {
    return Object.hasOwn(FRONTENDS, frontend);
  }

  // 表單以切換世代辨識晚到回應，避免離開時把舊結果存入共用 session。
  function version() { return generation; }

  // 同帳失權與重登不代表在途寫入未發生；只在換帳、切換或登出時作廢其畫面 owner。
  function writeVersion() { return writeGeneration; }

  // 外殼銷毀後取消可取消的傳輸，舊頁仍不得將取消當成伺服器回滾。
  function dispose() { generation++; sessionGeneration++; writeGeneration++; cancelRequests(); }

  // 已接受站內離頁時也要作廢舊請求；取消離頁則維持原操作與草稿。
  function leavePage() { generation++; writeGeneration++; cancelRequests(); }

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
    sessionGeneration++;
    writeGeneration++;
    cancelRequests();
    session.set(null); account.set(null); backend.set(next); authState.set('guest');
    const target = path === `${BASE}/login` ? returnTo : path;
    const safe = validReturn(target) ? target : `${BASE}/`;
    suppressUnload = true;
    window.location.assign(`${BASE}/login${safe !== `${BASE}/` ? `?return_to=${encodeURIComponent(safe)}` : ''}`);
  }

  // 有草稿先確認捨棄；伺服器確認撤銷後才清除本地憑證，失敗保留重試能力。
  async function logout() {
    if (get(pending)) return;
    if (get(dirty) && !window.confirm('尚有未儲存文字，確定要捨棄並登出嗎？')) return;
    const sent = get(session);
    if (!sent) return;
    // 登出屬於原憑證，站內換頁不能丟棄結果；較新的登入則不能被舊登出撤銷。
    const attempt = sessionGeneration;
    const pageOperation = writeGeneration;
    logoutPending = true; pending.set(true); notice.set(null);
    try {
      await apiRequest(get(backend), '/api/auth/logout', { token: sent.accessToken, method: 'POST' });
      if (attempt !== sessionGeneration || get(session)?.accessToken !== sent.accessToken) return;
      const local = storage();
      if (!local || !clearSessionIfOwned(local, sent)) {
        notice.set('登出已送出，但無法確認本機登入資料已清除，請關閉此分頁。');
        return;
      }
      const originalPage = pageOperation === writeGeneration;
      sessionGeneration++;
      // 原憑證仍須撤銷；新頁的文字與公開寫入不屬於先前同意捨棄的頁面，不能取消或強制導離。
      if (originalPage) {
        generation++; writeGeneration++; cancelRequests(); dirty.set(false);
      } else cancelRequests(true);
      session.set(null); account.set(null); authState.set('guest');
      notice.set('已登出目前這次登入。');
      if (originalPage && window.location.pathname !== `${BASE}/`) {
        suppressUnload = true; window.location.assign(`${BASE}/`);
      }
    } catch (failure) {
      if (attempt !== sessionGeneration) return;
      if (failure?.code === 'E0002' || failure?.code === 'E0102') invalidate(sent, failure);
      notice.set('登出未完成，請確認目前後端後重試。');
    } finally {
      // 這次登出已結束；仍以目前在途寫入重算，不能因離頁而留下永久等待或解開新操作。
      logoutPending = false; finishPending();
    }
  }

  // 登入成功僅存原 JWT 與到期時間；儲存失敗不能宣稱跨頁登入完成。
  function acceptLogin(data, expectedEmail) {
    if (!data || typeof data.access_token !== 'string' || !data.access_token
      || data.token_type !== 'Bearer' || typeof data.expires_at !== 'string'
      || !isAccount(data.user) || data.user.status !== 'active' || data.user.email !== expectedEmail) return false;
    const local = storage();
    if (!local) return false;
    const next = { backend: get(backend), accessToken: data.access_token, expiresAt: data.expires_at };
    try { if (!saveSession(local, next)) return false; }
    catch { return false; }
    generation++;
    sessionGeneration++;
    if (lastAccountId !== data.user.id) { writeGeneration++; cancelRequests(); }
    lastAccountId = data.user.id;
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
    initialize, request, switchFrontend, canSwitchFrontend, switchBackend, logout, acceptLogin, version,
    writeVersion, finishPending, leavePage, dispose, beforeUnload, pageshow };
}
