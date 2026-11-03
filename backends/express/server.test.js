'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { once } = require('node:events');
const { randomBytes } = require('node:crypto');
const { createApp } = require('./server');

// request 經真正 Express HTTP 入口保留原始 Query 拼法；假資料庫只用來偵測不該發生的查詢。
async function request(server, path, method = 'POST', raw = '{') {
  // Promise 追蹤連線及回應錯誤，讓測試不會把傳輸失敗當成 API 拒絕。
  return new Promise((resolve, reject) => {
    // 完整收齊小型測試回應後再解析，以觀察共同 envelope 而非私有函式。
    const req = http.request({ host: '127.0.0.1', port: server.address().port, path, method,
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(raw) } }, res => {
      const chunks = [];
      // 保留每段回應位元組，避免網路分段影響 JSON 判讀。
      res.on('data', chunk => chunks.push(chunk));
      res.on('error', reject);
      // 解析失敗也交回測試失敗，不讓 callback 的例外脫離 Promise。
      res.on('end', () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(Buffer.concat(chunks).toString('utf8')) }); }
        catch (error) { reject(error); }
      });
    });
    req.on('error', reject);
    req.end(raw);
  });
}

// openServer 只監聽本機隨機埠並登記清理，不啟動正式服務或接觸共用資料。
async function openServer(t) {
  const db = {
    // 這些案例應在格式邊界被拒絕；任何資料庫操作都代表順序退步。
    async query() { assert.fail('validation must not query the database'); },
  };
  const server = createApp(db, randomBytes(32)).listen(0, '127.0.0.1');
  // 即使斷言失敗也關閉測試 socket，避免殘留 listener 阻擋後續測試。
  t.after(() => {
    // executor 把 callback 轉成可等待的清理結果，關閉失敗仍應回報。
    return new Promise((resolve, reject) => {
      // close 完成才結束測試，確保 listener 已釋放。
      server.close(error => error ? reject(error) : resolve());
    });
  });
  await once(server, 'listening');
  return server;
}

// Query 身分取解碼位元組；可顯示的錯誤名稱碰撞不能製造重複或蓋掉真正的重複。
test('HTTP query errors preserve decoded identity and label priority', async t => {
  const server = await openServer(t);
  const unknown = ['不支援此欄位'];
  const duplicate = ['此參數不可重複'];
  const cases = [
    ['odd[x]=1', { 'odd[x]': unknown }],
    ['odd[x]=1&odd[x]=2', { 'odd[x]': duplicate }],
    ['filters[unexpected]=1', { 'filters.unexpected': unknown }],
    ['filters[unexpected][deep]=1', { 'filters[unexpected][deep]': unknown }],
    ['%FF=x', { '%FF': unknown }],
    ['%FF=x&%25FF=y', { '%FF': unknown }],
    ['%25FF=x&%FF=y', { '%FF': unknown }],
    ['%FF=x&%ff=y', { '%FF': duplicate }],
    ['%25FF=x&%25FF=y', { '%FF': duplicate }],
    ['%FF=x&%FF=y&%25FF=z', { '%FF': duplicate }],
    ['%FF=x&%25FF=y&%FF=z', { '%FF': duplicate }],
    ['%25FF=x&%FF=y&%FF=z', { '%FF': duplicate }],
    ['%25FF=x&%25FF=y&%FF=z', { '%FF': duplicate }],
    ['%25FF=x&%FF=y&%25FF=z', { '%FF': duplicate }],
    ['%FF=x&%25FF=y&%25FF=z', { '%FF': duplicate }],
    ['%FF=x&%25FF=y&%ff=z&%25FF=w', { '%FF': duplicate }],
    ['%25FF=x&%FF=y&%25FF=z&%ff=w', { '%FF': duplicate }],
    ['%ff=x&%25ff=y', { '%FF': unknown, '%ff': unknown }],
    ['%FF=x&%EF%BF%BD=y', { '%FF': unknown, '�': unknown }],
    ['%EF%BF%BD=x&%EF%BF%BD=y', { '�': duplicate }],
    ['%E6%9C%AA%E7%9F%A5=x', { '未知': unknown }],
    ['page[0]=x&page[1]=y', { 'page[0]': unknown, 'page[1]': unknown }],
    ['page%5B%FF%5D=x', { 'page%5B%FF%5D': unknown }],
    ['filters%5B%FF%5D=x', { 'filters%5B%FF%5D': unknown }],
    ['filters[unexpected]=x&filters.unexpected=y', { 'filters.unexpected': unknown }],
    ['filters[unexpected]=x&filters[unexpected]=y&filters.unexpected=z', { 'filters.unexpected': duplicate }],
  ];
  for (const [query, errors] of cases) {
    for (const path of ['/api/auth/register', '/api/auth/login']) {
      // 巢狀 subtest 保留失敗輸入；壞 JSON 不得遮蔽先發生的 Query 錯誤。
      await t.test(`${path}?${query}`, async () => {
        const response = await request(server, `${path}?${query}`);
        assert.equal(response.status, 422);
        assert.equal(response.body.error_code, 'E0001');
        assert.deepEqual(response.body.errors, errors);
      });
    }
  }
});

// 列表只收攏支援欄位的陣列；標籤碰撞時保留重複、型態、未知、格式的既定優先序。
test('HTTP list query errors retain supported array and aggregation semantics', async t => {
  const server = await openServer(t);
  const type = ['欄位型態不正確'];
  const unknown = ['不支援此欄位'];
  const duplicate = ['此參數不可重複'];
  const cases = [
    ['page[]=1&per_page=20', { page: type }],
    ['page[0]=1&page[1]=2&per_page=20', { page: duplicate }],
    ['page=1&page[0]=2&per_page=20', { page: duplicate }],
    ['page=bad&per_page=101', { page: ['欄位格式或範圍不正確'], per_page: ['欄位格式或範圍不正確'] }],
    ['page=1&per_page=20&filters[scope][]=bad', { 'filters.scope': type }],
    ['page=1&per_page=20&filters[scope]=bad&filters[scope][]=bad', { 'filters.scope': duplicate }],
    ['page=1&per_page=20&filters[scope][]=bad&filters.scope=x', { 'filters.scope': type }],
    ['page=1&per_page=20&filters.scope=x&filters[scope][]=bad', { 'filters.scope': type }],
    ['page=1&per_page=20&filters[scope]=bad&filters.scope=x', { 'filters.scope': unknown }],
    ['page=1&per_page=20&filters.scope=x&filters[scope]=bad', { 'filters.scope': unknown }],
    ['page=1&per_page=20&filters[scope][]=bad&filters.scope=x&filters.scope=y', { 'filters.scope': duplicate }],
    ['page=1&per_page=20&filters[keyword][]=x', { 'filters[keyword][]': unknown }],
    ['page=1&per_page=20&page%5B%FF%5D=x', { 'page%5B%FF%5D': unknown }],
  ];
  for (const [query, errors] of cases) {
    // GET 不帶 Body，讓斷言落在 Query 邊界而非 GET 本文的先行拒絕。
    await t.test(query, async () => {
      const response = await request(server, `/api/articles?${query}`, 'GET', '');
      assert.equal(response.status, 422);
      assert.equal(response.body.error_code, 'E0001');
      assert.deepEqual(response.body.errors, errors);
    });
  }
});
