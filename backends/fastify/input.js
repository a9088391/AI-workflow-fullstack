'use strict';

const INVALID = '欄位格式或範圍不正確';
const REQUIRED = '此欄位為必填';
const NULL = '此欄位不可為 null';
const TYPE = '欄位型態不正確';
const MAX_ID = 9007199254740991;
const EMAIL = /^[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;
const BLANK = /^[ \t\n\r\v\f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;
const VERSION_MARKER = Symbol('parsed version');

// ApiError 保存共同錯誤碼與欄位細節，避免 Fastify 原生格式流出。
class ApiError extends Error {
  // constructor 集中保存共同錯誤資料，讓回應處理器依契約輸出而不使用框架預設格式。
  constructor(code, errors, retry, status) {
    super(code);
    this.code = code;
    this.errors = errors;
    this.retry = retry;
    this.status = status;
  }
}

// fieldError 將單一欄位錯誤套用 API15 固定文案。
function fieldError(name, message) {
  return new ApiError('E0001', { [name]: [message] });
}

// urlInteger 僅接受 ASCII 十進位正整數，前導零可用但不可超安全範圍。
function urlInteger(value, name, max = MAX_ID) {
  if (typeof value !== 'string' || !/^[0-9]+$/.test(value)) throw fieldError(name, INVALID);
  const digits = value.replace(/^0+/, '');
  if (!digits || digits.length > 16 || Number(digits) > max) throw fieldError(name, INVALID);
  return Number(digits);
}

// queryName 保留解碼後的位元組身分；非法 UTF-8 只在錯誤標籤轉為百分比表示。
function queryName(raw) {
  const encoded = Buffer.from(raw.replace(/\+/g, ' '));
  const decoded = Buffer.alloc(encoded.length);
  let length = 0;
  for (let index = 0; index < encoded.length; index++) {
    // 只解一層百分比；字面的 %FF 與位元組 FF 必須仍是不同參數。
    const hex = encoded.subarray(index + 1, index + 3).toString('latin1');
    if (encoded[index] === 37 && /^[0-9a-f]{2}$/i.test(hex)) {
      decoded[length++] = Number.parseInt(hex, 16);
      index += 2;
    } else decoded[length++] = encoded[index];
  }
  const bytes = decoded.subarray(0, length);
  const identity = bytes.toString('hex');
  try {
    const name = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
    return { name, identity, label: name };
  } catch {
    // 只有無效 UTF-8 名稱採百分比標籤；不能用替代字元與合法 U+FFFD 混成同一身分。
    let label = '';
    for (const byte of bytes) {
      const char = String.fromCharCode(byte);
      label += /[A-Za-z0-9_.~-]/.test(char) ? char : byte === 32 ? '+' : `%${byte.toString(16).toUpperCase().padStart(2, '0')}`;
    }
    return { name: null, identity, label };
  }
}

// queryInput 讀原始 Query 而非 Fastify 解析結果，保留重複／陣列錯誤與關鍵字字面值。
function queryInput(req, allowed = []) {
  const values = Object.create(null);
  const errors = Object.create(null);
  const seen = new Set();
  const priorities = Object.create(null);
  const raw = req.raw.url.split('?').slice(1).join('?');
  for (const pair of raw.split('&')) {
    if (!pair) continue;
    const separator = pair.indexOf('=');
    const decoded = queryName(separator < 0 ? pair : pair.slice(0, separator));
    let canonical = decoded.name;
    // 僅支援的單值欄位收攏陣列鍵；未知鍵及非法 UTF-8 名稱保留各自身分。
    for (const key of allowed) {
      if (canonical?.startsWith(`${key}[`)) canonical = key;
    }
    const identity = canonical === null ? decoded.identity : Buffer.from(canonical).toString('hex');
    // 單層 filters 子欄位使用點記法，未知深層括號不得被改寫。
    const label = canonical === null ? decoded.label : canonical.replace(/^filters\[([^\[\]]+)\]$/, 'filters.$1');
    let message, priority = 0;
    if (seen.has(identity)) { message = '此參數不可重複'; priority = 4; }
    else if (canonical !== decoded.name && allowed.includes(canonical)) { message = TYPE; priority = 3; }
    else if (!allowed.includes(canonical)) { message = '不支援此欄位'; priority = 2; }
    seen.add(identity);
    // 顯示標籤可碰撞；保留較優先錯誤，不按顯示名稱判重複或受輸入順序影響。
    if (priority > (priorities[label] || 0)) {
      errors[label] = [message];
      priorities[label] = priority;
    }
    const value = new URLSearchParams(`value=${separator < 0 ? '' : pair.slice(separator + 1)}`).get('value');
    values[canonical] = value;
  }
  // 完成名稱與重複判定後才驗單值；既有同欄錯誤不可被值驗證覆蓋。
  if (allowed.includes('page')) {
    for (const name of ['page', 'per_page']) {
      if (!Object.hasOwn(values, name)) errors[name] = [REQUIRED];
      else if (!errors[name]) {
        try { values[name] = urlInteger(values[name], name, name === 'per_page' ? 100 : MAX_ID); }
        catch { errors[name] = [INVALID]; }
      }
    }
  }
  if (Object.hasOwn(values, 'filters[scope]') && !errors['filters.scope'] && values['filters[scope]'] !== 'management') {
    errors['filters.scope'] = [INVALID];
  }
  if (Object.hasOwn(values, 'filters[keyword]') && !errors['filters.keyword']) {
    // 關鍵字只裁 U+0020；拒絕實際 NUL，反斜線文字與其他 Unicode 字元仍按字面值驗證。
    values['filters[keyword]'] = values['filters[keyword]'].replace(/^ +| +$/g, '');
    if (values['filters[keyword]'].includes('\0') || [...values['filters[keyword]']].length > 254) errors['filters.keyword'] = [INVALID];
  }
  if (Object.keys(errors).length) throw new ApiError('E0001', Object.fromEntries(Object.entries(errors)));
  return Object.fromEntries(Object.entries(values));
}

// getBody 直接讀 GET 原始串流，因 Fastify 不解析 GET Body；限制大小避免無界緩衝。
async function getBody(req) {
  let length = 0;
  for await (const chunk of req.raw) {
    length += chunk.length;
    if (length > 1048576) throw new ApiError('E0005', undefined, undefined, 413);
  }
  if (length) throw fieldError('_body', '此 API 不接受 Request Body');
}

// validSurrogates 拒絕未配對 UTF-16 escape，避免 JSON.parse 靜默保留無效字元。
function validSurrogates(raw) {
  for (let i = 0; i < raw.length;) {
    if (raw[i] !== '\\') { i++; continue; }
    if (raw[i + 1] !== 'u') { i += 2; continue; }
    const value = Number.parseInt(raw.slice(i + 2, i + 6), 16);
    if (value >= 0xd800 && value <= 0xdbff) {
      if (raw.slice(i + 6, i + 8) !== '\\u') return false;
      const low = Number.parseInt(raw.slice(i + 8, i + 12), 16);
      if (!(low >= 0xdc00 && low <= 0xdfff)) return false;
      i += 12;
    } else {
      if (value >= 0xdc00 && value <= 0xdfff) return false;
      i += 6;
    }
  }
  return true;
}

// bodyInput 自行解析原始 JSON 與端點欄位，保留共用驗證錯誤順序。
function bodyInput(req, fields, optional = false) {
  if (!req.body?.length) {
    if (optional) return {};
    throw fieldError('_body', 'Request Body 必須為 JSON object');
  }
  if (req.headers && req.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
    throw fieldError('_body', 'Request Body 必須為 JSON object');
  }
  let body;
  try {
    const raw = new TextDecoder('utf-8', { fatal: true }).decode(req.body);
    if (!validSurrogates(raw)) throw Error();
    // callback 只標記整數原始詞形；私有 Symbol 避免 JSON object 偽造型態。
    body = JSON.parse(raw, function preserveVersion(name, value, context) {
      if (['version', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(name) && typeof value === 'number') return { [VERSION_MARKER]: value, source: context.source };
      return value;
    });
  }
  catch { throw new ApiError('E0005'); }
  // 語法合法後再判根結構，才能區分 400 解析錯誤與 422 欄位錯誤。
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw fieldError('_body', 'Request Body 必須為 JSON object');
  }
  const errors = Object.create(null);
  const result = {};
  for (const name of fields) {
    const value = body[name];
    if (!Object.hasOwn(body, name)) errors[name] = [REQUIRED];
    else if (value === null) errors[name] = [NULL];
    else if (['version', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(name)) {
      // 整數須保留十進位 JSON 原始寫法，避免指數與超大數被 JS 轉換後誤收。
      if (!value || typeof value !== 'object' || !Object.hasOwn(value, VERSION_MARKER)) errors[name] = [TYPE];
      else if (!/^-?(0|[1-9][0-9]*)$/.test(value.source)) errors[name] = [TYPE];
      else if (!Number.isSafeInteger(value[VERSION_MARKER]) || value[VERSION_MARKER] < 1 ||
          value[VERSION_MARKER] > ({ window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 }[name] || MAX_ID)) errors[name] = [INVALID];
      else result[name] = value[VERSION_MARKER];
    } else if (name === 'enabled') {
      if (typeof value !== 'boolean') errors[name] = [TYPE];
      else result[name] = value;
    } else if (typeof value !== 'string') errors[name] = [TYPE];
    else if (name === 'email') {
      // callback 只轉 ASCII 大寫，避免 Unicode 大小寫規則改變跨後端帳號比對。
      const normalized = value.replace(/^ +| +$/g, '').replace(/[A-Z]/g, c => c.toLowerCase());
      const local = normalized.split('@')[0];
      if (normalized.length > 254 || local.length > 64 || !EMAIL.test(normalized)) errors[name] = [INVALID];
      else result[name] = normalized;
    } else if (name === 'password') {
      if (!/^[A-Za-z0-9]{4,16}$/.test(value)) errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'nickname') {
      if (value.includes('\0') || [...value].length < 1 || [...value].length > 30 || BLANK.test(value)) errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'title' || name === 'body') {
      const limit = name === 'title' ? 200 : fields.includes('title') ? 20000 : 2000;
      if (value.includes('\0') || [...value].length < 1 || [...value].length > limit || BLANK.test(value)) errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'visibility') {
      if (value !== 'visible' && value !== 'hidden') errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'status') {
      if (value !== 'active' && value !== 'disabled') errors[name] = [INVALID];
      else result[name] = value;
    }
  }
  // 最後彙整未知鍵，連同必填、型態及格式錯誤一次回覆，不默默忽略。
  for (const name of Object.keys(body)) if (!fields.includes(name)) errors[name] = ['不支援此欄位'];
  if (Object.keys(errors).length) throw new ApiError('E0001', Object.fromEntries(Object.entries(errors)));
  return result;
}

module.exports = { ApiError, fieldError, urlInteger, queryInput, getBody, bodyInput };
