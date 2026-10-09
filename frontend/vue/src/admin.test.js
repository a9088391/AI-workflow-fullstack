import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import { compile } from '@vue/compiler-dom'
import { renderToString } from '@vue/server-renderer'
import * as vue from 'vue'
import * as auth from './auth.js'
import * as registration from './registration.js'
import * as articles from './articles.js'
import * as comments from './comments.js'
import * as admin from './admin.js'

const administrator = { id: 1, email: 'admin@example.test', nickname: '管理員', role: 'admin', status: 'active', created_at: '2026-09-24 09:00:00' }
const member = { id: 2, email: 'member@example.test', nickname: '阿明', role: 'member', status: 'active', created_at: '2026-09-24 09:00:00' }
const lockout = { enabled: true, window_minutes: 5, failure_threshold: 5, lock_seconds: 300 }
// 集中組出契約回應，讓各測試只改本次關注的狀態和資料。
const success = (data) => ({ success: true, message: 'OK', data })
// 分頁總數由測試明確提供，模擬後端合法超頁。
const list = (items, page = 1, total = items.length) => success({ list: items, pagination: { page, per_page: 20, total, total_pages: Math.ceil(total / 20) } })

// 測試真實 App script 與模板，只替換 HTTP、Storage 和導覽來控制交錯回應。
function app(path, search = '', token = 'saved-token', selectedBackend = auth.API_BASE) {
  const requests = []
  const redirects = []
  const focuses = []
  const lifecycle = {}
  const storage = new Map()
  const sessionStorage = {
    // 保留缺值語意，讓正式 session parser 決定是否有憑證。
    getItem(key) { return storage.get(key) ?? null },
    // 保存憑證供重新驗證與登出交錯案例使用。
    setItem(key, value) { storage.set(key, value) },
    // 記錄失效或登出撤銷，避免測試憑證殘留。
    removeItem(key) { storage.delete(key) },
  }
  // 渲染前先寫入同分頁選擇，驗證各後端都沿用目前選取的 API。
  if (selectedBackend !== auth.API_BASE) auth.selectBackend(sessionStorage, selectedBackend)
  if (token) auth.saveSession(sessionStorage, token, '2026-09-24 18:00:00')
  const context = {
    ...vue, ...auth, ...registration, ...articles, ...comments, ...admin,
    AbortController, URLSearchParams, performance, console, setTimeout, clearTimeout, setInterval, clearInterval, sessionStorage,
    document: {
      // 摘要點選需聚焦實際欄位 ID；用可觀察焦點替代真實 DOM。
      getElementById(id) { return {
        // 保存被聚焦的欄位，驗證搜尋錯誤摘要能回到輸入框。
        focus() { focuses.push(id) },
      } },
    },
    window: { sessionStorage, location: { pathname: path, search,
      // 記錄安全本機返回與搜尋導覽，不切離測試。
      assign(value) { redirects.push(value) },
    },
      // 測試直接呼叫掛載，不需瀏覽器事件監聽。
      addEventListener() {},
      // 正式 cleanup 仍可執行，測試環境沒有已註冊事件。
      removeEventListener() {},
    },
    // 保存掛載程序，使測試以正式登入驗證入口開始。
    onMounted(callback) { lifecycle.mount = callback },
    // 保存卸載程序，讓每例結束時撤銷 timer 與請求。
    onBeforeUnmount(callback) { lifecycle.unmount = callback },
    // 即使收到 abort 仍可回應，藉此驗證 epoch 不讓舊帳號資料重現。
    fetch(url, options) { const response = Promise.withResolvers(); requests.push({ url, ...options, ...response }); return response.promise },
  }
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const bindings = Object.keys(compileScript(descriptor, { id: 'admin-test' }).bindings)
  const script = descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
  const scope = vue.effectScope()
  // 在獨立 effect scope 執行正式 script，測試完成後可以整批清理 reactive watcher。
  const page = scope.run(() => runInNewContext(`${script}\n;({ ${bindings.join(', ')} })`, context))
  lifecycle.mount()
  return { page, requests, redirects, focuses,
    // 先執行元件卸載，再停止 effect scope，避免測試間殘留請求與計時器。
    close() { lifecycle.unmount(); scope.stop() },
  }
}

// Django 作者端點已支援；登入及註冊說明與其他後端使用同一發表承諾。
test('Django auth copy offers publishing without unsupported warnings', async (t) => {
  const djangoLogin = app('/vue/login', '', null, auth.BACKENDS.django)
  const djangoRegister = app('/vue/register', '', null, auth.BACKENDS.django)
  const laravelLogin = app('/vue/login', '', null)
  const laravelRegister = app('/vue/register', '', null)
  t.after(djangoLogin.close)
  t.after(djangoRegister.close)
  t.after(laravelLogin.close)
  t.after(laravelRegister.close)
  const loginHtml = await rendered(djangoLogin.page)
  assert.match(loginHtml, /登入後即可發表文章與查看自己的文章。/)
  assert.doesNotMatch(loginHtml, /發文與我的文章尚未提供/)
  const registrationHtml = await rendered(djangoRegister.page)
  assert.match(registrationHtml, /登入並發表文章/)
  assert.match(registrationHtml, /日後發表的內容旁/)
  assert.match(await rendered(laravelLogin.page), /登入後即可發表文章與查看自己的文章/)
  assert.match(await rendered(laravelRegister.page), /會顯示在您日後發表的內容旁/)
})

// Flask 作者 API 已開放，登入與暱稱說明應與其他後端提供相同發文入口。
test('Flask auth copy offers publishing', async (t) => {
  const flaskLogin = app('/vue/login', '', null, auth.BACKENDS.flask)
  const flaskRegister = app('/vue/register', '', null, auth.BACKENDS.flask)
  t.after(flaskLogin.close)
  t.after(flaskRegister.close)
  const loginHtml = await rendered(flaskLogin.page)
  const registrationHtml = await rendered(flaskRegister.page)
  assert.match(loginHtml, /登入後即可發表文章與查看自己的文章/)
  assert.match(registrationHtml, /登入並發表文章/)
  assert.match(registrationHtml, /會顯示在您日後發表的內容旁/)
})

// Flask 管理員的共用導覽同時提供作者與四個管理入口。
test('Flask admin navigation offers author and management links', async (t) => {
  const flask = app('/vue/', '', 'flask-token', auth.BACKENDS.flask)
  t.after(flask.close)
  await respond(flask.requests[0], 200, success(administrator))
  assert.equal(flask.requests[1].url, `${auth.BACKENDS.flask}/api/articles?page=1&per_page=20`)
  const html = await rendered(flask.page)
  for (const href of ['/vue/articles/new', '/vue/me/articles', '/vue/me/comments']) assert.match(html, new RegExp(`href="${href}"`))
  for (const href of ['/vue/admin/members', '/vue/admin/articles', '/vue/admin/comments', '/vue/admin/settings/login-lockout']) assert.match(html, new RegExp(`href="${href}"`))
})

// Flask 管理頁先驗目前角色，再按路由讀共用 API；會員及管理員 ID 不得露出會員資料。
test('Flask management routes enforce role and admin target boundaries', async (t) => {
  const routes = new Map([
    ['/vue/admin/members', '/api/members?page=1&per_page=20'],
    ['/vue/admin/members/2', '/api/members/2'],
    ['/vue/admin/articles', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'],
    ['/vue/admin/comments', '/api/comments?page=1&per_page=20'],
    ['/vue/admin/settings/login-lockout', '/api/settings/login-lockout'],
  ])
  for (const [path, endpoint] of routes) {
    const guest = app(path, '', null, auth.BACKENDS.flask)
    t.after(guest.close)
    assert.deepEqual(guest.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    const adminPage = app(path, '', 'flask-token', auth.BACKENDS.flask)
    t.after(adminPage.close)
    assert.equal(adminPage.requests[0].url, `${auth.BACKENDS.flask}/api/me`, path)
    await respond(adminPage.requests[0], 200, success(administrator))
    assert.equal(adminPage.requests[1].url, `${auth.BACKENDS.flask}${endpoint}`, path)
  }
  const denied = app('/vue/admin/members', '', 'flask-token', auth.BACKENDS.flask)
  t.after(denied.close)
  await respond(denied.requests[0], 200, success(member))
  assert.equal(denied.requests.length, 1)
  assert.match(await rendered(denied.page), /此頁僅供管理員使用/)
  const forbidden = app('/vue/admin/members/2', '', 'flask-token', auth.BACKENDS.flask)
  t.after(forbidden.close)
  await respond(forbidden.requests[0], 200, success(administrator))
  await respond(forbidden.requests[1], 403, { success: false, error_code: 'E0003' })
  assert.match(await rendered(forbidden.page), /沒有權限使用此功能/)
  assert.doesNotMatch(await rendered(forbidden.page), /停用會員|恢復會員/)
  const hiddenAdmin = app('/vue/admin/members/1', '', 'flask-token', auth.BACKENDS.flask)
  t.after(hiddenAdmin.close)
  await respond(hiddenAdmin.requests[0], 200, success(administrator))
  await respond(hiddenAdmin.requests[1], 404, { success: false, error_code: 'E0004' })
  const html = await rendered(hiddenAdmin.page)
  assert.match(html, /找不到指定資料或 API/)
  assert.doesNotMatch(html, /停用會員|恢復會員/)
})

// Flask 設定及停用使用現有讀回流程，顯示伺服器提交後的值。
test('Flask lockout and member status show confirmed readback', async (t) => {
  const settings = app('/vue/admin/settings/login-lockout', '', 'flask-token', auth.BACKENDS.flask)
  t.after(settings.close)
  await respond(settings.requests[0], 200, success(administrator))
  await respond(settings.requests[1], 200, success(lockout))
  settings.page.lockoutDraft.value.failure_threshold = 8
  const save = settings.page.submitLockoutSettings()
  assert.equal(settings.requests[2].url, `${auth.BACKENDS.flask}/api/settings/login-lockout`)
  assert.equal(settings.requests[2].method, 'PUT')
  assert.deepEqual(JSON.parse(settings.requests[2].body), { ...lockout, failure_threshold: 8 })
  await respond(settings.requests[2], 200, success({}))
  await respond(settings.requests[3], 200, success({ ...lockout, failure_threshold: 8 }))
  await save
  assert.match(await rendered(settings.page), /value="8"/)

  const detail = app('/vue/admin/members/2', '', 'flask-token', auth.BACKENDS.flask)
  t.after(detail.close)
  await respond(detail.requests[0], 200, success(administrator))
  await respond(detail.requests[1], 200, success(member))
  const disable = detail.page.submitMemberStatus()
  assert.equal(detail.requests[2].url, `${auth.BACKENDS.flask}/api/members/2/status`)
  assert.deepEqual(JSON.parse(detail.requests[2].body), { status: 'disabled' })
  await respond(detail.requests[2], 200, success({}))
  await respond(detail.requests[3], 200, success({ ...member, status: 'disabled' }))
  await disable
  assert.match(await rendered(detail.page), /已重讀會員，目前狀態為停用/)
})

// Echo 五種管理頁沿用共用驗權及固定端點；未驗權不得讀取資料。
test('Echo management routes require verified admin and show all management entries', async (t) => {
  const routes = new Map([
    ['/vue/admin/members', ['/api/members?page=1&per_page=20', list([member]), member.email]],
    ['/vue/admin/members/2', ['/api/members/2', success(member), member.email]],
    ['/vue/admin/articles', ['/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', list([{ id: 7, title: '管理文章', body: '內容', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'echo', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }]), '管理文章']],
    ['/vue/admin/comments', ['/api/comments?page=1&per_page=20', list([{ id: 9, article_id: 7, body: '管理留言', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'echo', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }]), '管理留言']],
    ['/vue/admin/settings/login-lockout', ['/api/settings/login-lockout', success(lockout), '目前伺服器值：啟用']],
  ])
  // 每個深連結先測訪客返回，再以目前 Echo 管理員身分確認讀取與畫面。
  for (const [path, [endpoint, response, visibleText]] of routes) {
    const guest = app(path, '', null, auth.BACKENDS.echo)
    t.after(guest.close)
    assert.deepEqual(guest.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    assert.equal(guest.requests.length, 0, path)
    const instance = app(path, '', 'echo-token', auth.BACKENDS.echo)
    t.after(instance.close)
    assert.equal(instance.requests[0].url, `${auth.BACKENDS.echo}/api/me`, path)
    assert.equal(instance.requests.length, 1, path)
    await respond(instance.requests[0], 200, success(administrator))
    assert.equal(instance.requests[1].url, `${auth.BACKENDS.echo}${endpoint}`, path)
    await respond(instance.requests[1], 200, response)
    assert.match(await rendered(instance.page), new RegExp(visibleText), path)
  }
  const denied = app('/vue/admin/members', '', 'member-token', auth.BACKENDS.echo)
  t.after(denied.close)
  await respond(denied.requests[0], 200, success(member))
  assert.equal(denied.requests.length, 1)
  assert.match(await rendered(denied.page), /此頁僅供管理員使用/)
  assert.doesNotMatch(await rendered(denied.page), /href="\/vue\/admin\//)

  const hiddenAdmin = app('/vue/admin/members/1', '', 'echo-token', auth.BACKENDS.echo)
  t.after(hiddenAdmin.close)
  await respond(hiddenAdmin.requests[0], 200, success(administrator))
  await respond(hiddenAdmin.requests[1], 404, { success: false, error_code: 'E0004' })
  const missing = await rendered(hiddenAdmin.page)
  assert.match(missing, /找不到指定資料或 API/)
  assert.doesNotMatch(missing, /停用會員|恢復會員/)

  const home = app('/vue/', '', 'echo-token', auth.BACKENDS.echo)
  t.after(home.close)
  await respond(home.requests[0], 200, success(administrator))
  const navigation = await rendered(home.page)
  for (const href of routes.keys()) if (href !== '/vue/admin/members/2') assert.match(navigation, new RegExp(`href="${href}"`))
})

// Fiber 管理深連結保留登入目標；驗證目前角色後才讀五種管理資料並顯示入口。
test('Fiber management routes require verified admin and show management navigation', async (t) => {
  const routes = new Map([
    ['/vue/admin/members', '/api/members?page=1&per_page=20'],
    ['/vue/admin/members/2', '/api/members/2'],
    ['/vue/admin/articles', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'],
    ['/vue/admin/comments', '/api/comments?page=1&per_page=20'],
    ['/vue/admin/settings/login-lockout', '/api/settings/login-lockout'],
  ])
  for (const [path, endpoint] of routes) {
    const guest = app(path, '', null, auth.BACKENDS.fiber)
    t.after(guest.close)
    assert.deepEqual(guest.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    assert.equal(guest.requests.length, 0, path)
    const signedIn = app(path, '', 'fiber-token', auth.BACKENDS.fiber)
    t.after(signedIn.close)
    assert.equal(signedIn.requests[0].url, `${auth.BACKENDS.fiber}/api/me`, path)
    assert.equal(signedIn.requests.length, 1, path)
    await respond(signedIn.requests[0], 200, success(administrator))
    assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.fiber}${endpoint}`, path)
    assert.equal(signedIn.requests[1].headers.Authorization, 'Bearer fiber-token', path)
  }
  const denied = app('/vue/admin/members', '', 'member-token', auth.BACKENDS.fiber)
  t.after(denied.close)
  await respond(denied.requests[0], 200, success(member))
  assert.equal(denied.requests.length, 1)
  const deniedHtml = await rendered(denied.page)
  assert.match(deniedHtml, /沒有權限使用此功能/)
  assert.doesNotMatch(deniedHtml, /href="\/vue\/admin\//)

  const menu = app('/vue/', '', 'fiber-token', auth.BACKENDS.fiber)
  t.after(menu.close)
  await respond(menu.requests[0], 200, success(administrator))
  const menuHtml = await rendered(menu.page)
  for (const href of routes.keys()) if (href !== '/vue/admin/members/2') assert.match(menuHtml, new RegExp(`href="${href}"`))
})

// Gin 管理頁沿用共用驗權與端點；訪客返回原頁，會員不得先讀取管理資料。
test('Gin management routes require verified admin and expose all management links', async (t) => {
  const content = { id: 7, body: '管理留言內容', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'gin', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }
  const routes = new Map([
    ['/vue/admin/members', ['/api/members?page=1&per_page=20', list([member]), member.email]],
    ['/vue/admin/members/2', ['/api/members/2', success(member), member.email]],
    ['/vue/admin/articles', ['/api/articles?page=1&per_page=20&filters%5Bscope%5D=management', list([{ ...content, title: '管理文章標題' }]), '管理文章標題']],
    ['/vue/admin/comments', ['/api/comments?page=1&per_page=20', list([{ ...content, article_id: 7 }]), '管理留言內容']],
    ['/vue/admin/settings/login-lockout', ['/api/settings/login-lockout', success(lockout), '目前伺服器值：啟用']],
  ])
  for (const [path, [endpoint, response, visibleText]] of routes) {
    const guest = app(path, '', null, auth.BACKENDS.gin)
    t.after(guest.close)
    assert.deepEqual(guest.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    assert.equal(guest.requests.length, 0, path)
    const instance = app(path, '', 'gin-token', auth.BACKENDS.gin)
    t.after(instance.close)
    assert.equal(instance.requests[0].url, `${auth.BACKENDS.gin}/api/me`, path)
    assert.equal(instance.requests.length, 1, path)
    await respond(instance.requests[0], 200, success(administrator))
    assert.equal(instance.requests[1].url, `${auth.BACKENDS.gin}${endpoint}`, path)
    await respond(instance.requests[1], 200, response)
    const html = await rendered(instance.page)
    assert.match(html, new RegExp(visibleText), path)
    assert.doesNotMatch(html, /Gin 的管理頁尚未提供/)
  }
  const denied = app('/vue/admin/members', '', 'gin-token', auth.BACKENDS.gin)
  t.after(denied.close)
  await respond(denied.requests[0], 200, success(member))
  assert.equal(denied.requests.length, 1)
  assert.doesNotMatch(await rendered(denied.page), /href="\/vue\/admin\/members"/)
  const home = app('/vue/', '', 'gin-token', auth.BACKENDS.gin)
  t.after(home.close)
  await respond(home.requests[0], 200, success(administrator))
  const html = await rendered(home.page)
  for (const href of routes.keys()) if (href !== '/vue/admin/members/2') assert.match(html, new RegExp(`href="${href}"`))
})

// Gin 的管理狀態與完整鎖定設定寫入後均讀回伺服器值，不用本機草稿冒充成功。
test('Gin member status and lockout settings use confirmed readback', async (t) => {
  const detail = app('/vue/admin/members/2', '', 'gin-token', auth.BACKENDS.gin)
  t.after(detail.close)
  await respond(detail.requests[0], 200, success(administrator))
  await respond(detail.requests[1], 200, success(member))
  const disable = detail.page.submitMemberStatus()
  assert.equal(detail.requests[2].url, `${auth.BACKENDS.gin}/api/members/2/status`)
  assert.deepEqual(JSON.parse(detail.requests[2].body), { status: 'disabled' })
  await respond(detail.requests[2], 200, success({}))
  await respond(detail.requests[3], 200, success({ ...member, status: 'disabled' }))
  await disable
  assert.match(await rendered(detail.page), /已重讀會員，目前狀態為停用/)

  const settings = app('/vue/admin/settings/login-lockout', '', 'gin-token', auth.BACKENDS.gin)
  t.after(settings.close)
  await respond(settings.requests[0], 200, success(administrator))
  await respond(settings.requests[1], 200, success(lockout))
  settings.page.lockoutDraft.value.failure_threshold = 8
  const save = settings.page.submitLockoutSettings()
  assert.equal(settings.requests[2].url, `${auth.BACKENDS.gin}/api/settings/login-lockout`)
  assert.equal(settings.requests[2].method, 'PUT')
  assert.deepEqual(JSON.parse(settings.requests[2].body), { ...lockout, failure_threshold: 8 })
  await respond(settings.requests[2], 200, success({}))
  await respond(settings.requests[3], 200, success({ ...lockout, failure_threshold: 9 }))
  await save
  assert.match(await rendered(settings.page), /目前伺服器值：啟用；5 分鐘內失敗 9 次/)
})

// Django 管理端點啟用後，管理員導覽需保留作者與全部管理入口。
test('Django admin navigation shows author and management links', async (t) => {
  const django = app('/vue/', '', 'django-token', auth.BACKENDS.django)
  t.after(django.close)
  await respond(django.requests[0], 200, success(administrator))
  assert.equal(django.requests[1].url, `${auth.BACKENDS.django}/api/articles?page=1&per_page=20`)
  const html = await rendered(django.page)
  assert.match(html, /href="\/vue\/articles\/new"/)
  assert.match(html, /href="\/vue\/me\/articles"/)
  assert.match(html, /href="\/vue\/me\/comments"/)
  for (const href of ['/vue/admin/members', '/vue/admin/articles', '/vue/admin/comments', '/vue/admin/settings/login-lockout']) {
    assert.match(html, new RegExp(`href="${href}"`))
  }
  assert.doesNotMatch(html, /Django 目前尚未支援管理功能/)
})

// 訪客仍須登入，非管理員重驗後不得送出管理讀取，也不能看見管理導覽。
test('Django management routes require current admin role', async (t) => {
  const guest = app('/vue/admin/members/2', '', null, auth.BACKENDS.django)
  t.after(guest.close)
  assert.deepEqual(guest.redirects, ['/vue/login?return_to=%2Fvue%2Fadmin%2Fmembers%2F2'])

  const denied = app('/vue/admin/members', '', 'member-token', auth.BACKENDS.django)
  t.after(denied.close)
  await respond(denied.requests[0], 200, success({ ...member }))
  assert.equal(denied.requests.length, 1)
  const html = await rendered(denied.page)
  assert.match(html, /沒有權限使用此功能/)
  assert.doesNotMatch(html, /href="\/vue\/admin\/members"|member@example.test/)

  const forbidden = app('/vue/admin/members/2', '', 'admin-token', auth.BACKENDS.django)
  t.after(forbidden.close)
  await respond(forbidden.requests[0], 200, success(administrator))
  await respond(forbidden.requests[1], 403, { success: false, error_code: 'E0003' })
  assert.match(await rendered(forbidden.page), /沒有權限使用此功能/)
  assert.doesNotMatch(await rendered(forbidden.page), /停用會員|恢復會員/)

  const hiddenAdmin = app('/vue/admin/members/1', '', 'admin-token', auth.BACKENDS.django)
  t.after(hiddenAdmin.close)
  await respond(hiddenAdmin.requests[0], 200, success(administrator))
  await respond(hiddenAdmin.requests[1], 404, { success: false, error_code: 'E0004' })
  assert.match(await rendered(hiddenAdmin.page), /找不到指定資料或 API/)
  assert.doesNotMatch(await rendered(hiddenAdmin.page), /停用會員|恢復會員/)
})

// Django 會員清單、詳情與停用都使用共同 API，寫入後必須重讀目前會員。
test('Django member management reads list and detail then confirms status', async (t) => {
  const members = app('/vue/admin/members', '?keyword=%25&page=2', 'admin-token', auth.BACKENDS.django)
  t.after(members.close)
  await respond(members.requests[0], 200, success(administrator))
  assert.equal(members.requests[1].url, `${auth.BACKENDS.django}/api/members?page=2&per_page=20&filters%5Bkeyword%5D=%25`)
  await respond(members.requests[1], 200, list([member], 2, 21))
  assert.match(await rendered(members.page), /member@example.test/)

  const detail = app('/vue/admin/members/2', '', 'admin-token', auth.BACKENDS.django)
  t.after(detail.close)
  await respond(detail.requests[0], 200, success(administrator))
  assert.equal(detail.requests[1].url, `${auth.BACKENDS.django}/api/members/2`)
  await respond(detail.requests[1], 200, success(member))
  assert.match(await rendered(detail.page), /停用會員/)
  const write = detail.page.submitMemberStatus()
  assert.equal(detail.requests[2].url, `${auth.BACKENDS.django}/api/members/2/status`)
  assert.deepEqual(JSON.parse(detail.requests[2].body), { status: 'disabled' })
  await respond(detail.requests[2], 200, success({}))
  assert.equal(detail.requests[3].url, `${auth.BACKENDS.django}/api/members/2`)
  await respond(detail.requests[3], 200, success({ ...member, status: 'disabled' }))
  await write
  assert.equal(detail.page.memberData.value.status, 'disabled')
  assert.match(await rendered(detail.page), /已重讀會員，目前狀態為停用/)
})

// Django 管理內容列表要帶明確範圍，詳情頁也提供返回原管理入口。
test('Django managed content lists and detail return links', async (t) => {
  const content = { id: 7, title: '下架文章', body: '保留的內容', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'django', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }
  const articleList = app('/vue/admin/articles', '', 'admin-token', auth.BACKENDS.django)
  t.after(articleList.close)
  await respond(articleList.requests[0], 200, success(administrator))
  assert.equal(articleList.requests[1].url, `${auth.BACKENDS.django}/api/articles?page=1&per_page=20&filters%5Bscope%5D=management`)
  await respond(articleList.requests[1], 200, list([content]))
  assert.match(await rendered(articleList.page), /href="\/vue\/articles\/7"/)

  const commentsList = app('/vue/admin/comments', '', 'admin-token', auth.BACKENDS.django)
  t.after(commentsList.close)
  await respond(commentsList.requests[0], 200, success(administrator))
  assert.equal(commentsList.requests[1].url, `${auth.BACKENDS.django}/api/comments?page=1&per_page=20`)
  await respond(commentsList.requests[1], 200, list([{ ...content, id: 9, article_id: 7 }]))
  assert.match(await rendered(commentsList.page), /href="\/vue\/comments\/9"/)

  const articleDetail = app('/vue/articles/7', '', 'admin-token', auth.BACKENDS.django)
  t.after(articleDetail.close)
  await respond(articleDetail.requests[0], 200, success(administrator))
  await respond(articleDetail.requests[1], 200, success(content))
  assert.match(await rendered(articleDetail.page), /href="\/vue\/admin\/articles"[^>]*>返回文章管理<\/a>/)

  const commentDetail = app('/vue/comments/9', '', 'admin-token', auth.BACKENDS.django)
  t.after(commentDetail.close)
  await respond(commentDetail.requests[0], 200, success(administrator))
  await respond(commentDetail.requests[1], 200, success({ ...content, id: 9, article_id: 7 }))
  assert.match(await rendered(commentDetail.page), /href="\/vue\/admin\/comments"[^>]*>返回留言管理<\/a>/)
})

// Django 設定只供管理員操作，完整 PUT 後以同後端 GET 的值更新畫面。
test('Django lockout settings authorize and read back full update', async (t) => {
  const denied = app('/vue/admin/settings/login-lockout', '', 'member-token', auth.BACKENDS.django)
  t.after(denied.close)
  await respond(denied.requests[0], 200, success(member))
  assert.equal(denied.requests.length, 1)
  assert.doesNotMatch(await rendered(denied.page), /儲存完整設定/)

  const settings = app('/vue/admin/settings/login-lockout', '', 'admin-token', auth.BACKENDS.django)
  t.after(settings.close)
  await respond(settings.requests[0], 200, success(administrator))
  assert.equal(settings.requests[1].url, `${auth.BACKENDS.django}/api/settings/login-lockout`)
  await respond(settings.requests[1], 200, success(lockout))
  assert.match(await rendered(settings.page), /儲存完整設定/)
  settings.page.lockoutDraft.value.window_minutes = 10
  const write = settings.page.submitLockoutSettings()
  assert.equal(settings.requests[2].url, `${auth.BACKENDS.django}/api/settings/login-lockout`)
  assert.deepEqual(JSON.parse(settings.requests[2].body), { ...lockout, window_minutes: 10 })
  await respond(settings.requests[2], 200, success({}))
  assert.equal(settings.requests[3].url, `${auth.BACKENDS.django}/api/settings/login-lockout`)
  await respond(settings.requests[3], 200, success({ ...lockout, window_minutes: 10 }))
  await write
  assert.match(await rendered(settings.page), /已重新讀取並顯示目前伺服器設定/)
})

// FastAPI 管理深連結先驗登入與角色，五個入口才可讀共同 API。
test('FastAPI management routes require verified admin and expose all entries', async (t) => {
  const routes = new Map([
    ['/vue/admin/members', '/api/members?page=1&per_page=20'],
    ['/vue/admin/members/2', '/api/members/2'],
    ['/vue/admin/articles', '/api/articles?page=1&per_page=20&filters%5Bscope%5D=management'],
    ['/vue/admin/comments', '/api/comments?page=1&per_page=20'],
    ['/vue/admin/settings/login-lockout', '/api/settings/login-lockout'],
  ])
  for (const [path, endpoint] of routes) {
    const guest = app(path, '', null, auth.BACKENDS.fastapi)
    t.after(guest.close)
    assert.deepEqual(guest.redirects, [`/vue/login?return_to=${encodeURIComponent(path)}`], path)
    assert.equal(guest.requests.length, 0, path)
    const signedIn = app(path, '', 'fastapi-token', auth.BACKENDS.fastapi)
    t.after(signedIn.close)
    assert.equal(signedIn.requests[0].url, `${auth.BACKENDS.fastapi}/api/me`, path)
    assert.equal(signedIn.requests.length, 1, path)
    await respond(signedIn.requests[0], 200, success(administrator))
    assert.equal(signedIn.requests[1].url, `${auth.BACKENDS.fastapi}${endpoint}`, path)
    assert.equal(signedIn.requests[1].headers.Authorization, 'Bearer fastapi-token', path)
  }
  const denied = app('/vue/admin/members', '', 'member-token', auth.BACKENDS.fastapi)
  t.after(denied.close)
  await respond(denied.requests[0], 200, success(member))
  assert.equal(denied.requests.length, 1)
  const deniedHtml = await rendered(denied.page)
  assert.match(deniedHtml, /沒有權限使用此功能/)
  assert.doesNotMatch(deniedHtml, /href="\/vue\/admin\/|member@example.test/)

  const hiddenAdmin = app('/vue/admin/members/1', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(hiddenAdmin.close)
  await respond(hiddenAdmin.requests[0], 200, success(administrator))
  await respond(hiddenAdmin.requests[1], 404, { success: false, error_code: 'E0004' })
  assert.match(await rendered(hiddenAdmin.page), /找不到指定資料或 API/)
  assert.doesNotMatch(await rendered(hiddenAdmin.page), /停用會員|恢復會員/)

  const menu = app('/vue/', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(menu.close)
  await respond(menu.requests[0], 200, success(administrator))
  const menuHtml = await rendered(menu.page)
  for (const href of routes.keys()) if (!href.includes('/2')) assert.match(menuHtml, new RegExp(`href="${href}"`))
  assert.doesNotMatch(menuHtml, /此後端尚未提供此頁/)
})

// FastAPI 管理內容共用既有列表與詳情，隱藏項仍須有詳情及返回管理入口。
test('FastAPI management lists and hidden content links', async (t) => {
  const content = { id: 7, title: '下架文章', body: '原文', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'fastapi', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }
  const articlesPage = app('/vue/admin/articles', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(articlesPage.close)
  await respond(articlesPage.requests[0], 200, success(administrator))
  await respond(articlesPage.requests[1], 200, list([content]))
  assert.match(await rendered(articlesPage.page), /href="\/vue\/articles\/7"/)
  const commentsPage = app('/vue/admin/comments', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(commentsPage.close)
  await respond(commentsPage.requests[0], 200, success(administrator))
  await respond(commentsPage.requests[1], 200, list([{ ...content, id: 9, article_id: 7 }]))
  assert.match(await rendered(commentsPage.page), /href="\/vue\/comments\/9"/)
  const article = app('/vue/articles/7', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(article.close)
  await respond(article.requests[0], 200, success(administrator))
  await respond(article.requests[1], 200, success(content))
  assert.match(await rendered(article.page), /href="\/vue\/admin\/articles"[^>]*>返回文章管理<\/a>/)
  const comment = app('/vue/comments/9', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(comment.close)
  await respond(comment.requests[0], 200, success(administrator))
  await respond(comment.requests[1], 200, success({ ...content, id: 9, article_id: 7 }))
  assert.match(await rendered(comment.page), /href="\/vue\/admin\/comments"[^>]*>返回留言管理<\/a>/)
})

// FastAPI 狀態與完整設定寫入後都重讀同後端，畫面只顯示已確認的伺服器值。
test('FastAPI member status and lockout update use same-backend readback', async (t) => {
  const detail = app('/vue/admin/members/2', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(detail.close)
  await respond(detail.requests[0], 200, success(administrator))
  await respond(detail.requests[1], 200, success(member))
  const disable = detail.page.submitMemberStatus()
  assert.equal(detail.requests[2].url, `${auth.BACKENDS.fastapi}/api/members/2/status`)
  assert.equal(detail.requests[2].method, 'PUT')
  assert.deepEqual(JSON.parse(detail.requests[2].body), { status: 'disabled' })
  await respond(detail.requests[2], 200, success({}))
  assert.equal(detail.requests[3].url, `${auth.BACKENDS.fastapi}/api/members/2`)
  assert.equal(detail.page.memberData.value, null, 'PUT 成功後先隱藏舊狀態，等 GET 確認')
  await respond(detail.requests[3], 200, success({ ...member, status: 'disabled' }))
  await disable
  assert.equal(detail.page.memberData.value.status, 'disabled')
  assert.match(await rendered(detail.page), /已重讀會員，目前狀態為停用/)

  const settings = app('/vue/admin/settings/login-lockout', '', 'fastapi-token', auth.BACKENDS.fastapi)
  t.after(settings.close)
  await respond(settings.requests[0], 200, success(administrator))
  await respond(settings.requests[1], 200, success(lockout))
  settings.page.lockoutDraft.value.window_minutes = 10
  const save = settings.page.submitLockoutSettings()
  assert.equal(settings.requests[2].url, `${auth.BACKENDS.fastapi}/api/settings/login-lockout`)
  assert.equal(settings.requests[2].method, 'PUT')
  assert.deepEqual(JSON.parse(settings.requests[2].body), { ...lockout, window_minutes: 10 })
  await respond(settings.requests[2], 200, success({}))
  assert.equal(settings.requests[3].url, `${auth.BACKENDS.fastapi}/api/settings/login-lockout`)
  await respond(settings.requests[3], 200, success({ ...lockout, window_minutes: 10 }))
  await save
  assert.deepEqual(settings.page.lockoutData.value, { ...lockout, window_minutes: 10 })
  assert.match(await rendered(settings.page), /value="10"/)
})

// 使用 Vue 正式模板條件產生 HTML，驗證提示和導覽是否真的出現在畫面。
async function rendered(state) {
  const { descriptor } = parse(readFileSync(new URL('./App.vue', import.meta.url), 'utf8'))
  const code = compile(descriptor.template.content, { mode: 'function', prefixIdentifiers: true }).code
  const render = runInNewContext(`(function () { ${code} })()`, { Vue: vue })
  return renderToString(vue.createSSRApp({
    // 交由 Vue 解包正式 refs，保持實際模板顯示條件。
    setup() { return state }, render,
  }))
}

// 以真實 Response 完成指定請求；Promise settle 後再觀察 reactive state。
async function respond(request, status, body) {
  request.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
  await setImmediate()
}

// 返回目標只接受安全管理路徑，伺服器列表也必須保持原頁碼與會員範圍。
test('admin routes and member envelope keep safe IDs and overpage totals', () => {
  assert.equal(articles.articlePage('/vue/admin/members/2'), 'member')
  assert.equal(articles.articlePage('/vue/admin/members/007'), 'member')
  assert.equal(articles.localPage('/vue/admin/members/007'), true)
  for (const path of ['/vue/admin/members/0', '/vue/admin/members/9007199254740992', '//evil.test']) assert.equal(articles.localPage(path), false)
  assert.equal(articles.localPage('/vue/admin/settings/login-lockout'), true)
  assert.equal(articles.articlePage('/vue/admin/settings/login-lockout'), 'lockout-settings')
  assert.equal(admin.readMember(200, success(member), 2)?.email, member.email)
  assert.equal(admin.readMember(200, success(member), 3), null)
  assert.equal(admin.readMemberList(200, list([], 9, 41), 9)?.pagination.total, 41)
  assert.equal(admin.readMemberList(200, list([administrator]), 1), null)
})

// 設定 envelope 僅接受四欄完整型態與邊界；空字串不可從 number input 混成零。
test('lockout settings contract rejects malformed values and validates form bounds', () => {
  assert.deepEqual(admin.readLockoutSettings(200, success(lockout)), lockout)
  assert.equal(admin.readLockoutSettings(200, success({ ...lockout, lock_seconds: '300' })), null)
  assert.equal(admin.readLockoutSettings(200, success({ ...lockout, extra: 1 })), null)
  assert.equal(admin.readLockoutSettings(200, success({ ...lockout, window_minutes: 61 })), null)
  assert.equal(admin.readLockoutSettings(200, { success: true, message: 'wrong', data: lockout }), null)
  assert.deepEqual(Object.keys(admin.validateLockoutSettings({ ...lockout, failure_threshold: '', lock_seconds: 86401 })), ['failure_threshold', 'lock_seconds'])
})

// 非管理員直達設定頁不得讀取 API 或看到管理導覽；管理員經 /me 驗證後才載入。
test('lockout route is protected and renders the admin form only after authorization', async (t) => {
  const denied = app('/vue/admin/settings/login-lockout')
  t.after(denied.close)
  await respond(denied.requests[0], 200, success({ ...administrator, role: 'member' }))
  assert.equal(denied.requests.length, 1)
  assert.doesNotMatch(await rendered(denied.page), /儲存完整設定/)
  assert.doesNotMatch(await rendered(denied.page), /href="\/vue\/admin\/settings\/login-lockout"/)

  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  assert.match(instance.requests[1].url, /\/api\/settings\/login-lockout$/)
  await respond(instance.requests[1], 200, success(lockout))
  const html = await rendered(instance.page)
  assert.match(html, /累計期間（分鐘）/)
  assert.match(html, /設定預覽/)
  assert.match(html, /href="\/vue\/admin\/settings\/login-lockout"/)
  assert.match(html, /目前伺服器值/)
})

// 422 保留輸入並顯示欄位錯誤；成功只在重新 GET 後更新伺服器顯示值。
test('lockout validation, 422 and success readback preserve the right draft', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.window_minutes = 0
  await instance.page.submitLockoutSettings()
  assert.equal(instance.requests.length, 2)
  assert.match(await rendered(instance.page), /請輸入 1 到 60 的整數/)
  instance.page.lockoutDraft.value.window_minutes = 10
  const invalid = instance.page.submitLockoutSettings()
  assert.deepEqual(JSON.parse(instance.requests[2].body), { ...lockout, window_minutes: 10 })
  await respond(instance.requests[2], 422, { success: false, error_code: 'E0001', errors: { window_minutes: ['後端欄位錯誤'] } })
  await invalid
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 10)
  assert.match(await rendered(instance.page), /後端欄位錯誤/)
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[3], 200, success({}))
  assert.match(instance.requests[4].url, /\/api\/settings\/login-lockout$/)
  await respond(instance.requests[4], 200, success({ ...lockout, window_minutes: 12 }))
  await write
  assert.equal(instance.page.lockoutData.value.window_minutes, 12)
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 12)
  const html = await rendered(instance.page)
  assert.match(html, /已重新讀取並顯示目前伺服器設定/)
  assert.doesNotMatch(html, /後端欄位錯誤|請輸入 1 到 60 的整數/)
  assert.doesNotMatch(html, /id="window_minutes"[^>]*aria-invalid="true"/)
})

// 未知 PUT 不重送；手動重讀僅確認目前值，後續成功也不能證明先前寫入結果。
test('lockout unknown outcome needs readback and remains distinct from later success', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.enabled = false
  const first = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 500, { success: false, error_code: 'E9999' })
  await first
  await instance.page.submitLockoutSettings()
  assert.equal(instance.requests.length, 3)
  assert.equal(instance.page.lockoutDraft.value.enabled, false)
  assert.match(await rendered(instance.page), /請先手動重新讀取目前伺服器設定/)
  const readback = instance.page.loadContent()
  await respond(instance.requests[3], 200, success(lockout))
  await readback
  assert.equal(instance.page.lockoutDraft.value.enabled, false)
  assert.match(await rendered(instance.page), /可能已有其他管理員更新/)
  const second = instance.page.submitLockoutSettings()
  await respond(instance.requests[4], 200, success({}))
  await respond(instance.requests[5], 200, success({ ...lockout, enabled: false }))
  await second
  assert.equal(instance.page.lockoutUnknown.value, true)
  assert.match(await rendered(instance.page), /無法藉此判定先前 PUT 是否成功/)
})

// 待寫入時登出與重讀不應交錯；401 要撤銷已授權畫面並保留手動重新登入入口。
test('lockout pending write blocks logout and 401 removes protected settings', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  const write = instance.page.submitLockoutSettings()
  await instance.page.submitLogout()
  await instance.page.loadContent()
  assert.equal(instance.requests.length, 3)
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(instance.page.lockoutData.value, null)
  assert.match(await rendered(instance.page), /請重新登入後讀取原管理頁/)
})

// 登出先開始時不能送設定 PUT；伺服器取消管理權後不能再顯示可操作表單。
test('lockout write is blocked by logout and 403 removes controls', async (t) => {
  const logoutFirst = app('/vue/admin/settings/login-lockout')
  t.after(logoutFirst.close)
  await respond(logoutFirst.requests[0], 200, success(administrator))
  await respond(logoutFirst.requests[1], 200, success(lockout))
  const logout = logoutFirst.page.submitLogout()
  await logoutFirst.page.submitLockoutSettings()
  assert.equal(logoutFirst.requests.length, 3)
  assert.match(logoutFirst.requests[2].url, /\/api\/auth\/logout$/)
  await respond(logoutFirst.requests[2], 200, success({}))
  await logout

  const forbidden = app('/vue/admin/settings/login-lockout')
  t.after(forbidden.close)
  await respond(forbidden.requests[0], 200, success(administrator))
  await respond(forbidden.requests[1], 403, { success: false, error_code: 'E0003' })
  assert.doesNotMatch(await rendered(forbidden.page), /儲存完整設定/)
  assert.match(await rendered(forbidden.page), /沒有權限使用此功能/)
})

// 200 非法 envelope 屬寫入結果不明；換帳號驗證後不能帶入前一人的草稿。
test('malformed write and account switch keep uncertainty isolated', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.failure_threshold = 8
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 200, success({ saved: true }))
  await write
  assert.equal(instance.page.lockoutNeedsReadback.value, true)
  assert.equal(instance.page.lockoutDraft.value.failure_threshold, 8)
  const verify = instance.page.verifyAccount()
  await respond(instance.requests[3], 200, success({ ...administrator, id: 9 }))
  await verify
  await respond(instance.requests[4], 200, success(lockout))
  assert.equal(instance.page.lockoutDraft.value.failure_threshold, 5)
  assert.equal(instance.page.lockoutUnknown.value, false)
})

// 設定 PUT 401 後換另一位管理員登入，必須在新 GET 前清掉前一人的草稿與查證狀態。
test('lockout 401 then different admin login cannot reuse prior draft', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.window_minutes = 9
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 9)
  instance.page.lockoutUnknown.value = true
  instance.page.lockoutNeedsReadback.value = true
  instance.page.lockoutNotice.value = '先前結果不明'
  instance.page.email.value = 'admin2@example.test'
  instance.page.password.value = 'abc123'
  await vue.nextTick()
  const login = instance.page.submitLogin()
  assert.match(instance.requests[3].url, /\/api\/auth\/login$/)
  await respond(instance.requests[3], 200, success({ access_token: 'other-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: { ...administrator, id: 9, email: 'admin2@example.test' } }))
  await login
  assert.equal(instance.page.account.value?.id, 9, instance.page.summary.value)
  await respond(instance.requests[4], 200, success(lockout))
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 5)
  assert.equal(instance.page.lockoutUnknown.value, false)
  assert.equal(instance.page.lockoutNeedsReadback.value, false)
  assert.equal(instance.page.lockoutNotice.value, '')
  assert.doesNotMatch(await rendered(instance.page), /先前結果不明/)
})

// 同一管理員重新登入仍保留記憶體草稿，GET 只供比較，不替使用者覆蓋未提交內容。
test('lockout 401 then same admin login retains draft for manual save', async (t) => {
  const instance = app('/vue/admin/settings/login-lockout')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(lockout))
  instance.page.lockoutDraft.value.window_minutes = 9
  const write = instance.page.submitLockoutSettings()
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  instance.page.lockoutUnknown.value = true
  instance.page.lockoutNeedsReadback.value = true
  instance.page.email.value = administrator.email
  instance.page.password.value = 'abc123'
  await vue.nextTick()
  const login = instance.page.submitLogin()
  assert.match(instance.requests[3].url, /\/api\/auth\/login$/)
  await respond(instance.requests[3], 200, success({ access_token: 'same-token', token_type: 'Bearer', expires_at: '2026-09-24 18:00:00', user: administrator }))
  await login
  assert.equal(instance.page.account.value?.id, administrator.id, instance.page.summary.value)
  await respond(instance.requests[4], 200, success(lockout))
  assert.equal(instance.page.lockoutData.value.window_minutes, 5)
  assert.equal(instance.page.lockoutDraft.value.window_minutes, 9)
  assert.equal(instance.page.lockoutUnknown.value, true)
  assert.equal(instance.page.lockoutNeedsReadback.value, false)
  assert.match(await rendered(instance.page), /啟用：9 分鐘內失敗/)
})

// 一般會員即使直達管理網址，也不能觸發管理 API 或看到管理導覽。
test('nonadmin direct route renders denial without loading management data', async (t) => {
  const instance = app('/vue/admin/members')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator.role === 'admin' ? { ...administrator, role: 'member' } : administrator))
  assert.equal(instance.requests.length, 1)
  const html = await rendered(instance.page)
  assert.match(html, /沒有權限使用此功能/)
  assert.doesNotMatch(html, /href="\/vue\/admin\/members"/)
  assert.doesNotMatch(html, /搜尋會員暱稱或 Email/)
})

// 搜尋保留百分號等字面值，合法超頁分辨原總數並提供回第一頁。
test('admin search sends literal keyword and renders overpage recovery', async (t) => {
  const instance = app('/vue/admin/members', '?keyword=%25&page=9')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  assert.match(instance.requests[1].url, /\/api\/members\?page=9&per_page=20&filters%5Bkeyword%5D=%25$/)
  await respond(instance.requests[1], 200, list([], 9, 41))
  const html = await rendered(instance.page)
  assert.match(html, /此頁沒有會員，總數仍保留/)
  assert.match(html, /回第一頁/)
  instance.page.keyword.value = ' A%_ '
  instance.page.submitMemberSearch()
  assert.deepEqual(instance.redirects, ['/vue/admin/members?keyword=A%25_'])
  const astral = '😀'.repeat(254)
  assert.doesNotMatch(html, /id="member-keyword"[^>]*maxlength=/)
  instance.page.keyword.value = astral
  instance.page.submitMemberSearch()
  assert.equal(instance.redirects.at(-1), `/vue/admin/members?keyword=${encodeURIComponent(astral)}`)
  instance.page.keyword.value = `${astral}😀`
  instance.page.submitMemberSearch()
  assert.equal(instance.redirects.length, 2, '255 Unicode code points must be rejected')
})

// 伺服器與本機 422 均要在搜尋欄旁顯示錯誤；未知欄位仍留在可聚焦摘要。
test('member search renders field and unmatched validation errors with focus target', async (t) => {
  const instance = app('/vue/admin/members', '?keyword=invalid')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 422, { success: false, error_code: 'E0001', errors: { 'filters.keyword': ['關鍵字格式不正確'], unexpected: ['其他參數不正確'] } })
  let html = await rendered(instance.page)
  assert.match(html, /搜尋關鍵字：關鍵字格式不正確/)
  assert.match(html, /id="member-keyword"[^>]*aria-invalid="true"[^>]*aria-describedby="member-keyword-error member-keyword-help"/)
  assert.match(html, /id="member-keyword-error"[^>]*>關鍵字格式不正確<\/p>/)
  assert.match(html, /其他參數不正確/)
  instance.page.focusField('filters.keyword')
  assert.deepEqual(instance.focuses, ['member-keyword'])

  instance.page.keyword.value = '😀'.repeat(255)
  instance.page.submitMemberSearch()
  await vue.nextTick()
  html = await rendered(instance.page)
  assert.match(html, /搜尋關鍵字：搜尋關鍵字最多 254 個字/)
  assert.match(html, /id="member-keyword-error"[^>]*>搜尋關鍵字最多 254 個字/)
  assert.equal(instance.redirects.length, 0)
})

// 直接開啟管理頁先保留安全返回路徑；驗證角色降為會員時，舊管理清單不得重現。
test('direct login return and role revalidation hide stale management data', async (t) => {
  const guest = app('/vue/admin/members/2', '', null)
  t.after(guest.close)
  assert.deepEqual(guest.redirects, ['/vue/login?return_to=%2Fvue%2Fadmin%2Fmembers%2F2'])

  const instance = app('/vue/admin/members')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  const stale = instance.requests[1]
  const verification = instance.page.verifyAccount()
  assert.equal(stale.signal.aborted, true)
  await respond(instance.requests[2], 200, success({ ...administrator, role: 'member' }))
  await verification
  await respond(stale, 200, list([member]))
  assert.equal(instance.page.memberList.value, null)
  const html = await rendered(instance.page)
  assert.match(html, /沒有權限使用此功能/)
  assert.doesNotMatch(html, /member@example.test/)
})

// 管理文章必須明確要求 management 範圍；管理留言使用專屬列表並沿用詳情頁。
test('managed content reads approved endpoints and renders hidden detail links', async (t) => {
  const content = { id: 7, body: '保留的內容', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'laravel', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }
  const articlePage = app('/vue/admin/articles')
  t.after(articlePage.close)
  await respond(articlePage.requests[0], 200, success(administrator))
  assert.match(articlePage.requests[1].url, /\/api\/articles\?page=1&per_page=20&filters%5Bscope%5D=management$/)
  await respond(articlePage.requests[1], 200, list([{ ...content, title: '下架文章' }]))
  let html = await rendered(articlePage.page)
  assert.match(html, /href="\/vue\/articles\/7"/)
  assert.match(html, /已下架/)

  const commentPage = app('/vue/admin/comments')
  t.after(commentPage.close)
  await respond(commentPage.requests[0], 200, success(administrator))
  assert.match(commentPage.requests[1].url, /\/api\/comments\?page=1&per_page=20$/)
  await respond(commentPage.requests[1], 200, list([{ ...content, id: 9, article_id: 7 }]))
  html = await rendered(commentPage.page)
  assert.match(html, /href="\/vue\/comments\/9"/)
  assert.match(html, /已下架/)
})

// 管理清單共用既有詳情；詳情仍須提供返回管理列表的落點，並保留原入口。
test('admin detail pages retain links back to their management lists', async (t) => {
  const content = { id: 7, body: '內容', author: { id: 2, nickname: '阿明' }, version: 1, created_backend: 'laravel', created_at: '2026-09-24 09:00:00', updated_at: '2026-09-24 09:00:00', visibility: 'hidden', can_restore: true }
  const articleDetail = app('/vue/articles/7')
  t.after(articleDetail.close)
  await respond(articleDetail.requests[0], 200, success(administrator))
  await respond(articleDetail.requests[1], 200, success({ ...content, title: '下架文章' }))
  let html = await rendered(articleDetail.page)
  assert.match(html, /<p class="detail-return-links">/)
  assert.match(html, /href="\/vue\/admin\/articles"[^>]*>返回文章管理<\/a>/)
  assert.match(html, /href="\/vue\/"[^>]*>返回文章列表<\/a>/)

  const commentDetail = app('/vue/comments/9')
  t.after(commentDetail.close)
  await respond(commentDetail.requests[0], 200, success(administrator))
  await respond(commentDetail.requests[1], 200, success({ ...content, id: 9, article_id: 7 }))
  html = await rendered(commentDetail.page)
  assert.match(html, /<p class="detail-return-links">/)
  assert.match(html, /href="\/vue\/admin\/comments"[^>]*>返回留言管理<\/a>/)
  assert.match(html, /href="\/vue\/me\/comments"[^>]*>返回我的留言<\/a>/)
})

// 結果不明不自動重送；讀回同一 ID 後仍明示先前操作結果未知，後續成功再依伺服器值顯示。
test('status unknown requires readback and preserves uncertainty after later success', async (t) => {
  const instance = app('/vue/admin/members/2')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(member))
  let html = await rendered(instance.page)
  assert.match(html, /停用會員/)
  const first = instance.page.submitMemberStatus()
  assert.equal(instance.requests[2].method, 'PUT')
  assert.equal(JSON.parse(instance.requests[2].body).status, 'disabled')
  await respond(instance.requests[2], 500, { success: false, error_code: 'E9999' })
  await first
  html = await rendered(instance.page)
  assert.match(html, /先前送出的操作是否完成仍無法判定/)
  assert.match(html, /重新讀取同一會員/)
  await instance.page.submitMemberStatus()
  assert.equal(instance.requests.length, 3, 'unknown write cannot be resent before readback')
  const readback = instance.page.loadContent()
  await respond(instance.requests[3], 200, success({ ...member, status: 'disabled' }))
  await readback
  html = await rendered(instance.page)
  assert.match(html, /先前送出的操作是否完成仍無法判定/)
  assert.match(html, /目前伺服器狀態為停用/)
  const second = instance.page.submitMemberStatus()
  await respond(instance.requests[4], 200, success({}))
  await respond(instance.requests[5], 200, success(member))
  await second
  html = await rendered(instance.page)
  assert.match(html, /目前伺服器狀態為啟用/)
  assert.match(html, /先前送出的操作是否完成仍無法判定/)
})

// 寫入成功但重讀失敗不能顯示過期狀態；404 也不能保留先前可操作會員。
test('status readback failure and not-found hide stale member controls', async (t) => {
  const instance = app('/vue/admin/members/2')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(member))
  const write = instance.page.submitMemberStatus()
  await respond(instance.requests[2], 200, success({}))
  await respond(instance.requests[3], 500, { success: false, error_code: 'E9999' })
  await write
  let html = await rendered(instance.page)
  assert.match(html, /尚未讀到最新會員狀態/)
  assert.doesNotMatch(html, /停用會員/)
  const retry = instance.page.loadContent()
  await respond(instance.requests[4], 404, { success: false, error_code: 'E0004' })
  await retry
  html = await rendered(instance.page)
  assert.match(html, /找不到指定資料或 API/)
  assert.doesNotMatch(html, /停用會員/)
})

// 寫入等待中不能被手動重讀取代；401 撤銷登入時即刻移除受保護資料與等待狀態。
test('pending status blocks read races and 401 removes member data', async (t) => {
  const instance = app('/vue/admin/members/2')
  t.after(instance.close)
  await respond(instance.requests[0], 200, success(administrator))
  await respond(instance.requests[1], 200, success(member))
  const write = instance.page.submitMemberStatus()
  await instance.page.loadContent()
  assert.equal(instance.requests.length, 3)
  assert.equal(instance.requests[2].signal.aborted, false)
  await respond(instance.requests[2], 401, { success: false, error_code: 'E0002' })
  await write
  assert.equal(instance.page.statusPending.value, false)
  assert.equal(instance.page.memberData.value, null)
  const html = await rendered(instance.page)
  assert.match(html, /請重新登入後讀取原管理頁/)
  assert.doesNotMatch(html, /member@example.test/)
})

// 登出已送出時不得再送會員狀態 PUT；反向順序仍由既有待寫入防線阻擋登出。
test('logout and member status cannot overlap in either order', async (t) => {
  const logoutFirst = app('/vue/admin/members/2')
  t.after(logoutFirst.close)
  await respond(logoutFirst.requests[0], 200, success(administrator))
  await respond(logoutFirst.requests[1], 200, success(member))
  const logout = logoutFirst.page.submitLogout()
  assert.match(logoutFirst.requests[2].url, /\/api\/auth\/logout$/)
  await logoutFirst.page.submitMemberStatus()
  assert.equal(logoutFirst.requests.length, 3)
  let html = await rendered(logoutFirst.page)
  assert.match(html, /<button[^>]*disabled[^>]*>停用會員<\/button>/)
  await respond(logoutFirst.requests[2], 200, success({}))
  await logout
  assert.equal(logoutFirst.page.memberData.value, null)
  assert.equal(logoutFirst.requests.length, 3)

  const statusFirst = app('/vue/admin/members/2')
  t.after(statusFirst.close)
  await respond(statusFirst.requests[0], 200, success(administrator))
  await respond(statusFirst.requests[1], 200, success(member))
  const write = statusFirst.page.submitMemberStatus()
  await statusFirst.page.submitLogout()
  assert.equal(statusFirst.requests.length, 3)
  assert.equal(statusFirst.requests[2].method, 'PUT')
  await respond(statusFirst.requests[2], 200, success({}))
  await respond(statusFirst.requests[3], 200, success({ ...member, status: 'disabled' }))
  await write
  assert.equal(statusFirst.page.memberData.value.status, 'disabled')
})
