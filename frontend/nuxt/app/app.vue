<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { BACKENDS, angularFrontendTarget, loginHref, nextFrontendTarget, reactFrontendTarget, svelteFrontendTarget, validReturn, vueFrontendTarget } from './utils/contracts.js'

const site = useSite()
const route = useRoute()
const logoutPending = ref(false)
const notice = ref('')
const noticeLogId = ref('')
const frontendTarget = ref(null)
const reactTarget = ref(null)
const nextTarget = ref(null)
const angularTarget = ref(null)
const svelteTarget = ref(null)
// 選單與標頭共用固定後端名稱，避免改選後顯示與實際 API 來源不一致。
const backendLabels = { [BACKENDS.laravel]: 'Laravel', [BACKENDS.symfony]: 'Symfony', [BACKENDS.aspnet]: 'ASP.NET Core Controllers', [BACKENDS.django]: 'Django', [BACKENDS.flask]: 'Flask', [BACKENDS.fastapi]: 'FastAPI', [BACKENDS.gin]: 'Gin', [BACKENDS.echo]: 'Echo', [BACKENDS.fiber]: 'Fiber', [BACKENDS.express]: 'Express', [BACKENDS.fastify]: 'Fastify', [BACKENDS.nestjs]: 'NestJS' }

// 掛載後才能讀同一分頁的 sessionStorage，伺服器只傳不含憑證的外殼。
onMounted(
  // 首頁與深層頁都先向目前後端確認既有登入，實際瀏覽器路徑供切換保留 ID。
  () => {
    frontendTarget.value = vueFrontendTarget(window.location.pathname)
    reactTarget.value = reactFrontendTarget(window.location.pathname, route.query.return_to)
    nextTarget.value = nextFrontendTarget(window.location.pathname, route.query.return_to)
    angularTarget.value = angularFrontendTarget(window.location.pathname, route.query.return_to)
    svelteTarget.value = svelteFrontendTarget(window.location.pathname, route.query.return_to)
    window.addEventListener('beforeunload', warnOnLeave)
    window.addEventListener('pageshow', restoreFromCache)
    site.initialize()
  },
)

// 離開應用時移除草稿提示，避免跨頁留住舊頁面的事件。
onBeforeUnmount(
  // 清理本應用持有的瀏覽器監聽。
  () => {
    window.removeEventListener('beforeunload', warnOnLeave)
    window.removeEventListener('pageshow', restoreFromCache)
  },
)

// BFCache 會保留已離頁的 Nuxt 狀態；返回時重載以共用 session 重新驗證。
function restoreFromCache(event) {
  if (event.persisted) window.location.reload()
}

// 一般連結離頁使用瀏覽器原生提示；版本選單另提供明確取消與捨棄。
function warnOnLeave(event) {
  if (!site.dirty.value) return
  event.preventDefault()
  event.returnValue = ''
}

// 只導向目標已完成的同類頁面，原後端、JWT 和到期時間保持原樣。
function chooseFrontend(event) {
  if (event.target.value === 'nuxt') return
  const target = event.target.value === 'vue' ? frontendTarget.value : event.target.value === 'react' ? reactTarget.value : event.target.value === 'next' ? nextTarget.value : event.target.value === 'angular' ? angularTarget.value : event.target.value === 'sveltekit' ? svelteTarget.value : null
  if (!target) { event.target.value = 'nuxt'; return }
  if (logoutPending.value) { event.target.value = 'nuxt'; notice.value = '登出處理中，請等待結果後再切換。'; return }
  // 已送出的管理寫入可能仍在伺服器完成，確認後只撤銷本頁請求與畫面狀態。
  const warning = site.adminPending.value ? '管理操作已送出，伺服器仍可能完成；確定捨棄目前畫面並切換前端嗎？' : '尚有未儲存內容或未確認的操作，確定捨棄並切換前端嗎？'
  if ((site.adminPending.value || site.dirty.value) && !window.confirm(warning)) { event.target.value = 'nuxt'; return }
  // 文件真正離開前同步作廢來源認證與表單／內容請求，舊回應不得再導回 Nuxt。
  site.leaveFrontend()
  if (event.target.value === 'react' || event.target.value === 'next' || event.target.value === 'angular' || event.target.value === 'sveltekit') { window.location.assign(target); return }
  const returnTarget = window.location.pathname === '/nuxt/login' && validReturn(route.query.return_to) ? vueFrontendTarget(route.query.return_to) : null
  const query = returnTarget ? `?return_to=${encodeURIComponent(returnTarget)}` : ''
  window.location.assign(`${target}${query}`)
}

// 後端切換先確認草稿或在途管理寫入，再清除憑證並回到登入。
function chooseBackend(event) {
  const next = event.target.value
  if (next === site.backend.value) return
  // 瀏覽器撤銷請求不保證伺服器撤銷已送出的 PUT，使用者需先確認這層不確定性。
  const warning = site.adminPending.value ? '管理操作已送出，伺服器仍可能完成；確定捨棄目前畫面並切換後端嗎？' : '尚有未儲存內容或未確認的操作，確定捨棄並切換後端嗎？'
  if ((site.adminPending.value || site.dirty.value) && !window.confirm(warning)) { event.target.value = site.backend.value; return }
  if (!site.switchBackend(next)) {
    event.target.value = site.backend.value
    notice.value = '無法保存後端選擇；目前仍使用原後端。'
    return
  }
  // 登入頁可沿用已驗證的返回目標；其他頁只傳已知語意路徑，絕不帶 JWT。
  const path = window.location.pathname
  const target = path === '/nuxt/login' && validReturn(route.query.return_to) ? route.query.return_to : validReturn(path) ? path : '/nuxt/'
  window.location.assign(`/nuxt/login?return_to=${encodeURIComponent(target)}`)
}

// 共用登出控制等候後端撤銷結果，失敗可重試且不誤報完成。
async function submitLogout() {
  if (logoutPending.value || site.adminPending.value) return
  if (site.dirty.value && !window.confirm('尚有未儲存內容或未確認的操作，確定捨棄並登出嗎？')) return
  logoutPending.value = true
  notice.value = ''
  noticeLogId.value = ''
  try {
    const result = await site.logout()
    notice.value = result.ok ? '已登出。' : result.message
    noticeLogId.value = result.logId || ''
  } finally { logoutPending.value = false }
}
</script>

<template>
  <div class="shell">
    <header class="masthead">
      <div class="masthead-top">
        <a class="wordmark"
          href="/nuxt/"
          aria-label="文章集，回文章列表">文章集<span class="wordmark-dot">.</span></a>
        <p class="edition">一份關於文字的日常刊物</p>
        <div class="environment"
          aria-label="目前版本與後端"><span>目前前端 <strong>Nuxt</strong></span><span>目前後端 <strong>{{ backendLabels[site.backend.value] }}</strong></span></div>
      </div>
      <div class="masthead-rule" />
      <nav class="main-nav"
        aria-label="主要導覽">
        <a href="/nuxt/">所有文章</a>
        <span class="nav-spacer" />
        <template v-if="site.account.value">
          <a href="/nuxt/articles/new">發文</a>
          <a href="/nuxt/me/articles">我的文章</a>
          <a href="/nuxt/me/comments">我的留言</a>
          <template v-if="site.account.value.role === 'admin'"><a href="/nuxt/admin/members">會員管理</a><a href="/nuxt/admin/articles">文章管理</a><a href="/nuxt/admin/comments">留言管理</a><a href="/nuxt/admin/settings/login-lockout">鎖定設定</a></template>
          <span class="reader-name">{{ site.account.value.nickname }}</span>
          <button type="button"
            class="text-button"
            :disabled="logoutPending || site.adminPending.value"
            @click="submitLogout">{{ logoutPending ? '登出中…' : '登出' }}</button>
        </template>
        <template v-else><a href="/nuxt/login">登入</a><a href="/nuxt/register">註冊</a></template>
      </nav>
      <details class="version-controls">
        <summary>切換版本</summary>
        <div class="version-fields">
          <div><label for="nuxt-frontend-version">前端版本</label><select id="nuxt-frontend-version"
              value="nuxt"
              @change="chooseFrontend"><option value="nuxt">Nuxt（目前使用）</option><option value="vue"
                :disabled="!frontendTarget">Vue 3＋Vite{{ frontendTarget ? '' : '（此頁不可切換）' }}</option><option value="react"
                :disabled="!reactTarget">React＋Vite{{ reactTarget ? '' : '（此頁不可切換）' }}</option><option value="next"
                :disabled="!nextTarget">Next.js{{ nextTarget ? '' : '（此頁不可切換）' }}</option><option value="angular"
                :disabled="!angularTarget">Angular{{ angularTarget ? '' : '（此頁不可切換）' }}</option><option value="sveltekit"
                :disabled="!svelteTarget">SvelteKit{{ svelteTarget ? '' : '（此頁不可切換）' }}</option></select></div>
          <div><label for="nuxt-backend-version">後端版本</label><select id="nuxt-backend-version"
              :value="site.backend.value"
              @change="chooseBackend"><option v-for="(label, value) in backendLabels"
                :key="value"
                :value="value">{{ label }}</option></select></div>
          <p>切換後端會清除目前登入並要求重新登入。</p>
        </div>
      </details>
      <details class="mobile-nav">
        <summary>導覽選單</summary>
        <nav aria-label="手機導覽">
          <a href="/nuxt/">所有文章</a>
          <template v-if="site.account.value">
            <a href="/nuxt/articles/new">發文</a>
            <a href="/nuxt/me/articles">我的文章</a>
            <a href="/nuxt/me/comments">我的留言</a>
            <template v-if="site.account.value.role === 'admin'"><a href="/nuxt/admin/members">會員管理</a><a href="/nuxt/admin/articles">文章管理</a><a href="/nuxt/admin/comments">留言管理</a><a href="/nuxt/admin/settings/login-lockout">鎖定設定</a></template>
            <span class="reader-name">{{ site.account.value.nickname }}</span>
            <button type="button"
              class="text-button"
              :disabled="logoutPending || site.adminPending.value"
              @click="submitLogout">{{ logoutPending ? '登出中…' : '登出' }}</button>
          </template>
          <template v-else><a href="/nuxt/login">登入</a><a href="/nuxt/register">註冊</a></template>
        </nav>
      </details>
    </header>
    <div v-if="!site.ready.value"
      class="site-status"
      role="status">正在確認登入狀態…</div>
    <div v-else-if="site.authError.value"
      class="site-status"
      role="alert">
      {{ site.authError.value }}
      <button v-if="site.credential.value"
        type="button"
        class="inline-button"
        :disabled="site.checking.value"
        @click="site.verify()">重新驗證</button>
      <a v-else
        :href="loginHref(route.path)">前往登入</a>
    </div>
    <div v-if="notice"
      class="site-status"
      role="status">{{ notice }} <small v-if="noticeLogId">log_id：{{ noticeLogId }}</small></div>
    <div :class="{ 'admin-layout': route.path.startsWith('/admin') && site.account.value?.role === 'admin' }">
      <aside v-if="route.path.startsWith('/admin') && site.account.value?.role === 'admin'"
        class="admin-sidebar"
        aria-label="管理後台">
        <h2>管理後台</h2>
        <nav aria-label="管理導覽">
          <a href="/nuxt/admin/members"
            :aria-current="route.path.startsWith('/admin/members') ? 'page' : undefined">會員管理</a>
          <a href="/nuxt/admin/articles"
            :aria-current="route.path === '/admin/articles' ? 'page' : undefined">文章管理</a>
          <a href="/nuxt/admin/comments"
            :aria-current="route.path === '/admin/comments' ? 'page' : undefined">留言管理</a>
          <a href="/nuxt/admin/settings/login-lockout"
            :aria-current="route.path === '/admin/settings/login-lockout' ? 'page' : undefined">系統設定</a>
        </nav>
      </aside>
      <NuxtPage />
    </div>
    <footer class="footer">
      <span>文章集 / Nuxt</span>
      <span>留下文字，也留下時間。</span>
    </footer>
  </div>
</template>
