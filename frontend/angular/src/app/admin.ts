export type Member = { id: number; email: string; nickname: string; role: 'member'; status: 'active' | 'disabled'; created_at: string };
export type LockoutSettings = { enabled: boolean; window_minutes: number; failure_threshold: number; lock_seconds: number };

// 管理員只操作完整的一般會員資料，避免破損回應被當成可停用帳號。
export function isMember(value: any): value is Member {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Number.isSafeInteger(value.id) && value.id > 0 && typeof value.email === 'string' && value.email.length > 0
    && typeof value.nickname === 'string' && value.nickname.length > 0 && value.role === 'member'
    && (value.status === 'active' || value.status === 'disabled')
    && typeof value.created_at === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value.created_at);
}

// 四欄設定需完整且在共同範圍內；讀回與送出使用同一規則。
export function settingErrors(value: LockoutSettings): Record<string, string[]> {
  const errors: Record<string, string[]> = {};
  if (typeof value.enabled !== 'boolean') errors['enabled'] = ['請選擇是否啟用。'];
  for (const [key, max] of Object.entries({ window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 })) {
    const number = value[key as 'window_minutes' | 'failure_threshold' | 'lock_seconds'];
    if (!Number.isInteger(number) || number < 1 || number > max) errors[key] = [`請輸入 1 到 ${max} 的整數。`];
  }
  return errors;
}

// 讀取時排除多餘與缺少欄位，避免表單顯示不屬於核准契約的設定。
export function isLockoutSettings(value: any): value is LockoutSettings {
  return value && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 4 && Object.keys(settingErrors(value)).length === 0;
}

// 搜尋僅修剪 ASCII 空格並保留字面內容；網址編碼由 URLSearchParams 負責。
export function memberKeyword(value: string): string { return value.replace(/^ +| +$/g, ''); }
