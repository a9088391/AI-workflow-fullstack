"""Ticket 16 的七個共用 API 操作與 HTTP 錯誤邊界。"""

import logging
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import unquote_plus

import psycopg
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .core import (
    ApiError, DECIMAL, INVALID, NULL, PASSWORD, REQUIRED, TYPE,
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
                if request.method == "OPTIONS":
                    response = HttpResponse(status=204)
                    response["Access-Control-Allow-Methods"] = (
                        "GET, POST, PUT, DELETE" if method == "*" else method
                    ) + ", OPTIONS"
                    response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
                elif method != "*" and request.method != method:
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
            if origin == "http://localhost:8080":
                response["Access-Control-Allow-Origin"] = origin
                response["Vary"] = "Origin"
                response["Access-Control-Expose-Headers"] = "Retry-After"
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


# 先解析原始 Query 鍵，保留重複與不支援欄位而不受 Django QueryDict 覆蓋。
def query_values(request, allowed):
    raw = request.META.get("QUERY_STRING", "")
    values = {}
    errors = {}
    for pair in raw.split("&") if raw else ():
        encoded_name, _, encoded_value = pair.partition("=")
        decoded = unquote_plus(encoded_name, errors="replace")
        # 非法 UTF-8 鍵保留原始百分比編碼，避免替代字元與合法欄位撞名。
        name = decoded if "\ufffd" not in decoded else encoded_name.upper()
        # 陣列式鍵先對回原欄位，才能回型態錯誤或與單值鍵的重複錯誤。
        array_match = re.match(r"(page|per_page|filters\[(?:scope|keyword)\])\[", name)
        canonical = array_match.group(1) if array_match else name
        label = re.sub(r"^filters\[([^]]+)\]$", r"filters.\1", canonical)
        # 重複優先於陣列型態，其後才是未知欄位，維持共用錯誤先後。
        if canonical in values:
            errors[label] = ["此參數不可重複"]
        elif array_match and canonical in allowed:
            errors[label] = [TYPE]
        elif canonical not in allowed:
            errors[label] = ["不支援此欄位"]
        values[canonical] = unquote_plus(encoded_value, errors="replace")
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


# GET 的任何非空 Body 均為驗證錯誤，不先依 Content-Type 解讀。
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


# 解析 JSON object 後依必填、null、型態、格式順序回每欄第一個錯誤。
def json_body(request, fields, empty_allowed=False):
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
        body = json.loads(decoded, parse_constant=reject_non_json_number)
        # PHP 等後端把不成對的 UTF-16 surrogate 視為解析錯誤。
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise ApiError("E0005") from None
    if not isinstance(body, dict):
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    errors = {}
    for field in fields:
        if field not in body:
            errors[field] = [REQUIRED]
        elif body[field] is None:
            errors[field] = [NULL]
        elif not isinstance(body[field], str):
            errors[field] = [TYPE]
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
    if errors:
        raise ApiError("E0001", errors)
    return body


# 列表先驗 Body 與 Query，管理權門禁已由呼叫端先處理。
def list_input(request, allow_scope=False):
    no_get_body(request)
    allowed = {"page", "per_page"}
    if allow_scope:
        allowed.add("filters[scope]")
    values, errors = query_values(request, allowed)
    numbers = {}
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
    if errors:
        raise ApiError("E0001", errors)
    # 沿用已驗證數值，避免大量前導零在原字串二次轉換時觸發 Python 上限。
    return numbers["page"], numbers["per_page"], values.get("filters[scope]")


# 精確總數與原頁碼組成共同分頁格式，超頁仍保留 total。
def pagination(page, per_page, total):
    return {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    }


# 註冊先驗欄位再檢查唯一 Email；DB unique index 裁決跨後端並行。
@api("POST")
def register(request):
    no_query(request)
    body = json_body(request, ("email", "password", "nickname"))
    with connect() as connection:
        if connection.execute("SELECT 1 FROM accounts WHERE email = %s", (body["email"],)).fetchone():
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]})
        encoded = password_hash(body["password"])
        try:
            connection.execute(
                "INSERT INTO accounts (email, password_hash, nickname, role, status) "
                "VALUES (%s, %s, %s, 'member', 'active')",
                (body["email"], encoded, body["nickname"]),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]}) from exc
    return success(status=201)


# 登入在設定列→帳號列鎖後取 DB 時間，序列化跨後端失敗計數。
@api("POST")
def login(request):
    no_query(request)
    body = json_body(request, ("email", "password"))
    with connect() as connection:
        settings = connection.execute("SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE").fetchone()
        if settings is None:
            raise ApiError("E0104")
        account = connection.execute(
            "SELECT * FROM accounts WHERE email = %s FOR UPDATE", (body["email"],)
        ).fetchone()
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if account is None:
            return failure(ApiError("E0101"), request.api_log_id)
        if account["status"] == "disabled":
            return failure(ApiError("E0102"), request.api_log_id)
        # 到期鎖與關閉的鎖先清除並換世代；未到期鎖不驗密碼也不延長。
        if account["role"] == "member" and account["locked_until"] is not None:
            until = account["locked_until"]
            if settings["enabled"] and now < until:
                retry = max(1, -((now - until) // timedelta(seconds=1)))
                return failure(ApiError("E0105", retry_after=retry), request.api_log_id)
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
                    return failure(ApiError("E0105", retry_after=settings["lock_seconds"]), request.api_log_id)
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


# 未指定 scope 時即使管理員也只見 visible；management 必須明確請求。
@api("GET")
def articles(request):
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


# 文章詳情先驗可選 JWT 與格式，再隱藏無權讀取的下架文章。
@api("GET")
def article(request, article_id):
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


# 留言列表先確認父文章可讀，作者預覽只見 visible 留言。
@api("GET")
def comments(request, article_id):
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


# 尚未實作的 API 固定回 404，不讓 Django HTML 錯誤或假成功外洩。
@api("*")
def not_found(request):
    raise ApiError("E0004")
