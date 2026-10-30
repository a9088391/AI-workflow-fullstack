<script setup>
import { computed } from 'vue'

const props = defineProps({
  mode: { type: String, required: true },
  title: { type: String, default: '' },
  body: { type: String, required: true },
  errors: { type: Object, required: true },
  disabled: Boolean,
  pending: Boolean,
  conflict: Boolean,
  version: { type: Number, default: null },
})
const emit = defineEmits(['update:title', 'update:body', 'submit'])

// 同一組作者欄位保留發文、留言與編修既有文案，不自行持有或重設上層草稿。
const editing = computed(() => props.mode === 'article-edit' || props.mode === 'comment-edit')
// 文章才有標題欄位，留言僅送 body，避免新增未核准 API 欄位。
const article = computed(() => props.mode === 'article-new' || props.mode === 'article-edit')
// 編修與建立使用原有狀態文字，等待只描述本表單自己的操作。
const buttonText = computed(() => {
  if (editing.value) return props.pending ? '儲存處理中…' : '儲存修改'
  if (article.value) return props.pending ? '發表處理中…' : '發表文章'
  return props.pending ? '留言處理中…' : '發表留言'
})
// 各表單使用原有純文字限制與說明，讓可存取名稱保持一致。
const bodyHelp = computed(() => {
  if (editing.value) return `${article.value ? '1–20,000' : '1–2,000'} 個字，不可全為空白。以純文字保留，不解析 HTML。`
  return article.value ? '1–20,000 個字；原樣保留換行。' : '1–2,000 個字，不可全為空白；原樣保留換行。'
})
// 欄位說明沿用既有 ID，錯誤摘要與 aria-describedby 都能指向目前頁面同一欄位。
const bodyHelpId = computed(() => props.mode === 'comment-new' ? 'comment-help' : 'body-help')
// 新文章保留長度說明；編輯標題沿用原先只有欄位錯誤的關聯方式。
const titleDescription = computed(() => {
  if (editing.value) return props.errors.title ? 'title-error' : undefined
  return props.errors.title ? 'title-error title-help' : 'title-help'
})
// 等待文案不混用另一項操作的狀態，避免 visibility 等待被描述為留言發表。
const pendingText = computed(() => {
  if (editing.value) return '正在送出，請稍候；不要重複提交。'
  return article.value ? '正在發表，請稍候；不要重複送出。' : '正在送出留言，請稍候；不要重複提交。'
})

// 只有實際 input 才更新原始標題，元件掛載不整理或截斷伺服器原文。
function inputTitle(event) {
  emit('update:title', event.target.value)
}

// textarea 只在使用者輸入後採用瀏覽器值，初讀 CR／LF 不因元件拆分被正規化。
function inputBody(event) {
  emit('update:body', event.target.value)
}

// 真正提交仍由作者 owner 驗證與互斥；元件只提供同一個鍵盤／按鈕事件入口。
function submit() {
  emit('submit')
}
</script>

<template>
  <form :class="editing ? 'content-editor' : undefined" novalidate @submit.prevent="submit">
    <slot />
    <div v-if="article" class="field">
      <label for="title">標題</label>
      <input id="title" :value="title" type="text" :disabled="disabled"
        :aria-invalid="Boolean(errors.title)"
        :aria-describedby="titleDescription"
        @input="inputTitle" />
      <p v-if="!editing" id="title-help" class="field-help">1–200 個字，不可全為空白。</p>
      <p v-if="errors.title" id="title-error" class="field-error">{{ errors.title.join('；') }}</p>
    </div>
    <div class="field">
      <label for="body">{{ mode === 'comment-new' ? '留言內容' : '內文' }}</label>
      <textarea id="body" :value="body" :disabled="disabled"
        :aria-invalid="Boolean(errors.body)"
        :aria-describedby="errors.body ? `body-error ${bodyHelpId}` : bodyHelpId"
        @input="inputBody"></textarea>
      <p :id="bodyHelpId" class="field-help">{{ bodyHelp }}</p>
      <p v-if="errors.body" id="body-error" class="field-error" :role="mode === 'comment-new' ? 'alert' : undefined">{{ errors.body.join('；') }}</p>
    </div>
    <p v-if="editing" class="muted">目前草稿使用版本 {{ version }}</p>
    <button class="primary-button" type="submit" :disabled="disabled || conflict">{{ buttonText }}</button>
    <p v-if="pending" class="pending-note" role="status">{{ pendingText }}</p>
  </form>
</template>
