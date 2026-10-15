'use strict';

const INVALID = '欄位格式或範圍不正確';
const REQUIRED = '此欄位為必填';
const NULL = '此欄位不可為 null';
const TYPE = '欄位型態不正確';
const MAX_ID = 9007199254740991;
const EMAIL = /^[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/;
const BLANK = /^[ \t\n\r\v\f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;
const INTEGER_MARKER = Symbol('parsed integer');

// ApiError 保存共同錯誤碼與欄位細節，避免 NestJS 原生格式流出。
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

// queryInput 讀原始 Query 而非 NestJS 解析結果，保留重複與陣列錯誤。
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
  // 搜尋僅移除前後 U+0020 空格，並限制 254 個 Unicode 碼點，以維持跨後端搜尋契約。
  if (Object.hasOwn(values, 'filters[keyword]') && !errors['filters.keyword']) {
    const keyword = values['filters[keyword]'].replace(/^ +| +$/g, '');
    if ([...keyword].length > 254) errors['filters.keyword'] = [INVALID];
    else values['filters[keyword]'] = keyword;
  }
  if (Object.keys(errors).length) throw new ApiError('E0001', Object.fromEntries(Object.entries(errors)));
  return Object.fromEntries(Object.entries(values));
}

// getBody 拒絕 raw parser 已有界收集的 GET 本文，保留 API14 的驗證順序。
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
    // reviver 保留所有整數欄位的原始寫法；私有 Symbol 避免 object 偽造型態。
    body = JSON.parse(raw, function preserveInteger(name, value, context) {
      if (['version', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(name) && typeof value === 'number') return { [INTEGER_MARKER]: value, source: context.source };
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
    else if (['version', 'window_minutes', 'failure_threshold', 'lock_seconds'].includes(name)) {
      // JSON 整數須為十進位原始寫法，避免指數及超大數在 JS 轉換後誤收。
      if (!value || typeof value !== 'object' || !Object.hasOwn(value, INTEGER_MARKER)) errors[name] = [TYPE];
      else if (!/^-?(0|[1-9][0-9]*)$/.test(value.source)) errors[name] = [TYPE];
      else if (!Number.isSafeInteger(value[INTEGER_MARKER]) || value[INTEGER_MARKER] < 1 || value[INTEGER_MARKER] >
        ({ version: MAX_ID, window_minutes: 60, failure_threshold: 100, lock_seconds: 86400 })[name]) errors[name] = [INVALID];
      else result[name] = value[INTEGER_MARKER];
    }
    else if (name === 'enabled') {
      if (typeof value !== 'boolean') errors[name] = [TYPE];
      else result[name] = value;
    }
    else if (typeof value !== 'string') errors[name] = [TYPE];
    else if (name === 'email') {
      // 只轉 ASCII 大寫，避免 Unicode 大小寫規則改變跨後端帳號比對。
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
  for (const name of Object.keys(body)) if (!fields.includes(name)) errors[name] = ['不支援此欄位'];
  if (Object.keys(errors).length) throw new ApiError('E0001', Object.fromEntries(Object.entries(errors)));
  return result;
}

module.exports = { ApiError, fieldError, urlInteger, queryInput, getBody, bodyInput };
