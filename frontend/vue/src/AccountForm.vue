<script setup>
import { computed } from 'vue'
// 表單只負責輸入、欄位錯誤與等待提示；session owner 決定驗證、HTTP 及成功後動作。
const props = defineProps({
  registration: Boolean,
  relogin: Boolean,
  pending: Boolean,
  blocked: Boolean,
  locked: Boolean,
  errors: { type: Object, required: true },
  lockText: { type: String, default: '' },
})
defineEmits(['submit'])
// 三欄仍由同頁 session 持有，原頁重登不建立第二份輸入或保存密碼。
const email = defineModel('email', { type: String, required: true })
const password = defineModel('password', { type: String, required: true })
const nickname = defineModel('nickname', { type: String, default: '' })

// 按表單用途與目前等待狀態顯示操作文字，讓模板不必交錯多層條件。
const submitText = computed(() => {
  if (props.registration) return props.pending ? '註冊處理中…' : '建立帳號'
  if (props.pending) return '登入處理中…'
  return props.relogin ? '重新登入' : '登入'
})
</script>

<template>
  <!-- blocked 只停用其他認證操作期間的控制；pending 才表示本表單已送出，兩者不能共用送出提示。 -->
  <!-- 所有提交方式仍交給同一事件與 session guard，包含 Enter，不以 disabled 取代流程互斥。 -->
  <form :class="{ 'relogin-form': relogin }" novalidate @submit.prevent="$emit('submit')">
    <div class="field">
      <label for="email">Email</label>
      <input id="email" v-model="email" type="email" inputmode="email" autocomplete="email"
        :disabled="pending || locked || blocked" :aria-invalid="Boolean(errors.email)"
        :aria-describedby="errors.email ? 'email-error' : undefined" />
      <p v-if="errors.email" id="email-error" class="field-error">{{ errors.email.join('；') }}</p>
    </div>
    <div class="field">
      <label for="password">密碼</label>
      <input id="password" v-model="password" type="password"
        :autocomplete="registration ? 'new-password' : 'current-password'"
        :disabled="pending || locked || blocked" :aria-invalid="Boolean(errors.password)"
        :aria-describedby="registration
          ? errors.password ? 'password-error password-help' : 'password-help'
          : errors.password ? 'password-error' : undefined" />
      <p v-if="registration" id="password-help" class="field-help">4–16 碼英文字母或數字，可只用其中一種。</p>
      <p v-if="errors.password" id="password-error" class="field-error">{{ errors.password.join('；') }}</p>
    </div>
    <div v-if="registration" class="field">
      <label for="nickname">暱稱</label>
      <input id="nickname" v-model="nickname" type="text" autocomplete="nickname"
        :disabled="pending || blocked" :aria-invalid="Boolean(errors.nickname)"
        :aria-describedby="errors.nickname ? 'nickname-error nickname-help' : 'nickname-help'" />
      <p id="nickname-help" class="field-help">1–30 個字；會顯示在您日後發表的內容旁。</p>
      <p v-if="errors.nickname" id="nickname-error" class="field-error">{{ errors.nickname.join('；') }}</p>
    </div>
    <button class="primary-button" type="submit" :disabled="pending || locked || blocked">
      {{ submitText }}
    </button>
    <p v-if="pending && !relogin" class="pending-note" role="status">
      {{ registration ? '正在送出，請稍候；不要重複提交。' : '正在確認帳號，請稍候。' }}
    </p>
    <p v-if="lockText" class="lock-note" aria-live="off">{{ lockText }}</p>
  </form>
</template>
