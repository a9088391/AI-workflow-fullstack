import assert from 'node:assert/strict';
import test from 'node:test';
import { classifyWrite } from './contracts.ts';

// 成功必須符合方法、HTTP 及完整空 data，避免破損回應清稿或導覽。
test('一般寫入只接受完整成功', function completeWrite() {
  const success = { success: true, message: 'OK', data: {} };
  assert.equal(classifyWrite(201, success, 'POST').kind, 'success');
  assert.equal(classifyWrite(200, success, 'PUT').kind, 'success');
  for (const body of [{ success: true }, { ...success, message: 'wrong' }, { ...success, data: null },
    { ...success, data: [] }, { ...success, data: { id: 3 } }]) {
    assert.equal(classifyWrite(201, body, 'POST').kind, 'unknown');
  }
  assert.equal(classifyWrite(200, success, 'POST').kind, 'unknown');
  assert.equal(classifyWrite(201, success, 'PUT').kind, 'unknown');
  assert.equal(classifyWrite(409, { success: false, error_code: 'E0201', message: '內容已更新，請確認最新內容後再送出', log_id: 'fixture' }, 'POST').kind, 'unknown');
});

// 固定配對及可顯示明細才是已知拒絕；未繪製欄位也須完整保留。
test('一般寫入已知拒絕與安全明細', function rejectedWrite() {
  const cases = [[422, 'E0001', '欄位資料不正確，請確認後再送出'], [401, 'E0002', '登入已逾期，請重新登入'],
    [403, 'E0003', '沒有權限使用此功能'], [403, 'E0102', '此帳號已停用，請聯絡系統管理員'],
    [404, 'E0004', '找不到指定資料或 API'], [409, 'E0201', '內容已更新，請確認最新內容後再送出'],
    [400, 'E0005', '系統暫時無法處理，請稍後再試'], [405, 'E0005', '系統暫時無法處理，請稍後再試']] as const;
  for (const [status, error_code, message] of cases) {
    const body = { success: false, error_code, message, log_id: 'fixture',
      ...(status === 422 ? { errors: { body: ['請修正'], other: ['另欄錯誤'] } } : {}) };
    const result = classifyWrite(status, body, 'PUT');
    assert.equal(result.kind, 'rejected', error_code);
    if (result.kind === 'rejected' && status === 422) assert.deepEqual(result.problem.errors, body.errors);
  }
});

// 5xx 包含合法錯誤仍可能完成寫入；不合法錯誤也不得撤權或允許盲送。
test('一般寫入無法確認結果', function unknownWrite() {
  const error = { success: false, error_code: 'E9999', message: '系統暫時無法處理，請稍後再試', log_id: 'fixture' };
  for (const [status, body] of [[500, error], [503, error], [0, null], [200, error],
    [500, { ...error, error_code: 'E0002' }], [401, { ...error, error_code: 'E0101' }],
    [422, { ...error, error_code: 'E0001', errors: { body: [null] } }],
    [422, { ...error, error_code: 'E0001', errors: {} }],
    [422, { ...error, error_code: 'E0001', errors: null }],
    [422, { ...error, error_code: 'E0001', errors: [] }],
    [422, { ...error, error_code: 'E0001', errors: { body: 'bad' } }],
    [403, { ...error, error_code: 'E0102', message: '破損文案' }],
    [401, { success: false, error_code: 'E0002', message: '登入已逾期，請重新登入' }]]) {
    const result = classifyWrite(status as number, body, 'PUT');
    assert.equal(result.kind, 'unknown');
    if (result.kind !== 'success') assert.equal(result.problem.code, undefined);
  }
});
