"""Flask 的輸入、JWT 與共用資料庫操作。"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from flask import jsonify, make_response
from psycopg.rows import dict_row

from .dto import AccountDto, ArticleDto, CommentDto


MESSAGES = {
    "E0001": (422, "欄位資料不正確，請確認後再送出"),
    "E0002": (401, "登入已逾期，請重新登入"),
    "E0003": (403, "沒有權限使用此功能"),
    "E0004": (404, "找不到指定資料或 API"),
    "E0005": (400, "系統暫時無法處理，請稍後再試"),
    "E0101": (401, "帳號或密碼錯誤"),
    "E0102": (403, "此帳號已停用，請聯絡系統管理員"),
    "E0104": (500, "後台認證設定異常，請聯絡系統管理員"),
    "E0105": (429, "登入失敗次數過多，請稍後再試"),
    "E0201": (409, "內容已更新，請確認最新內容後再送出"),
    "E9999": (500, "系統暫時無法處理，請稍後再試"),
}
INVALID = "欄位格式或範圍不正確"
REQUIRED = "此欄位為必填"
NULL = "此欄位不可為 null"
TYPE = "欄位型態不正確"
# 空白集合依跨後端契約列舉，不能改用各語言定義不同的 strip／isspace。
SPACE = frozenset(chr(i) for i in range(9, 14)) | frozenset(
    "\u0020\u0085\u00a0\u1680\u2028\u2029\u202f\u205f\u3000"
) | frozenset(chr(i) for i in range(0x2000, 0x200b))
EMAIL = re.compile(r"[A-Za-z0-9_%+-]+(?:\.[A-Za-z0-9_%+-]+)*@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\Z")
PASSWORD = re.compile(r"[A-Za-z0-9]{4,16}\Z")
PHC = re.compile(r"\$argon2id\$v=19\$m=19456,t=2,p=1\$([A-Za-z0-9+/]{22})\$([A-Za-z0-9+/]{43})\Z")
TOKEN_PART = re.compile(r"[A-Za-z0-9_-]+\Z")
DECIMAL = re.compile(r"[0-9]+\Z")
HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)
TAIPEI = ZoneInfo("Asia/Taipei")


# 明確保存業務錯誤，讓 HTTP 入口建立逐字一致的安全回應。
class ApiError(Exception):
    # 儲存精確錯誤碼與可選欄位訊息，由同一回應函式序列化。
    def __init__(self, code, errors=None, retry_after=None):
        super().__init__(code)
        self.code = code
        self.errors = errors
        self.retry_after = retry_after


# 統一產生成功 envelope，避免框架預設序列化欄位。
def success(data=None, status=200):
    return make_response(jsonify({"success": True, "message": "OK", "data": {} if data is None else data}), status)


# 統一產生失敗 envelope；欄位錯誤與鎖定標頭分別按契約輸出。
def failure(error, log_id):
    status, message = MESSAGES[error.code]
    body = {"success": False, "message": message, "error_code": error.code}
    if error.errors is not None:
        body["errors"] = error.errors
    body["log_id"] = log_id
    response = make_response(jsonify(body), status)
    if error.retry_after is not None:
        response.headers["Retry-After"] = str(error.retry_after)
    return response


# 取得共用資料庫連線並設定時區；每個請求用短連線避免交易殘留。
def connect():
    connection = psycopg.connect(
        host=os.environ.get("DB_HOST", "db"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_DATABASE", "articles"),
        user=os.environ.get("DB_USERNAME", "articles_app"),
        password=os.environ.get("DB_PASSWORD", ""),
        row_factory=dict_row,
    )
    connection.execute("SET TIME ZONE 'Asia/Taipei'")
    connection.commit()
    return connection


# 依已核准規則僅裁切 ASCII 空格及折疊 ASCII 大寫。
def normalize_email(value):
    return value.strip(" ").translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


# 驗證 Email 簡化格式；字串長度與 local part 各自有獨立上限。
def valid_email(value):
    return len(value) <= 254 and EMAIL.fullmatch(value) is not None and len(value.split("@", 1)[0]) <= 64


# 固定 Unicode 空白集合與 NUL 規則，保持跨語言暱稱判斷一致。
def valid_nickname(value):
    return 1 <= len(value) <= 30 and "\0" not in value and any(character not in SPACE for character in value)


# 寫入原生 Argon2id PHC，讓其他後端可用同一格式驗密碼。
def password_hash(password):
    return HASHER.hash(password)


# 先驗 PHC 的成本與正規 Base64，再交給 Argon2id 驗證，毀損不計失敗。
def verify_password(password, encoded):
    # 先限制成本與正規 PHC 表示，避免把損壞或不支援的儲存格式當成登入失敗。
    match = PHC.fullmatch(encoded)
    if match is None:
        raise ApiError("E0104")
    for text, size in zip(match.groups(), (16, 32)):
        try:
            raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
        except binascii.Error as exc:
            raise ApiError("E0104") from exc
        if len(raw) != size or base64.b64encode(raw).decode().rstrip("=") != text:
            raise ApiError("E0104")
    # 密碼不符才回 False；雜湊驗證器異常仍交由共同認證設定錯誤處理。
    try:
        return HASHER.verify(encoded, password)
    except VerifyMismatchError:
        return False
    except (InvalidHashError, VerificationError) as exc:
        raise ApiError("E0104") from exc


# 將位元組轉成 JWT 與 jti 使用的無 padding base64url。
def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


# 拒絕非正規 JWT 區段，避免相同憑證出現多種字串表示。
def unb64url(value):
    if not isinstance(value, str) or TOKEN_PART.fullmatch(value) is None:
        raise ApiError("E0002")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ApiError("E0002") from exc
    if b64url(raw) != value:
        raise ApiError("E0002")
    return raw


# 僅讀已持久保存的 Flask 金鑰；缺漏或格式錯誤不降級。
def jwt_key():
    try:
        with open("/run/secrets/flask-jwt.key", "rb") as source:
            encoded = source.read().rstrip(b"\n")
        key = base64.b64decode(encoded, validate=True)
    except (OSError, binascii.Error) as exc:
        raise ApiError("E0104") from exc
    if len(key) != 32 or base64.b64encode(key) != encoded:
        raise ApiError("E0104")
    return key


# 用固定 HS256、issuer、audience 與八小時整數秒 claims 簽發本後端 JWT。
def issue_token(account_id, jti, issued):
    header = b64url(b'{"alg":"HS256","typ":"JWT"}')
    claims = {
        "iss": "ai-workflow-fullstack:flask",
        "aud": "ai-workflow-fullstack-api:flask",
        "sub": str(account_id),
        "jti": jti,
        "iat": issued,
        "nbf": issued,
        "exp": issued + 28800,
    }
    # 簽名只使用固定 header 與後端產生的 claims，不把角色或帳密放進憑證。
    payload = b64url(json.dumps(claims, separators=(",", ":"), ensure_ascii=True).encode())
    signed = header + "." + payload
    return signed + "." + b64url(hmac.new(jwt_key(), signed.encode(), hashlib.sha256).digest())


# JWT JSON 不接受重複欄位，防止不同解析器對簽名內容解讀不同。
def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


# JSON 標準不包含 NaN／Infinity；拒絕後再核對 JWT claims。
def reject_non_json_number(value):
    raise ValueError("invalid JSON number")


# 驗證固定 header、簽章、claims 型態與嚴格期限，再交由 DB 驗身分。
def verify_token(token):
    # 固定本後端金鑰與 HS256；先驗簽章，不依外來 header 選擇演算法或讀取外部金鑰。
    key = jwt_key()
    parts = token.split(".")
    if len(parts) != 3:
        raise ApiError("E0002")
    signed = (parts[0] + "." + parts[1]).encode()
    if not hmac.compare_digest(hmac.new(key, signed, hashlib.sha256).digest(), unb64url(parts[2])):
        raise ApiError("E0002")
    # JWT 的重複欄位拒絕僅適用憑證，不改變一般 Request Body 的 JSON 解析規則。
    try:
        header = json.loads(unb64url(parts[0]), object_pairs_hook=unique_object, parse_constant=reject_non_json_number)
        claims = json.loads(unb64url(parts[1]), object_pairs_hook=unique_object, parse_constant=reject_non_json_number)
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ApiError("E0002") from exc
    if header != {"alg": "HS256", "typ": "JWT"} or not isinstance(claims, dict):
        raise ApiError("E0002")
    # 型態與欄位關係先成立，才可安全比較固定八小時且無寬限的有效期限。
    subject = claims.get("sub")
    jti = claims.get("jti")
    issued = claims.get("iat")
    if (
        claims.get("iss") != "ai-workflow-fullstack:flask"
        or claims.get("aud") != "ai-workflow-fullstack-api:flask"
        or not isinstance(subject, str)
        or re.fullmatch(r"[1-9][0-9]*", subject) is None
        or len(subject) > 16
        or int(subject) > 9007199254740991
        or not isinstance(jti, str)
        or len(unb64url(jti)) != 16
        or type(issued) is not int
        or issued < 0
        or type(claims.get("nbf")) is not int
        or claims["nbf"] != issued
        or type(claims.get("exp")) is not int
        or claims["exp"] != issued + 28800
    ):
        raise ApiError("E0002")
    now = int(datetime.now(timezone.utc).timestamp())
    if now < issued or now >= claims["exp"]:
        raise ApiError("E0002")
    return claims


# 先核對憑證的簽章與型態，再依目前帳號狀態及登入紀錄授權。
def authenticate(request, connection, required=True):
    authorization = request.headers.get("Authorization")
    if authorization is None and not required:
        return None
    match = re.fullmatch(r"Bearer ([^\s]+)", authorization or "", flags=re.IGNORECASE)
    if match is None:
        raise ApiError("E0002")
    # 只有 JWT 身分可驗證後才查共用資料；DB 無法查核時由入口回系統錯誤，不能放行。
    claims = verify_token(match.group(1))
    account = connection.execute(
        "SELECT id, email, nickname, role, status, created_at FROM accounts WHERE id = %s",
        (int(claims["sub"]),),
    ).fetchone()
    if account is None:
        raise ApiError("E0002")
    session = connection.execute(
        "SELECT account_id, backend_id, issued_at, expires_at, revoked_at FROM login_sessions WHERE jti = %s",
        (claims["jti"],),
    ).fetchone()
    # 精確比較含微秒的 DB 時刻，避免 int(timestamp()) 截斷後接受偏移紀錄。
    if (
        session is None
        or session["account_id"] != account["id"]
        or session["backend_id"] != "flask"
        or session["issued_at"] != datetime.fromtimestamp(claims["iat"], timezone.utc)
        or session["expires_at"] != datetime.fromtimestamp(claims["exp"], timezone.utc)
    ):
        raise ApiError("E0002")
    # 已確認同一登入身分後，停用優先於撤銷；恢復帳號仍不會復活原登入。
    if account["status"] == "disabled":
        raise ApiError("E0102")
    if session["revoked_at"] is not None:
        raise ApiError("E0002")
    return account, claims


# 寫入交易依帳號、session 順序鎖列再重驗，與登出及停用明確排序。
def authenticate_write(request, connection):
    account, claims = authenticate(request, connection)
    # 等待期間可能已登出或停用，取得相容列鎖後必須重新讀取，不沿用外層結果。
    connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
    connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
    # 呼叫端在同一連線交易繼續鎖內容；離開 context 才提交或因例外回滾並釋放所有列鎖。
    return authenticate(request, connection)[0]


# 統一輸出台北業務時間，避免資料庫連線時區差異。
def business_time(value):
    return value.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")


# 只輸出目前帳號允許的欄位，角色與狀態採資料庫當下值。
def account_resource(account) -> AccountDto:
    return {key: account[key] for key in ("id", "email", "nickname", "role", "status")} | {
        "created_at": business_time(account["created_at"])
    }


# 依可見性與恢復控制者計算布林值，不把內部欄位洩漏到 API。
def content_resource(row, viewer, kind) -> ArticleDto | CommentDto:
    # 呼叫端先裁決可讀性；只輸出公開作者欄位及同形資源，不洩漏 restore_control。
    resource = {
        "id": row["id"],
        "author": {"id": row["author_id"], "nickname": row["nickname"]},
        "version": row["version"],
        "created_backend": row["created_backend"],
        "created_at": business_time(row["created_at"]),
        "updated_at": business_time(row["updated_at"]),
        "visibility": row["visibility"],
        # 此旗標只描述該筆恢復資格；留言的父文章仍可下架，恢復不會使父文對外公開。
        "can_restore": row["visibility"] == "hidden" and viewer is not None and (
            viewer["role"] == "admin"
            or (viewer["id"] == row["author_id"] and row["restore_control"] == "author")
        ),
    }
    if kind == "article":
        resource.update(title=row["title"], body=row["body"])
    else:
        resource.update(article_id=row["article_id"], body=row["body"])
    return resource
