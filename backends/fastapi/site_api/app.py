"""FastAPI 公開閱讀與帳號 API，使用共用 SQL 與 HTTP 契約。"""

import logging
import secrets
import uuid
from datetime import timedelta

import anyio
import psycopg
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, Response

from .core import (ApiError, account_resource, authenticate, b64url,
                   business_time, connect, content_resource, failure, issue_token,
                   jwt_key, password_hash, success, verify_password)
from .input import (json_body, list_input, management_requested, no_get_body,
                    no_query, pagination, url_integer)

LOGGER = logging.getLogger("site_api")
# 每次請求的 IN／EX／OUT 必須可由同一 log_id 在容器 stderr 追查。
LOGGER.setLevel(logging.INFO)
if not LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    LOGGER.addHandler(handler)
LOGGER.propagate = False
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
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)


# 凍結已讀取的 ASGI 原始輸入，供同步資料庫函式執行而不阻塞 event loop。
class Input:
    # 已讀取的原始 Body、Query 與標頭可安全送進同步 worker。
    def __init__(self, request, body):
        self.headers = request.headers
        self.query_string = request.scope.get("query_string", b"")
        self.content_type = request.headers.get("content-type")
        self.body = body

    # 對應共同驗證器讀取 Body 的介面；同一請求只讀一次。
    def get_data(self, cache=True):
        return self.body


# 註冊先驗證欄位；唯一索引裁決跨後端同時註冊。
def register(request, _):
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


# 鎖定變更先提交，再以新的 DB 時鐘計算實際剩餘秒數。
def locked_error(connection, until):
    connection.commit()
    fresh_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    retry = max(1, -((fresh_now - until) // timedelta(seconds=1)))
    return ApiError("E0105", retry_after=retry)


# 設定列、帳號列鎖定後取 DB 時刻，維持跨後端相同失敗視窗與提交順序。
def login(request, _):
    no_query(request)
    body = json_body(request, ("email", "password"))
    with connect() as connection:
        settings = connection.execute("SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE").fetchone()
        if settings is None:
            raise ApiError("E0104")
        account = connection.execute("SELECT * FROM accounts WHERE email = %s FOR UPDATE", (body["email"],)).fetchone()
        now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
        if account is None:
            return ApiError("E0101")
        if account["status"] == "disabled":
            return ApiError("E0102")
        # 到期鎖重設世代；未到期鎖維持原截止，不再驗密碼。
        if account["role"] == "member" and account["locked_until"] is not None:
            until = account["locked_until"]
            if settings["enabled"] and now < until:
                return locked_error(connection, until)
            account["failure_epoch"] += 1
            connection.execute(
                "UPDATE accounts SET failure_epoch = %s, locked_until = NULL WHERE id = %s",
                (account["failure_epoch"], account["id"]),
            )
        if not verify_password(body["password"], account["password_hash"]):
            if settings["enabled"] and account["role"] == "member":
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
                    return locked_error(connection, until)
            # 回傳值由 HTTP 邊界轉錯誤，避免例外造成失敗事件 rollback。
            return ApiError("E0101")
        issued = int(now.timestamp())
        jti = b64url(secrets.token_bytes(16))
        token = issue_token(account["id"], jti, issued)
        connection.execute(
            "UPDATE accounts SET failure_epoch = failure_epoch + 1, locked_until = NULL WHERE id = %s",
            (account["id"],),
        )
        connection.execute(
            "INSERT INTO login_sessions (jti, account_id, backend_id, issued_at, expires_at) "
            "VALUES (%s, %s, 'fastapi', to_timestamp(%s), to_timestamp(%s))",
            (jti, account["id"], issued, issued + 28800),
        )
        data = {
            "access_token": token, "token_type": "Bearer",
            "expires_at": business_time(now.replace(microsecond=0) + timedelta(hours=8)),
            "user": account_resource(account),
        }
    return success(data)


# 登出按帳號及 session 順序加鎖並重驗，僅撤銷目前 jti。
def logout(request, _):
    with connect() as connection:
        account, claims = authenticate(request, connection)
        no_query(request)
        json_body(request, (), empty_allowed=True)
        connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        authenticate(request, connection)
        connection.execute("UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE jti = %s", (claims["jti"],))
    return success()


# me 只讀目前帳號資料，不簽發或延長登入。
def me(request, _):
    with connect() as connection:
        account, _ = authenticate(request, connection)
        no_get_body(request)
        no_query(request)
        return success(account_resource(account))


# 公開列表在 SQL 完成權限篩選、精確計數、排序及分頁。
def articles(request, _):
    with connect() as connection:
        management = management_requested(request)
        identity = authenticate(request, connection, required=management)
        viewer = identity[0] if identity else None
        if management and viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, scope = list_input(request, allow_scope=True)
        where = "" if scope == "management" else "WHERE articles.visibility = 'visible'"
        from_sql = "FROM articles JOIN accounts ON accounts.id = articles.author_id " + where
        total = connection.execute("SELECT count(*) " + from_sql).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT " + ARTICLE_COLUMNS + " " + from_sql
            + " ORDER BY articles.created_at DESC, articles.id DESC LIMIT %s OFFSET %s",
            (per_page, offset),
        ).fetchall()
        return success({"list": [content_resource(row, viewer, "article") for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 文章詳情只向作者或管理員顯示下架內容。
def article(request, article_id):
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        no_get_body(request)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        row = connection.execute(
            "SELECT " + ARTICLE_COLUMNS + " FROM articles JOIN accounts ON accounts.id = articles.author_id "
            "WHERE articles.id = %s", (content_id,),
        ).fetchone()
        if row is None or (row["visibility"] == "hidden" and
                           (viewer is None or (viewer["role"] != "admin" and viewer["id"] != row["author_id"]))):
            raise ApiError("E0004")
        return success(content_resource(row, viewer, "article"))


# 留言先檢查父文章可讀，再於 SQL 限定個別下架留言的管理範圍。
def comments(request, article_id):
    with connect() as connection:
        identity = authenticate(request, connection, required=False)
        viewer = identity[0] if identity else None
        page, per_page, _ = list_input(request)
        content_id = url_integer(article_id, "article_id")
        parent = connection.execute("SELECT author_id, visibility FROM articles WHERE id = %s", (content_id,)).fetchone()
        if parent is None or (parent["visibility"] == "hidden" and
                              (viewer is None or (viewer["role"] != "admin" and viewer["id"] != parent["author_id"]))):
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
        return success({"list": [content_resource(row, viewer, "comment") for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 寫入先鎖帳號及目前 session 並重驗，與登出及停用操作序列化。
def authenticate_write(request, connection):
    account, claims = authenticate(request, connection)
    connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
    connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
    return authenticate(request, connection)[0]


# 正確版本但同值不更新；實際變更才增加版本及資料庫時間。
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


# 作者自行下架可自復；管理員下架他人內容會接管其恢復權。
def visibility_changes(row, viewer, visibility):
    own = viewer["id"] == row["author_id"]
    if visibility == "visible" and row["visibility"] == "hidden" and viewer["role"] != "admin" and row["restore_control"] != "author":
        raise ApiError("E0003")
    control = None if visibility == "visible" else row["restore_control"] or "author"
    if visibility == "hidden" and viewer["role"] == "admin" and not own:
        control = "admin"
    return {"visibility": visibility, "restore_control": control}


# 作者與建立來源由可信後端指定，回應不猜資料庫產生的文章 ID。
def create_article(request, _):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        body = json_body(request, ("title", "body"), content_limits={"title": 200, "body": 20000})
        connection.execute(
            "INSERT INTO articles (author_id, title, body, visibility, restore_control, version, "
            "created_backend, created_at, updated_at) "
            "SELECT %s, %s, %s, 'visible', NULL, 1, 'fastapi', now, now "
            "FROM (SELECT clock_timestamp() AS now) AS stamp",
            (viewer["id"], body["title"], body["body"]),
        )
    return success(status=201)


# 我的內容在 SQL 篩選、計數及排序，包含本人下架與父文章下架的留言。
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


# 我的文章使用完整文章欄位，讓下架與恢復權可在列表呈現。
def my_articles(request, _):
    return my_content(request, "articles", ARTICLE_COLUMNS, "article")


# 我的留言只帶父文章 ID，不因列表查詢洩漏其標題或讀取權。
def my_comments(request, _):
    return my_content(request, "comments", COMMENT_COLUMNS, "comment")


# 文章列鎖保護版本、內文與下架狀態，並與留言寫入依同一父列排序。
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
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"title": body["title"], "body": body["body"]}
        save_content(connection, "articles", row, body["version"], changes)
    return success()


# 文章內文僅作者可改，含作者自己下架的文章。
def update_article(request, article_id):
    return mutate_article(request, article_id, visibility=False)


# 管理員可下架他人文章；恢復資格在比較版本前檢查。
def article_visibility(request, article_id):
    return mutate_article(request, article_id, visibility=True)


# 新增留言鎖父文章，確保公開狀態與 INSERT 在同一交易裁決。
def create_comment(request, article_id):
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
        connection.execute(
            "INSERT INTO comments (article_id, author_id, body, visibility, restore_control, version, "
            "created_backend, created_at, updated_at) "
            "SELECT %s, %s, %s, 'visible', NULL, 1, 'fastapi', now, now "
            "FROM (SELECT clock_timestamp() AS now) AS stamp",
            (content_id, viewer["id"], body["body"]),
        )
    return success(status=201)


# 留言作者可讀自己的下架留言，即使父文章不可讀；他人按可見性回 403/404。
def comment(request, comment_id):
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


# 留言先找父 ID，再依文章、留言順序鎖列；父文章下架時禁改文字但允許合法狀態操作。
def mutate_comment(request, comment_id, visibility):
    with connect() as connection:
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(comment_id, "comment_id")
        fields = ("visibility", "version") if visibility else ("body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"body": 2000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
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


# 留言內文僅作者且父文章公開時可改，包含自己個別下架的留言。
def update_comment(request, comment_id):
    return mutate_comment(request, comment_id, visibility=False)


# 留言個別下架與恢復沿用父文章鎖及恢復權限判定。
def comment_visibility(request, comment_id):
    return mutate_comment(request, comment_id, visibility=True)


# 健康檢查同時確認業務表與正式 JWT 金鑰，避免無法登入卻宣稱就緒。
def health():
    try:
        with connect() as connection:
            connection.execute("SELECT 1 FROM accounts, login_sessions, login_failures, "
                               "login_lockout_settings, articles, comments LIMIT 0")
        jwt_key()
    except (psycopg.Error, ApiError):
        return PlainTextResponse("unavailable", status_code=503)
    return PlainTextResponse("OK")


# HTTP 邊界在 worker thread 執行同步 psycopg 與 Argon2，統一回應與 log_id。
async def dispatch(request, operation, article_id=None):
    log_id = str(uuid.uuid4())
    LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.url.path)
    try:
        raw = await request.body()
        result = await anyio.to_thread.run_sync(operation, Input(request, raw), article_id)
        response = failure(result, log_id) if isinstance(result, ApiError) else result
    except ApiError as error:
        if error.code == "E0104":
            LOGGER.error("ERROR log_id=%s code=E0104", log_id)
        response = failure(error, log_id)
    except Exception:
        LOGGER.exception("ERROR log_id=%s code=E9999", log_id)
        response = failure(ApiError("E9999"), log_id)
    if request.headers.get("origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    if response.status_code >= 400:
        LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response


# 將同一路徑的讀寫方法綁在一起，OPTIONS 回傳完整允許方法。
def bind(path, operations):
    # 避免 FastAPI 對資源 ID 先做型態轉換，保留共同 URL 整數驗證。
    async def endpoint(request: Request):
        if request.method == "OPTIONS":
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Methods"] = ", ".join((*operations, "OPTIONS"))
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
            if request.headers.get("origin") == "http://localhost:8080":
                response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = "Retry-After"
            return response
        content_id = request.path_params.get("article_id", request.path_params.get("comment_id"))
        return await dispatch(request, operations[request.method], content_id)
    app.add_api_route(path, endpoint, methods=[*operations, "OPTIONS"], include_in_schema=False)


bind("/api/auth/register", {"POST": register})
bind("/api/auth/login", {"POST": login})
bind("/api/auth/logout", {"POST": logout})
bind("/api/me", {"GET": me})
bind("/api/articles", {"GET": articles, "POST": create_article})
bind("/api/articles/{article_id}", {"GET": article, "PUT": update_article})
bind("/api/articles/{article_id}/visibility", {"PUT": article_visibility})
bind("/api/articles/{article_id}/comments", {"GET": comments, "POST": create_comment})
bind("/api/me/articles", {"GET": my_articles})
bind("/api/me/comments", {"GET": my_comments})
bind("/api/comments/{comment_id}", {"GET": comment, "PUT": update_comment})
bind("/api/comments/{comment_id}/visibility", {"PUT": comment_visibility})
app.add_api_route("/up", health, methods=["GET"], include_in_schema=False)


# 不存在路由及不支援方法仍使用共同錯誤格式。
@app.exception_handler(HTTPException)
async def http_error(request, error):
    log_id = str(uuid.uuid4())
    response = failure(ApiError("E0004" if error.status_code == 404 else "E0005"), log_id)
    if error.status_code != 404:
        response.status_code = error.status_code
    if request.headers.get("origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.url.path)
    LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response
