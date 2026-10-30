<script setup>
const props = defineProps({
  draft: { type: Object, required: true },
  errors: { type: Object, required: true },
  pending: Boolean,
  blocked: Boolean,
})
const emit = defineEmits(['update:draft', 'submit'])

// 設定草稿屬於設定 owner；選項只送出新物件，不在欄位元件修改上層 props。
function inputEnabled(event) {
  emit('update:draft', { ...props.draft, enabled: event.target.value === 'true' })
}

// 保留既有 v-model.number 的 parseFloat／空值語意，交由 owner 驗證完整四欄。
function inputNumber(event) {
  const raw = event.target.value
  const value = Number.parseFloat(raw)
  emit('update:draft', { ...props.draft, [event.target.id]: Number.isNaN(value) ? raw : value })
}

// 鍵盤與按鈕走同一提交入口；真正驗證、互斥及讀回都由設定 owner 執行。
function submit() {
  emit('submit')
}
</script>

<template>
  <form class="lockout-form" novalidate @submit.prevent="submit">
    <div class="field">
      <label for="enabled">鎖定功能</label>
      <select id="enabled" :value="draft.enabled" @change="inputEnabled"
        :disabled="pending" :aria-invalid="Boolean(errors.enabled?.length)"
        :aria-describedby="errors.enabled?.length ? 'enabled-error enabled-help' : 'enabled-help'">
        <option :value="true">啟用</option>
        <option :value="false">關閉</option>
      </select>
      <p id="enabled-help" class="field-help">關閉後仍保存並驗證以下三個數值；重新啟用時沿用。</p>
      <p v-if="errors.enabled?.length" id="enabled-error" class="field-error">{{ errors.enabled.join('；') }}</p>
    </div>
    <div class="field">
      <label for="window_minutes">累計期間（分鐘）</label>
      <input id="window_minutes" :value="draft.window_minutes" @input="inputNumber" type="number" min="1" max="60" step="1" inputmode="numeric"
        :disabled="pending" :aria-invalid="Boolean(errors.window_minutes?.length)"
        :aria-describedby="errors.window_minutes?.length ? 'window_minutes-error window_minutes-help' : 'window_minutes-help'" />
      <p id="window_minutes-help" class="field-help">可設定 1–60 分鐘；範圍調大時，仍在新範圍內的既有失敗事件可能計入。</p>
      <p v-if="errors.window_minutes?.length" id="window_minutes-error" class="field-error">{{ errors.window_minutes.join('；') }}</p>
    </div>
    <div class="field">
      <label for="failure_threshold">失敗門檻（次）</label>
      <input id="failure_threshold" :value="draft.failure_threshold" @input="inputNumber" type="number" min="1" max="100" step="1" inputmode="numeric"
        :disabled="pending" :aria-invalid="Boolean(errors.failure_threshold?.length)"
        :aria-describedby="errors.failure_threshold?.length ? 'failure_threshold-error failure_threshold-help' : 'failure_threshold-help'" />
      <p id="failure_threshold-help" class="field-help">可設定 1–100 次；達到門檻的當次登入即開始鎖定。</p>
      <p v-if="errors.failure_threshold?.length" id="failure_threshold-error" class="field-error">{{ errors.failure_threshold.join('；') }}</p>
    </div>
    <div class="field">
      <label for="lock_seconds">鎖定時間（秒）</label>
      <input id="lock_seconds" :value="draft.lock_seconds" @input="inputNumber" type="number" min="1" max="86400" step="1" inputmode="numeric"
        :disabled="pending" :aria-invalid="Boolean(errors.lock_seconds?.length)"
        :aria-describedby="errors.lock_seconds?.length ? 'lock_seconds-error lock_seconds-help' : 'lock_seconds-help'" />
      <p id="lock_seconds-help" class="field-help">可設定 1–86,400 秒；調整不延長已存在鎖定的到期時間。</p>
      <p v-if="errors.lock_seconds?.length" id="lock_seconds-error" class="field-error">{{ errors.lock_seconds.join('；') }}</p>
    </div>
    <div class="lockout-preview" aria-live="polite">
      <strong>設定預覽</strong>
      <p>{{ draft.enabled
        ? `啟用：${draft.window_minutes} 分鐘內失敗 ${draft.failure_threshold} 次，鎖定 ${draft.lock_seconds} 秒。`
        : '關閉：會員登入失敗不會觸發暫時鎖定。' }}</p>
    </div>
    <button class="primary-button" type="submit" :disabled="pending || blocked">{{ pending ? '儲存中…' : '儲存完整設定' }}</button>
  </form>
</template>
