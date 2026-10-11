'use strict';

const INVALID = '欄位格式或範圍不正確';
const REQUIRED = '此欄位為必填';
const NULL = '此欄位不可為 null';
const TYPE = '欄位型態不正確';
const MAX_ID = 9007199254740991;
const EMAIL = /^[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;

// ApiError 保存共同錯誤碼與欄位細節，避免 Express 預設 HTML 錯誤流出。
class ApiError extends Error {
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

// queryInput 讀原始 Query 而非 Express 解析結果，保留重複／陣列錯誤與關鍵字字面值。
function queryInput(req, allowed = []) {
  const values = Object.create(null);
  const errors = Object.create(null);
  const raw = req.originalUrl.split('?').slice(1).join('?');
  for (const [name, value] of new URLSearchParams(raw)) {
    let canonical = name;
    for (const key of ['page', 'per_page', 'filters[scope]', 'filters[keyword]']) {
      if (name.startsWith(`${key}[`)) canonical = key;
    }
    // 只有完整的巢狀欄位名稱轉為共同點記法；畸形未知欄位保留原名。
    const label = canonical.replace(/^filters\[([^\]]+)\]$/, 'filters.$1');
    if (Object.hasOwn(values, canonical)) errors[label] = ['此參數不可重複'];
    else if (canonical !== name && allowed.includes(canonical)) errors[label] = [TYPE];
    else if (!allowed.includes(canonical)) errors[label] = ['不支援此欄位'];
    values[canonical] = value;
  }
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
    // 關鍵字只裁 U+0020，其他 Unicode 字元與萬用符號保持字面值。
    values['filters[keyword]'] = values['filters[keyword]'].replace(/^ +| +$/g, '');
    if ([...values['filters[keyword]']].length > 254) errors['filters.keyword'] = [INVALID];
  }
  if (Object.keys(errors).length) throw new ApiError('E0001', Object.fromEntries(Object.entries(errors)));
  return Object.fromEntries(Object.entries(values));
}

// getBody 拒絕任何非空 GET 本文；規格要求先判此錯，再判 Query。
function getBody(req) {
  if (req.body?.length) throw fieldError('_body', '此 API 不接受 Request Body');
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

// bodyInput 將 JSON 解析、原始整數詞形與欄位驗證分開，保留共同錯誤順序。
function bodyInput(req, fields, optional = false) {
  if (!req.body?.length) {
    if (optional) return {};
    throw fieldError('_body', 'Request Body 必須為 JSON object');
  }
  if (req.headers && req.headers['content-type']?.split(';')[0].trim().toLowerCase() !== 'application/json') {
    throw fieldError('_body', 'Request Body 必須為 JSON object');
  }
  let body;
  const integerSyntax = new WeakMap();
  try {
    const raw = new TextDecoder('utf-8', { fatal: true }).decode(req.body);
    if (!validSurrogates(raw)) throw Error();
    // 依 reviver 的父物件保存原始詞形；巢狀數字不得污染頂層欄位。
    body = JSON.parse(raw, function (name, value, context) {
      if (['version', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(name) && typeof value === 'number') {
        let fields = integerSyntax.get(this);
        if (!fields) { fields = {}; integerSyntax.set(this, fields); }
        fields[name] = context.source;
      }
      return value;
    });
  }
  catch { throw new ApiError('E0005'); }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw fieldError('_body', 'Request Body 必須為 JSON object');
  }
  const errors = Object.create(null);
  const result = {};
  for (const name of fields) {
    const value = body[name];
    if (!Object.hasOwn(body, name)) errors[name] = [REQUIRED];
    else if (value === null) errors[name] = [NULL];
    else if (name === 'version') {
      if (typeof value !== 'number' || !/^-?(0|[1-9][0-9]*)$/.test(integerSyntax.get(body)?.[name] || '')) errors[name] = [TYPE];
      else if (!Number.isSafeInteger(value) || value < 1) errors[name] = [INVALID];
      else result[name] = value;
    } else if (['window_minutes', 'failure_threshold', 'lock_seconds'].includes(name)) {
      // 設定只接受 JSON 整數詞形；範圍錯誤與字串／小數型態錯誤分開回覆。
      const limit = name === 'window_minutes' ? 60 : name === 'failure_threshold' ? 100 : 86400;
      if (typeof value !== 'number' || !/^-?(0|[1-9][0-9]*)$/.test(integerSyntax.get(body)?.[name] || '')) errors[name] = [TYPE];
      else if (!Number.isSafeInteger(value) || value < 1 || value > limit) errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'enabled') {
      if (typeof value !== 'boolean') errors[name] = [TYPE];
      else result[name] = value;
    } else if (typeof value !== 'string') errors[name] = [TYPE];
    else if (name === 'email') {
      const normalized = value.replace(/^ +| +$/g, '').replace(/[A-Z]/g, c => c.toLowerCase());
      const local = normalized.split('@')[0];
      if (normalized.length > 254 || local.length > 64 || !EMAIL.test(normalized)) errors[name] = [INVALID];
      else result[name] = normalized;
    } else if (name === 'password') {
      if (!/^[A-Za-z0-9]{4,16}$/.test(value)) errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'nickname') {
      if (value.includes('\0') || [...value].length < 1 || [...value].length > 30 || /^[ \t\n\r\v\f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u.test(value)) errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'title' || name === 'body') {
      // 文字保留原樣；僅依共同空白集合、NUL 與 Unicode 碼點長度驗證。
      const limit = name === 'title' ? 200 : fields.includes('title') ? 20000 : 2000;
      if (value.includes('\0') || [...value].length < 1 || [...value].length > limit || /^[ \t\n\r\v\f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u.test(value)) errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'visibility') {
      if (value !== 'visible' && value !== 'hidden') errors[name] = [INVALID];
      else result[name] = value;
    } else if (name === 'status') {
      if (value !== 'active' && value !== 'disabled') errors[name] = [INVALID];
      else result[name] = value;
    }
  }
  for (const name of Object.keys(body)) if (!fields.includes(name)) errors[name] = ['不支援此欄位'];
  if (Object.keys(errors).length) throw new ApiError('E0001', Object.fromEntries(Object.entries(errors)));
  return result;
}

module.exports = { ApiError, fieldError, urlInteger, queryInput, getBody, bodyInput, INVALID };
