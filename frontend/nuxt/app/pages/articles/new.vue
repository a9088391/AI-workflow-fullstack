<script setup>
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { apiFailure, apiRequest, emptySuccess, loginHref, validateContent } from '../../utils/contracts.js'

useHead({ title: '發表文章｜文章集' })
const site = useSite()
const route = useRoute()
const title = ref('')
const body = ref('')
const errors = ref({})
const message = ref('')
const logId = ref('')
const pending = ref(false)
const uncertain = ref(false)
const ownerId = ref(null)
const summaryElement = ref(null)
const titleElement = ref(null)
const bodyElement = ref(null)
let controller = null

// 草稿只保留在此頁記憶體，供切換前確認；從不寫入共用登入儲存。
watch([title, body],
  // 初次輸入時綁定已驗證帳號，避免重登後把文字送給別人。
  () => { if ((title.value || body.value) && ownerId.value === null && site.account.value) ownerId.value = site.account.value.id; site.dirty.value = Boolean(title.value || body.value) },
)
watch(site.account,
  // 換成另一個已驗證帳號時丟棄舊人的草稿。
  (account) => { if (account && ownerId.value !== null && account.id !== ownerId.value) clearDraft() },
)
watch([site.backend, site.leaving],
  // 切換當下作廢舊寫入回應；後端變更後不能保留前一來源草稿。
  () => { controller?.abort(); if (!site.leaving.value) clearDraft() },
  { flush: 'sync' },
)
onBeforeUnmount(
  // 離開新文章頁時清理未完成請求與本頁草稿旗標。
  () => { controller?.abort(); site.dirty.value = false },
)

// 不同帳號或後端不得沿用草稿；只有確實丟棄文字才顯示清除通知。
function clearDraft() {
  const hadDraft = Boolean(title.value || body.value)
  title.value = ''
  body.value = ''
  ownerId.value = site.account.value?.id ?? null
  uncertain.value = false
  site.dirty.value = false
  message.value = hadDraft ? '已清除上一個帳號的文字，請重新輸入。' : ''
}

// 表單摘要先可聚焦，再由使用者跳至第一個實際錯誤欄位。
function focusFirstError() { if (errors.value.title?.length) titleElement.value?.focus(); else if (errors.value.body?.length) bodyElement.value?.focus() }

// 新增只接受空 data 的 201；成功固定前往我的文章第一頁，不猜資源 ID。
async function submit() {
  if (pending.value || uncertain.value || site.leaving.value || !site.account.value || !site.token.value || ownerId.value !== null && ownerId.value !== site.account.value.id) return
  const checked = validateContent({ title: title.value, body: body.value })
  if (Object.keys(checked).length) { errors.value = checked; message.value = '欄位資料不正確，請確認後再送出'; await nextTick(); summaryElement.value?.focus(); return }
  const request = new AbortController()
  controller = request
  const sentToken = site.token.value
  const sentBackend = site.backend.value
  const sentOwner = site.account.value.id
  pending.value = true
  message.value = ''
  errors.value = {}
  logId.value = ''
  try {
    const response = await apiRequest('/api/articles', { method: 'POST', body: { title: title.value, body: body.value }, token: sentToken, signal: request.signal })
    if (request.signal.aborted || site.leaving.value || sentBackend !== site.backend.value) return
    // 舊憑證的明確驗證拒絕保證未建立；其他晚到結果仍由同作者查證，不能撤銷新憑證。
    if (sentToken !== site.token.value) {
      if (ownerId.value === sentOwner && (!site.account.value || site.account.value.id === sentOwner)) {
        const failure = apiFailure(response.status, response.body)
        const rejected = response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102'
        uncertain.value = !rejected
        message.value = rejected ? failure.message : '發表結果尚未確認。請先到我的文章查證，再決定是否重新送出。'
        logId.value = rejected ? failure.logId : ''
      }
      return
    }
    if (emptySuccess(response.status, response.body, 201)) { site.dirty.value = false; window.location.assign('/nuxt/me/articles?published=1'); return }
    const failure = apiFailure(response.status, response.body, ['title', 'body'])
    if (response.status === 401 && failure.code === 'E0002' || response.status === 403 && failure.code === 'E0102') site.forget(failure.message)
    if (response.status >= 500 || !failure.code) uncertain.value = true
    errors.value = failure.errors
    message.value = uncertain.value ? '發表結果尚未確認。請先到我的文章查證，再決定是否重新送出。' : failure.message
    logId.value = failure.logId
    if (response.status === 422) { await nextTick(); summaryElement.value?.focus() }
  } catch {
    if (!request.signal.aborted && !site.leaving.value && sentBackend === site.backend.value && ownerId.value === sentOwner && (!site.account.value || site.account.value.id === sentOwner)) { uncertain.value = true; message.value = '發表結果尚未確認。請先到我的文章查證，再決定是否重新送出。' }
  } finally { if (controller === request) { controller = null; pending.value = false } }
}

// 查證完成後才重新開放人工送出；此操作本身不發出寫入。
function checkedResult() { uncertain.value = false; message.value = '請確認尚未發表，再手動送出。' }

// 同帳號重登不能覆蓋尚待查證的發表結果提示。
function restored() { if (!uncertain.value) message.value = '已恢復同帳號登入，請確認文字後手動送出。' }
</script>

<template>
  <main class="reading-page author-page">
    <a class="back-link" href="/nuxt/">← 返回文章列表</a><div class="author-heading"><div><p class="section-label">會員發表</p><h1>發表文章</h1></div></div>
    <p v-if="!site.ready.value" class="state-block" role="status">正在確認登入狀態…</p>
    <div v-else-if="!site.account.value && ownerId === null" class="state-block"><p>請先登入後發表文章。</p><a :href="loginHref(route.path)">前往登入</a></div>
    <template v-else>
      <ReauthForm v-if="!site.account.value" :owner-id="ownerId" @restored="restored" @different="clearDraft" />
      <p class="field-hint">文章以純文字發表，換行會保留；公開來源由目前後端記錄。</p>
      <form class="editor-form" novalidate @submit.prevent="submit">
        <p v-if="message" ref="summaryElement" class="form-error" tabindex="-1" role="alert">{{ message }}<small v-if="logId">log_id：{{ logId }}</small><button v-if="errors.title?.length || errors.body?.length" type="button" class="inline-button" @click="focusFirstError">前往第一個錯誤欄位</button></p>
        <div v-if="uncertain" class="state-actions"><a href="/nuxt/me/articles">查看我的文章</a><button type="button" class="inline-button" @click="checkedResult">我已查證結果</button></div>
        <label for="new-title">標題</label><input id="new-title" ref="titleElement" v-model="title" :aria-invalid="Boolean(errors.title)" :aria-describedby="errors.title ? 'new-title-error' : undefined" :disabled="pending || !site.account.value"><small v-if="errors.title" id="new-title-error" class="field-error">{{ errors.title.join('；') }}</small>
        <label for="new-body">內文</label><textarea id="new-body" ref="bodyElement" v-model="body" rows="14" :aria-invalid="Boolean(errors.body)" :aria-describedby="errors.body ? 'new-body-error' : undefined" :disabled="pending || !site.account.value" /><small v-if="errors.body" id="new-body-error" class="field-error">{{ errors.body.join('；') }}</small>
        <button class="primary-button" type="submit" :disabled="pending || uncertain || !site.account.value">{{ pending ? '發表中…' : '發表文章' }}</button>
      </form>
    </template>
  </main>
</template>
