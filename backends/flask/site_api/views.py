"""Flask 帳號、內容與管理 API，維持共同 HTTP／資料庫契約。"""

import logging
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import unquote_plus, unquote_to_bytes
from functools import wraps

import psycopg
from flask import Flask, Response, g, make_response, request

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
        # 捕捉業務與系統錯誤，保持同一 log_id 供伺服器追查。
        @wraps(operation)
        def wrapped(**kwargs):
            log_id = str(uuid.uuid4())
            g.api_log_id = log_id
            origin = request.headers.get("Origin")
            LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.path)
            try:
                if request.method == "OPTIONS":
                    response = make_response("", 204)
                    response.headers["Access-Control-Allow-Methods"] = ", ".join((method,) if isinstance(method, str) else method) + ", OPTIONS"
                    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
                else:
                    response = operation(request, **kwargs)
            except ApiError as error:
                # 認證設定錯誤只記代碼，不記密碼、token 或雜湊。
                if error.code == "E0104":
                    LOGGER.error("ERROR log_id=%s code=E0104", log_id)
                response = failure(error, log_id)
            except Exception:
                LOGGER.exception("ERROR log_id=%s code=E9999", log_id)
                response = failure(ApiError("E9999"), log_id)
            # 成功與失敗共用來源白名單，讓登入鎖定的 Retry-After 可由前端讀取。
            if origin == "http://localhost:8080":
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = "Retry-After"
            if response.status_code >= 400:
                LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
            LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
            return response
        return wrapped
    return decorate


# 健康檢查確認共用業務表可讀，避免資料庫失聯時仍宣稱就緒。
def health():
    try:
        with connect() as connection:
            connection.execute(
                "SELECT 1 FROM accounts, login_sessions, login_failures, "
                "login_lockout_settings, articles, comments LIMIT 0"
            )
    except psycopg.Error:
        return Response("unavailable", status=503)
    return Response("OK")


# Flask 路由明確列出已實作端點，OPTIONS 也經過共同 CORS 邊界。
def create_app():
    app = Flask(__name__)
    # Flask 預設不輸出本模組 INFO；只安裝一次 stderr handler 並避免 root 重複輸出。
    LOGGER.setLevel(logging.INFO)
    if not LOGGER.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        LOGGER.addHandler(handler)
    LOGGER.propagate = False
    app.add_url_rule("/up", view_func=health)
    routes = (
        ("/api/auth/register", register, ("POST",)),
        ("/api/auth/login", login, ("POST",)),
        ("/api/auth/logout", logout, ("POST",)),
        ("/api/me", me, ("GET",)),
        ("/api/articles", articles, ("GET", "POST")),
        ("/api/articles/<article_id>", article, ("GET", "PUT")),
        ("/api/articles/<article_id>/visibility", article_visibility, ("PUT",)),
        ("/api/articles/<article_id>/comments", comments, ("GET", "POST")),
        ("/api/me/articles", my_articles, ("GET",)),
        ("/api/me/comments", my_comments, ("GET",)),
        ("/api/comments/<comment_id>", comment, ("GET", "PUT")),
        ("/api/comments/<comment_id>/visibility", comment_visibility, ("PUT",)),
        ("/api/members", members, ("GET",)),
        ("/api/members/<member_id>", member, ("GET",)),
        ("/api/members/<member_id>/status", member_status, ("PUT",)),
        ("/api/comments", management_comments, ("GET",)),
        ("/api/settings/login-lockout", login_lockout_settings, ("GET", "PUT")),
    )
    for path, view, methods in routes:
        app.add_url_rule(path, view_func=view, methods=(*methods, "OPTIONS"), provide_automatic_options=False)

    # 不存在路徑仍使用共同錯誤 envelope，避免框架 HTML 錯誤頁。
    @app.errorhandler(404)
    def missing_route(error):
        return route_error(ApiError("E0004"))

    # 方法不支援時保留 HTTP 405，並輸出共同錯誤欄位。
    @app.errorhandler(405)
    def unsupported_method(error):
        return route_error(ApiError("E0005"), status=405)

    return app


# 路由層錯誤也留下可追查 log_id，並讓已允許來源取得相同 CORS 標頭。
def route_error(error, status=None):
    log_id = str(uuid.uuid4())
    LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.path)
    response = failure(error, log_id)
    if status is not None:
        response.status_code = status
    if request.headers.get("Origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response


# 解析原始 Query 鍵與值，保留重複、陣列形式及非法 UTF-8 的錯誤。
def query_values(request, allowed):
    raw = request.query_string.decode("latin1")
    values = {}
    errors = {}
    seen = set()
    for pair in raw.split("&") if raw else ():
        encoded_name, _, encoded_value = pair.partition("=")
        identity = unquote_to_bytes(encoded_name.replace("+", " ").encode("latin1"))
        # 嚴格解碼才能區分合法 U+FFFD 與非法 bytes；顯示用的百分比標籤不作為參數身分。
        try:
            name = identity.decode("utf-8")
        except UnicodeDecodeError:
            # callback 只統一百分比 escape 的 hex 大小寫，避免連原欄位 ASCII 名稱也改寫。
            name = re.sub(r"%[0-9A-Fa-f]{2}", lambda match: match.group().upper(), encoded_name)
        # 只有本端點支援的單值欄位才合併陣列別名；未知巢狀鍵保留各自的身分與名稱。
        array_match = re.match(r"(page|per_page|filters\[(?:scope|keyword)\])\[", name)
        canonical = array_match.group(1) if array_match and array_match.group(1) in allowed else name
        if canonical != name:
            identity = canonical.encode("utf-8")
        label = re.sub(r"^filters\[([^]]+)\]$", r"filters.\1", canonical)
        # 相同顯示標籤可能來自不同未知鍵；後來的未知錯誤不可覆蓋已找到的真正重複。
        if identity in seen:
            errors[label] = ["此參數不可重複"]
        elif array_match and canonical in allowed:
            errors.setdefault(label, [TYPE])
        elif canonical not in allowed:
            errors.setdefault(label, ["不支援此欄位"])
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
    for pair in request.query_string.decode("latin1").split("&"):
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
    if request.get_data(cache=True):
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


# 直接解析 UTF-8 JSON，依共用順序驗證帳號、內容與設定欄位型態。
def json_body(request, fields, empty_allowed=False, content_limits=None, field_types=None, numeric_limits=None):
    raw = request.get_data(cache=True)
    if empty_allowed and not raw:
        return {}
    if not raw or not (request.content_type or "").lower().startswith("application/json"):
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    try:
        import json
        decoded = raw.decode("utf-8")
        # 原始容器過深即拒絕，避免同名鍵覆蓋後隱藏過深內容。
        depth = 0
        quoted = escaped = False
        for character in decoded:
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
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (UnicodeError, ValueError, RecursionError):
        raise ApiError("E0005") from None
    if not isinstance(body, dict):
        raise ApiError("E0001", {"_body": ["Request Body 必須為 JSON object"]})
    # 逐欄先處理必填／null／型態，再彙整格式與未知欄位，避免單一錯誤遮蔽其他欄位。
    errors = {}
    for field in fields:
        if field not in body:
            errors[field] = [REQUIRED]
        elif body[field] is None:
            errors[field] = [NULL]
        elif type(body[field]) is OversizedJsonInteger:
            errors[field] = [INVALID if field == "version" or numeric_limits and field in numeric_limits else TYPE]
        elif type(body[field]) is not (
            field_types[field] if field_types and field in field_types
            else int if field == "version" and content_limits is not None else str
        ):
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
    # 會員狀態與其他欄位一起彙整錯誤，避免未知欄位遮蔽狀態格式錯誤。
    if "status" in fields and "status" not in errors and body["status"] not in ("active", "disabled"):
        errors["status"] = [INVALID]
    # 內容保留原文字與換行，只拒絕超界、NUL 及固定集合的全空白。
    if content_limits is not None:
        for field, limit in content_limits.items():
            if field in errors:
                continue
            value = body[field]
            if field == "version":
                valid = 1 <= value <= 9007199254740991
            elif field == "visibility":
                valid = value in ("visible", "hidden")
            else:
                valid = 1 <= len(value) <= limit and "\0" not in value and any(char not in SPACE for char in value)
            if not valid:
                errors[field] = [INVALID]
    if numeric_limits is not None:
        for field, maximum in numeric_limits.items():
            if field not in errors and not 1 <= body[field] <= maximum:
                errors[field] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    return body


# 列表欄位先驗證完整，再將安全整數傳入分頁 SQL。
def list_input(request, allow_scope=False, allow_keyword=False):
    no_get_body(request)
    allowed = {"page", "per_page"}
    if allow_scope:
        allowed.add("filters[scope]")
    if allow_keyword:
        allowed.add("filters[keyword]")
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
    keyword = values.get("filters[keyword]", "").strip(" ")
    if allow_keyword and "filters[keyword]" in values and "filters.keyword" not in errors and len(keyword) > 254:
        errors["filters.keyword"] = [INVALID]
    if errors:
        raise ApiError("E0001", errors)
    # 沿用已驗證數值，避免大量前導零在原字串二次轉換時觸發 Python 上限。
    return numbers["page"], numbers["per_page"], keyword if allow_keyword else values.get("filters[scope]")


# 精確總數與原頁碼組成共同分頁格式，超頁仍保留 total。
def pagination(page, per_page, total):
    return {
        "page": page, "per_page": per_page, "total": total,
        "total_pages": (total + per_page - 1) // per_page,
    }


# 寫入交易依帳號、session 順序鎖列再重驗，與登出及停用明確排序。
def authenticate_write(request, connection):
    account, claims = authenticate(request, connection)
    # 等待期間可能已登出或停用，取得相容列鎖後必須重新讀取，不沿用外層結果。
    connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
    connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
    # 呼叫端在同一連線交易繼續鎖內容；離開 context 才提交或因例外回滾並釋放所有列鎖。
    return authenticate(request, connection)[0]


# 先比版本，即使提交同值也拒絕過期；實際變更才增加版本與時間。
def save_content(connection, table, row, version, changes):
    # table／changes 只來自下方兩種內容操作；呼叫端已鎖目標列並裁決讀取與操作權。
    if row["version"] != version:
        raise ApiError("E0201")
    if all(row[key] == value for key, value in changes.items()):
        return
    # 不改作者、建立來源或建立時間；條件 UPDATE 再守住版本，實際變更只遞增一次。
    columns = list(changes)
    assignments = ", ".join(f"{column} = %s" for column in columns)
    result = connection.execute(
        f"UPDATE {table} SET {assignments}, version = version + 1, updated_at = clock_timestamp() "
        "WHERE id = %s AND version = %s",
        (*[changes[column] for column in columns], row["id"], version),
    )
    if result.rowcount != 1:
        raise ApiError("E0201")


# 作者只能恢復自行下架內容；管理員下架他人內容會取得恢復權。
def visibility_changes(row, viewer, visibility):
    own = viewer["id"] == row["author_id"]
    if visibility == "visible" and row["visibility"] == "hidden" and viewer["role"] != "admin" and row["restore_control"] != "author":
        raise ApiError("E0003")
    # 恢復會清除控制者；重下架保留既有管理限制，管理員自己的內容仍視為作者操作。
    control = None if visibility == "visible" else row["restore_control"] or "author"
    if visibility == "hidden" and viewer["role"] == "admin" and not own:
        control = "admin"
    return {"visibility": visibility, "restore_control": control}



# 註冊先驗欄位再檢查唯一 Email；DB unique index 裁決跨後端並行。
@api("POST")
def register(request):
    no_query(request)
    body = json_body(request, ("email", "password", "nickname"))
    with connect() as connection:
        if connection.execute("SELECT 1 FROM accounts WHERE email = %s", (body["email"],)).fetchone():
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]})
        # 預查可提早回報；真正的並行唯一性仍由 INSERT 的 unique constraint 保證。
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


# 先提交失敗事件與鎖定，再用新的 DB 時鐘計算回應當下的剩餘整秒。
def locked_response(connection, until):
    connection.commit()
    fresh_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    retry = max(1, -((fresh_now - until) // timedelta(seconds=1)))
    return failure(ApiError("E0105", retry_after=retry), g.api_log_id)


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
        # 鎖等待結束後才取實際 DB 時間，不能用交易開始時間計算鎖定或滑動視窗。
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if account is None:
            return failure(ApiError("E0101"), g.api_log_id)
        if account["status"] == "disabled":
            return failure(ApiError("E0102"), g.api_log_id)
        # 到期鎖與關閉的鎖先清除並換世代；未到期鎖不驗密碼也不延長。
        if account["role"] == "member" and account["locked_until"] is not None:
            until = account["locked_until"]
            if settings["enabled"] and now < until:
                return locked_response(connection, until)
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
                    return locked_response(connection, until)
            # 正常離開連線 context 會先提交失敗事件／到期清鎖，再送出 401；不可改成拋例外。
            return failure(ApiError("E0101"), g.api_log_id)
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
            "VALUES (%s, %s, 'flask', to_timestamp(%s), to_timestamp(%s))",
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
        # 輸入合法後按帳號→session 鎖定並重驗，與停用及受保護寫入互斥。
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


# 公開列表維持原有範圍；POST 則在重驗登入的交易建立作者文章。
@api(("GET", "POST"))
def articles(request):
    if request.method == "POST":
        with connect() as connection:
            viewer = authenticate_write(request, connection)
            no_query(request)
            body = json_body(request, ("title", "body"), content_limits={"title": 200, "body": 20000})
            # 同一資料庫時刻建立兩個時間欄，作者和來源由後端指定。
            connection.execute(
                "INSERT INTO articles (author_id, title, body, visibility, restore_control, version, "
                "created_backend, created_at, updated_at) "
                "SELECT %s, %s, %s, 'visible', NULL, 1, 'flask', now, now "
                "FROM (SELECT clock_timestamp() AS now) AS stamp",
                (viewer["id"], body["title"], body["body"]),
            )
        return success(status=201)
    with connect() as connection:
        # 明確 management 才啟用管理門禁；即使登入管理員，省略 scope 仍只列公開文章。
        management = management_requested(request)
        identity = authenticate(request, connection, required=management)
        viewer = identity[0] if identity else None
        if management and viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, scope = list_input(request, allow_scope=True)
        if scope == "management" and (viewer is None or viewer["role"] != "admin"):
            raise ApiError("E0003")
        where = "" if scope == "management" else "WHERE articles.visibility = 'visible'"
        # 計數與列表共用範圍，超頁仍保留真實總數；作者 JOIN 避免逐筆讀取暱稱。
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


# 詳情隱藏無權讀取的下架文章；PUT 進入作者編修交易。
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
        # 先驗登入與輸入才查資源；無權讀下架內容時和不存在使用同一 404。
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
            # 帳號與 session 已鎖定；再鎖父文章，讓下架和新增依父列取得順序完成。
            parent = connection.execute(
                "SELECT author_id, visibility FROM articles WHERE id = %s FOR UPDATE", (content_id,)
            ).fetchone()
            if parent is None or (parent["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != parent["author_id"]):
                raise ApiError("E0004")
            if parent["visibility"] == "hidden":
                raise ApiError("E0003")
            # 父文章仍公開才在相同交易新增，避免與並行下架交錯。
            connection.execute(
                "INSERT INTO comments (article_id, author_id, body, visibility, restore_control, version, "
                "created_backend, created_at, updated_at) "
                "SELECT %s, %s, %s, 'visible', NULL, 1, 'flask', now, now "
                "FROM (SELECT clock_timestamp() AS now) AS stamp",
                (content_id, viewer["id"], body["body"]),
            )
        return success(status=201)
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        page, per_page, _ = list_input(request)
        content_id = url_integer(article_id, "article_id")
        # 父文預覽只授予父文章作者／管理員；留言作者身分本身不取得父文讀取權。
        parent = connection.execute(
            "SELECT author_id, visibility FROM articles WHERE id = %s", (content_id,)
        ).fetchone()
        if parent is None or (
            parent["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != parent["author_id"]))
        ):
            raise ApiError("E0004")
        where = "WHERE comments.article_id = %s"
        # 父文作者的預覽仍排除個別下架留言；管理員才可在這個列表讀取全部。
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


# 我的文章只按目前帳號篩選，包含自行或管理員下架內容。
@api("GET")
def my_articles(request):
    return my_content(request, "articles", ARTICLE_COLUMNS, "article")


# 我的留言不讀父文章內文；父文章下架仍可列自己的留言。
@api("GET")
def my_comments(request):
    return my_content(request, "comments", COMMENT_COLUMNS, "comment")


# 精確計數及排序分頁留給資料庫，避免整表載入或逐筆查詢。
def my_content(request, table, columns, kind):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        page, per_page, _ = list_input(request)
        # table／columns／kind 僅由兩個本人列表指定；不接受 Query 決定 SQL 識別字。
        total = connection.execute(f"SELECT count(*) FROM {table} WHERE author_id = %s", (viewer["id"],)).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            f"SELECT {columns} FROM {table} JOIN accounts ON accounts.id = {table}.author_id "
            f"WHERE {table}.author_id = %s ORDER BY {table}.created_at DESC, {table}.id DESC "
            "LIMIT %s OFFSET %s", (viewer["id"], per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, kind) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 文章下架和留言新增、修改同鎖文章列，權限與版本在交易內裁決。
@api("PUT")
def article_visibility(request, article_id):
    return mutate_article(request, article_id, visibility=True)


# 文章內文限作者編修；管理員可透過下架操作處理他人文章。
def mutate_article(request, article_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        fields = ("visibility", "version") if visibility else ("title", "body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"title": 200, "body": 20000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        # 帳號→session→文章鎖在同一交易；先隱藏不可讀目標，再驗操作權，最後才驗版本。
        row = connection.execute("SELECT * FROM articles WHERE id = %s FOR UPDATE", (content_id,)).fetchone()
        if row is None or (row["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != row["author_id"]):
            raise ApiError("E0004")
        if viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin"):
            raise ApiError("E0003")
        # 恢復資格須先於版本判定；同值與過期判定由共用儲存流程處理。
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"title": body["title"], "body": body["body"]}
        save_content(connection, "articles", row, body["version"], changes)
    return success()


# 留言作者即使父文章下架仍可讀自己的詳情；他人先依可見性隱藏存在。
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
        # 作者可讀自己的下架留言，管理員可讀全部；其他人先依留言及父文隱藏性決定 404 或 403。
        if viewer["role"] != "admin" and viewer["id"] != row["author_id"]:
            if row["visibility"] == "hidden" or row["article_visibility"] == "hidden":
                raise ApiError("E0004")
            raise ApiError("E0003")
        return success(content_resource(row, viewer, "comment"))


# 留言下架與恢復不因父文章下架而禁止；留言恢復也不會改變父文章的隱藏狀態。
@api("PUT")
def comment_visibility(request, comment_id):
    return mutate_comment(request, comment_id, visibility=True)


# 父文章、留言依序取得列鎖；修改內文必須確認父文章仍公開。
def mutate_comment(request, comment_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(comment_id, "comment_id")
        fields = ("visibility", "version") if visibility else ("body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"body": 2000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        # 先讀父 ID 而不鎖留言，之後先鎖文章再鎖留言；鎖後複核關聯。
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
        # 作者或管理員可獨立管理留言狀態；改內文則必須是本人且父文仍公開。
        if (viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin")) or (
            not visibility and parent["visibility"] == "hidden"
        ):
            raise ApiError("E0003")
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"body": body["body"]}
        save_content(connection, "comments", row, body["version"], changes)
    return success()


# 管理端點先確認目前角色，再驗輸入；寫入交易取得鎖後仍須重驗。
def management_identity(request, connection):
    account, claims = authenticate(request, connection)
    if account["role"] != "admin":
        raise ApiError("E0003")
    return account, claims


# 僅搜尋一般會員；資料庫以 ASCII folding 和字面 strpos 完成計數及分頁。
@api("GET")
def members(request):
    with connect() as connection:
        management_identity(request, connection)
        page, per_page, keyword = list_input(request, allow_keyword=True)
        # PostgreSQL text 不能包含 NUL，且資料欄位也不會包含它。
        if "\0" in keyword:
            return success({"list": [], "pagination": pagination(page, per_page, 0)})
        folded = keyword.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        where = "FROM accounts WHERE role = 'member'"
        params = ()
        if folded:
            where += (
                " AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0"
                " OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)"
            )
            params = (folded, folded)
        total = connection.execute("SELECT count(*) " + where, params).fetchone()["count"]
        offset = (page - 1) * per_page
        # 已超出總數的頁碼不再執行注定為空的列表查詢。
        rows = [] if offset >= total else connection.execute(
            "SELECT id, email, nickname, role, status, created_at " + where
            + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (*params, per_page, offset),
        ).fetchall()
        return success({"list": [account_resource(row) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 會員詳情不揭露管理員存在，也不輸出認證與鎖定欄位。
@api("GET")
def member(request, member_id):
    with connect() as connection:
        management_identity(request, connection)
        no_get_body(request)
        no_query(request)
        account_id = url_integer(member_id, "member_id")
        row = connection.execute(
            "SELECT id, email, nickname, role, status, created_at FROM accounts "
            "WHERE id = %s AND role = 'member'", (account_id,),
        ).fetchone()
        if row is None:
            raise ApiError("E0004")
        return success(account_resource(row))


# 停用與撤銷登入同交易；先按 ID 鎖兩帳號，後鎖管理員 session 並重驗。
@api("PUT")
def member_status(request, member_id):
    with connect() as connection:
        manager, claims = management_identity(request, connection)
    # 初驗角色先於輸入；實際交易內還會在列鎖後重驗，避免等待時權限已失效。
    no_query(request)
    account_id = url_integer(member_id, "member_id")
    body = json_body(request, ("status",))
    with connect() as connection:
        locked = connection.execute(
            "SELECT id, role, status FROM accounts WHERE id IN (%s, %s) ORDER BY id FOR UPDATE",
            (manager["id"], account_id),
        ).fetchall()
        accounts = {row["id"]: row for row in locked}
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        management_identity(request, connection)
        # 管理員不屬於可操作會員集合；同值不再撤銷 session，恢復只改 status。
        target = accounts.get(account_id)
        if target is None or target["role"] != "member":
            raise ApiError("E0004")
        if target["status"] != body["status"]:
            connection.execute("UPDATE accounts SET status = %s WHERE id = %s", (body["status"], account_id))
            if body["status"] == "disabled":
                # 帳號列鎖阻止新登入穿越停用提交；既有登入全部同時撤銷。
                connection.execute(
                    "UPDATE login_sessions SET revoked_at = clock_timestamp() "
                    "WHERE account_id = %s AND revoked_at IS NULL", (account_id,),
                )
    return success()


# 管理留言包含個別及父文章下架內容，JOIN 作者一次取齊完整資源。
@api("GET")
def management_comments(request):
    with connect() as connection:
        viewer, _ = management_identity(request, connection)
        page, per_page, _ = list_input(request)
        # 管理列表不套用父文或留言 visibility 篩選，計數與分頁使用相同完整範圍。
        from_sql = "FROM comments JOIN accounts ON accounts.id = comments.author_id"
        total = connection.execute("SELECT count(*) " + from_sql).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + COMMENT_COLUMNS + " " + from_sql
            + " ORDER BY comments.created_at DESC, comments.id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, "comment") for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 設定寫入必須提供完整四欄，沿用共同 JSON 解析及逐欄錯誤格式。
def lockout_body(request):
    fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
    return json_body(request, fields, field_types={
        "enabled": bool, "window_minutes": int, "failure_threshold": int, "lock_seconds": int,
    }, numeric_limits={"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400})


# 管理設定讀取只回公開四欄；更新依設定→帳號 ID→session 鎖序提交。
@api(("GET", "PUT"))
def login_lockout_settings(request):
    # Flask 自動開放 HEAD；它與 GET 共用唯讀驗證，不能落入 PUT 寫入。
    if request.method in ("GET", "HEAD"):
        with connect() as connection:
            management_identity(request, connection)
            no_get_body(request)
            no_query(request)
            row = connection.execute(
                "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
                "FROM login_lockout_settings WHERE id = 1"
            ).fetchone()
            if row is None:
                raise ApiError("E0104")
            return success({key: row[key] for key in ("enabled", "window_minutes", "failure_threshold", "lock_seconds")})
    with connect() as connection:
        manager, claims = management_identity(request, connection)
    # 完整四欄先驗證，才開始更新交易；預先驗過角色不能取代鎖後的再次查核。
    no_query(request)
    body = lockout_body(request)
    with connect() as connection:
        settings = connection.execute(
            "SELECT enabled, window_minutes, failure_threshold, lock_seconds "
            "FROM login_lockout_settings WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if settings is None:
            raise ApiError("E0104")
        disabling = settings["enabled"] and not body["enabled"]
        # 關閉須先鎖全部帳號；一般修改只鎖管理員，避免反向鎖序。
        if disabling:
            connection.execute("SELECT id FROM accounts ORDER BY id FOR UPDATE").fetchall()
        else:
            connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (manager["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        management_identity(request, connection)
        # 四欄以同一 UPDATE 提交；同值不重做清鎖，數值更新不改任何現有鎖定期限。
        if any(settings[key] != body[key] for key in body):
            connection.execute(
                "UPDATE login_lockout_settings SET enabled = %s, window_minutes = %s, "
                "failure_threshold = %s, lock_seconds = %s WHERE id = 1",
                tuple(body[key] for key in ("enabled", "window_minutes", "failure_threshold", "lock_seconds")),
            )
            if disabling:
                # 換世代使舊失敗事件不再計數；保留登入與停用狀態。
                connection.execute(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                    "WHERE role = 'member'"
                )
    return success()
