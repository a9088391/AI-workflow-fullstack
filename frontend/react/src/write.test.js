import assert from 'node:assert/strict'
import test from 'node:test'
import { validWriteSuccess } from './contracts.js'

// 原四個 hook 競態案例改由 CQ14 真瀏覽器驗證；此處只補正式結果判讀的輸入邊界。
for (const status of [200, 201]) {
  // 狀態必須對應該操作，避免新增與更新互相誤認成功。
  test(`write ${status} accepts only its complete empty-object success`, () => {
    assert.equal(validWriteSuccess(status, { success: true, message: 'OK', data: {} }, status), true)
    assert.equal(validWriteSuccess(status === 200 ? 201 : 200, { success: true, message: 'OK', data: {} }, status), false)
    for (const data of [true, 1, null, undefined, [], '', { unexpected: true }]) {
      assert.equal(validWriteSuccess(status, { success: true, message: 'OK', data }, status), false)
    }
    for (const body of [null, {}, { success: false, message: 'OK', data: {} }, { success: true, message: 'not OK', data: {} }]) {
      assert.equal(validWriteSuccess(status, body, status), false)
    }
  })
}
