<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { BACKENDS, loginHref, validReturn, vueFrontendTarget } from './utils/contracts.js'

const site = useSite()
const route = useRoute()
const logoutPending = ref(false)
const notice = ref('')
const noticeLogId = ref('')
const frontendTarget = ref(null)

// 掛載後才能讀同一分頁的 sessionStorage，伺服器只傳不含憑證的外殼。
onMounted(
  // 首頁與深層頁都先向目前後端確認既有登入，實際瀏覽器路徑供切換保留 ID。
  () => {
    frontendTarget.value = vueFrontendTarget(window.location.pathname)
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

// 兩版共同頁面使用同分頁導覽，原後端、JWT 和到期時間保持原樣。
function chooseFrontend(event) {
  if (event.target.value === 'nuxt') return
  if (!frontendTarget.value) { event.target.value = 'nuxt'; return }
  if (logoutPending.value) { event.target.value = 'nuxt'; notice.value = '登出處理中，請等待結果後再切換。'; return }
  if (site.dirty.value && !window.confirm('尚有未儲存文字，確定捨棄並切換前端嗎？')) { event.target.value = 'nuxt'; return }
  // 文件真正離開前同步作廢來源認證與表單／內容請求，舊回應不得再導回 Nuxt。
  site.leaveFrontend()
  const returnTarget = window.location.pathname === '/nuxt/login' && validReturn(route.query.return_to) ? vueFrontendTarget(route.query.return_to) : null
  const query = returnTarget ? `?return_to=${encodeURIComponent(returnTarget)}` : ''
  window.location.assign(`${frontendTarget.value}${query}`)
}

// 後端切換先確認草稿，再清除憑證並回到登入；返回目標只允許已實作的本機頁。
function chooseBackend(event) {
  const next = event.target.value
  if (next === site.backend.value) return
  if (site.dirty.value && !window.confirm('尚有未儲存文字，確定捨棄並切換後端嗎？')) { event.target.value = site.backend.value; return }
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
  if (logoutPending.value) return
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
        <a class="wordmark" href="/nuxt/" aria-label="文章集，回文章列表">文章集<span class="wordmark-dot">.</span></a>
        <p class="edition">一份關於文字的日常刊物</p>
        <div class="environment" aria-label="目前版本與後端">
          <span>目前前端 <strong>Nuxt</strong></span>
          <span>目前後端 <strong>{{ site.backend.value === BACKENDS.symfony ? 'Symfony' : 'Laravel' }}</strong></span>
        </div>
      </div>
      <div class="masthead-rule" />
      <nav class="main-nav" aria-label="主要導覽">
        <a href="/nuxt/">所有文章</a>
        <span class="nav-spacer" />
        <template v-if="site.account.value">
          <span class="reader-name">{{ site.account.value.nickname }}</span>
          <button type="button" class="text-button" :disabled="logoutPending" @click="submitLogout">{{ logoutPending ? '登出中…' : '登出' }}</button>
        </template>
        <template v-else>
          <a href="/nuxt/login">登入</a>
          <a href="/nuxt/register">註冊</a>
        </template>
      </nav>
      <details class="version-controls">
        <summary>切換版本</summary>
        <div class="version-fields">
          <div><label for="nuxt-frontend-version">前端版本</label><select id="nuxt-frontend-version" value="nuxt" @change="chooseFrontend"><option value="nuxt">Nuxt（目前使用）</option><option value="vue" :disabled="!frontendTarget">Vue 3＋Vite{{ frontendTarget ? '' : '（此頁不可切換）' }}</option><option disabled>React＋Vite（尚未提供）</option><option disabled>Next.js（尚未提供）</option><option disabled>Angular（尚未提供）</option><option disabled>SvelteKit（尚未提供）</option></select></div>
          <div><label for="nuxt-backend-version">後端版本</label><select id="nuxt-backend-version" :value="site.backend.value" @change="chooseBackend"><option :value="BACKENDS.laravel">Laravel</option><option :value="BACKENDS.symfony">Symfony</option></select></div>
          <p>切換後端會清除目前登入並要求重新登入。</p>
        </div>
      </details>
      <details class="mobile-nav">
        <summary>導覽選單</summary>
        <nav aria-label="手機導覽">
          <a href="/nuxt/">所有文章</a>
          <template v-if="site.account.value">
            <span class="reader-name">{{ site.account.value.nickname }}</span>
            <button type="button" class="text-button" :disabled="logoutPending" @click="submitLogout">{{ logoutPending ? '登出中…' : '登出' }}</button>
          </template>
          <template v-else>
            <a href="/nuxt/login">登入</a>
            <a href="/nuxt/register">註冊</a>
          </template>
        </nav>
      </details>
    </header>
    <div v-if="!site.ready.value" class="site-status" role="status">正在確認登入狀態…</div>
    <div v-else-if="site.authError.value" class="site-status" role="alert">
      {{ site.authError.value }}
      <button v-if="site.credential.value" type="button" class="inline-button" :disabled="site.checking.value" @click="site.verify()">重新驗證</button>
      <a v-else :href="loginHref(route.path)">前往登入</a>
    </div>
    <div v-if="notice" class="site-status" role="status">{{ notice }} <small v-if="noticeLogId">log_id：{{ noticeLogId }}</small></div>
    <NuxtPage />
    <footer class="footer"><span>文章集 / Nuxt</span><span>留下文字，也留下時間。</span></footer>
  </div>
</template>
