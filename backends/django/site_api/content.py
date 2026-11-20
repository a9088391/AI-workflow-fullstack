"""文章與留言用途；在既有交易內裁決可讀性、操作權與版本。"""

from .core import ApiError, authenticate, authenticate_write, connect, content_resource, success
from .http import api
from .input import (
    json_body,
    list_input,
    management_requested,
    no_get_body,
    no_query,
    pagination,
    url_integer,
)


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


# 已持有內容列鎖時先比版本；同值不動時間，變更時仍以 version 條件更新。
def save_content(connection, table, row, version, changes):
    # 先拒絕舊版本，再判斷同值，避免過期重送被當成成功；呼叫端已完成授權並鎖住該列。
    if row["version"] != version:
        raise ApiError("E0201")
    if all(row[key] == value for key, value in changes.items()):
        return
    # table 與欄位只由下方固定內容流程提供；保留條件更新，版本及時間與資料一起提交。
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
    # 作者不可恢復管理員控制的下架內容，且此權限檢查必須先於版本比較。
    if (
        visibility == "visible" and row["visibility"] == "hidden"
        and viewer["role"] != "admin" and row["restore_control"] != "author"
    ):
        raise ApiError("E0003")
    # 作者重下架保留既有控制者；管理員只在處理他人內容時接管，恢復則清除控制者。
    control = None if visibility == "visible" else row["restore_control"] or "author"
    if visibility == "hidden" and viewer["role"] == "admin" and not own:
        control = "admin"
    return {"visibility": visibility, "restore_control": control}


# GET 預設只見公開內容，POST 在認證鎖保護下建立作者文章。
@api(("GET", "POST"))
def articles(request):
    if request.method == "POST":
        with connect() as connection:
            # 同一交易鎖帳號與 session 後重驗登入，再驗輸入，避免撤銷後仍發表成功。
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
        # 管理範圍先驗登入與角色，再驗分頁；一般列表即使由管理員讀取也只列公開文章。
        management = management_requested(request)
        identity = authenticate(request, connection, required=management)
        viewer = identity[0] if identity else None
        if management and viewer["role"] != "admin":
            raise ApiError("E0003")
        page, per_page, scope = list_input(request, allow_scope=True)
        if scope == "management" and (viewer is None or viewer["role"] != "admin"):
            raise ApiError("E0003")
        # 在 DB 篩選、精確計數與排序分頁；超頁不抓資料但仍回原頁碼和總數。
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
        # 公開讀取可省略登入，但攜帶憑證仍先驗證，再處理格式與可讀性。
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
        # 作者與管理員可預覽下架文章；其他人以 404 隱藏其存在，不洩漏版本或內文。
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
            # 認證與格式先於父文章判定；鎖同一父列，讓新增留言與文章下架依提交順序決定。
            viewer = authenticate_write(request, connection)
            no_query(request)
            content_id = url_integer(article_id, "article_id")
            body = json_body(request, ("body",), content_limits={"body": 2000})
            parent = connection.execute(
                "SELECT author_id, visibility FROM articles WHERE id = %s FOR UPDATE", (content_id,)
            ).fetchone()
            if parent is None or (
                parent["visibility"] == "hidden"
                and viewer["role"] != "admin" and viewer["id"] != parent["author_id"]
            ):
                raise ApiError("E0004")
            # 有預覽權仍無新增權，包括管理員；先通過可讀性才回 403。
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
        # 先驗可選登入及分頁，再判父文可讀性；留言作者身分不授予父文章預覽權。
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
        # 作者預覽也只列公開留言；個別下架留言走本人入口，管理員列表則包含全部。
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


# 由兩個本人入口提供固定表名／欄位，以登入帳號限定內容並在 DB 計數分頁。
def my_content(request, table, columns, kind):
    with connect() as connection:
        viewer, _ = authenticate(request, connection)
        page, per_page, _ = list_input(request)
        # 不加可見性條件，也不讀父文章，保留自己的下架內容且不授予額外父文讀取權。
        total = connection.execute(
            f"SELECT count(*) FROM {table} WHERE author_id = %s", (viewer["id"],)
        ).fetchone()["count"]
        offset = (page - 1) * per_page
        rows = [] if offset >= total else connection.execute(
            f"SELECT {columns} FROM {table} JOIN accounts ON accounts.id = {table}.author_id "
            f"WHERE {table}.author_id = %s ORDER BY {table}.created_at DESC, {table}.id DESC "
            "LIMIT %s OFFSET %s", (viewer["id"], per_page, offset),
        ).fetchall()
        return success({
            "list": [content_resource(row, viewer, kind) for row in rows],
            "pagination": pagination(page, per_page, total),
        })


# 文章下架與留言寫入鎖同一文章列，授權、版本及恢復權在鎖內裁決。
@api("PUT")
def article_visibility(request, article_id):
    return mutate_article(request, article_id, visibility=True)


# 文章修改只允許作者；管理員只在下架操作可處理他人文章。
def mutate_article(request, article_id, visibility):
    with connect() as connection:
        # 共用編修與可見性入口，依認證、格式、可讀性、操作權、版本順序裁決。
        viewer = authenticate_write(request, connection)
        no_query(request)
        content_id = url_integer(article_id, "article_id")
        fields = ("visibility", "version") if visibility else ("title", "body", "version")
        limits = {"visibility": 0, "version": 0} if visibility else {"title": 200, "body": 20000, "version": 0}
        body = json_body(request, fields, content_limits=limits)
        row = connection.execute("SELECT * FROM articles WHERE id = %s FOR UPDATE", (content_id,)).fetchone()
        if row is None or (
            row["visibility"] == "hidden"
            and viewer["role"] != "admin" and viewer["id"] != row["author_id"]
        ):
            raise ApiError("E0004")
        if viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin"):
            raise ApiError("E0003")
        # 恢復權先於版本檢查，避免以 409 暴露無權操作的版本資訊。
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {
            "title": body["title"], "body": body["body"],
        }
        save_content(connection, "articles", row, body["version"], changes)
    return success()


# 作者或管理員可獨立讀取留言；他人公開留言先判可讀再回操作權 403。
@api(("GET", "PUT"))
def comment(request, comment_id):
    if request.method == "PUT":
        return mutate_comment(request, comment_id, visibility=False)
    with connect() as connection:
        # 此詳情是作者／管理員專用，必須先驗登入與格式，不能當成公開留言入口。
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
        # 作者可獨立讀自己的留言；其他會員先按父文及留言隱藏回 404，公開但無操作資格才回 403。
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
        # 交易內重驗登入後先驗格式；父文章與留言鎖會維持到權限、版本與更新一併提交。
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
        # 只有內文編修要求父文公開；下架／恢復仍可獨立操作，且管理員不能改寫他人內文。
        if (viewer["id"] != row["author_id"] and (not visibility or viewer["role"] != "admin")) or (
            not visibility and parent["visibility"] == "hidden"
        ):
            raise ApiError("E0003")
        # 恢復權先於版本；父文隱藏不會被恢復留言繞過，因讀取入口仍另驗父文。
        changes = visibility_changes(row, viewer, body["visibility"]) if visibility else {"body": body["body"]}
        save_content(connection, "comments", row, body["version"], changes)
    return success()
