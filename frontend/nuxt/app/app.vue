<script setup>
import { onMounted, ref } from 'vue'
import { backendName, loginHref } from './utils/contracts.js'

const site = useSite()
const route = useRoute()
const logoutPending = ref(false)
const notice = ref('')
const noticeLogId = ref('')

// 掛載後才能讀同一分頁的 sessionStorage，伺服器只傳不含憑證的外殼。
onMounted(
  // 首頁與深層頁都先向目前後端確認既有登入。
  () => { site.initialize() },
)

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
          <span>目前後端 <strong>{{ backendName('laravel') }}</strong></span>
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
