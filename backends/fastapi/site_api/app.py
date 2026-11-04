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
        self.method = request.method
        self.headers = request.headers
        self.query_string = request.scope.get("query_string", b"")
        self.content_type = request.headers.get("content-type")
        self.body = body

    # 對應共同驗證器讀取 Body 的介面；同一請求只讀一次。
    def get_data(self, cache=True):
        return self.body


# 註冊先驗證欄位；唯一索引裁決跨後端同時註冊。
def register(request, _):
    # 公開註冊不沿用舊 JWT；格式驗證須先於帳號查重與新增。
    no_query(request)
    body = json_body(request, ("email", "password", "nickname"))
    with connect() as connection:
        if connection.execute("SELECT 1 FROM accounts WHERE email = %s", (body["email"],)).fetchone():
            raise ApiError("E0001", {"email": ["此 Email 已註冊"]})
        encoded = password_hash(body["password"])
        try:
            # 查重後仍可能有其他後端同時新增，最後由唯一索引裁決並轉為共同文案。
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
    # COMMIT 的等待也會消耗鎖定時間；回應只提供原截止的剩餘秒數，不重新起算。
    fresh_now = connection.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    retry = max(1, -((fresh_now - until) // timedelta(seconds=1)))
    return ApiError("E0105", retry_after=retry)


# 設定列、帳號列鎖定後取 DB 時刻，維持跨後端相同失敗視窗與提交順序。
def login(request, _):
    # 格式錯誤不進入鎖定計數；公開登入也不使用請求可能攜帶的舊 token。
    no_query(request)
    body = json_body(request, ("email", "password"))
    with connect() as connection:
        settings = connection.execute("SELECT * FROM login_lockout_settings WHERE id = 1 FOR UPDATE").fetchone()
        if settings is None:
            raise ApiError("E0104")
        account = connection.execute("SELECT * FROM accounts WHERE email = %s FOR UPDATE", (body["email"],)).fetchone()
        # 排隊取得設定及帳號鎖後才取一次實際 DB 時間，避免等待時間扭曲視窗或到期判斷。
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
        # 停用與未到期鎖已先處理；只有啟用鎖定的一般會員密碼錯誤能新增事件。
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
                # 本次事件已包含在左開右閉視窗內；達門檻當次就保存原截止並回 429。
                if count >= settings["failure_threshold"]:
                    until = now + timedelta(seconds=settings["lock_seconds"])
                    connection.execute("UPDATE accounts SET locked_until = %s WHERE id = %s", (until, account["id"]))
                    return locked_error(connection, until)
            # 回傳值由 HTTP 邊界轉錯誤，避免例外造成失敗事件 rollback。
            return ApiError("E0101")
        # 成功登入的世代與 session 同交易提交；JWT 與 DB 共用整秒起點及固定八小時。
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
        # 認證先於輸入錯誤；空 Body 與空 object 均可登出。
        account, claims = authenticate(request, connection)
        no_query(request)
        json_body(request, (), empty_allowed=True)
        connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        # 等鎖期間可能已停用、撤銷或重設密碼，不能沿用鎖前的認證結果。
        authenticate(request, connection)
        connection.execute("UPDATE login_sessions SET revoked_at = clock_timestamp() WHERE jti = %s", (claims["jti"],))
    return success()


# me 只讀目前帳號資料，不簽發或延長登入。
def me(request, _):
    with connect() as connection:
        # 先確認目前身分，再拒絕 GET Body 與多餘 Query；讀取不改 session。
        account, _ = authenticate(request, connection)
        no_get_body(request)
        no_query(request)
        return success(account_resource(account))


# 公開列表在 SQL 完成權限篩選、精確計數、排序及分頁。
def articles(request, _):
    with connect() as connection:
        # 明示管理範圍先驗角色，再驗分頁；管理員未指定 scope 仍只看公開文章。
        management = management_requested(request)
        identity = authenticate(request, connection, required=management)
        viewer = identity[0] if identity else None
        if management and viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, scope = list_input(request, allow_scope=True)
        where = "" if scope == "management" else "WHERE articles.visibility = 'visible'"
        from_sql = "FROM articles JOIN accounts ON accounts.id = articles.author_id " + where
        # 計數與列表共用範圍，JOIN 一次取齊作者；超頁不再查注定為空的列表。
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
        # 先隱藏不可讀資源的存在，再輸出完整資源；公開讀取的無效憑證已在前面拒絕。
        if row is None or (
            row["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != row["author_id"]))
        ):
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
        if parent is None or (
            parent["visibility"] == "hidden"
            and (viewer is None or (viewer["role"] != "admin" and viewer["id"] != parent["author_id"]))
        ):
            raise ApiError("E0004")
        # 作者預覽下架文章仍只看公開留言；自己的下架留言由本人列表／詳情讀取。
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
    # 內容寫入共用此鎖後重驗，等待期間的停用、登出或密碼重設必須立即生效。
    return authenticate(request, connection)[0]


# 正確版本但同值不更新；實際變更才增加版本及資料庫時間。
def save_content(connection, table, row, version, changes):
    # 呼叫端已鎖列並驗操作權；過期同值仍衝突，只有正確版本的同值可略過 UPDATE。
    if row["version"] != version:
        raise ApiError("E0201")
    if all(row[key] == value for key, value in changes.items()):
        return
    columns = list(changes)
    # 表名與欄名僅來自內部呼叫；值一律綁定，條件版本更新保留共同原子寫入契約。
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
    if (
        visibility == "visible" and row["visibility"] == "hidden"
        and viewer["role"] != "admin" and row["restore_control"] != "author"
    ):
        raise ApiError("E0003")
    # 作者重下架不能移除管理員限制；管理員自己的內容仍依作者規則處理。
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
        # 同一 DB 時刻填入建立與更新時間，作者及來源不接受客戶端指定。
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
        # table／columns／kind 由下方兩個固定入口提供；本人範圍不受父文章或留言狀態排除。
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
        # 帳號→session→文章在同一交易加鎖；不可讀先 404，再驗操作權與恢復權，最後才比版本。
        row = connection.execute("SELECT * FROM articles WHERE id = %s FOR UPDATE", (content_id,)).fetchone()
        if row is None or (row["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != row["author_id"]):
            raise ApiError("E0004")
        if viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin"):
            raise ApiError("E0003")
        changes = (
            visibility_changes(row, viewer, body["visibility"])
            if visibility else {"title": body["title"], "body": body["body"]}
        )
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
        # 與文章下架鎖同一父列，取得鎖後才判公開狀態，避免依等待前的狀態新增。
        parent = connection.execute(
            "SELECT author_id, visibility FROM articles WHERE id = %s FOR UPDATE", (content_id,)
        ).fetchone()
        if parent is None or (parent["visibility"] == "hidden" and viewer["role"] != "admin" and viewer["id"] != parent["author_id"]):
            raise ApiError("E0004")
        if parent["visibility"] == "hidden":
            raise ApiError("E0003")
        # 有預覽權也不能對下架父文新增；通過後才寫入同時刻、固定來源的留言。
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
        # 作者例外只授予自己的留言；其他人先按父文及留言隱藏性裁決 404，再回操作權 403。
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
        # 先讀父 ID 而不鎖留言，再依文章→留言加鎖，鎖後複核關聯以保持共同鎖序。
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
        # 下架父文禁止任何人改留言內文，包含管理員本人；合法狀態操作仍可繼續。
        if (viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin")) or (
            not visibility and parent["visibility"] == "hidden"
        ):
            raise ApiError("E0003")
        changes = (
            visibility_changes(row, viewer, body["visibility"])
            if visibility else {"body": body["body"]}
        )
        save_content(connection, "comments", row, body["version"], changes)
    return success()


# 留言內文僅作者且父文章公開時可改，包含自己個別下架的留言。
def update_comment(request, comment_id):
    return mutate_comment(request, comment_id, visibility=False)


# 留言個別下架與恢復沿用父文章鎖及恢復權限判定。
def comment_visibility(request, comment_id):
    return mutate_comment(request, comment_id, visibility=True)


# 管理端點先確認目前角色，避免在輸入錯誤時洩漏管理資源；寫入時須鎖後重驗。
def management_identity(request, connection):
    account, claims = authenticate(request, connection)
    if account["role"] != "admin":
        raise ApiError("E0003")
    return account, claims


# 僅搜尋一般會員；SQL 的 ASCII folding 和字面 strpos 共用於計數及分頁。
def members(request, _):
    with connect() as connection:
        management_identity(request, connection)
        page, per_page, keyword = list_input(request, allow_keyword=True)
        folded = keyword.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        from_sql = "FROM accounts WHERE role = 'member'"
        params = ()
        if folded:
            from_sql += (
                " AND (strpos(translate(nickname, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0"
                " OR strpos(translate(email, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), %s) > 0)"
            )
            params = (folded, folded)
        # count 與列表使用同一條件；超頁仍回原頁碼及完整 total，並以時間、ID 降冪排序。
        total = connection.execute("SELECT count(*) " + from_sql, params).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            "SELECT id, email, nickname, role, status, created_at " + from_sql
            + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            (*params, per_page, offset),
        ).fetchall()
        return success({"list": [account_resource(row) for row in rows],
                        "pagination": pagination(page, per_page, total)})


# 會員詳情只回公開 Account 欄位，管理員 ID 一律當成不存在。
def member(request, member_id):
    with connect() as connection:
        # 先驗管理角色及輸入，才查一般會員，避免把管理員帳號當成可管理目標。
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


# 停用與撤銷全部登入同交易；兩帳號按 ID 鎖後鎖管理員 session 並重驗。
def member_status(request, member_id):
    with connect() as connection:
        manager, claims = management_identity(request, connection)
    # 角色門禁先於格式；輸入驗完才進入寫入交易，鎖後仍需再次查核管理員。
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
        # 管理員 ID 與不存在目標同為 404；同值不撤銷，恢復只改狀態而不復活舊登入。
        target = accounts.get(account_id)
        if target is None or target["role"] != "member":
            raise ApiError("E0004")
        if target["status"] != body["status"]:
            connection.execute("UPDATE accounts SET status = %s WHERE id = %s", (body["status"], account_id))
            if body["status"] == "disabled":
                # 帳號鎖擋住新登入及受保護寫入；舊登入全部在同次提交撤銷。
                connection.execute(
                    "UPDATE login_sessions SET revoked_at = clock_timestamp() "
                    "WHERE account_id = %s AND revoked_at IS NULL", (account_id,),
                )
    return success()


# 管理列表包含個別及父文章下架留言，一次 JOIN 作者避免逐列查詢。
def management_comments(request, _):
    with connect() as connection:
        viewer, _ = management_identity(request, connection)
        page, per_page, _ = list_input(request)
        # 管理範圍不排除父文或留言下架；計數及列表共用 JOIN，不逐列查詢作者。
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


# 四欄必須一起提供，嚴格分辨 JSON boolean、integer 及範圍。
def lockout_body(request):
    fields = ("enabled", "window_minutes", "failure_threshold", "lock_seconds")
    return json_body(request, fields, field_types={
        "enabled": bool, "window_minutes": int, "failure_threshold": int, "lock_seconds": int,
    }, numeric_limits={"window_minutes": 60, "failure_threshold": 100, "lock_seconds": 86400})


# 讀設定只回四欄；更新按設定、帳號 ID、session 鎖序原子提交。
def login_lockout_settings(request, _):
    if request.method == "GET":
        with connect() as connection:
            # 管理權先於 GET 輸入驗證，讀取只投影四個公開設定欄位。
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
    # 四欄先完整驗證，再鎖設定及帳號；外層角色檢查不能取代交易內重驗。
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
        # 關閉時鎖全部帳號阻止登入計數競爭；其餘更新只鎖管理員。
        if disabling:
            connection.execute("SELECT id FROM accounts ORDER BY id FOR UPDATE").fetchall()
        else:
            connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (manager["id"],))
        connection.execute("SELECT jti FROM login_sessions WHERE jti = %s FOR UPDATE", (claims["jti"],))
        management_identity(request, connection)
        # 四欄以單次 UPDATE 保存；同值無副作用，單改數值不動現有鎖定截止。
        if any(settings[key] != body[key] for key in body):
            connection.execute(
                "UPDATE login_lockout_settings SET enabled = %s, window_minutes = %s, "
                "failure_threshold = %s, lock_seconds = %s WHERE id = 1",
                tuple(body[key] for key in ("enabled", "window_minutes", "failure_threshold", "lock_seconds")),
            )
            if disabling:
                # 換世代並清除未到期鎖，保留停用狀態及既有登入。
                connection.execute(
                    "UPDATE accounts SET locked_until = NULL, failure_epoch = failure_epoch + 1 "
                    "WHERE role = 'member'"
                )
    return success()


# 健康檢查同時確認業務表與正式 JWT 金鑰，避免無法登入卻宣稱就緒。
def health():
    try:
        with connect() as connection:
            # 只檢查所需表可存取，不建立測資；金鑰也有效才回應就緒。
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
        # Body 先由 ASGI 讀一次，再凍結輸入交給同步 worker，避免 DB／雜湊阻塞 event loop。
        raw = await request.body()
        result = await anyio.to_thread.run_sync(operation, Input(request, raw), article_id)
        # 登入可在提交交易後回傳業務錯誤；統一序列化不能反過來使該交易 rollback。
        response = failure(result, log_id) if isinstance(result, ApiError) else result
    except ApiError as error:
        if error.code == "E0104":
            LOGGER.error("ERROR log_id=%s code=E0104", log_id)
        response = failure(error, log_id)
    except Exception:
        # DB 不可用等未預期錯誤保持安全的系統回應，不冒充憑證失效或成功。
        LOGGER.exception("ERROR log_id=%s code=E9999", log_id)
        response = failure(ApiError("E9999"), log_id)
    # 成功與失敗都只向固定來源提供 CORS，並讓前端可讀取鎖定剩餘秒數。
    if request.headers.get("origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    # 同一 log_id 串起 IN、錯誤 EX 及 OUT，供共同契約追查實際回應。
    if response.status_code >= 400:
        LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response


# 將同一路徑的讀寫方法綁在一起，OPTIONS 回傳完整允許方法。
def bind(path, operations):
    # 路由 callback 保留原始 ID 給共同驗證器，避免 FastAPI 原生 422 搶先改寫契約。
    async def endpoint(request: Request):
        if request.method == "OPTIONS":
            # 預檢只宣告此路由的方法；實際業務請求仍須通過共同認證及驗證。
            response = Response(status_code=204)
            response.headers["Access-Control-Allow-Methods"] = ", ".join((*operations, "OPTIONS"))
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept"
            if request.headers.get("origin") == "http://localhost:8080":
                response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
                response.headers["Vary"] = "Origin"
                response.headers["Access-Control-Expose-Headers"] = "Retry-After"
            return response
        # 三種 ID 都維持網址字串，格式與權限的先後交由對應 operation 裁決。
        content_id = next((request.path_params[key] for key in ("article_id", "comment_id", "member_id")
                           if key in request.path_params), None)
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
bind("/api/members", {"GET": members})
bind("/api/members/{member_id}", {"GET": member})
bind("/api/members/{member_id}/status", {"PUT": member_status})
bind("/api/comments", {"GET": management_comments})
bind("/api/settings/login-lockout", {"GET": login_lockout_settings, "PUT": login_lockout_settings})
app.add_api_route("/up", health, methods=["GET"], include_in_schema=False)


# 不存在路由及不支援方法仍使用共同錯誤格式。
@app.exception_handler(HTTPException)
async def http_error(request, error):
    log_id = str(uuid.uuid4())
    response = failure(ApiError("E0004" if error.status_code == 404 else "E0005"), log_id)
    # 沿用實際 HTTP 狀態（例如 405），只把錯誤格式統一為共同 envelope。
    if error.status_code != 404:
        response.status_code = error.status_code
    # 路由層未進 dispatch，也要提供相同固定來源 CORS 與可追查記錄。
    if request.headers.get("origin") == "http://localhost:8080":
        response.headers["Access-Control-Allow-Origin"] = "http://localhost:8080"
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Expose-Headers"] = "Retry-After"
    LOGGER.info("IN log_id=%s method=%s path=%s", log_id, request.method, request.url.path)
    LOGGER.warning("EX log_id=%s status=%s", log_id, response.status_code)
    LOGGER.info("OUT log_id=%s status=%s", log_id, response.status_code)
    return response
