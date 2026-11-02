"""Django 的帳號、內容與管理 HTTP 流程，共用驗證、授權及錯誤邊界。"""

import logging
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import unquote_plus, unquote_to_bytes

import psycopg
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .core import (
    ApiError, DECIMAL, INVALID, NULL, PASSWORD, REQUIRED, SPACE, TYPE,
    account_resource, authenticate, b64url, business_time, connect,
    content_resource, failure, issue_token, normalize_email, password_hash,
    reject_non_json_number, success, valid_email, valid_nickname, verify_password,
)

LOGGER = logging.getLogger("site_api")
ARTICLE_COLUMNS = (
    "articles.id, articles.author_id, articles.title, articles.body, "
    "articles.visibility, articles.restore_control, articles.version, "
    "articles.created_backend, articles.created_at, articles.updated_at, accounts.nickname"
)
COMMENT_COLUMNS = (
    "comments.id, comments.author_id, comments.article_id, comments.body, "
    "comments.visibility, comments.restore_control, comments.version, "
    "comments.created_backend, comments.created_at, comments.updated_at, accounts.nickname"
)


# 對每次 API 呼叫統一處理方法、CORS、log_id 與安全錯誤格式。
def api(method):
    # 包裝各端點，集中記錄 IN/EX/OUT 而不記錄密碼、token 或 Body。
    def decorate(operation):
        # 捕捉業務與系統錯誤，並保持同一 log_id 供伺服器追查。
        @csrf_exempt
        def wrapped(request, *args, **kwargs):
            log_id = str(uuid.uuid4())
            request.api_log_id = log_id
            origin = request.headers.get("Origin")
            LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.path)
            try:
                # 同一路徑可共用 GET 與寫入方法；其餘方法仍回共同 405 格式。
                methods = (method,) if isinstance(method, str) else method
                if request.method == "OPTIONS":
                    response = HttpResponse(status=204)
                    response["Access-Control-Allow-Methods"] = (
                        "GET, POST, PUT, DELETE" if method == "*" else ", ".join(methods)
                    ) + ", OPTIONS"
                    response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
                elif method != "*" and request.method not in methods:
                    error = ApiError("E0005")
                    response = failure(error, log_id)
                    response.status_code = 405
                else:
                    response = operation(request, *args, **kwargs)
            except ApiError as error:
                # 毀損 PHC 等認證設定異常只記固定代碼與 log_id，不記密碼或雜湊。
                if error.code == "E0104":
                    LOGGER.error("ERROR log_id=%s code=E0104", log_id)
                response = failure(error, log_id)
            except Exception:
                LOGGER.exception("ERROR log_id=%s code=E9999", log_id)
                response = failure(ApiError("E9999"), log_id)
            # 只有固定前端入口可讀跨來源回應；倒數秒數由同一 Retry-After 標頭提供。
            if origin == "http://localhost:8080":
                response["Access-Control-Allow-Origin"] = origin
                response["Vary"] = "Origin"
                response["Access-Control-Expose-Headers"] = "Retry-After"
            # 業務錯誤也要留下同次 EX／OUT，讓前端回報的 log_id 可追查。
            if response.status_code >= 400:
                LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
            LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
            return response
        return wrapped
    return decorate


# 健康檢查要確認共用業務表可讀，避免資料庫失聯時 Compose 仍宣稱就緒。
def health(request):
    try:
        with connect() as connection:
            connection.execute(
                "SELECT 1 FROM accounts, login_sessions, login_failures, "
                "login_lockout_settings, articles, comments LIMIT 0"
            )
    except psycopg.Error:
        return HttpResponse("unavailable", status=503)
    return HttpResponse("OK")


# 先解析原始 Query 鍵，保留重複與不支援欄位；非法 UTF-8 值不能被替代字元吞掉。
def query_values(request, allowed):
    raw = request.META.get("QUERY_STRING", "")
    values = {}
    errors = {}
    seen = set()
    for pair in raw.split("&") if raw else ():
        encoded_name, _, encoded_value = pair.partition("=")
        # 合法 U+FFFD 保留原名；非法 UTF-8 另存 bytes 身分，不與字面百分比名稱混同。
        try:
            name = unquote_plus(encoded_name, errors="strict")
            invalid_name = None
        except UnicodeDecodeError:
            name = encoded_name.upper()
            invalid_name = unquote_to_bytes(encoded_name.replace("+", " "))
        # 陣列式鍵先對回原欄位，才能回型態錯誤或與單值鍵的重複錯誤。
        array_match = re.match(r"(page|per_page|filters\[(?:scope|keyword)\])\[", name)
        canonical = array_match.group(1) if array_match else name
        label = re.sub(r"^filters\[([^]]+)\]$", r"filters.\1", canonical)
        identity = canonical if invalid_name is None else invalid_name
        # 真重複優先；不同身分撞同一 errors 名稱時，後來的未知欄位不能蓋掉已判定的重複。
        if identity in seen:
            errors[label] = ["此參數不可重複"]
        elif label not in errors:
            if array_match and canonical in allowed:
                errors[label] = [TYPE]
            elif canonical not in allowed:
                errors[label] = ["不支援此欄位"]
        seen.add(identity)
        # 值採嚴格 UTF-8；遇到非法位元組保留欄位錯誤，不交給資料庫搜尋。
        try:
            values[canonical] = unquote_plus(encoded_value, errors="strict")
        except UnicodeDecodeError:
            values[canonical] = ""
            if canonical in allowed and label not in errors:
                errors[label] = [INVALID]
    return values, errors


# 只把明確的 filters[scope]=management 當管理請求，先執行角色門禁。
def management_requested(request):
    for pair in request.META.get("QUERY_STRING", "").split("&"):
        name, _, value = pair.partition("=")
        if unquote_plus(name) == "filters[scope]" and unquote_plus(value) == "management":
            return True
    return False


# 無 Query 的端點也拒絕未知與重複參數，避免框架忽略多餘輸入。
def no_query(request):
    _, errors = query_values(request, set())
    if errors:
        raise ApiError("E0001", errors)


# GET 的非空 Body 一律拒絕，避免 Content-Type 或框架預設改變共同驗證順序。
def no_get_body(request):
    if request.body:
        raise ApiError("E0001", {"_body": ["此 API 不接受 Request Body"]})


# URL 整數接受前導零，拒絕符號、空白及超過安全整數的值。
def url_integer(value, field):
    digits = value.lstrip("0")
    if (
        DECIMAL.fullmatch(value) is None or not digits
        or len(digits) > 16
        or (len(digits) == 16 and digits > "9007199254740991")
    ):
        raise ApiError("E0001", {field: [INVALID]})
    return int(digits)


# 為極長 JSON 整數保留來源型態，避免與真正的 JSON 字串混淆。
class OversizedJsonInteger(str):
    pass


# 只轉換最多 19 位的整數；更長的數字用固定標記避免 Python 字數上限與巨量轉換。
def parse_json_integer(value):
    if len(value.lstrip("-")) > 19:
        return OversizedJsonInteger("")
    return int(value)


# 解析 JSON object 後彙整所有欄位；每欄依必填、null、型態、格式順序只回第一個錯誤。
def json_body(request, fields, empty_allowed=False, content_limits=None, field_types=None):
    if empty_allowed and not request.body:
        return {}
    if not request.body or not request.content_type.lower().startswith("application/json"):
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    try:
        import json
        # bytes 直接給 json.loads 會自動接受 UTF-16／32；共用 API 只接受 UTF-8。
        decoded = request.body.decode("utf-8")
        # 解析前計算原始容器深度；同名鍵被覆蓋後也不能抹掉過深的舊值。
        depth = 0
        quoted = False
        escaped = False
        for character in decoded:
            # 字串內的括號不是結構；反斜線會跳過下一字元，含跳脫引號。
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
            elif character == '"':
                quoted = True
            elif character in "[{":
                depth += 1
                if depth >= 512:
                    raise ApiError("E0005")
            elif character in "]}":
                depth -= 1
        body = json.loads(decoded, parse_int=parse_json_integer, parse_constant=reject_non_json_number)
        # PHP 等後端把不成對的 UTF-16 surrogate 視為解析錯誤。
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise ApiError("E0005") from None
    if not isinstance(body, dict):
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    errors = {}
    field_types = field_types or {}
    # JSON 整數不因 Python／其他 runtime 的表示寬度改變型態；數值界線由欄位契約判斷。
    for field in fields:
        if field not in body:
            errors[field] = [REQUIRED]
        elif body[field] is None:
            errors[field] = [NULL]
        elif type(body[field]) is OversizedJsonInteger:
            errors[field] = [INVALID if field == "version" or field_types.get(field) is int else TYPE]
        elif type(body[field]) is not field_types.get(
            field, int if field == "version" and content_limits is not None else str
        ):
            errors[field] = [TYPE]
    # 未知欄位與已知欄位錯誤一起回報；只對通過型態檢查的帳號欄位套格式驗證。
    for field in body:
        if field not in fields:
            errors[field] = ["不支援此欄位"]
    if "email" in fields and "email" not in errors:
        body["email"] = normalize_email(body["email"])
        if not valid_email(body["email"]):
            errors["email"] = [INVALID]
    if "password" in fields and "password" not in errors and PASSWORD.fullmatch(body["password"]) is None:
        errors["password"] = [INVALID]
    if "nickname" in fields and "nickname" not in errors and not valid_nickname(body["nickname"]):
        errors["nickname"] = [INVALID]
    # 內容欄位保留原文；長度用 Python Unicode 碼點，NUL 在進入 PostgreSQL 前拒絕。
    if content_limits is not None:
        for field, limit in content_limits.items():
            if field in errors:
                continue
            value = body[field]
            if field == "version":
                valid = 1 <= value <= 9007199254740991
            elif field == "visibility":
                valid = value in ("visible", "hidden")
            elif field == "status":
                valid = value in ("active", "disabled")
            elif type(value) is int:
                valid = 1 <= value <= limit
            else:
                valid = 1 <= len(value) <= limit and "\0" not in value and any(character not in SPACE for character in value)
            if not valid:
                errors[field] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    return body


# 寫入前依帳號→登入紀錄取得列鎖並重驗撤銷，讓登出與內容寫入有明確提交順序。
def authenticate_write(request, connection):
    account, claims = authenticate(request, connection)
    connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
    connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
    # 等待鎖期間可能已被停用或登出，不能直接沿用鎖前查到的身分。
    return authenticate(request, connection)[0]


# 已持有內容列鎖時先比版本；同值不動時間，變更時仍以 version 條件更新。
def save_content(connection, table, row, version, changes):
    if row["version"] != version:
        raise ApiError("E0201")
    if all(row[key] == value for key, value in changes.items()):
        return
    columns = list(changes)
    assignments = ", ".join(f"{column} = %s" for column in columns)
    result = connection.execute(
        f"UPDATE {table} SET {assignments}, version = version + 1, updated_at = clock_timestamp() "
        "WHERE id = %s AND version = %s",
        (*[changes[column] for column in columns], row["id"], version),
    )
    if result.rowcount != 1:
        raise ApiError("E0201")


# 下架權由實際操作人決定；管理員接管他人內容時即使已隱藏也算變更。
def visibility_changes(row, viewer, visibility):
    own = viewer["id"] == row["author_id"]
    if visibility == "visible" and row["visibility"] == "hidden" and viewer["role"] != "admin" and row["restore_control"] != "author":
        raise ApiError("E0003")
    control = None if visibility == "visible" else row["restore_control"] or "author"
    if visibility == "hidden" and viewer["role"] == "admin" and not own:
        control = "admin"
    return {"visibility": visibility, "restore_control": control}


# 列表先驗 Body 與 Query，會員關鍵字只裁切 ASCII 空格；管理權門禁由呼叫端先處理。
def list_input(request, allow_scope=False, allow_keyword=False):
    no_get_body(request)
    allowed = {"page", "per_page"}
    if allow_scope:
        allowed.add("filters[scope]")
    if allow_keyword:
        allowed.add("filters[keyword]")
    values, errors = query_values(request, allowed)
    numbers = {}
    # 兩個分頁欄位都驗完才回錯誤，避免第一欄錯誤遮蔽其他欄位的必填或範圍問題。
    for name in ("page", "per_page"):
        if name not in values:
            errors[name] = [REQUIRED]
        elif name not in errors:
            try:
                number = url_integer(values[name], name)
            except ApiError:
                errors[name] = [INVALID]
            else:
                if name == "per_page" and number > 100:
                    errors[name] = [INVALID]
                else:
                    numbers[name] = number
    if allow_scope and "filters[scope]" in values and "filters.scope" not in errors and values["filters[scope]"] != "management":
        errors["filters.scope"] = [INVALID]
    if allow_keyword and "filters[keyword]" in values and "filters.keyword" not in errors:
        values["filters[keyword]"] = values["filters[keyword]"].strip(" ")
        if len(values["filters[keyword]"]) > 254:
            errors["filters.keyword"] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    # 沿用已驗證數值，避免大量前導零在原字串二次轉換時觸發 Python 上限。
    return numbers["page"], numbers["per_page"], values.get("filters[keyword]" if allow_keyword else "filters[scope]")


# 精確總數與原頁碼組成共同分頁格式，超頁仍保留 total。
def pagination(page, per_page, total):
    return {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    }


# 註冊先驗欄位再檢查唯一 Email；DB unique index 裁決跨後端並行。
@api("POST")
def register(request):
    # 公開註冊不沿用舊 JWT；格式錯誤必須先於 Email 唯一性檢查。
    no_query(request)
    body = json_body(request, ("email", "password", "nickname"))
    with connect() as connection:
        if connection.execute("SELECT 1 FROM accounts WHERE email = %s", (body["email"],)).fetchone():
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]})
        encoded = password_hash(body["password"])
        try:
            # 前置查重不能阻止其他後端同時新增；唯一索引的衝突仍轉為共同 Email 文案。
            connection.execute(
                "INSERT INTO accounts (email, password_hash, nickname, role, status) "
                "VALUES (%s, %s, %s, 'member', 'active')",
                (body["email"], encoded, body["nickname"]),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]}) from exc
    return success(status=201)


# 先完成登入交易才計算鎖定剩餘秒數，避免雜湊或 COMMIT 等待讓 Retry-After 多算。
def locked_response(connection, until, log_id):
    connection.commit()
    # 新 DB 時間只供回應倒數；外層 with 之後只需結束這個讀取交易，不重算登入業務時間。
    now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    retry = max(1, -((now - until) // timedelta(seconds=1)))
    return failure(ApiError("E0105", retry_after=retry), log_id)


# 登入在設定列→帳號列鎖後取 DB 時間，序列化跨後端失敗計數。
@api("POST")
def login(request):
    # 格式錯誤不屬錯誤密碼事件，先完整驗證才進入共用鎖定交易。
    no_query(request)
    body = json_body(request, ("email", "password"))
    with connect() as connection:
        settings = connection.execute("SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE").fetchone()
        if settings is None:
            raise ApiError("E0104")
        account = connection.execute(
            "SELECT * FROM accounts WHERE email = %s FOR UPDATE", (body["email"],)
        ).fetchone()
        # 鎖後只取一次業務時間 t；帳號不存在及停用先判定，不進入計數或驗密碼。
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if account is None:
            return failure(ApiError("E0101"), request.api_log_id)
        if account["status"] == "disabled":
            return failure(ApiError("E0102"), request.api_log_id)
        # 到期鎖與關閉的鎖先清除並換世代；未到期鎖不驗密碼也不延長。
        if account["role"] == "member" and account["locked_until"] is not None:
            until = account["locked_until"]
            if settings["enabled"] and now < until:
                return locked_response(connection, until, request.api_log_id)
            account["failure_epoch"] += 1
            connection.execute(
                "UPDATE accounts SET failure_epoch = %s, locked_until = NULL WHERE id = %s",
                (account["failure_epoch"], account["id"]),
            )
        if not verify_password(body["password"], account["password_hash"]):
            if settings["enabled"] and account["role"] == "member":
                # 只計目前世代的 (t-N, t]；門檻當次先持久化，再回 429。
                connection.execute(
                    "INSERT INTO login_failures (account_id, failure_epoch, occurred_at) VALUES (%s, %s, %s)",
                    (account["id"], account["failure_epoch"], now),
                )
                count = connection.execute(
                    "SELECT count(*) FROM login_failures WHERE account_id = %s AND failure_epoch = %s "
                    "AND occurred_at > %s AND occurred_at <= %s",
                    (account["id"], account["failure_epoch"], now - timedelta(minutes=settings["window_minutes"]), now),
                ).fetchone()["count"]
                if count >= settings["failure_threshold"]:
                    until = now + timedelta(seconds=settings["lock_seconds"])
                    connection.execute("UPDATE accounts SET locked_until = %s WHERE id = %s", (until, account["id"]))
                    return locked_response(connection, until, request.api_log_id)
            # 正常 return 會讓 with 提交失敗事件；若拋業務例外反而會回滾計數。
            return failure(ApiError("E0101"), request.api_log_id)
        # 成功時換世代並保存與 JWT 同秒數的登入紀錄；金鑰失敗會回滾。
        issued = int(now.timestamp())
        jti = b64url(secrets.token_bytes(16))
        token = issue_token(account["id"], jti, issued)
        connection.execute(
            "UPDATE accounts SET failure_epoch = failure_epoch + 1, locked_until = NULL WHERE id = %s",
            (account["id"],),
        )
        connection.execute(
            "INSERT INTO login_sessions (jti, account_id, backend_id, issued_at, expires_at) "
            "VALUES (%s, %s, 'django', to_timestamp(%s), to_timestamp(%s))",
            (jti, account["id"], issued, issued + 28800),
        )
        data = {
            "access_token": token, "token_type": "Bearer",
            "expires_at": business_time(now.replace(microsecond=0) + timedelta(hours=8)),
            "user": account_resource(account),
        }
    return success(data)


# 登出只撤銷目前 jti；帳號與 session 依共同順序鎖定到提交。
@api("POST")
def logout(request):
    with connect() as connection:
        identity = authenticate(request, connection)
        no_query(request)
        json_body(request, (), empty_allowed=True)
        account, claims = identity
        # 鎖後重驗，讓登出與其他受保護寫入依同一帳號→session 順序完成。
        connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        authenticate(request, connection)
        connection.execute(
            "UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE jti = %s", (claims["jti"],)
        )
    return success()


# me 以目前資料庫角色與狀態回應，不建立登入或延長到期。
@api("GET")
def me(request):
    with connect() as connection:
        account, _ = authenticate(request, connection)
        no_get_body(request)
        no_query(request)
        return success(account_resource(account))


# GET 預設只見公開內容，POST 在認證鎖保護下建立作者文章。
@api(("GET", "POST"))
def articles(request):
    if request.method == "POST":
        with connect() as connection:
            viewer = authenticate_write(request, connection)
            no_query(request)
            body = json_body(request, ("title", "body"), content_limits={"title": 200, "body": 20000})
            # 資料庫單次時鐘同時填入建立與更新時間，來源固定為本後端。
            connection.execute(
                "INSERT INTO articles (author_id, title, body, visibility, restore_control, version, "
                "created_backend, created_at, updated_at) "
                "SELECT %s, %s, %s, 'visible', NULL, 1, 'django', now, now "
                "FROM (SELECT clock_timestamp() AS now) AS stamp",
                (viewer["id"], body["title"], body["body"]),
            )
        return success(status=201)
    with connect() as connection:
        management = management_requested(request)
        identity = authenticate(request, connection, required=management)
        viewer = identity[0] if identity else None
        if management and viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, scope = list_input(request, allow_scope=True)
        if scope == "management" and (viewer is None or viewer["role"] != "admin"):
            raise ApiError("E0003")
        where = "" if scope == "management" else "WHERE articles.visibility = 'visible'"
        from_sql = "FROM articles JOIN accounts ON accounts.id = articles.author_id " + where
        total = connection.execute("SELECT count(*) " + from_sql).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + ARTICLE_COLUMNS + " " + from_sql
            + " ORDER BY articles.created_at DESC, articles.id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        ).fetchall()
        return success({
            "list": [content_resource(row, viewer, "article") for row in rows],
            "pagination": pagination(page, per_page, total),
        })


# GET 隱藏無權讀取的下架文章；PUT 在同一列鎖內處理作者編修。
@api(("GET", "PUT"))
def article(request, article_id):
    if request.method == "PUT":
        return mutate_article(request, article_id, visibility=False)
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        no_get_body(request)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        row = connection.execute(
            "SELECT " + ARTICLE_COLUMNS
            + " FROM articles JOIN accounts ON accounts.id = articles.author_id WHERE articles.id = %s",
            (content_id,),
        ).fetchone()
        if row is None or (
            row["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != row["author_id"]))
        ):
            raise ApiError("E0004")
        return success(content_resource(row, viewer, "article"))


# GET 先確認父文章可讀；POST 鎖同一父文章以和下架序列化。
@api(("GET", "POST"))
def comments(request, article_id):
    if request.method == "POST":
        with connect() as connection:
            viewer = authenticate_write(request, connection)
            no_query(request)
            content_id = url_integer(article_id, "article_id")
            body = json_body(request, ("body",), content_limits={"body": 2000})
            parent = connection.execute(
                "SELECT author_id, visibility FROM articles WHERE id = %s FOR UPDATE", (content_id,)
            ).fetchone()
            if parent is None or (parent["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != parent["author_id"]):
                raise ApiError("E0004")
            if parent["visibility"] == "hidden":
                raise ApiError("E0003")
            # 作者與來源從已重驗的登入及本後端取得；同一 DB 時刻填兩個時間欄位。
            connection.execute(
                "INSERT INTO comments (article_id, author_id, body, visibility, restore_control, version, "
                "created_backend, created_at, updated_at) "
                "SELECT %s, %s, %s, 'visible', NULL, 1, 'django', now, now "
                "FROM (SELECT clock_timestamp() AS now) AS stamp",
                (content_id, viewer["id"], body["body"]),
            )
        return success(status=201)
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        page, per_page, _ = list_input(request)
        content_id = url_integer(article_id, "article_id")
        parent = connection.execute(
            "SELECT author_id, visibility FROM articles WHERE id = %s", (content_id,)
        ).fetchone()
        if parent is None or (
            parent["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != parent["author_id"]))
        ):
            raise ApiError("E0004")
        where = "WHERE comments.article_id = %s"
        if viewer is None or viewer["role"] != "admin":
            where += " AND comments.visibility = 'visible'"
        from_sql = "FROM comments JOIN accounts ON accounts.id = comments.author_id " + where
        total = connection.execute("SELECT count(*) " + from_sql, (content_id,)).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + COMMENT_COLUMNS + " " + from_sql
            + " ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s",
            (content_id, per_page, offset),
        ).fetchall()
        return success({
            "list": [content_resource(row, viewer, "comment") for row in rows],
            "pagination": pagination(page, per_page, total),
        })


# 個人列表只由已驗證帳號限定作者，包含個別及父文章的下架內容。
@api("GET")
def my_articles(request):
    return my_content(request, "articles", ARTICLE_COLUMNS, "article")


# 留言個人列表不讀父文章內文，故下架父文章也不洩漏其他資料。
@api("GET")
def my_comments(request):
    return my_content(request, "comments", COMMENT_COLUMNS, "comment")


# 共同分頁在資料庫精確計數與排序，避免全量載入及逐列查詢。
def my_content(request, table, columns, kind):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        page, per_page, _ = list_input(request)
        total = connection.execute(f"SELECT count(*) FROM {table} WHERE author_id = %s", (viewer["id"],)).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            f"SELECT {columns} FROM {table} JOIN accounts ON accounts.id = {table}.author_id "
            f"WHERE {table}.author_id = %s ORDER BY {table}.created_at DESC, {table}.id DESC "
            "LIMIT %s OFFSET %s", (viewer["id"], per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, kind) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 管理員只搜尋一般會員；SQL 同時負責字面包含、精確計數與排序分頁。
@api("GET")
def members(request):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, keyword = list_input(request, allow_keyword=True)
        # translate 僅折疊 ASCII，strpos 將百分號、底線及反斜線視為普通字元。
        folded = (keyword or "").translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        # PostgreSQL text 無法包含 NUL；此分支保留目前回空列表的既有處理。
        if "\0" in folded:
            return success({"list": [], "pagination": pagination(page, per_page, 0)})
        condition = "WHERE role = 'member'"
        params = ()
        if folded:
            condition += (
                " AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0"
                " OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)"
            )
            params = (folded, folded)
        total = connection.execute("SELECT count(*) FROM accounts " + condition, params).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT id, email, nickname, role, status, created_at FROM accounts " + condition
            + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s", (*params, per_page, offset),
        ).fetchall()
        return success({"list": [account_resource(row) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 會員詳情先驗管理員與輸入；管理員 ID 及不存在目標統一 404，避免透露管理帳號。
@api("GET")
def member(request, member_id):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        no_get_body(request)
        no_query(request)
        target_id = url_integer(member_id, "member_id")
        row = connection.execute(
            "SELECT id, email, nickname, role, status, created_at FROM accounts "
            "WHERE id = %s AND role = 'member'", (target_id,),
        ).fetchone()
        if row is None:
            raise ApiError("E0004")
        return success(account_resource(row))


# 狀態變更先驗權與輸入，寫入交易再依 ID 鎖帳號及 session 重驗權限。
@api("PUT")
def member_status(request, member_id):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        admin_id = viewer["id"]
    no_query(request)
    target_id = url_integer(member_id, "member_id")
    body = json_body(request, ("status",), content_limits={"status": 0})
    with connect() as connection:
        # 目標與管理員帳號一律依 ID 鎖定，防止停用、登入與寫入交錯。
        rows = connection.execute(
            "SELECT id, role, status FROM accounts WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
            (admin_id, target_id),
        ).fetchall()
        accounts = {row["id"]: row for row in rows}
        # 外層認證只決定是否可驗輸入；實際修改前仍須在帳號與 session 鎖內重驗。
        current, claims = authenticate(request, connection)
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        current, _ = authenticate(request, connection)
        if current["role"] != "admin":
            raise ApiError("E0003")
        target = accounts.get(target_id)
        if target is None or target["role"] != "member":
            raise ApiError("E0004")
        # 同值不重新撤銷；只有 active→disabled 的變更才同交易撤銷所有後端登入。
        if target["status"] != body["status"]:
            connection.execute("UPDATE accounts SET status = %s WHERE id = %s", (body["status"], target_id))
            if body["status"] == "disabled":
                # 帳號鎖阻止新登入；同一提交撤銷全部後端的既有 session。
                connection.execute(
                    "UPDATE login_sessions SET revoked_at = clock_timestamp() "
                    "WHERE account_id = %s AND revoked_at IS NULL", (target_id,),
                )
    return success()


# 管理員留言列表包含個別及父文章下架的留言，不藉文章狀態省略資料。
@api("GET")
def admin_comments(request):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, _ = list_input(request)
        total = connection.execute("SELECT count(*) FROM comments").fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + COMMENT_COLUMNS + " FROM comments JOIN accounts ON accounts.id = comments.author_id "
            "ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s", (per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, "comment") for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 管理員讀取或完整更新四個共用鎖定設定欄位；設定缺失屬認證設定錯誤。
@api(("GET", "PUT"))
def login_lockout_settings(request):
    if request.method == "GET":
        with connect() as connection:
            viewer, _ = authenticate(request, connection)
            if viewer["role"] != "admin":
                raise ApiError("E0003")
            no_get_body(request)
            no_query(request)
            row = connection.execute(
                "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
                "FROM login_lockout_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise ApiError("E0104")
            return success(row)
    # PUT 先驗角色再驗完整四欄，不能讓無權者先取得輸入或設定資訊。
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        if viewer["role"] != "admin":
            raise ApiError("E0003")
    no_query(request)
    fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
    body = json_body(
        request, fields, field_types={"enabled": bool, "window_minutes": int,
                                      "failure_threshold": int, "lock_seconds": int},
        content_limits={"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400},
    )
    with connect() as connection:
        # 登入也先鎖這筆設定；真正關閉時依序鎖全部帳號，最後重驗管理員 session。
        row = connection.execute(
            "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
            "FROM login_lockout_settings WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if row is None:
            raise ApiError("E0104")
        disabling = row["enabled"] and not body["enabled"]
        if disabling:
            connection.execute("SELECT id FROM accounts ORDER BY id FOR UPDATE").fetchall()
        current = authenticate_write(request, connection)
        if current["role"] != "admin":
            raise ApiError("E0003")
        # 最後提交的整組值為準；同值不更新，也不重做關閉鎖定的副作用。
        if any(row[field] != body[field] for field in fields):
            connection.execute(
                "UPDATE login_lockout_settings SET enabled = %s, window_minutes = %s, "
                "failure_threshold = %s, lock_seconds = %s WHERE id = 1",
                tuple(body[field] for field in fields),
            )
            if disabling:
                # 保留歷史失敗事件，但切換世代並解除會員目前的暫時鎖定。
                connection.execute(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                    "WHERE role = 'member'"
                )
    return success()


# 文章下架與留言寫入鎖同一文章列，授權、版本及恢復權在鎖內裁決。
@api("PUT")
def article_visibility(request, article_id):
    return mutate_article(request, article_id, visibility=True)


# 文章修改只允許作者；管理員只在下架操作可處理他人文章。
def mutate_article(request, article_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        fields = ("visibility", "version") if visibility else ("title", "body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"title": 200, "body": 20000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        row = connection.execute("SELECT * FROM articles WHERE id = %s FOR UPDATE", (content_id,)).fetchone()
        if row is None or (row["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != row["author_id"]):
            raise ApiError("E0004")
        if viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin"):
            raise ApiError("E0003")
        # 恢復權先於版本檢查，避免以 409 暴露無權操作的版本資訊。
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"title": body["title"], "body": body["body"]}
        save_content(connection, "articles", row, body["version"], changes)
    return success()


# 作者或管理員可獨立讀取留言；他人公開留言先判可讀再回操作權 403。
@api(("GET", "PUT"))
def comment(request, comment_id):
    if request.method == "PUT":
        return mutate_comment(request, comment_id, visibility=False)
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        no_get_body(request)
        no_query(request)
        content_id = url_integer(comment_id, "comment_id")
        row = connection.execute(
            "SELECT " + COMMENT_COLUMNS + ", articles.visibility AS article_visibility "
            "FROM comments JOIN accounts ON accounts.id = comments.author_id "
            "JOIN articles ON articles.id = comments.article_id WHERE comments.id = %s", (content_id,),
        ).fetchone()
        if row is None:
            raise ApiError("E0004")
        if viewer["role"] != "admin" and viewer["id"] != row["author_id"]:
            if row["visibility"] == "hidden" or row["article_visibility"] == "hidden":
                raise ApiError("E0004")
            raise ApiError("E0003")
        return success(content_resource(row, viewer, "comment"))


# 個別下架與恢復仍以目前作者或管理員權限判定，父文章下架不封鎖此操作。
@api("PUT")
def comment_visibility(request, comment_id):
    return mutate_comment(request, comment_id, visibility=True)


# 同一交易重驗登入與鎖定父文章、留言；內容編修須父文章目前公開。
def mutate_comment(request, comment_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(comment_id, "comment_id")
        fields = ("visibility", "version") if visibility else ("body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"body": 2000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        # 先不鎖留言地查父 ID，才能先鎖文章再鎖留言；文章列鎖使留言修改與文章下架依序判定。
        # 鎖定後複核 article_id，避免探索後的關聯變動影響授權與寫入。
        initial = connection.execute("SELECT article_id FROM comments WHERE id = %s", (content_id,)).fetchone()
        parent = None if initial is None else connection.execute(
            "SELECT id, visibility FROM articles WHERE id = %s FOR UPDATE", (initial["article_id"],)
        ).fetchone()
        row = None if parent is None else connection.execute(
            "SELECT * FROM comments WHERE id = %s FOR UPDATE", (content_id,)
        ).fetchone()
        if row is None or row["article_id"] != parent["id"] or (
            viewer["role"] != "admin" and viewer["id"] != row["author_id"]
            and (row["visibility"] == "hidden" or parent["visibility"] == "hidden")
        ):
            raise ApiError("E0004")
        if (viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin")) or (
            not visibility and parent["visibility"] == "hidden"
        ):
            raise ApiError("E0003")
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"body": body["body"]}
        save_content(connection, "comments", row, body["version"], changes)
    return success()


# 未知 API 固定回共同 404，避免落入 Django 的 HTML 錯誤回應。
@api("*")
def not_found(request):
    raise ApiError("E0004")
